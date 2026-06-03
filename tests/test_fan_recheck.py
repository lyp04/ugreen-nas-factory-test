"""风扇/温度读数：单页判定 + 重测不复用旧失败截图（修复「读旧数据再次失败」）。

这两个文件都不经过 cli -> browser_control(WinDLL)，所以本测试在 macOS/Linux/Windows
都能跑（不像 test_cli_measurement_validation.py 只能在 Windows 上收集）。
"""
from __future__ import annotations

import json
from pathlib import Path

from src import measurements as m
from src.flows import capture


# --- 单页判定（抓取时重抓 / 重测护栏 / 最终校验三处共用的同一套规则）---------------

def test_fan_mode_value_failures_pass() -> None:
    assert m.fan_mode_value_failures(
        "fan_full_speed", {"cpu_temp": "60 ℃", "device_fan_rpm": "1500 转/分"}, 70
    ) == []


def test_fan_mode_value_failures_temp_over_limit() -> None:
    reasons = m.fan_mode_value_failures(
        "fan_full_speed", {"cpu_temp": "85 ℃", "device_fan_rpm": "1500"}, 70
    )
    assert reasons and "CPU 温度" in reasons[0]


def test_fan_mode_value_failures_zero_rpm_non_silent_fails() -> None:
    assert m.fan_mode_page_is_failing("fan_normal", {"device_fan_rpm": "0"}, 70) is True


def test_fan_mode_value_failures_silent_zero_rpm_ok() -> None:
    # 静音模式 0 转属正常工况，只要温度合格就不算失败。
    assert m.fan_mode_page_is_failing(
        "fan_silent", {"cpu_temp": "55", "device_fan_rpm": "0"}, 70
    ) is False


def test_fan_mode_value_failures_missing_rpm() -> None:
    reasons = m.fan_mode_value_failures("fan_full_speed", {"cpu_temp": "55"}, 70)
    assert reasons and "未读取" in reasons[0]


def test_non_fan_page_never_fails() -> None:
    assert m.fan_mode_value_failures("hdd_write", {"rate_mbps": "5"}, 70) == []


def test_single_page_predicate_matches_aggregate_validator() -> None:
    # 单页版与多页版必须给出一致结论，否则「抓取时判合格、最终判失败」会自相矛盾。
    hot = {"cpu_temp": "85", "device_fan_rpm": "1500"}
    assert bool(m.cpu_temp_failing_pages({"fan_full_speed": hot}, 70)) == \
        m.fan_mode_page_is_failing("fan_full_speed", hot, 70)


# --- 重测护栏：上次留下的失败读数不复用（这正是原始 bug 的回归测试）-----------------

def _seed_capture(tmp_path: Path, page_key: str, values: dict[str, str]) -> tuple[Path, str]:
    sn = "TESTSN0001"
    base = tmp_path / sn
    screenshots = base / "图片"
    screenshots.mkdir(parents=True)
    shot = screenshots / f"{sn}_{page_key}_20200101_000000.png"
    shot.write_bytes(b"\x89PNG\r\n\x1a\n")  # 非空即可，_valid_existing_capture 只看大小
    report = {"captured": {page_key: str(shot)}, "captured_values": {page_key: values}}
    (base / "test_report.json").write_text(json.dumps(report), encoding="utf-8")
    return screenshots, sn


def test_existing_capture_rejected_when_recorded_temp_over_limit(tmp_path: Path) -> None:
    # 上次记录的就是超标温度——重测时必须不复用，强制重抓（否则拿旧值又一次失败）。
    screenshots, sn = _seed_capture(
        tmp_path, "fan_full_speed", {"cpu_temp": "85 ℃", "device_fan_rpm": "1500 转/分"}
    )
    result = capture._existing_capture_path(
        screenshots, sn, "fan_full_speed",
        require_reported_values=True, fan_mode_max_c=70.0,
    )
    assert result is None


def test_existing_capture_rejected_when_recorded_rpm_zero(tmp_path: Path) -> None:
    screenshots, sn = _seed_capture(
        tmp_path, "fan_full_speed", {"cpu_temp": "55 ℃", "device_fan_rpm": "0 转/分"}
    )
    result = capture._existing_capture_path(
        screenshots, sn, "fan_full_speed",
        require_reported_values=True, fan_mode_max_c=70.0,
    )
    assert result is None


def test_existing_capture_reused_when_recorded_values_pass(tmp_path: Path) -> None:
    # 上次读数合格——复用旧截图省时间，符合 resume 设计。
    screenshots, sn = _seed_capture(
        tmp_path, "fan_full_speed", {"cpu_temp": "60 ℃", "device_fan_rpm": "1500 转/分"}
    )
    result = capture._existing_capture_path(
        screenshots, sn, "fan_full_speed",
        require_reported_values=True, fan_mode_max_c=70.0,
    )
    assert result is not None
    assert result.name == f"{sn}_fan_full_speed_20200101_000000.png"
