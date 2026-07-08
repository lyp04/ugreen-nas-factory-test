"""把密码 / token / MAC 等敏感串从将要上传到 GitHub 的文本里抹掉。

对应内部的 Redactor。SN、邮箱、机型这类排障必需的信息保留；只去掉
真正的密钥材料（admin 密码、PAT、Bearer、长 base64 blob）和设备唯一的 MAC。
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable

# key=value / key: value 形式的密钥
_SECRET_KV_RE = re.compile(
    r"(?i)\b(password|passwd|pwd|psk|secret|token|access_token|api_key|apikey|authorization)\b"
    r"\s*[:=]\s*\S+"
)
# Bearer xxxxx
_BEARER_RE = re.compile(r"(?i)\b(bearer)\s+\S+")
# GitHub PAT（classic ghp_ / fine-grained github_pat_ 等）
_GH_PAT_RE = re.compile(r"\b(?:gh[posu]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")
# MAC 地址
_MAC_RE = re.compile(r"(?i)\b(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}\b")
# 长 base64 blob（密钥/证书等）
_LONG_B64_RE = re.compile(r"[A-Za-z0-9+/]{32,}={0,2}")


def hash_short(value: str, length: int = 6) -> str:
    """稳定、不可逆的短哈希（用于在需要保留「同一个值」语义但不暴露原文时）。"""
    if not value:
        return ""
    return hashlib.sha1(value.encode("utf-8")).hexdigest()[:length]


def mask_mac(mac: str) -> str:
    """只保留 OUI（前 3 段），后 3 段打码，厂商可见、主机位隐藏。"""
    if not mac:
        return ""
    parts = mac.replace("-", ":").strip().split(":")
    if len(parts) < 6:
        return "??"
    return ":".join(parts[:3] + ["xx", "xx", "xx"]).upper()


def scrub_text(text: object, *, extra_secrets: Iterable[str] = (), max_len: int = 0) -> str:
    """抹掉文本里的密钥材料。

    extra_secrets：调用方已知的明文密钥（如 config 里的 admin 密码、本次用的 token），
                   长度 >= 4 才替换，避免把常见短词误伤。
    max_len：>0 时截断到该长度并加省略号。
    """
    if text is None:
        return ""
    result = str(text)
    for secret in extra_secrets:
        secret = str(secret or "")
        if len(secret) >= 4:
            result = result.replace(secret, "<redacted>")
    result = _GH_PAT_RE.sub("<token>", result)
    result = _BEARER_RE.sub(r"\1 <redacted>", result)
    result = _SECRET_KV_RE.sub(lambda m: f"{m.group(1)}=<redacted>", result)
    result = _LONG_B64_RE.sub(lambda m: f"<b64:{len(m.group(0))}B>", result)
    result = _MAC_RE.sub("<mac>", result)
    if max_len and len(result) > max_len:
        result = result[:max_len] + "…"
    return result


__all__ = ["hash_short", "mask_mac", "scrub_text"]
