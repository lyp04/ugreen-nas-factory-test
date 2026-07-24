"""把某个 SN 的全部测试产物收齐：test_report.json、run.log 末尾、以及整目录打成 zip。

zip 用于上传到 GitHub Release 资产（含 图片/*.png 截图）；前两者用于直接嵌进
Issue 正文，方便不下载 zip 就能看个大概。
"""
from __future__ import annotations

import io
import json
import zipfile
from collections.abc import Iterable
from pathlib import Path

from .redact import scrub_text

# 单个 zip 的大小上限，超过的文件跳过（截图一般几 MB，正常打不满）。
DEFAULT_MAX_ZIP_BYTES = 80 * 1024 * 1024
# 只把明确的诊断文本和截图带出机器。未知二进制一律不收，避免一个误放在
# SN 目录里的数据库、密钥库或压缩包被当作普通附件原样上传。
_TEXT_SUFFIXES = {
    ".cfg",
    ".conf",
    ".csv",
    ".htm",
    ".html",
    ".ini",
    ".json",
    ".jsonl",
    ".log",
    ".md",
    ".ps1",
    ".py",
    ".tsv",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
_SAFE_BINARY_SUFFIXES = {".bmp", ".gif", ".jpeg", ".jpg", ".png", ".webp"}
_SKIP_SUFFIXES = {
    ".env",
    ".jks",
    ".key",
    ".keystore",
    ".p12",
    ".pem",
    ".pfx",
    ".ppk",
    ".tmp",
}
_SKIP_DIR_NAMES = {
    ".git",
    ".ssh",
    "__pycache__",
    "config",
    "credentials",
    "secrets",
    "state",
    "traces",
}
_HIGH_RISK_NAMES = {
    ".env",
    "accounts.local.json",
    "config.json",
    "config.yaml",
    "config.yml",
    "credentials",
    "credentials.json",
    "id_dsa",
    "id_ecdsa",
    "id_ed25519",
    "id_rsa",
    "secrets.json",
    "update-config.json",
}


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


def _is_high_risk_file(path: Path) -> bool:
    name = path.name.casefold()
    if name in _HIGH_RISK_NAMES:
        return True
    if name.startswith(".env.") or ".local." in name:
        return True
    if name.startswith(("config.", "credentials.", "secret.", "secrets.")):
        return True
    return path.suffix.casefold() in _SKIP_SUFFIXES


def _decode_text(data: bytes) -> str:
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16", errors="replace")
    return data.decode("utf-8-sig", errors="replace")


def _safe_binary_matches_suffix(suffix: str, data: bytes) -> bool:
    if suffix == ".png":
        return data.startswith(b"\x89PNG\r\n\x1a\n")
    if suffix in {".jpg", ".jpeg"}:
        return data.startswith(b"\xff\xd8\xff")
    if suffix == ".gif":
        return data.startswith((b"GIF87a", b"GIF89a"))
    if suffix == ".webp":
        return len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP"
    if suffix == ".bmp":
        return data.startswith(b"BM")
    return False


def _sanitized_attachment_bytes(path: Path, *, extra_secrets: Iterable[str]) -> bytes | None:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    suffix = path.suffix.casefold()
    if suffix in _TEXT_SUFFIXES:
        cleaned = scrub_text(_decode_text(data), extra_secrets=extra_secrets)
        return cleaned.encode("utf-8")
    if suffix in _SAFE_BINARY_SUFFIXES and _safe_binary_matches_suffix(suffix, data):
        return data
    return None


def zip_sn_dir(
    sn_dir: Path,
    max_total_bytes: int = DEFAULT_MAX_ZIP_BYTES,
    *,
    extra_secrets: Iterable[str] = (),
) -> tuple[bytes, list[str]]:
    """把整个 <output>/<sn>/ 目录打成内存 zip。返回 (zip 字节, 收录文件清单)。

    zip 内顶层目录就是 SN 名（arcname 相对 sn_dir 的父目录）。
    """
    included: list[str] = []
    if not sn_dir.exists():
        return b"", included
    secrets = tuple(str(item or "") for item in extra_secrets)
    buffer = io.BytesIO()
    total = 0
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(sn_dir.rglob("*")):
            if path.is_symlink():
                continue
            if path.is_dir():
                continue
            relative = path.relative_to(sn_dir)
            if _is_high_risk_file(path):
                continue
            if any(part.casefold() in _SKIP_DIR_NAMES for part in relative.parts[:-1]):
                continue
            try:
                size = path.stat().st_size
            except OSError:
                continue
            # Do not read a single oversized file into memory just to discover that
            # it cannot fit in the bounded in-memory zip.
            if size > max_total_bytes:
                included.append(f"(跳过 {path.name}: 超出打包大小上限)")
                continue
            payload = _sanitized_attachment_bytes(path, extra_secrets=secrets)
            if payload is None:
                continue
            if total + len(payload) > max_total_bytes:
                included.append(f"(跳过 {path.name}: 超出打包大小上限)")
                continue
            arcname = path.relative_to(sn_dir.parent).as_posix()
            archive.writestr(arcname, payload)
            included.append(arcname)
            total += len(payload)
    return buffer.getvalue(), included


__all__ = ["read_test_report", "read_run_log_tail", "zip_sn_dir"]
