"""分析子包：技术指标与统计风险指标。

本包内所有模块都只依赖标准库，且都不做 IO——
输入是纯数值序列，输出是纯 Python 结构。因此它们可以被单元测试完整覆盖。
"""

from __future__ import annotations

from . import indicators, stats

__all__ = ["indicators", "stats"]
