"""随机游走基线。

预测值 = 最后观测值（``drift=False``）或再加上 h 倍的历史平均日变动。

**为什么"什么都不做"的方法值得单独实现一个类** ——这是整个预测模块里
最重要的一段设计说明：

    在日频价格序列上，随机游走极难被打败。Meese 和 Rogoff 在 1983 年
    就发现，汇率模型在样本外几乎全都跑不赢随机游走；这个结论后来在
    股票、商品上都反复被验证。原因不神秘：价格的一阶自相关接近 0，
    也就是说"昨天到今天的变动"对"今天到明天的变动"几乎不提供信息。

    于是就有了本项目的核心立场：
        任何预测方法，如果不跟随机游走比 MSE，它报出来的"准确率"就没有意义
        ——那些数字可能只是把昨天的收盘价抄了一遍。

    所以这里不是一个凑数的实现，而是所有回测的**默认对照基准**，
    并且回测会把 ``skill = 1 - MSE_model / MSE_randomwalk`` 作为首要指标暴露出来。
    技能分为负，意味着这个方法在样本外还不如"预测明天和今天一样"。
"""

from __future__ import annotations

import math
from typing import List, Tuple

from .base import Forecaster
from .registry import register


@register
class RandomWalk(Forecaster):
    """随机游走 / 带漂移随机游走。"""

    name = "randomwalk"
    description = "随机游走基线（可选漂移）—— 所有预测方法的默认对照"

    def __init__(self, drift: bool = False) -> None:
        self.use_drift = bool(drift)
        self._last = 0.0
        self._drift = 0.0
        self._sigma = 0.0
        self._n = 0

    def fit(self, series: List[float]) -> None:
        if len(series) < 3:
            raise ValueError("随机游走基线至少需要 3 个观测")

        xs = [float(v) for v in series]
        self._n = len(xs)
        self._last = xs[-1]

        diffs = [xs[i] - xs[i - 1] for i in range(1, len(xs))]
        m = sum(diffs) / len(diffs)
        self._drift = m if self.use_drift else 0.0

        # 差分序列的样本标准差（ddof=1）
        if len(diffs) > 1:
            var = sum((d - m) ** 2 for d in diffs) / (len(diffs) - 1)
            self._sigma = math.sqrt(var)
        else:
            self._sigma = 0.0

    def predict(self, horizon: int) -> List[float]:
        return [self._last + self._drift * h for h in range(1, horizon + 1)]

    def interval(self, horizon: int, level: float = 0.95) -> List[Tuple[float, float]]:
        # 无漂移随机游走的 h 步预测误差方差 = h · σ²（增量独立同分布）。
        # 这是精确结论，不是启发式。
        z = 1.96 if abs(level - 0.95) < 1e-9 else _z_for(level)
        out: List[Tuple[float, float]] = []
        for h in range(1, horizon + 1):
            mid = self._last + self._drift * h
            band = z * self._sigma * math.sqrt(h)
            out.append((mid - band, mid + band))
        return out

    def meta(self) -> dict:
        return {
            "method_kind": "randomwalk",
            "last": round(self._last, 4),
            "drift_per_bar": round(self._drift, 6),
            "sigma_per_bar": round(self._sigma, 6),
            "use_drift": self.use_drift,
            "n_obs": self._n,
        }


def _z_for(level: float) -> float:
    """常见置信水平到标准正态分位数的查表。

    真要任意 level 就得实现 inverse CDF（Acklam 近似），
    但那属于"看起来很厉害、实际用不上"的复杂度。这里覆盖 90/95/99 就够了。
    """
    table = {0.90: 1.645, 0.95: 1.960, 0.98: 2.326, 0.99: 2.576}
    best = min(table, key=lambda k: abs(k - level))
    return table[best]
