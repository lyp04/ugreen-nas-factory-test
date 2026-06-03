"""耗时饼图：排队等待 / 跨会话恢复的空闲时间不得计入测试耗时。

修复「20 分钟的机器饼图显示跑了两个小时」：build_timing_slices 之前从 task.logs 的
第一行（"已加入队列" 用入队时刻打戳，或恢复时并入的上一次磁盘 run.log）开始累计，
把排队/跨会话空闲算成了一个阶段。

gui 经由 cli 会 import browser_control（Windows 专属 WinDLL），故先塞桩再 import，
这样本测试在 macOS/Linux/Windows 都能跑。
"""
from __future__ import annotations

import sys
import types

if "src.utils.browser_control" not in sys.modules:
    _stub = types.ModuleType("src.utils.browser_control")
    for _name in (
        "close_managed_context",
        "launch_managed_context",
        "show_browser_windows",
        "terminate_browser_process",
    ):
        setattr(_stub, _name, lambda *a, **k: None)
    sys.modules["src.utils.browser_control"] = _stub

from src import gui  # noqa: E402


def _total(logs: list[str]) -> int:
    return sum(s.seconds for s in gui.build_timing_slices(logs))


# 13:27:42 → 14:00:05 的一次真实运行，约 32 分钟。
_RUN = [f"13:{m:02d}:00 | INFO    | Capturing page" for m in range(28, 60, 2)] + [
    "14:00:05 | INFO    | NAS 192.168.0.186: device lock released"
]
_RUN_SECONDS = _total(_RUN)


def test_plain_run_is_about_half_hour() -> None:
    assert 30 * 60 <= _RUN_SECONDS <= 34 * 60


def test_queue_wait_is_not_counted() -> None:
    # 12:00 入队、13:28 才开跑：修复前会把 ~1.5h 排队算进去（总 ~2h）。
    queued = ["12:00:00 | INFO    | SN HB670 已加入队列，来源=auto，模式=setup"] + _RUN
    assert _total(queued) == _RUN_SECONDS


def test_cross_session_restore_gap_is_not_counted() -> None:
    # 上一次会话 11:00~11:30 的旧 run.log + 13:27 恢复行 + 本次运行：
    # 修复前会把 ~2h 跨会话空闲（外加旧运行本身）算进去（总 ~3h）。
    old = [f"11:{m:02d}:00 | INFO    | (上次会话旧日志)" for m in range(0, 31, 3)]
    restored = old + ["13:27:00 | INFO    | 已从上次队列恢复"] + _RUN
    assert _total(restored) == _RUN_SECONDS


def test_auto_resume_marker_resets_too() -> None:
    old = [f"10:{m:02d}:00 | INFO    | (更早的旧日志)" for m in range(0, 31, 3)]
    restored = old + ["13:27:00 | INFO    | 检测到设备仍然在线，自动恢复执行"] + _RUN
    assert _total(restored) == _RUN_SECONDS


def test_within_run_waits_are_preserved() -> None:
    # 运行内部的真实等待（如等传输槽 7 分钟）必须保留，不能被误当成边界丢弃。
    with_wait = [
        "13:28:00 | INFO    | hdd_write: transfer slot busy, waiting on hold",
        "13:35:00 | INFO    | hdd_write: slot acquired",  # 7 分钟空档
        "13:36:00 | INFO    | done",
    ]
    assert _total(with_wait) >= 7 * 60
