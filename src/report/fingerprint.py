"""把一条故障折叠成稳定的 8 位 hex 指纹，用于「同一类故障合并到一个 Issue」。

对应 anker 的 Fingerprint。易变字段（IP / MAC / 时间戳 / 路径 / 具体数字 / SN 尾号）
在算指纹前先归一化掉，这样不同机器、不同时间出现的同一类问题会得到相同指纹。
"""
from __future__ import annotations

import hashlib
import re

# 顺序很重要：先把 IP/MAC/时间戳/路径替换掉，最后再把残余数字统一成 <n>，
# 否则数字正则会先吃掉 IP 里的数字。
_VOLATILE_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"\b\d{1,3}(?:\.\d{1,3}){3}\b"), "<ip>"),
    (re.compile(r"(?i)\b(?:[0-9a-f]{2}[:-]){5}[0-9a-f]{2}\b"), "<mac>"),
    (re.compile(r"\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}"), "<ts>"),
    (re.compile(r"[A-Za-z]:\\[^\s'\"]+"), "<path>"),          # Windows 路径
    (re.compile(r"(?:/[^\s'\"/]+){2,}"), "<path>"),            # Unix 路径
    (re.compile(r"\b[0-9a-fA-F]{8,}\b"), "<hex>"),             # 长 hex（trace id 等）
    (re.compile(r"[-+]?\d+(?:\.\d+)?"), "<n>"),                # 残余数字
]


def normalize_signature(message: object, max_len: int = 200) -> str:
    """取错误信息的首行并归一化掉易变片段，得到稳定的「错误骨架」。"""
    text = str(message or "").strip()
    if not text:
        return ""
    text = text.splitlines()[0]
    for pattern, repl in _VOLATILE_PATTERNS:
        text = pattern.sub(repl, text)
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) > max_len:
        text = text[:max_len]
    return text


def compute(category: str, model: str, signature: str, length: int = 8) -> str:
    """category + 机型 + 错误骨架 → 稳定指纹。"""
    seed = f"{category or ''}|{model or ''}|{signature or ''}"
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:length]


__all__ = ["normalize_signature", "compute"]
