"""描述性统计与风险指标。

三条约定：

1. 收益率默认用**简单收益率** ``r_t = P_t / P_{t-1} - 1``。
   对数收益率理论上更漂亮（可加性），但夏普、最大回撤这些指标业界普遍
   基于简单收益率计算。混用会让这里算出的数字跟别人报告的对不上——
   需要对数收益率时请显式调 ``log_returns()``。

2. 年化一律按 **252 个交易日**。

3. 样本不足时返回 ``None``，绝不返回 0。
   0 会被下游理解成"波动率真的是零"，那是个错误结论，而且不报错。

另外 ``autocorr_lag1`` 这个指标值得单独说：它衡量"昨天的价格对今天还有多少
解释力"。日频股票的这个值通常在 0 附近徘徊——这正是随机游走难以被打败的
经验证据，也是后面预测模块必须把随机游走作为基准的原因。
"""

from __future__ import annotations

import math
from typing import Dict, List, Optional, Sequence

TRADING_DAYS_PER_YEAR = 252


# ── 基础 ──────────────────────────────────────────────────

def returns(prices: Sequence[float]) -> List[float]:
    """简单收益率序列，长度为 ``len(prices) - 1``。"""
    out: List[float] = []
    for i in range(1, len(prices)):
        prev = float(prices[i - 1])
        if prev == 0.0:
            continue
        out.append(float(prices[i]) / prev - 1.0)
    return out


def log_returns(prices: Sequence[float]) -> List[float]:
    out: List[float] = []
    for i in range(1, len(prices)):
        prev = float(prices[i - 1])
        cur = float(prices[i])
        if prev <= 0.0 or cur <= 0.0:
            continue
        out.append(math.log(cur / prev))
    return out


def mean(xs: Sequence[float]) -> Optional[float]:
    return sum(xs) / len(xs) if xs else None


def stdev(xs: Sequence[float], ddof: int = 1) -> Optional[float]:
    n = len(xs)
    if n - ddof <= 0:
        return None
    m = sum(xs) / n
    var = sum((x - m) ** 2 for x in xs) / (n - ddof)
    return math.sqrt(var)


def _percentile(sorted_xs: Sequence[float], q: float) -> Optional[float]:
    """线性插值分位数，与 numpy.percentile 的默认行为一致。"""
    n = len(sorted_xs)
    if n == 0:
        return None
    if n == 1:
        return float(sorted_xs[0])
    pos = q * (n - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, n - 1)
    frac = pos - lo
    return float(sorted_xs[lo]) * (1.0 - frac) + float(sorted_xs[hi]) * frac


# ── 收益与风险 ────────────────────────────────────────────

def total_return(prices: Sequence[float]) -> Optional[float]:
    if len(prices) < 2 or float(prices[0]) == 0.0:
        return None
    return float(prices[-1]) / float(prices[0]) - 1.0


def cagr(prices: Sequence[float]) -> Optional[float]:
    """复合年化增长率。按交易日数折成年。"""
    n = len(prices)
    if n < 2 or float(prices[0]) <= 0.0:
        return None
    years = (n - 1) / TRADING_DAYS_PER_YEAR
    if years <= 0:
        return None
    growth = float(prices[-1]) / float(prices[0])
    if growth <= 0:
        return None
    return growth ** (1.0 / years) - 1.0


def annualized_volatility(prices: Sequence[float], ddof: int = 1) -> Optional[float]:
    """日收益标准差 × sqrt(252)。

    这里隐含假设收益不相关（方差可加）。对日频股票这是个不错的近似，
    但如果你拿它去算高频数据，会显著高估年化波动率——
    因为高频收益存在负自相关。
    """
    rs = returns(prices)
    sd = stdev(rs, ddof)
    return None if sd is None else sd * math.sqrt(TRADING_DAYS_PER_YEAR)


def sharpe(prices: Sequence[float], risk_free_annual: float = 0.0) -> Optional[float]:
    rs = returns(prices)
    sd = stdev(rs)
    if sd is None or sd == 0.0:
        return None
    rf_daily = risk_free_annual / TRADING_DAYS_PER_YEAR
    excess = [r - rf_daily for r in rs]
    m = sum(excess) / len(excess)
    return m / sd * math.sqrt(TRADING_DAYS_PER_YEAR)


def sortino(prices: Sequence[float], risk_free_annual: float = 0.0) -> Optional[float]:
    """索提诺比率：只把**下行**波动当作风险。

    不同于夏普把上涨波动也当风险——很多实务派认为这更贴近投资者的真实感受。
    没有下行样本时返回 None（不是无穷大，那个数字没法用）。
    """
    rs = returns(prices)
    if not rs:
        return None
    rf_daily = risk_free_annual / TRADING_DAYS_PER_YEAR
    excess = [r - rf_daily for r in rs]
    downside = [x for x in excess if x < 0.0]
    if not downside:
        return None
    dd = math.sqrt(sum(x * x for x in downside) / len(downside))
    if dd == 0.0:
        return None
    m = sum(excess) / len(excess)
    return m / dd * math.sqrt(TRADING_DAYS_PER_YEAR)


def max_drawdown(prices: Sequence[float]) -> Dict[str, object]:
    """最大回撤及其发生区间。

    返回的 ``recovery_index`` 为 None 表示到样本结束还没收复失地——
    这个信息本身很重要（"深度回撤 + 未修复"和"浅回撤 + 已修复"是两个故事）。
    """
    if len(prices) < 2:
        return {"value": None, "peak_index": None, "trough_index": None, "recovery_index": None}

    peak = float(prices[0])
    peak_idx = 0
    worst = 0.0
    worst_peak = 0
    worst_trough = 0

    for i, p in enumerate(prices):
        p = float(p)
        if p > peak:
            peak = p
            peak_idx = i
        if peak > 0:
            dd = p / peak - 1.0
            if dd < worst:
                worst = dd
                worst_peak = peak_idx
                worst_trough = i

    recovery = None
    if worst < 0.0:
        peak_value = float(prices[worst_peak])
        for i in range(worst_trough + 1, len(prices)):
            if float(prices[i]) >= peak_value:
                recovery = i
                break

    return {
        "value": worst if worst < 0.0 else 0.0,
        "peak_index": worst_peak,
        "trough_index": worst_trough,
        "recovery_index": recovery,
    }


def value_at_risk(rs: Sequence[float], confidence: float = 0.95) -> Dict[str, Optional[float]]:
    """历史模拟法 VaR / CVaR（期望损失）。

    没有假设正态分布——日收益的厚尾会让正态假设下的 VaR 系统性偏低，
    而这恰好是最危险的方向。历史模拟法直接取经验分位数，代价是需要
    足够长的样本才稳（本项目默认给 250 根，够用但不宽裕）。
    """
    if not rs:
        return {"var": None, "cvar": None}
    ordered = sorted(rs)
    var = _percentile(ordered, 1.0 - confidence)
    if var is None:
        return {"var": None, "cvar": None}
    tail = [r for r in ordered if r <= var]
    cvar = (sum(tail) / len(tail)) if tail else var
    return {"var": var, "cvar": cvar}


# ── 分布形态 ──────────────────────────────────────────────

def skewness(xs: Sequence[float]) -> Optional[float]:
    """样本偏度（Fisher–Pearson 的 G1，含小样本修正）。

    股票日收益通常是**负偏**的：涨的时候慢慢涨，跌的时候一天跌掉很多。
    正偏的股票收益序列基本可以断定数据有问题。
    """
    n = len(xs)
    if n < 3:
        return None
    m = sum(xs) / n
    m2 = sum((x - m) ** 2 for x in xs) / n
    if m2 == 0:
        return None
    m3 = sum((x - m) ** 3 for x in xs) / n
    g1 = m3 / (m2 ** 1.5)
    return math.sqrt(n * (n - 1)) / (n - 2) * g1


def kurtosis(xs: Sequence[float]) -> Optional[float]:
    """超额峰度（正态分布基准为 0）。

    股票日收益的超额峰度常见在 3~10。接近 0 说明数据被平滑过，
    或者根本就是合成的（本项目自己的合成源会给出约 3~8，因为它带了跳变混合）。
    """
    n = len(xs)
    if n < 4:
        return None
    m = sum(xs) / n
    m2 = sum((x - m) ** 2 for x in xs) / n
    if m2 == 0:
        return None
    m4 = sum((x - m) ** 4 for x in xs) / n
    g2 = m4 / (m2 * m2) - 3.0
    return (n - 1) / ((n - 2) * (n - 3)) * ((n + 1) * g2 + 6.0)


def autocorrelation(xs: Sequence[float], lag: int = 1) -> Optional[float]:
    """滞后 ``lag`` 期的自相关系数。

    用总体均值/方差做分母（而不是前 n-lag 个子样本的），
    这样分母一致，不同 lag 之间可以直接比较。
    """
    n = len(xs)
    if n <= lag + 1:
        return None
    m = sum(xs) / n
    denom = sum((x - m) ** 2 for x in xs)
    if denom == 0:
        return None
    num = sum((xs[i] - m) * (xs[i - lag] - m) for i in range(lag, n))
    return num / denom


# ── 汇总 ──────────────────────────────────────────────────

def _pct(v: Optional[float]) -> Optional[float]:
    return None if v is None else round(v * 100.0, 4)


def _num(v: Optional[float], nd: int = 4) -> Optional[float]:
    if v is None or not math.isfinite(v):
        return None
    return round(v, nd)


def summary(
    prices: Sequence[float],
    timestamps: Optional[Sequence[int]] = None,
    risk_free_annual: float = 0.0,
) -> Dict[str, object]:
    """一次性算完所有指标。

    C++ 侧的"风险概览"面板就是这个函数的输出直接渲染的，
    所以字段名要稳定，改了要同步改 UI。
    """
    px = [float(p) for p in prices]
    rs = returns(px)
    mdd = max_drawdown(px)
    var = value_at_risk(rs, 0.95)

    out: Dict[str, object] = {
        "bars": len(px),
        "first_price": _num(px[0], 4) if px else None,
        "last_price": _num(px[-1], 4) if px else None,
        "total_return_pct": _pct(total_return(px)),
        "cagr_pct": _pct(cagr(px)),
        "ann_vol_pct": _pct(annualized_volatility(px)),
        "daily_vol_pct": _pct(stdev(rs)),
        "sharpe": _num(sharpe(px, risk_free_annual), 3),
        "sortino": _num(sortino(px, risk_free_annual), 3),
        "max_drawdown_pct": _pct(mdd["value"]),  # type: ignore[arg-type]
        "max_drawdown_peak_index": mdd["peak_index"],
        "max_drawdown_trough_index": mdd["trough_index"],
        "max_drawdown_recovery_index": mdd["recovery_index"],
        "skew": _num(skewness(rs), 4),
        "excess_kurtosis": _num(kurtosis(rs), 4),
        "autocorr_lag1": _num(autocorrelation(rs, 1), 4),
        "autocorr_lag5": _num(autocorrelation(rs, 5), 4),
        "var95_pct": _pct(var["var"]),
        "cvar95_pct": _pct(var["cvar"]),
        "best_day_pct": _pct(max(rs)) if rs else None,
        "worst_day_pct": _pct(min(rs)) if rs else None,
        "positive_days_pct": _pct(sum(1 for r in rs if r > 0) / len(rs)) if rs else None,
    }

    if timestamps is not None and len(timestamps) == len(px):
        out["first_ts"] = int(timestamps[0])
        out["last_ts"] = int(timestamps[-1])

    return out
