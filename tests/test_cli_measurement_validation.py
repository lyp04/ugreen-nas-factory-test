import pytest

from src import cli


def test_zero_fan_rpm_fails_measurement_validation() -> None:
    captured = {
        "resource_monitor": {"cpu_temp": "55 °C", "device_fan_rpm": "0 转/分"},
        "fan_full_speed": {"cpu_temp": "55 °C", "device_fan_rpm": "0 转/分"},
    }

    with pytest.raises(RuntimeError) as exc_info:
        cli._validate_captured_measurements(captured)

    assert "风扇转速异常" in str(exc_info.value)
    assert "风扇全速模式" in str(exc_info.value)
    assert cli.failure_stage_for_error(exc_info.value) == "风扇转速异常"


def test_positive_fan_rpm_passes_measurement_validation() -> None:
    cli._validate_captured_measurements(
        {
            "resource_monitor": {"cpu_temp": "55 °C", "device_fan_rpm": "1,250 转/分"},
            "fan_full_speed": {"cpu_temp": "55 °C", "device_fan_rpm": "2500 rpm"},
        }
    )


def test_cpu_temp_over_threshold_fails() -> None:
    captured = {
        "resource_monitor": {"cpu_temp": "55 °C", "device_fan_rpm": "1200 转/分"},
        "fan_normal": {"cpu_temp": "62 °C", "device_fan_rpm": "1500 转/分"},
        "fan_full_speed": {"cpu_temp": "70 °C", "device_fan_rpm": "2500 转/分"},
    }

    with pytest.raises(RuntimeError) as exc_info:
        cli._validate_captured_measurements(captured)

    msg = str(exc_info.value)
    assert "CPU 温度过高" in msg
    assert "风扇标准模式" in msg and "62" in msg
    assert "风扇全速模式" in msg and "70" in msg
    stage = cli.failure_stage_for_error(exc_info.value)
    assert stage.startswith("CPU 温度过高")
    assert "62℃" in stage and "60℃" in stage


def test_cpu_temp_resource_monitor_is_ignored() -> None:
    # resource_monitor 是满载尾巴的瞬时温度，不参与判定（见 6F6F 误报）。
    cli._validate_captured_measurements(
        {
            "resource_monitor": {"cpu_temp": "65 °C", "device_fan_rpm": "827 转/分"},
            "fan_normal": {"cpu_temp": "53 °C", "device_fan_rpm": "734 转/分"},
            "fan_silent": {"cpu_temp": "53 °C", "device_fan_rpm": "440 转/分"},
            "fan_full_speed": {"cpu_temp": "52 °C", "device_fan_rpm": "2454 转/分"},
        }
    )


def test_cpu_temp_at_threshold_passes() -> None:
    cli._validate_captured_measurements(
        {
            "resource_monitor": {"cpu_temp": "60 °C", "device_fan_rpm": "1200 转/分"},
            "fan_full_speed": {"cpu_temp": "60.0 °C", "device_fan_rpm": "2500 转/分"},
        }
    )


def test_cpu_temp_custom_threshold() -> None:
    captured = {
        "resource_monitor": {"cpu_temp": "55 °C", "device_fan_rpm": "1200 转/分"},
        "fan_full_speed": {"cpu_temp": "75 °C", "device_fan_rpm": "2500 转/分"},
    }
    cli._validate_captured_measurements(captured, cpu_temp_max_c=80)
    with pytest.raises(RuntimeError) as exc_info:
        cli._validate_captured_measurements(captured, cpu_temp_max_c=50)
    assert "CPU 温度过高" in str(exc_info.value)


def test_cpu_temp_missing_does_not_fail() -> None:
    cli._validate_captured_measurements(
        {
            "resource_monitor": {"device_fan_rpm": "1200 转/分"},
            "fan_full_speed": {"device_fan_rpm": "2500 转/分"},
        }
    )
