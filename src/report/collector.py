"""把某个 SN 的全部测试产物收齐：test_report.json、run.log 末尾、以及整目录打成 zip。

zip 用于上传到 GitHub Release 资产（含 图片/*.png 截图）；前两者用于直接嵌进
Issue 正文，方便不下载 zip 就能看个大概。
"""
from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path

# 单个 zip 的大小上限，超过的文件跳过（截图一般几 MB，正常打不满）。
DEFAULT_MAX_ZIP_BYTES = 80 * 1024 * 1024
# 这些后缀不进 zip：原子写的临时文件 / 已被 playwright 删掉的 trace。
_SKIP_SUFFIXES = {".tmp"}
_SKIP_DIR_NAMES = {"traces"}


def read_test_report(sn_dir: Path) -> dict:
    try:
        return json.loads((sn_dir / "test_report.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def read_run_log_tail(sn_dir: Path, max_lines: int = 200, max_bytes: int = 60_000) -> str:
    path = sn_dir / "run.log"
    if not path.exists():
        return ""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return ""
    tail = "\n".join(text.splitlines()[-max_lines:])
    if len(tail) > max_bytes:
        tail = tail[-max_bytes:]
    return tail


def zip_sn_dir(sn_dir: Path, max_total_bytes: int = DEFAULT_MAX_ZIP_BYTES) -> tuple[bytes, list[str]]:
    """把整个 <output>/<sn>/ 目录打成内存 zip。返回 (zip 字节, 收录文件清单)。

    zip 内顶层目录就是 SN 名（arcname 相对 sn_dir 的父目录）。
    """
    included: list[str] = []
    if not sn_dir.exists():
        return b"", included
    buffer = io.BytesIO()
    total = 0
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(sn_dir.rglob("*")):
            if path.is_dir():
                continue
            if path.suffix in _SKIP_SUFFIXES:
                continue
            if any(part in _SKIP_DIR_NAMES for part in path.relative_to(sn_dir).parts[:-1]):
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            if total + size > max_total_bytes:
                included.append(f"(跳过 {path.name}: 超出打包大小上限)")
                continue
            arcname = str(path.relative_to(sn_dir.parent))
            try:
                archive.write(path, arcname)
            except OSError:
                continue
            included.append(arcname)
            total += size
    return buffer.getvalue(), included


__all__ = ["read_test_report", "read_run_log_tail", "zip_sn_dir"]
