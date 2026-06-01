import pytest

from src import cli


def test_zero_fan_rpm_fails_measurement_validation() -> None:
    captured = {
        # resource_monitor 不参与风扇判定，即便 0 也不算失败……
        "resource_monitor": {"cpu_temp": "55 °C", "device_fan_rpm": "0 转/分"},
        # ……但风扇全速模式 0 转速一定是真故障。
        "fan_full_speed": {"cpu_temp": "55 °C", "device_fan_rpm": "0 转/分"},
    }

    with pytest.raises(RuntimeError) as exc_info:
        cli._validate_captured_measurements(captured)

    msg = str(exc_info.value)
    assert "风扇转速异常" in msg
    assert "风扇全速模式" in msg
    assert "资源监控" not in msg  # resource_monitor 已从风扇判定中排除
    assert cli.failure_stage_for_error(exc_info.value) == "风扇转速异常"


def test_resource_monitor_zero_rpm_ignored() -> None:
    # 实测复现（HB670EE52241F8CD）：整机偏凉时 resource_monitor 与安静模式都读到 0 转，
    # 但风扇全速能上到 2400+，说明风扇没坏——不应判失败。
    cli._validate_captured_measurements(
        {
            "resource_monitor": {"cpu_temp": "42 °C", "device_fan_rpm": "0 转/分"},
            "fan_normal": {"cpu_temp": "42 °C", "device_fan_rpm": "285 转/分"},
            "fan_silent": {"cpu_temp": "42 °C", "device_fan_rpm": "0 转/分"},
            "fan_full_speed": {"cpu_temp": "43 °C", "device_fan_rpm": "2419 转/分"},
        }
    )


def test_positive_fan_rpm_passes_measurement_validation() -> None:
    cli._validate_captured_measurements(
        {
            "resource_monitor": {"cpu_temp": "55 °C", "device_fan_rpm": "1,250 转/分"},
            "fan_full_speed": {"cpu_temp": "55 °C", "device_fan_rpm": "2500 rpm"},
        }
    )


def test_fan_silent_zero_rpm_passes() -> None:
    # 安静（静音）模式风扇可完全停转，0 转速属正常工况，不判失败。
    cli._validate_captured_measurements(
        {
            "resource_monitor": {"cpu_temp": "55 °C", "device_fan_rpm": "827 转/分"},
            "fan_normal": {"cpu_temp": "53 °C", "device_fan_rpm": "734 转/分"},
            "fan_silent": {"cpu_temp": "52 °C", "device_fan_rpm": "0 转/分"},
            "fan_full_speed": {"cpu_temp": "52 °C", "device_fan_rpm": "2454 转/分"},
        }
    )


def test_zero_fan_rpm_still_fails_for_non_silent_pages() -> None:
    # 只豁免安静模式；其余模式 0 转速仍判失败。
    with pytest.raises(RuntimeError) as exc_info:
        cli._validate_captured_measurements(
            {
                "fan_silent": {"cpu_temp": "52 °C", "device_fan_rpm": "0 转/分"},
                "fan_normal": {"cpu_temp": "52 °C", "device_fan_rpm": "0 转/分"},
            }
        )

    msg = str(exc_info.value)
    assert "风扇转速异常" in msg
    assert "风扇标准模式" in msg
    assert "风扇静音模式" not in msg


def test_cpu_temp_over_threshold_fails() -> None:
    captured = {
        "resource_monitor": {"cpu_temp": "55 °C", "device_fan_rpm": "1200 转/分"},
        "fan_normal": {"cpu_temp": "75 °C", "device_fan_rpm": "1500 转/分"},
        "fan_full_speed": {"cpu_temp": "72 °C", "device_fan_rpm": "2500 转/分"},
    }

    with pytest.raises(RuntimeError) as exc_info:
        cli._validate_captured_measurements(captured)

    msg = str(exc_info.value)
    assert "CPU 温度过高" in msg
    assert "风扇标准模式" in msg and "75" in msg
    assert "风扇全速模式" in msg and "72" in msg
    stage = cli.failure_stage_for_error(exc_info.value)
    assert stage.startswith("CPU 温度过高")
    assert "75℃" in stage and "70℃" in stage


def test_cpu_temp_resource_monitor_is_ignored() -> None:
    # resource_monitor 是满载尾巴的瞬时温度，不参与判定（见 6F6F 误报）。
    # 用高于阈值的 75℃ 确保即便被检查也会失败，从而验证它确实被忽略。
    cli._validate_captured_measurements(
        {
            "resource_monitor": {"cpu_temp": "75 °C", "device_fan_rpm": "827 转/分"},
            "fan_normal": {"cpu_temp": "53 °C", "device_fan_rpm": "734 转/分"},
            "fan_silent": {"cpu_temp": "53 °C", "device_fan_rpm": "440 转/分"},
            "fan_full_speed": {"cpu_temp": "52 °C", "device_fan_rpm": "2454 转/分"},
        }
    )


def test_cpu_temp_at_threshold_passes() -> None:
    # 恰好等于阈值（70℃）通过；只有严格大于 70℃ 才判失败。
    cli._validate_captured_measurements(
        {
            "resource_monitor": {"cpu_temp": "70 °C", "device_fan_rpm": "1200 转/分"},
            "fan_full_speed": {"cpu_temp": "70.0 °C", "device_fan_rpm": "2500 转/分"},
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
