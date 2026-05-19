import pytest

from src import cli


def test_zero_fan_rpm_fails_measurement_validation() -> None:
    captured = {
        "resource_monitor": {"cpu_temp": "64 °C", "device_fan_rpm": "0 转/分"},
        "fan_full_speed": {"cpu_temp": "64 °C", "device_fan_rpm": "0 转/分"},
    }

    with pytest.raises(RuntimeError) as exc_info:
        cli._validate_captured_measurements(captured)

    assert "风扇转速异常" in str(exc_info.value)
    assert "风扇全速模式" in str(exc_info.value)
    assert cli.failure_stage_for_error(exc_info.value) == "风扇转速异常"


def test_positive_fan_rpm_passes_measurement_validation() -> None:
    cli._validate_captured_measurements(
        {
            "resource_monitor": {"cpu_temp": "64 °C", "device_fan_rpm": "1,250 转/分"},
            "fan_full_speed": {"cpu_temp": "64 °C", "device_fan_rpm": "2500 rpm"},
        }
    )
