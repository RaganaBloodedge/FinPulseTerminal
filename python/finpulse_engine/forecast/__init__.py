"""预测子包：基线、AR 模型、滚动回测。

新增预测方法 = 建个模块 + 写个类 + ``@register``。引擎启动时会自动发现，
并把它列进 handshake 上报的能力清单。
"""

from __future__ import annotations

from .base import Forecaster
from . import backtest, registry

__all__ = ["Forecaster", "backtest", "registry"]
