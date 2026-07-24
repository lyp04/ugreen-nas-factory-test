"""把密码 / token / MAC 等敏感串从将要上传到 GitHub 的文本里抹掉。

对应内部的 Redactor。SN、邮箱、机型这类排障必需的信息保留；只去掉
真正的密钥材料（admin 密码、PAT、Bearer、长 base64 blob）和设备唯一的 MAC。
"""
from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable
from typing import Any

# key=value / key: value，以及 JSON / Python dict 的 "key": "value" 形式。
# 这里刻意只匹配明确的敏感字段名，避免把普通诊断字段整段删掉。
_SECRET_KEY_PATTERN = (
    r"password|passwd|pwd|psk|client_secret|secret|refresh_token|access_token|id_token|token|"
    r"api_key|apikey|private_key|authorization|auth|credentials?|set_cookie|cookie"
)
_SECRET_KV_RE = re.compile(
    rf"(?ix)"
    rf"(?P<prefix>"
    rf"(?<![A-Za-z0-9_])"
    rf"(?P<key_quote>[\"']?)"
    rf"(?P<key>{_SECRET_KEY_PATTERN})"
    rf"(?P=key_quote)"
    rf"(?![A-Za-z0-9_])"
    rf"\s*[:=]\s*"
    rf")"
    rf"(?P<value>"
    rf'"(?:\\.|[^"\\])*"'
    rf"|'(?:\\.|[^'\\])*'"
    rf"|[^\s,;}}\]]+"
    rf")"
)
# Authorization: Basic/Bearer 必须在普通 key/value 规则前处理；否则普通规则只会
# 吃掉 ``Basic``，把后面的凭据原样留下。
_AUTH_SCHEME_RE = re.compile(
    r"(?i)\b(authorization)\s*[:=]\s*(?:basic|bearer)\s+[^\s,;}\]]+"
)
# 独立出现的 Bearer / Basic xxxxx
_BEARER_RE = re.compile(r"(?i)\b(bearer)\s+\S+")
_BASIC_RE = re.compile(r"(?i)\b(basic)\s+[A-Za-z0-9+/_=-]{4,}")
# GitHub PAT（classic ghp_ / fine-grained github_pat_ 等）
_GH_PAT_RE = re.compile(r"\b(?:gh[posu]_[A-Za-z0-9_]{20,}|github_pat_[A-Za-z0-9_]{20,})\b")
# MAC 地址
_MAC_RE = re.compile(r"(?i)\b(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}\b")
# 长 base64 blob（密钥/证书等）
_LONG_B64_RE = re.compile(r"[A-Za-z0-9+/]{32,}={0,2}")

_SECRET_FIELD_NAMES = {
    "password",
    "passwd",
    "pwd",
    "psk",
    "clientsecret",
    "secret",
    "refreshtoken",
    "accesstoken",
    "idtoken",
    "token",
    "apikey",
    "privatekey",
    "authorization",
    "auth",
    "credential",
    "credentials",
    "setcookie",
    "cookie",
}


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


def scrub_text(
    text: object,
    *,
    extra_secrets: Iterable[str] = (),
    max_len: int = 0,
    mask_identifiers: bool = True,
) -> str:
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
    result = _AUTH_SCHEME_RE.sub(r"\1=<redacted>", result)
    result = _BEARER_RE.sub(r"\1 <redacted>", result)
    result = _BASIC_RE.sub(r"\1 <redacted>", result)

    def replace_secret_kv(match: re.Match[str]) -> str:
        value = match.group("value")
        quote = value[0] if value and value[0] in "\"'" else ""
        replacement = f"{quote}<redacted>{quote}" if quote else "<redacted>"
        return f"{match.group('prefix')}{replacement}"

    result = _SECRET_KV_RE.sub(replace_secret_kv, result)
    if mask_identifiers:
        result = _LONG_B64_RE.sub(lambda m: f"<b64:{len(m.group(0))}B>", result)
        result = _MAC_RE.sub("<mac>", result)
    if max_len and len(result) > max_len:
        result = result[:max_len] + "…"
    return result


def scrub_data(
    value: Any,
    *,
    extra_secrets: Iterable[str] = (),
    mask_identifiers: bool = True,
) -> Any:
    """递归脱敏 JSON-like 数据，同时保留原有容器结构。

    先按字段名处理结构化秘密，再对所有字符串运行通用文本规则。这样即使序列化
    后的 JSON / Python dict 使用带引号的 key，也不会依赖正则猜测 value 边界。
    """
    secrets = tuple(str(item or "") for item in extra_secrets)
    if isinstance(value, dict):
        cleaned: dict[Any, Any] = {}
        for key, item in value.items():
            normalized_key = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if normalized_key in _SECRET_FIELD_NAMES:
                cleaned[key] = "<redacted>"
            else:
                cleaned[key] = scrub_data(
                    item,
                    extra_secrets=secrets,
                    mask_identifiers=mask_identifiers,
                )
        return cleaned
    if isinstance(value, list):
        return [
            scrub_data(item, extra_secrets=secrets, mask_identifiers=mask_identifiers)
            for item in value
        ]
    if isinstance(value, tuple):
        return tuple(
            scrub_data(item, extra_secrets=secrets, mask_identifiers=mask_identifiers)
            for item in value
        )
    if isinstance(value, str):
        return scrub_text(
            value,
            extra_secrets=secrets,
            mask_identifiers=mask_identifiers,
        )
    return value


__all__ = ["hash_short", "mask_mac", "scrub_data", "scrub_text"]
