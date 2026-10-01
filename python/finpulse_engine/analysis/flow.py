"""量价与波动状态分析。

为什么单独一个模块：``indicators`` 只看价格算均线动量，``stats`` 只看收益率
算风险。**成交量被两个模块都忽略了**——而"这段上涨是放量还是缩量"是价格
形态本身回答不了的问题。

这里的每一项都刻意做成"可验证的数值"，而不是"量能温和"这类形容词：
形容词没法进护栏检查，也没法在回测里复用。

和 indicators / stats 保持同一套约定：
* 只用历史数据，绝不看未来；
* 样本不足时返回 ``None``，**不返回 0**（0 是合法观测值，用 0 代替"不知道"
  会让下游把缺失当成事实）；
* 不依赖任何第三方库。
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence

#: A 股/美股通用的年化交易日数。与 stats.py 保持一致。
TRADING_DAYS = 252


def _mean(xs: Sequence[float]) -> Optional[float]:
    return sum(xs) / len(xs) if xs else None


def _stdev(xs: Sequence[float]) -> Optional[float]:
    n = len(xs)
    if n < 2:
        return None
    m = sum(xs) / n
    var = sum((x - m) ** 2 for x in xs) / (n - 1)
    return math.sqrt(var)


def _safe_ratio(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or b is None or b == 0:
        return None
    return a / b


def log_returns(closes: Sequence[float]) -> List[float]:
    """对数收益。比简单收益更适合做波动与自相关（可加性、对称性）。"""
    out: List[float] = []
    for i in range(1, len(closes)):
        prev, cur = closes[i - 1], closes[i]
        if prev > 0 and cur > 0:
            out.append(math.log(cur / prev))
    return out


def volume_autocorr_abs(returns: Sequence[float], lag: int = 1) -> Optional[float]:
    """|收益| 的 lag-k 自相关 —— 波动聚集的度量。

    收益率本身几乎不相关（有效市场的表现），但**绝对收益率**有明显正自相关：
    大波动后面跟着大波动。这就是 GARCH 类模型存在的理由。

    返回正数表示存在聚集；接近 0 表示波动接近独立。
    """
    n = len(returns)
    if n < lag + 8:
        return None
    a = [abs(r) for r in returns]
    m = sum(a) / n
    denom = sum((x - m) ** 2 for x in a)
    if denom <= 0:
        # 所有收益都相同（退化序列）：自相关无定义，不要硬凑一个 0 出来。
        return None
    num = sum((a[i] - m) * (a[i - lag] - m) for i in range(lag, n))
    return num / denom


def rolling_volatility(closes: Sequence[float], window: int = 20) -> List[Optional[float]]:
    """滚动年化波动率。前 ``window`` 个位置填 ``None``。"""
    rets = log_returns(closes)
    out: List[Optional[float]] = []
    sd = _stdev(rets[:window]) if len(rets) >= window else None
    out.append(sd * math.sqrt(TRADING_DAYS) if sd is not None else None)
    for i in range(1, len(rets)):
        lo = max(0, i - window + 1)
        sd = _stdev(rets[lo:i + 1]) if i + 1 - lo >= 2 else None
        out.append(sd * math.sqrt(TRADING_DAYS) if sd is not None else None)
    return out


def volatility_regime(closes: Sequence[float], window: int = 20) -> Dict[str, Any]:
    """当前波动率在自身历史分布里的位置。

    单看"波动率 28%"没法判断高还是低——得看它相对自己处于什么分位。
    这是波动率择时的基本做法，也是判断"当前是否适合趋势跟随"的依据。
    """
    series = [v for v in rolling_volatility(closes, window) if v is not None]
    if len(series) < 10:
        return {"state": None, "current": None, "percentile": None, "samples": len(series),
                "note": f"有效滚动窗口只有 {len(series)} 个，不足以判断分位"}

    current = series[-1]
    below = sum(1 for v in series if v < current)
    pct = below / len(series)
    if pct >= 0.75:
        state = "偏高"
    elif pct <= 0.25:
        state = "偏低"
    else:
        state = "中性"
    return {
        "state": state,
        "current": current,
        "percentile": pct,
        "min": min(series),
        "max": max(series),
        "samples": len(series),
        "window": window,
    }


def volume_profile(
    closes: Sequence[float],
    volumes: Sequence[float],
    highs: Optional[Sequence[float]] = None,
    lows: Optional[Sequence[float]] = None,
    window: int = 20,
) -> Dict[str, Any]:
    """量价画像。返回的每个字段都可能为 ``None``，调用方必须处理。"""
    n = min(len(closes), len(volumes))
    if n < window + 2:
        return {
            "bars": n,
            "note": f"样本 {n} 根，少于 window+2={window + 2}，量价指标不可靠",
            "avg_volume": None, "recent_avg_volume": None, "volume_ratio": None,
            "volume_trend": None, "up_day_avg_volume": None,
            "down_day_avg_volume": None, "up_down_volume_ratio": None,
            "autocorr_abs_ret": None, "volatility_regime": {},
            "divergence": None,
        }

    closes = list(closes[:n])
    volumes = list(volumes[:n])
    highs = list(highs[:n]) if highs else closes
    lows = list(lows[:n]) if lows else closes

    avg_all = _mean(volumes)
    recent = volumes[-window:]
    prev = volumes[-2 * window:-window]
    avg_recent = _mean(recent)
    avg_prev = _mean(prev)

    up_vols: List[float] = []
    down_vols: List[float] = []
    for i in range(1, n):
        if closes[i] > closes[i - 1]:
            up_vols.append(volumes[i])
        elif closes[i] < closes[i - 1]:
            down_vols.append(volumes[i])
    up_avg = _mean(up_vols)
    down_avg = _mean(down_vols)

    rets = log_returns(closes)
    autocorr = volume_autocorr_abs(rets)

    # ── 量价背离 ──
    # 价格创出区间新高/新低，但同期量能没有同步放大。
    # 只检查最近 window 根、且只跟**此前**的极值比 —— 不能用未来数据。
    divergence: Optional[Dict[str, Any]] = None
    if n >= 2 * window:
        prior_high = max(highs[:n - window])
        prior_low = min(lows[:n - window])
        recent_high = max(highs[-window:])
        recent_low = min(lows[-window:])
        vol_ratio = _safe_ratio(avg_recent, avg_prev)
        if recent_high > prior_high and vol_ratio is not None and vol_ratio < 1.0:
            divergence = {
                "type": "价格新高但量能萎缩",
                "price_prior_high": prior_high,
                "price_recent_high": recent_high,
                "volume_ratio": vol_ratio,
            }
        elif recent_low < prior_low and vol_ratio is not None and vol_ratio < 1.0:
            divergence = {
                "type": "价格新低但量能萎缩",
                "price_prior_low": prior_low,
                "price_recent_low": recent_low,
                "volume_ratio": vol_ratio,
            }

    return {
        "bars": n,
        "window": window,
        "avg_volume": avg_all,
        "recent_avg_volume": avg_recent,
        "volume_ratio": _safe_ratio(avg_recent, avg_all),
        "volume_trend": _safe_ratio(avg_recent, avg_prev),
        "up_day_avg_volume": up_avg,
        "down_day_avg_volume": down_avg,
        "up_down_volume_ratio": _safe_ratio(up_avg, down_avg),
        "up_days": len(up_vols),
        "down_days": len(down_vols),
        "autocorr_abs_ret": autocorr,
        "volatility_regime": volatility_regime(closes, window),
        "divergence": divergence,
        "note": None,
    }
