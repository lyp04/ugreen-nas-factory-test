"""src/report 故障上报包的单元测试。

只依赖 stdlib + 该包本身（不 import src.cli，所以任何平台都能跑、也进 CI 的精简清单）。
"""
from __future__ import annotations

import io
import json
import urllib.error
import zipfile
from pathlib import Path

import pytest

from src.report import collector, fingerprint, github_issues, redact
from src.report.reporter import FaultReporter


# --------------------------- redact ---------------------------


def test_scrub_text_removes_secrets() -> None:
    raw = (
        "login password=Bsd123456 then token: ghp_ABCDEFGHIJKLMNOPQRSTUVWX0123456789 "
        "Authorization: Bearer abcdef.token.value dev mac 00:11:22:33:44:55"
    )
    out = redact.scrub_text(raw, extra_secrets=["Bsd123456"])
    assert "Bsd123456" not in out
    assert "ghp_" not in out
    assert "00:11:22:33:44:55" not in out
    assert "<mac>" in out
    assert "password=<redacted>" in out


def test_scrub_text_extra_secret_too_short_ignored() -> None:
    # 少于 4 字符的「密钥」不替换，避免误伤
    assert redact.scrub_text("abc def", extra_secrets=["a"]) == "abc def"


def test_scrub_text_truncates() -> None:
    # 用带空格的串，避免被「长 base64 blob」规则整段替换，从而单测纯截断
    out = redact.scrub_text("a " * 100, max_len=10)
    assert out.endswith("…") and len(out) == 11


def test_mask_mac_and_hash_short() -> None:
    assert redact.mask_mac("00:11:22:33:44:55") == "00:11:22:XX:XX:XX"
    assert redact.mask_mac("bad") == "??"
    h = redact.hash_short("ugreen")
    assert len(h) == 6 and h == redact.hash_short("ugreen")  # 稳定
    assert redact.hash_short("") == ""


# --------------------------- fingerprint ---------------------------


def test_signature_normalizes_volatile_bits() -> None:
    a = fingerprint.normalize_signature("UGOS at 192.168.0.50:9999 did not become ready within 90s")
    b = fingerprint.normalize_signature("UGOS at 10.0.0.7:9999 did not become ready within 120s")
    assert a == b  # IP / 数字 归一化后一致
    assert "<ip>" in a and "<n>" in a


def test_signature_strips_volatile_context_suffix() -> None:
    # 同一类「服务启动卡死」故障，仅尾部 last page 转储不同，不应拆成两个指纹
    a = fingerprint.normalize_signature(
        "UGOS setup page stayed on 'service starting' screen for 300s; "
        "last page: 服务启动中 您可以尝试手动刷新页面 刷新"
    )
    b = fingerprint.normalize_signature(
        "UGOS setup page stayed on 'service starting' screen for 280s; last page: <empty>"
    )
    assert a == b
    assert "last page" not in a
    # 候选 IP 列表长度变化也不该打散指纹
    c = fingerprint.normalize_signature(
        "No unused UGOS NAS matching SN tail 00D3 became available before timeout: "
        "No discovered UGOS NAS matched SN tail 00D3; candidates: ['192.168.0.1', '192.168.0.2']"
    )
    d = fingerprint.normalize_signature(
        "No unused UGOS NAS matching SN tail 00D3 became available before timeout: "
        "No discovered UGOS NAS matched SN tail 00D3; candidates: ['10.0.0.9']"
    )
    assert c == d
    assert "candidates" not in c


def test_fingerprint_stable_and_discriminating() -> None:
    sig = fingerprint.normalize_signature("weird crash at step foo")
    base = fingerprint.compute("other", "4800", sig)
    assert base == fingerprint.compute("other", "4800", sig)  # 稳定
    assert base != fingerprint.compute("other", "2800", sig)  # 机型敏感
    assert base != fingerprint.compute("rw_speed", "4800", sig)  # 类别敏感
    assert len(base) == 8


# --------------------------- collector ---------------------------


def _make_sn_dir(tmp_path: Path, sn: str = "SN12345") -> Path:
    sn_dir = tmp_path / sn
    (sn_dir / "图片").mkdir(parents=True)
    (sn_dir / "traces").mkdir(parents=True)
    (sn_dir / "test_report.json").write_text(
        json.dumps({"sn": sn, "status": "failed", "error": "boom"}), encoding="utf-8"
    )
    (sn_dir / "run.log").write_text("\n".join(f"line {i}" for i in range(500)), encoding="utf-8")
    (sn_dir / "图片" / "p1.png").write_bytes(b"\x89PNG\r\n")
    (sn_dir / "test_report.json.tmp").write_text("partial", encoding="utf-8")
    (sn_dir / "traces" / "t.zip").write_bytes(b"trace")
    return sn_dir


def test_zip_sn_dir_includes_artifacts_and_skips_transient(tmp_path: Path) -> None:
    sn_dir = _make_sn_dir(tmp_path)
    data, included = collector.zip_sn_dir(sn_dir)
    names = zipfile.ZipFile(io.BytesIO(data)).namelist()
    assert any(n.endswith("test_report.json") for n in names)
    assert any(n.endswith("p1.png") for n in names)
    assert not any(n.endswith(".tmp") for n in names)  # 跳过原子写临时文件
    assert not any("/traces/" in n for n in names)  # 跳过 traces
    assert all(n.startswith("SN12345/") for n in names)  # zip 顶层是 SN 目录


def test_read_report_and_log_tail(tmp_path: Path) -> None:
    sn_dir = _make_sn_dir(tmp_path)
    assert collector.read_test_report(sn_dir)["status"] == "failed"
    tail = collector.read_run_log_tail(sn_dir, max_lines=10)
    assert "line 499" in tail and "line 0" not in tail


def test_read_missing_artifacts(tmp_path: Path) -> None:
    assert collector.read_test_report(tmp_path / "nope") == {}
    assert collector.read_run_log_tail(tmp_path / "nope") == ""
    data, included = collector.zip_sn_dir(tmp_path / "nope")
    assert data == b"" and included == []


# --------------------------- github client ---------------------------


class _FakeResp:
    def __init__(self, payload: dict) -> None:
        self._payload = json.dumps(payload).encode("utf-8")

    def read(self) -> bytes:
        return self._payload

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *args: object) -> bool:
        return False


def test_github_client_request_shapes(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[str, str]] = []

    def fake_urlopen(request, timeout=None):  # noqa: ANN001
        calls.append((request.get_method(), request.full_url))
        url = request.full_url
        if "/search/issues" in url:
            return _FakeResp({"items": [{"title": "[ff001122] boom", "number": 7}]})
        if url.endswith("/issues"):
            return _FakeResp({"number": 42})
        if "/releases/tags/" in url:
            return _FakeResp({"id": 5})
        if "/assets?name=" in url:
            return _FakeResp({"browser_download_url": "https://dl/x.zip"})
        return _FakeResp({})

    monkeypatch.setattr(github_issues.urllib.request, "urlopen", fake_urlopen)
    client = github_issues.GitHubIssuesClient("o", "r", "t")
    assert client.find_issue_by_fingerprint("ff001122") == 7
    assert client.create_issue("title", "body", ["auto:failure"]) == 42
    client.patch_issue_body(42, "new body")
    assert client.ensure_release("fault-reports")["id"] == 5
    assert client.upload_release_asset(5, "a.zip", b"data") == "https://dl/x.zip"
    methods = {m for m, _ in calls}
    assert {"GET", "POST", "PATCH"} <= methods


def test_github_error_on_non_2xx(monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(request, timeout=None):  # noqa: ANN001
        raise urllib.error.HTTPError(
            request.full_url, 403, "Forbidden", {}, io.BytesIO(b'{"message":"nope"}')
        )

    monkeypatch.setattr(github_issues.urllib.request, "urlopen", boom)
    client = github_issues.GitHubIssuesClient("o", "r", "t")
    with pytest.raises(github_issues.GitHubError) as exc_info:
        client.get_issue_body(1)
    assert exc_info.value.status == 403


# --------------------------- reporter ---------------------------


class _FakeClient:
    def __init__(self) -> None:
        self.issues: dict[int, str] = {}
        self.created: list[tuple] = []
        self.patched: list[tuple] = []
        self.assets: list[tuple] = []
        self.next_number = 100
        self.find_result: int | None = None

    def is_configured(self) -> bool:
        return True

    def find_issue_by_fingerprint(self, fp: str):
        return self.find_result

    def create_issue(self, title, body, labels=None):
        number = self.next_number
        self.next_number += 1
        self.issues[number] = body
        self.created.append((title, body, labels, number))
        return number

    def get_issue_body(self, number):
        return self.issues.get(number, "")

    def patch_issue_body(self, number, body):
        self.issues[number] = body
        self.patched.append((number, body))

    def ensure_release(self, tag, name=None):
        return {"id": 999}

    def upload_release_asset(self, release_id, name, data, content_type="application/zip"):
        self.assets.append((release_id, name, len(data)))
        return f"https://example/releases/{name}"


def _reporter(tmp_path: Path, **cfg) -> FaultReporter:
    base = {"enabled": True, "owner": "o", "repo": "r", "token": "t", "dedup_window_minutes": 0}
    base.update(cfg)
    reporter = FaultReporter(base, tmp_path)
    reporter.client = _FakeClient()
    return reporter


def test_disabled_when_token_empty(tmp_path: Path) -> None:
    reporter = FaultReporter({"enabled": True, "owner": "o", "repo": "r", "token": ""}, tmp_path)
    assert not reporter.available()
    reporter.report_async(sn="x", sn_dir=str(tmp_path), model="", category="other", stage="s", message="m")
    assert reporter._snapshot() == []  # 未配置 → 完全 no-op


def test_dedup_window_suppresses_rapid_repeat(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    reporter = _reporter(tmp_path, dedup_window_minutes=5)
    monkeypatch.setattr(reporter, "flush_in_background", lambda: None)  # 只看入队，不起线程
    kwargs = dict(sn="SN1", sn_dir=str(tmp_path / "SN1"), model="4800", category="other", stage="s", message="same boom")
    reporter.report_async(**kwargs)
    reporter.report_async(**kwargs)
    assert len(reporter._snapshot()) == 1  # 第二次被去重窗口压掉


def test_upload_creates_then_bumps(tmp_path: Path) -> None:
    sn_dir = _make_sn_dir(tmp_path, "SNX")
    (sn_dir / "run.log").write_text("boot\nadmin password=Bsd123456\nweird boom\n", encoding="utf-8")
    reporter = _reporter(tmp_path)
    reporter.secrets = ["Bsd123456"]
    event = {
        "fingerprint": "abcd1234",
        "sn": "SNX",
        "sn_dir": str(sn_dir),
        "model": "4800",
        "category": "other",
        "stage": "测试失败",
        "message": "weird boom at step foo",
        "ts": "2026-06-01 10:00:00",
    }

    assert reporter._upload_one(dict(event)) is True
    client = reporter.client
    assert len(client.created) == 1
    title, body, labels, number = client.created[0]
    assert "[abcd1234]" in title and "4800" in title
    assert {"auto:failure", "category:other", "model:4800"} <= set(labels)
    assert "**累计次数**：1" in body
    assert "https://example/releases/" in body  # 资产链接入正文
    assert "Bsd123456" not in body  # run.log 末尾里的密码被脱敏
    assert len(client.assets) == 1

    # 第二次出现（同指纹）→ 走缓存命中 → bump，不新建 issue
    event2 = dict(event)
    event2["ts"] = "2026-06-01 11:00:00"
    assert reporter._upload_one(event2) is True
    assert len(client.created) == 1
    assert client.patched, "应当 PATCH 已有 issue"
    bumped = client.issues[number]
    assert "**累计次数**：2" in bumped
    assert bumped.count("- 2026-06-01") >= 2  # 两条「最近记录」


def test_upload_drops_on_auth_error(tmp_path: Path) -> None:
    reporter = _reporter(tmp_path)

    def boom(*args, **kwargs):
        raise github_issues.GitHubError(401, "bad creds")

    reporter.client.find_issue_by_fingerprint = boom  # type: ignore[assignment]
    reporter._upload_asset = lambda *a, **k: None  # 跳过打包
    event = {"fingerprint": "x", "sn": "S", "sn_dir": str(tmp_path / "S"), "model": "", "ts": "2026-06-01 10:00:00", "message": "m", "stage": "s"}
    # 401 应视为「已处理（丢弃）」→ True，避免坏事件卡死队列
    assert reporter._upload_one(event) is True


def test_bump_body_without_meta_marker(tmp_path: Path) -> None:
    reporter = _reporter(tmp_path)
    body = reporter._bump_body("legacy body without markers\n", {"fingerprint": "x", "ts": "2026-06-01 09:00:00"})
    assert "<!-- meta:count=2 -->" in body
    assert "最近记录" in body


def test_queue_roundtrip(tmp_path: Path) -> None:
    reporter = _reporter(tmp_path)
    reporter._enqueue({"a": 1})
    reporter._enqueue({"a": 2})
    assert [e["a"] for e in reporter._snapshot()] == [1, 2]
    reporter._drop_first(1)
    assert [e["a"] for e in reporter._snapshot()] == [2]
