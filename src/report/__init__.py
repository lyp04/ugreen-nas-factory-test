"""故障上报（自动 GitHub Issue + 日志打包）。

对应隔壁 internal-factory-test 的 `report` 包，但用纯 stdlib（urllib/zipfile）实现，
不引入任何新依赖，且整个包在 macOS 上也能 import（不碰 Windows-only 的 ctypes）。

只在「测试失败且归类为其他/未分类」时由 cli.run_test 自动触发：
非读写速度 / 非转速 / 非温度 / 非存储池 的故障，把该 SN 的全部日志收齐上报。
"""
from __future__ import annotations

from .reporter import FaultReporter, get_reporter, report_failure

__all__ = ["FaultReporter", "get_reporter", "report_failure"]
