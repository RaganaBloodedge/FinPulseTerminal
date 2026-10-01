"""合成行情数据源。

用途：离线演示、单元测试的确定性输入、压力测试用的长序列。

价格过程 = 几何布朗运动 + GARCH 风格波动率聚集 + 隔夜跳空 + 跳变混合。

**为什么不是纯 GBM。** 纯 GBM 的日收益是独立同分布的，拿它去算 RSI 会得到
一条几乎恒在 50 附近抖动的线，MACD 也几乎贴着零轴 —— 演示图一眼就假。
真实日线相对 GBM 最显著的三处偏离是：

    1. 波动率聚集（大波动之后还跟着大波动）→ 用 GARCH(1,1) 的方差递推；
    2. 收益厚尾 → 用 5% 概率的跳变混合近似（而不是完整的 t 分布，
       那需要 Gamma/卡方采样，为它引 SciPy 不值得）；
    3. 隔夜跳空 → 开盘价独立于前收盘生成。

补上这三点后，合成序列在年化波动率、超额峰度、最大回撤这些统计量上
才跟真实日线处在同一个量级，演示和回测才有意义。
"""

from __future__ import annotations

import logging
import math
import random
from typing import Any, List

from ..rpc import BadParams
from ..timeutil import recent_trading_days
from .base import Bar, DataSource
from .registry import register

log = logging.getLogger("finpulse.datasource.synthetic")

#: GARCH(1,1) 参数。取常见的股票日频量级：alpha+beta≈0.95 表示
#: 波动率冲击衰减得比较慢，符合实际观察。
_GARCH_ALPHA = 0.10
_GARCH_BETA = 0.85

#: 跳变混合：以该概率把当天的标准化冲击放大，制造厚尾
_JUMP_PROB = 0.05
_JUMP_SCALE = 2.5

#: 隔夜跳空的标准差，以当日条件波动的倍数表示
_GAP_SCALE = 0.35

_TRADING_DAYS_PER_YEAR = 252


@register
class SyntheticSource(DataSource):
    name = "synthetic"
    description = "几何布朗运动 + 波动率聚集的可复现合成日线"
    requires_network = False

    #: 给几个固定的演示符号，UI 下拉框直接可用
    _SYMBOLS = ["SYNTH", "DEMO-A", "DEMO-B", "DEMO-C"]

    def symbols(self) -> List[str]:
        return list(self._SYMBOLS)

    def load(  # type: ignore[override]
        self,
        symbol: str = "SYNTH",
        bars: int = 250,
        seed: int = 42,
        start_price: float = 100.0,
        annual_drift: float = 0.08,
        annual_vol: float = 0.28,
        **kwargs: Any,
    ) -> List[Bar]:
        if bars <= 0:
            raise BadParams("bars 必须为正整数")
        if bars > 20000:
            raise BadParams("bars 上限 20000（再多就不是合成演示而是压力测试了）")
        if annual_vol <= 0:
            raise BadParams("annual_vol 必须为正")

        rng = random.Random(int(seed))
        n = int(bars)
        ts_list = recent_trading_days(n)
        dt = 1.0 / _TRADING_DAYS_PER_YEAR

        # 单期方差。GARCH 的长期方差必须锚在 annual_vol 上，
        # 否则参数一改，实际波动率水平就跟调用方给的对不上了。
        #
        # 这里有个容易踩的坑：稳态方差满足
        #     E[var] = omega / (1 - alpha·E[z²] - beta)
        # 而跳变混合会把标准化冲击的二阶矩从 1 抬到
        #     E[z²] = (1-p)·1 + p·scale²  ≈ 1.26
        # 如果图省事用 (1 - alpha - beta) 当分母，实际年化波动率会比
        # annual_vol 高出约 45%——参数名叫 annual_vol 却给不出对应的
        # 波动率，是个会实实在在误导使用者的 bug（本项目第一版就写错了，
        # 是看 stats 输出的 53.9% 才发现）。
        # 这里有个容易踩的坑。一根 K 线的对数收益是
        #     log(C_t / C_{t-1}) = drift + gap_t + shock_t
        # 它由两部分随机项组成，所以无条件方差是
        #     Var = E[var] · (E[z²] + gap_scale²)
        # 而 E[z²] 因为跳变混合不是 1，是 (1-p) + p·scale² ≈ 1.26。
        # GARCH 的稳态又满足 E[var] = omega / (1 - alpha·E[z²] - beta)。
        # 两边联立解出 omega，才能真正让 annual_vol 名副其实。
        #
        # 第一版只用了 (1-alpha-beta)，结果 annual_vol=0.28 实测跑出 0.54；
        # 第二版补了跳变项但漏了跳空项，还有 0.38。
        # 是拿 stats 输出一项项对回去才发现前后两处都被漏掉的——
        # 这类"参数名和实际行为不一致"的 bug 不会报错，只会让人得出
        # 错误的结论，所以值得在这里把推导写清楚。
        alpha, beta = _GARCH_ALPHA, _GARCH_BETA
        e_z2 = (1.0 - _JUMP_PROB) + _JUMP_PROB * (_JUMP_SCALE ** 2)
        persistence = alpha * e_z2 + beta
        if persistence >= 1.0:
            raise BadParams(
                f"GARCH 参数不满足平稳性: alpha·E[z²]+beta = {persistence:.4f} >= 1"
            )
        total_scale = e_z2 + _GAP_SCALE ** 2
        var_long = (annual_vol ** 2) * dt
        omega = (var_long / total_scale) * (1.0 - persistence)
        var = var_long / total_scale

        # 用符号名做一个稳定的价格偏移：不同 symbol 看起来不同，
        # 但同一个 symbol 在不同进程里必须复现。
        # 注意不能用内置 hash() —— 字符串哈希带随机盐，跨进程不稳定。
        offset = sum(ord(c) for c in symbol) % 1000 / 1000.0
        price = float(start_price) * (0.6 + 0.8 * offset)

        out: List[Bar] = []
        drift_per_bar = annual_drift * dt

        for i in range(n):
            eps_std = math.sqrt(var)

            z = rng.normalvariate(0.0, 1.0)
            if rng.random() < _JUMP_PROB:
                z *= _JUMP_SCALE
            shock = eps_std * z

            # 隔夜跳空：开盘相对前收的偏离
            gap = rng.normalvariate(0.0, eps_std * _GAP_SCALE)

            prev_close = price
            open_ = prev_close * math.exp(gap)
            close = open_ * math.exp(drift_per_bar + shock)

            # 日内影线长度用半正态近似
            span = abs(rng.normalvariate(0.0, 1.0)) * eps_std * 0.8
            high = max(open_, close) * math.exp(span)
            low = min(open_, close) * math.exp(-span)

            # 成交量：与波动正相关（放量伴随大波动），叠一层对数正态噪声
            base_volume = 2_000_000.0
            volume = int(
                base_volume
                * math.exp(rng.normalvariate(0.0, 0.35))
                * (1.0 + 4.0 * abs(shock))
            )

            out.append(
                Bar(
                    ts=ts_list[i],
                    open=round(open_, 2),
                    high=round(high, 2),
                    low=round(low, 2),
                    close=round(close, 2),
                    volume=max(0, volume),
                )
            )

            # 推进波动率：这一期的冲击越大，下一期的方差越高
            var = omega + alpha * (shock ** 2) + beta * var
            price = close

        log.debug("合成 %s 共 %d 根（seed=%s, drift=%.2f, vol=%.2f）",
                  symbol, n, seed, annual_drift, annual_vol)
        return out
