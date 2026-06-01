"""FaultReporter：把一条未分类失败 → 去重 → 落盘队列 → 异步上传成 GitHub Issue。

对应 anker 的 FailureReporter。流程：
  report_async() 同步做（极快）：算指纹、去重窗口、append 到 JSONL 队列，然后起后台线程。
  后台线程 _flush()：逐条上传——先把该 SN 整目录打包传成 Release 资产，再
  按指纹 找/建 Issue（重复出现就 PATCH 计数器 + 追加「最近记录」，不刷评论）。

健壮性：
  - 队列落盘（output_root/_fault_reports/queue.jsonl），进程崩了下次启动接着传。
  - 指纹→issue 号本地缓存，规避 GitHub 搜索索引延迟导致的重复建 issue。
  - 任何异常都吞掉，绝不影响测试主流程。
"""
from __future__ import annotations

import json
import platform
import re
import socket
import threading
import time
from datetime import datetime
from pathlib import Path

from ..version import VERSION_CODE, VERSION_NAME
from . import collector, fingerprint
from .github_issues import GitHubError, GitHubIssuesClient
from .redact import scrub_text

_META_COUNT_PREFIX = "<!-- meta:count="
_META_COUNT_SUFFIX = " -->"
_SECTION_RECENT = "### 最近记录"
_RECENT_KEEP = 8


def _now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


class FaultReporter:
    def __init__(self, cfg: dict, output_root: Path, *, secrets: list[str] | None = None) -> None:
        self.cfg = cfg or {}
        self.output_root = Path(output_root)
        self.enabled = bool(self.cfg.get("enabled", True))
        self.client = GitHubIssuesClient(
            self.cfg.get("owner"), self.cfg.get("repo"), self.cfg.get("token")
        )
        self.release_tag = str(self.cfg.get("release_tag") or "fault-reports")
        self.tail_lines = int(self.cfg.get("run_log_tail_lines", 200))
        self.dedup_window_s = float(self.cfg.get("dedup_window_minutes", 5)) * 60.0
        self.secrets = [s for s in (secrets or []) if s]
        self.queue_dir = self.output_root / "_fault_reports"
        self.queue_file = self.queue_dir / "queue.jsonl"
        self.issue_map_file = self.queue_dir / "issue_map.json"
        self._io_lock = threading.Lock()
        self._flush_guard = threading.Lock()
        self._last_seen: dict[str, float] = {}

    def available(self) -> bool:
        return self.enabled and self.client.is_configured()

    # ---------------- public entry ----------------

    def report_async(
        self,
        *,
        sn: str,
        sn_dir: object,
        model: str,
        category: str,
        stage: str,
        message: str,
    ) -> None:
        if not self.available():
            return
        try:
            signature = fingerprint.normalize_signature(message)
            fp = fingerprint.compute(category or "other", model or "", signature)
            now = time.time()
            last = self._last_seen.get(fp)
            if last is not None and now - last < self.dedup_window_s:
                return  # 同一指纹的快速重试，丢弃
            self._last_seen[fp] = now
            event = {
                "fingerprint": fp,
                "sn": sn or "",
                "sn_dir": str(sn_dir or (self.output_root / (sn or ""))),
                "model": model or "",
                "category": category or "other",
                "stage": stage or "测试失败",
                "message": message or "",
                "ts": _now_str(),
            }
            self._enqueue(event)
        except Exception:
            return
        self.flush_in_background()

    def flush_in_background(self) -> None:
        thread = threading.Thread(target=self._flush_safe, name="fault-report-upload", daemon=True)
        thread.start()

    # ---------------- queue (JSONL) ----------------

    def _enqueue(self, event: dict) -> None:
        self.queue_dir.mkdir(parents=True, exist_ok=True)
        with self._io_lock, open(self.queue_file, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False) + "\n")

    def _snapshot(self) -> list[dict]:
        if not self.queue_file.exists():
            return []
        events: list[dict] = []
        with self._io_lock:
            for line in self.queue_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except Exception:
                    continue
        return events

    def _drop_first(self, count: int) -> None:
        if count <= 0 or not self.queue_file.exists():
            return
        with self._io_lock:
            lines = [ln for ln in self.queue_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
            kept = lines[count:]
            self.queue_file.write_text(
                ("\n".join(kept) + "\n") if kept else "", encoding="utf-8"
            )

    # ---------------- flush / upload ----------------

    def _flush_safe(self) -> None:
        if not self._flush_guard.acquire(blocking=False):
            return  # 已有一个 flusher 在跑
        try:
            while True:
                events = self._snapshot()
                if not events:
                    return
                uploaded = 0
                for event in events:
                    if not self._upload_one(event):
                        return  # 传输错误，留在队列下次重试
                    uploaded += 1
                self._drop_first(uploaded)
                # 循环一次，捕获上传期间新入队的事件
        except Exception:
            return
        finally:
            self._flush_guard.release()

    def _upload_one(self, event: dict) -> bool:
        """返回 True = 该事件已处理完（可出队），False = 传输失败应重试。"""
        try:
            fp = event["fingerprint"]
            sn_dir = Path(event.get("sn_dir") or (self.output_root / event.get("sn", "")))
            event["asset_url"] = self._upload_asset(event, sn_dir, fp)
            number = self._cached_issue(fp)
            if number is None:
                number = self.client.find_issue_by_fingerprint(fp)
            if number:
                body = self._bump_body(self.client.get_issue_body(number), event)
                self.client.patch_issue_body(number, body)
            else:
                number = self.client.create_issue(
                    self._title(event), self._body(event, 1, sn_dir), self._labels(event)
                )
            if number:
                self._cache_issue(fp, number)
            return True
        except GitHubError as error:
            # 鉴权/配置/不可处理：丢弃，避免一条坏事件把整个队列卡死
            return error.status in (401, 403, 404, 422)
        except Exception:
            return False

    def _upload_asset(self, event: dict, sn_dir: Path, fp: str) -> str | None:
        try:
            data, _included = collector.zip_sn_dir(sn_dir)
            if not data:
                return None
            release = self.client.ensure_release(self.release_tag)
            release_id = release.get("id")
            if not release_id:
                return None
            stamp = re.sub(r"[^0-9]", "", event.get("ts", "")) or "log"
            raw_name = f"{event.get('sn') or 'unknown'}_{fp}_{stamp}.zip"
            asset_name = re.sub(r"[^A-Za-z0-9._-]", "_", raw_name)
            return self.client.upload_release_asset(release_id, asset_name, data)
        except GitHubError:
            return None  # 资产传不上去不该挡住 issue 本身
        except Exception:
            return None

    # ---------------- issue rendering ----------------

    def _title(self, event: dict) -> str:
        fp = event["fingerprint"]
        model = event.get("model") or "?"
        stage = event.get("stage") or "测试失败"
        short = fingerprint.normalize_signature(event.get("message", ""), max_len=80)
        title = f"[{fp}] [其他/未分类·{model}] {stage}"
        if short:
            title += f" — {short}"
        return title[:240]

    def _labels(self, event: dict) -> list[str]:
        labels = ["auto:failure", "category:other"]
        model = event.get("model")
        if model in ("2800", "4800", "4800Plus"):
            labels.append(f"model:{model}")
        return labels

    def _recent_bullet(self, event: dict) -> str:
        url = event.get("asset_url")
        link = f"[日志 zip]({url})" if url else "(无打包)"
        return f"- {event.get('ts', _now_str())} — {link}"

    def _env_lines(self) -> list[str]:
        try:
            host = socket.gethostname()
        except Exception:
            host = "?"
        info = {
            "app_version": f"{VERSION_NAME} (code {VERSION_CODE})",
            "host": host,
            "platform": platform.platform(),
            "python": platform.python_version(),
        }
        return [f"- `{key}` = `{value}`" for key, value in info.items()]

    def _body(self, event: dict, count: int, sn_dir: Path) -> str:
        secrets = self.secrets
        ts = event.get("ts", _now_str())
        lines: list[str] = []
        lines.append(
            "> 由 ugreen-nas-factory-test 自动上报：未归类故障"
            "（非读写速度 / 非转速 / 非温度 / 非存储池）。\n"
        )
        lines.append(f"**累计次数**：{count}")
        lines.append(f"**首次**：{ts}")
        lines.append(f"**最近**：{ts}")
        lines.append(f"**机型**：{event.get('model') or '?'}")
        lines.append(f"**SN**：`{scrub_text(event.get('sn', ''), extra_secrets=secrets)}`")
        lines.append(f"**判定阶段**：{event.get('stage') or '测试失败'}")
        lines.append(f"**指纹**：`{event['fingerprint']}`")
        lines.append("")
        lines.append(f"{_META_COUNT_PREFIX}{count}{_META_COUNT_SUFFIX}")

        message = scrub_text(event.get("message", ""), extra_secrets=secrets, max_len=1500)
        if message:
            lines.append("\n### 错误信息\n```\n" + message + "\n```")

        if event.get("asset_url"):
            lines.append(
                "\n### 完整日志打包\n"
                f"[下载 zip（含 run.log + test_report.json + 截图）]({event['asset_url']})"
            )

        lines.append(f"\n{_SECTION_RECENT}（保留最近 {_RECENT_KEEP} 次）")
        lines.append(self._recent_bullet(event))

        lines.append("\n### 运行环境")
        lines.extend(self._env_lines())

        report = collector.read_test_report(sn_dir)
        if report:
            dumped = scrub_text(
                json.dumps(report, ensure_ascii=False, indent=2), extra_secrets=secrets, max_len=12_000
            )
            lines.append(
                "\n### test_report.json\n<details><summary>展开</summary>\n\n```json\n"
                + dumped
                + "\n```\n</details>"
            )

        tail = collector.read_run_log_tail(sn_dir, self.tail_lines)
        if tail:
            scrubbed = scrub_text(tail, extra_secrets=secrets, max_len=30_000)
            lines.append(
                "\n### run.log 末尾\n<details><summary>展开</summary>\n\n```\n"
                + scrubbed
                + "\n```\n</details>"
            )
        return "\n".join(lines) + "\n"

    def _bump_body(self, existing: str, event: dict) -> str:
        """重复出现：累加计数器、更新「最近」、把新记录插到「最近记录」顶部并保留 N 条。"""
        existing = existing or ""
        count = self._parse_count(existing) + 1
        ts = event.get("ts", _now_str())
        existing = self._replace_line(existing, "**累计次数**：", f"**累计次数**：{count}")
        existing = self._replace_line(existing, "**最近**：", f"**最近**：{ts}")

        start = existing.find(_META_COUNT_PREFIX)
        if start >= 0:
            end = existing.find(_META_COUNT_SUFFIX, start)
            if end > start:
                existing = (
                    existing[:start]
                    + _META_COUNT_PREFIX
                    + str(count)
                    + _META_COUNT_SUFFIX
                    + existing[end + len(_META_COUNT_SUFFIX):]
                )
        else:
            existing += f"\n{_META_COUNT_PREFIX}{count}{_META_COUNT_SUFFIX}\n"

        bullet = self._recent_bullet(event)
        section_start = existing.find(_SECTION_RECENT)
        if section_start >= 0:
            header_end = existing.find("\n", section_start)
            if header_end < 0:
                header_end = len(existing)
            next_section = existing.find("\n### ", header_end + 1)
            if next_section < 0:
                next_section = len(existing)
            header = existing[section_start:header_end + 1]
            section = existing[header_end + 1:next_section]
            bullets = [ln for ln in section.split("\n") if ln.startswith("- ")]
            bullets.insert(0, bullet)
            bullets = bullets[:_RECENT_KEEP]
            existing = existing[:section_start] + header + "\n".join(bullets) + "\n" + existing[next_section:]
        else:
            existing += f"\n{_SECTION_RECENT}（保留最近 {_RECENT_KEEP} 次）\n{bullet}\n"
        return existing

    @staticmethod
    def _parse_count(body: str) -> int:
        start = body.find(_META_COUNT_PREFIX)
        if start < 0:
            return 1
        end = body.find(_META_COUNT_SUFFIX, start)
        if end < 0:
            return 1
        try:
            return int(body[start + len(_META_COUNT_PREFIX):end].strip())
        except ValueError:
            return 1

    @staticmethod
    def _replace_line(body: str, prefix: str, new_line: str) -> str:
        start = body.find(prefix)
        if start < 0:
            return body
        end = body.find("\n", start)
        if end < 0:
            end = len(body)
        return body[:start] + new_line + body[end:]

    # ---------------- fingerprint -> issue cache ----------------

    def _cached_issue(self, fingerprint_hex: str) -> int | None:
        try:
            mapping = json.loads(self.issue_map_file.read_text(encoding="utf-8"))
            value = mapping.get(fingerprint_hex)
            return int(value) if value else None
        except Exception:
            return None

    def _cache_issue(self, fingerprint_hex: str, number: int) -> None:
        try:
            self.queue_dir.mkdir(parents=True, exist_ok=True)
            with self._io_lock:
                try:
                    mapping = json.loads(self.issue_map_file.read_text(encoding="utf-8"))
                except Exception:
                    mapping = {}
                mapping[fingerprint_hex] = number
                self.issue_map_file.write_text(json.dumps(mapping, ensure_ascii=False), encoding="utf-8")
        except Exception:
            return


# ------------------ module-level convenience ------------------

_reporter_cache: dict[str, FaultReporter] = {}
_cache_lock = threading.Lock()


def get_reporter(cfg: dict, output_root: object, *, secrets: list[str] | None = None) -> FaultReporter:
    """按 (owner, repo, output_root) 缓存单例，让去重窗口在进程内持续生效。"""
    cfg = cfg or {}
    key = json.dumps(
        {"owner": cfg.get("owner"), "repo": cfg.get("repo"), "root": str(output_root)}, sort_keys=True
    )
    with _cache_lock:
        instance = _reporter_cache.get(key)
        if instance is None:
            instance = FaultReporter(cfg, Path(output_root), secrets=secrets)
            _reporter_cache[key] = instance
            # 首次创建时顺手把上次进程残留的队列冲一遍
            if instance.available():
                instance.flush_in_background()
        return instance


def report_failure(
    cfg: dict,
    output_root: object,
    *,
    sn: str,
    sn_dir: object,
    model: str | None,
    category: str,
    stage: str,
    message: str,
    secrets: list[str] | None = None,
) -> None:
    """cli.run_test 的入口：永不抛异常。"""
    try:
        reporter = get_reporter(cfg, output_root, secrets=secrets)
        if not reporter.available():
            return
        reporter.report_async(
            sn=sn,
            sn_dir=sn_dir,
            model=model or "",
            category=category or "other",
            stage=stage or "测试失败",
            message=message or "",
        )
    except Exception:
        return


__all__ = ["FaultReporter", "get_reporter", "report_failure"]
