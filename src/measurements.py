"""CPU 温度 / 风扇转速读数的统一判定。

这里是「某张风扇模式页的读数是否合格」的唯一事实来源：最终的整体校验（cli）、
抓取时「不合格就重抓」（flows.capture）、重测时「不复用旧失败截图」（flows.capture）
都从这里取阈值与豁免规则，确保三处永不分叉。
"""
from __future__ import annotations

import re

# 与 CPU 温度判定一致：resource_monitor 是满载尾巴的瞬时读数（风扇可能正降速、整机偏凉时为 0），
# 不可靠，不参与风扇转速判定。只看 3 个风扇模式页（各 wait 12s 进入稳态）。
FAN_RPM_KEYS = ("fan_normal", "fan_silent", "fan_full_speed")
# 安静（静音）模式下风扇可以完全停转——0 转速属正常工况，不当作故障；其余模式仍要求 > 0。
FAN_RPM_ZERO_OK_KEYS = frozenset({"fan_silent"})
# resource_monitor 在 4 个 SMB 传输测试之后立即抓，是满载尾巴的瞬时温度——会偏高但不代表散热坏。
# 只看 3 个风扇模式页（各 wait 12s 进入稳态）。风扇全速若还压不下来，才是真的散热故障。
CPU_TEMP_KEYS = ("fan_normal", "fan_silent", "fan_full_speed")
DEFAULT_CPU_TEMP_MAX_C = 70.0

NUMBER_RE = re.compile(r"[-+]?(?:\d+(?:,\d{3})+|\d+)(?:\.\d+)?")


def _first_number(value: object) -> float | None:
    match = NUMBER_RE.search(str(value or ""))
    if not match:
        return None
    try:
        return float(match.group(0).replace(",", ""))
    except ValueError:
        return None


def fan_rpm_failing_pages(captured_values: dict) -> dict[str, str]:
    """page_key -> 失败原因（'missing' 未读取 / 'zero' 转速≤0）。仅检查需风扇运转的页；
    安静模式 0 转豁免、resource_monitor 不参与。供判定与界面标红共用，避免两边逻辑分叉。"""
    bad: dict[str, str] = {}
    for page_key in FAN_RPM_KEYS:
        values = captured_values.get(page_key)
        if not isinstance(values, dict):
            continue
        rpm = _first_number(values.get("device_fan_rpm"))
        if rpm is None:
            bad[page_key] = "missing"
        elif rpm <= 0 and page_key not in FAN_RPM_ZERO_OK_KEYS:
            bad[page_key] = "zero"
    return bad


def cpu_temp_failing_pages(captured_values: dict, max_c: float) -> dict[str, float]:
    """page_key -> 实测温度，仅含超过 max_c 的风扇模式页（resource_monitor 不参与）。"""
    over: dict[str, float] = {}
    for page_key in CPU_TEMP_KEYS:
        values = captured_values.get(page_key)
        if not isinstance(values, dict):
            continue
        temp = _first_number(values.get("cpu_temp"))
        if temp is not None and temp > max_c:
            over[page_key] = temp
    return over


def fan_mode_value_failures(
    page_key: str,
    values: dict | None,
    cpu_temp_max_c: float = DEFAULT_CPU_TEMP_MAX_C,
) -> list[str]:
    """单页版判定：返回该风扇模式页读数不合格的原因（空列表 = 合格）。

    与 fan_rpm_failing_pages / cpu_temp_failing_pages 共用同一套阈值与豁免规则——
    供抓取时「不合格就重抓」与重测时「不复用旧失败截图」使用，确保与最终整体判定一致。
    非风扇模式页（page_key 不在 KEYS 里）永远返回空列表。
    """
    if not isinstance(values, dict):
        values = {}
    reasons: list[str] = []
    if page_key in FAN_RPM_KEYS:
        rpm = _first_number(values.get("device_fan_rpm"))
        if rpm is None:
            reasons.append("风扇转速未读取")
        elif rpm <= 0 and page_key not in FAN_RPM_ZERO_OK_KEYS:
            reasons.append(f"风扇转速 {values.get('device_fan_rpm')} ≤ 0")
    if page_key in CPU_TEMP_KEYS:
        temp = _first_number(values.get("cpu_temp"))
        if temp is not None and temp > cpu_temp_max_c:
            reasons.append(f"CPU 温度 {temp:g}℃ > {cpu_temp_max_c:g}℃")
    return reasons


def fan_mode_page_is_failing(
    page_key: str,
    values: dict | None,
    cpu_temp_max_c: float = DEFAULT_CPU_TEMP_MAX_C,
) -> bool:
    """该风扇模式页的读数是否会被最终校验判失败。"""
    return bool(fan_mode_value_failures(page_key, values, cpu_temp_max_c))
