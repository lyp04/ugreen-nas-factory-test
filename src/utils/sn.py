from __future__ import annotations

import re


SN_TAIL_LENGTH = 4
FULL_SN_PREFIXES = ("HB", "EC")
FULL_SN_RE = re.compile(r"(?<![0-9A-Z])[A-Z]{2}[0-9][0-9A-Z]{9,29}(?![0-9A-Z])")
EXPLICIT_FULL_SN_RE = re.compile(
    r"""(?ix)
    (?:
        ["']?(?:sn|serial[_\s-]*number|device[_\s-]*sn)["']?\s*[:=]\s*["']?
        | \bSN\b\s*[:=]\s*["']?
        | 序列号\s*[:：]?\s*
        | 序列號\s*[:：]?\s*
        | 设备序列号\s*[:：]?\s*
        | 設備序列號\s*[:：]?\s*
    )
    ([0-9A-Z][0-9A-Z_-]{7,40})
    """
)
TRUNCATED_TAIL_MODELS = ("DXP4800PLUS",)
AUTO_SN_PREFIX = "AUTO"
SN_MODEL_PREFIXES = (
    ("EC752", "4800Plus"),
    ("EC671", "4800"),
    ("HB", "2800"),
)


def normalize_sn(value: str) -> str:
    return re.sub(r"[^0-9A-Za-z]", "", value or "").upper()


def is_auto_sn_placeholder(value: str) -> bool:
    return normalize_sn(value).startswith(AUTO_SN_PREFIX)


def is_full_sn_candidate(value: str) -> bool:
    normalized = normalize_sn(value)
    if is_auto_sn_placeholder(normalized):
        return False
    if not re.fullmatch(r"[A-Z]{2}[0-9][0-9A-Z]{9,29}", normalized):
        return False
    return sum(1 for ch in normalized if ch.isdigit()) >= 4


def sn_tail(value: str, length: int = SN_TAIL_LENGTH) -> str:
    normalized = normalize_sn(value)
    return normalized[-length:] if len(normalized) >= length else normalized


def model_key_from_sn(value: str) -> str | None:
    normalized = normalize_sn(value)
    if is_auto_sn_placeholder(normalized):
        return None
    for prefix, model_key in SN_MODEL_PREFIXES:
        if normalized.startswith(prefix):
            return model_key
    return None


def sn_matches_text(sn: str, text: str) -> bool:
    normalized_sn = normalize_sn(sn)
    tail = sn_tail(sn)
    normalized_text = normalize_sn(text)
    return (
        bool(tail and tail in normalized_text)
        or bool(normalized_sn and normalized_sn in normalized_text)
        or _matches_truncated_tail_label(tail, normalized_text)
    )


def _matches_truncated_tail_label(tail: str, normalized_text: str) -> bool:
    if len(tail) < SN_TAIL_LENGTH:
        return False
    if not any(model in normalized_text for model in TRUNCATED_TAIL_MODELS):
        return False
    return tail[: SN_TAIL_LENGTH - 1] in normalized_text


def extract_full_sn(text: str, expected_tail: str = "") -> str | None:
    tail = sn_tail(expected_tail)
    for match in EXPLICIT_FULL_SN_RE.finditer((text or "").upper()):
        candidate = normalize_sn(match.group(1))
        if not is_full_sn_candidate(candidate):
            continue
        if tail and sn_tail(candidate) != tail:
            continue
        return candidate

    for match in FULL_SN_RE.finditer((text or "").upper()):
        if not tail:
            break
        candidate = normalize_sn(match.group(0))
        if not is_full_sn_candidate(candidate):
            continue
        if tail and sn_tail(candidate) != tail:
            continue
        return candidate
    return None


def same_sn_identity(left: str, right: str) -> bool:
    normalized_left = normalize_sn(left)
    normalized_right = normalize_sn(right)
    if is_auto_sn_placeholder(normalized_left) or is_auto_sn_placeholder(normalized_right):
        return False
    if is_full_sn_candidate(normalized_left) and is_full_sn_candidate(normalized_right):
        return normalized_left == normalized_right
    left_tail = sn_tail(left)
    right_tail = sn_tail(right)
    return bool(left_tail and right_tail and left_tail == right_tail)
