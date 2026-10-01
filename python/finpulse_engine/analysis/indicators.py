"""技术指标。

三条贯穿全文件的约定：

1. **输出与输入等长。** 每个指标都返回 ``len(values)`` 长度的列表，
   前面数据不足的位置填 ``None``。这样 C++ 侧画图时指标线和 K 线可以
   直接按下标对齐，不需要再维护一套"指标从第几根开始"的偏移量逻辑。
   代价是每算一个指标都要过一遍序列，但这个规模（几千个点）完全无所谓。

2. **滚动窗口内只用历史数据。** 所有实现都严格在 ``[i-period+1, i]``
   范围内取值。任何"用了未来数据"的实现都会让回测结果虚高，
   而且极难从结果上看出来。

3. **不引第三方库。** 见包 docstring 里的说明。
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence

Num = Optional[float]
Series = List[Num]


# ── 基础工具 ──────────────────────────────────────────────

def _as_floats(values: Sequence[float]) -> List[float]:
    return [float(v) for v in values]


def _none_filled(n: int) -> Series:
    return [None] * n


def _round_opt(v: Optional[float], nd: int = 6) -> Num:
    if v is None:
        return None
    if not math.isfinite(v):
        return None
    return round(v, nd)


# ── 均线族 ────────────────────────────────────────────────

def sma(values: Sequence[float], period: int) -> Series:
    """简单移动平均。

    用滑动和（加上一个减出）把复杂度压到 O(n)，而不是每个窗口重算一遍。
    """
    if period <= 0:
        raise ValueError("period 必须为正")
    xs = _as_floats(values)
    n = len(xs)
    out: Series = _none_filled(n)
    if n < period:
        return out

    window_sum = sum(xs[:period])
    out[period - 1] = _round_opt(window_sum / period)
    for i in range(period, n):
        window_sum += xs[i] - xs[i - period]
        out[i] = _round_opt(window_sum / period)
    return out


def ema(values: Sequence[float], period: int) -> Series:
    """指数移动平均。

    种子取第一个观测值本身（而不是前 period 个的均值）。两种约定都有人用，
    差别只体现在前 period 根上；选前者是因为它让序列从第 0 根就有值，
    画图时不会出现一段空白。
    """
    if period <= 0:
        raise ValueError("period 必须为正")
    xs = _as_floats(values)
    n = len(xs)
    out: Series = _none_filled(n)
    if n == 0:
        return out

    k = 2.0 / (period + 1.0)
    prev = xs[0]
    out[0] = _round_opt(prev)
    for i in range(1, n):
        prev = xs[i] * k + prev * (1.0 - k)
        out[i] = _round_opt(prev)
    return out


# ── 动量族 ────────────────────────────────────────────────

def rsi(values: Sequence[float], period: int = 14) -> Series:
    """相对强弱指标，Wilder 平滑。

    注意这里用的不是简单平均，而是 Wilder 的递归平滑
    ``avg = (avg*(p-1) + cur) / p`` —— 它等价于 alpha = 1/p 的 EMA。
    用简单平均算出来的是另一个指标，数值会有可见差异。
    """
    if period <= 0:
        raise ValueError("period 必须为正")
    xs = _as_floats(values)
    n = len(xs)
    out: Series = _none_filled(n)
    if n <= period:
        return out

    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        delta = xs[i] - xs[i - 1]
        if delta >= 0:
            gains += delta
        else:
            losses -= delta

    avg_gain = gains / period
    avg_loss = losses / period
    out[period] = _round_opt(_rsi_value(avg_gain, avg_loss))

    for i in range(period + 1, n):
        delta = xs[i] - xs[i - 1]
        gain = delta if delta > 0 else 0.0
        loss = -delta if delta < 0 else 0.0
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        out[i] = _round_opt(_rsi_value(avg_gain, avg_loss))
    return out


def _rsi_value(avg_gain: float, avg_loss: float) -> float:
    if avg_loss == 0.0:
        # 全涨：RSI 定义为 100。全平（gain 也是 0）时没有动量，
        # 返回 50 比返回 100 更符合直觉。
        return 100.0 if avg_gain > 0 else 50.0
    rs = avg_gain / avg_loss
    return 100.0 - 100.0 / (1.0 + rs)


def macd(
    values: Sequence[float],
    fast: int = 12,
    slow: int = 26,
    signal: int = 9,
) -> Dict[str, Series]:
    """MACD。

    返回三条线：macd（差离值）、signal（信号线）、hist（柱）。
    柱 = macd - signal，这个符号约定要与常见看盘软件一致，
    否则同一段行情在别人软件里是红的、在你这里是绿的。
    """
    if not (0 < fast < slow):
        raise ValueError("要求 0 < fast < slow")
    if signal <= 0:
        raise ValueError("signal 必须为正")

    xs = _as_floats(values)
    ema_fast = ema(xs, fast)
    ema_slow = ema(xs, slow)

    macd_line: Series = _none_filled(len(xs))
    for i in range(len(xs)):
        f, s = ema_fast[i], ema_slow[i]
        macd_line[i] = None if (f is None or s is None) else _round_opt(f - s)

    # 信号线是 MACD 线的 EMA。MACD 线从第 0 根就有值（因为 EMA 种子取首值），
    # 所以信号线也从第 0 根开始，不会有额外的空洞。
    raw_signal = ema([0.0 if v is None else v for v in macd_line], signal)

    hist: Series = _none_filled(len(xs))
    out_signal: Series = _none_filled(len(xs))
    for i in range(len(xs)):
        m, s = macd_line[i], raw_signal[i]
        if m is None or s is None:
            continue
        out_signal[i] = _round_opt(s)
        hist[i] = _round_opt(m - s)

    return {"macd": macd_line, "signal": out_signal, "hist": hist}


# ── 波动族 ────────────────────────────────────────────────

def bollinger(
    values: Sequence[float],
    period: int = 20,
    num_std: float = 2.0,
) -> Dict[str, Series]:
    """布林带。

    标准差用**总体**标准差（分母 N），不是样本标准差（分母 N-1）。
    这是技术分析领域的通行约定（John Bollinger 本人的定义），
    虽然统计书上更常教的是样本标准差。
    """
    if period <= 0:
        raise ValueError("period 必须为正")
    if num_std <= 0:
        raise ValueError("num_std 必须为正")

    xs = _as_floats(values)
    n = len(xs)
    mid: Series = _none_filled(n)
    upper: Series = _none_filled(n)
    lower: Series = _none_filled(n)
    if n < period:
        return {"mid": mid, "upper": upper, "lower": lower}

    # 滑动和 + 滑动平方和 → O(n)
    s = 0.0
    s2 = 0.0
    for i in range(n):
        s += xs[i]
        s2 += xs[i] * xs[i]
        if i >= period:
            s -= xs[i - period]
            s2 -= xs[i - period] * xs[i - period]
        if i >= period - 1:
            mean = s / period
            var = max(0.0, s2 / period - mean * mean)  # 理论上非负，浮点可能给出 -1e-18
            sd = math.sqrt(var)
            mid[i] = _round_opt(mean)
            upper[i] = _round_opt(mean + num_std * sd)
            lower[i] = _round_opt(mean - num_std * sd)
    return {"mid": mid, "upper": upper, "lower": lower}


def true_range(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float]) -> Series:
    """真实波幅 TR = max(H-L, |H-C_prev|, |L-C_prev|)。

    第 0 根没有前收，退化成 H-L。
    """
    n = len(closes)
    out: Series = _none_filled(n)
    for i in range(n):
        if i == 0:
            out[i] = _round_opt(highs[i] - lows[i])
            continue
        prev_close = closes[i - 1]
        out[i] = _round_opt(
            max(highs[i] - lows[i], abs(highs[i] - prev_close), abs(lows[i] - prev_close))
        )
    return out


def atr(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 14,
) -> Series:
    """平均真实波幅（Wilder 平滑，与 RSI 用同一种平滑）。"""
    tr = [0.0 if v is None else v for v in true_range(highs, lows, closes)]
    return _wilder_smooth(tr, period)


def _wilder_smooth(xs: Sequence[float], period: int) -> Series:
    if period <= 0:
        raise ValueError("period 必须为正")
    n = len(xs)
    out: Series = _none_filled(n)
    if n < period:
        return out

    acc = sum(xs[:period]) / period
    out[period - 1] = _round_opt(acc)
    for i in range(period, n):
        acc = (acc * (period - 1) + xs[i]) / period
        out[i] = _round_opt(acc)
    return out


def stochastic(
    highs: Sequence[float],
    lows: Sequence[float],
    closes: Sequence[float],
    period: int = 14,
    smooth_d: int = 3,
) -> Dict[str, Series]:
    """随机指标 KD（%K 与 %D）。

    这里用的是"慢速"约定：%K 已经是原始随机值的 smooth_d 期简单平均，
    %D 再对 %K 平滑一次。国内看盘软件默认走的就是慢速版本。
    """
    if period <= 0:
        raise ValueError("period 必须为正")
    n = len(closes)
    raw_k: Series = _none_filled(n)

    for i in range(period - 1, n):
        window_high = max(highs[i - period + 1: i + 1])
        window_low = min(lows[i - period + 1: i + 1])
        span = window_high - window_low
        # 一字板（最高=最低）时分母为 0，约定取 50（中性）
        raw_k[i] = 50.0 if span == 0 else (closes[i] - window_low) / span * 100.0

    filled = [0.0 if v is None else v for v in raw_k]
    k_line = sma(filled, smooth_d) if smooth_d > 1 else raw_k
    d_line = sma([0.0 if v is None else v for v in k_line], smooth_d)
    # 上面两次 sma 会在开头造出一段"用 0 填充"的假值，补回 None
    for i in range(n):
        if raw_k[i] is None:
            k_line[i] = None
            d_line[i] = None
    return {"k": k_line, "d": d_line}


# ── 规格解析与分发 ────────────────────────────────────────

SUPPORTED = ("ma", "ema", "rsi", "macd", "boll", "atr", "stoch")


def compute_spec(
    spec: str,
    closes: Sequence[float],
    highs: Sequence[float],
    lows: Sequence[float],
) -> Dict[str, Any]:
    """执行一条指标规格，如 ``"ma:5,20"``、``"macd:12,26,9"``。"""
    if ":" in spec:
        kind, _, arg_text = spec.partition(":")
        args = [float(a) for a in arg_text.split(",") if a.strip()]
    else:
        kind, args = spec, []

    kind = kind.strip().lower()
    int_args = [int(a) for a in args]

    if kind == "ma":
        periods = int_args or [5, 20]
        return {
            "spec": spec, "kind": "ma",
            "lines": {f"ma{p}": sma(closes, p) for p in periods},
            "meta": {"periods": periods},
        }

    if kind == "ema":
        periods = int_args or [12, 26]
        return {
            "spec": spec, "kind": "ema",
            "lines": {f"ema{p}": ema(closes, p) for p in periods},
            "meta": {"periods": periods},
        }

    if kind == "rsi":
        period = int_args[0] if int_args else 14
        return {
            "spec": spec, "kind": "rsi",
            "lines": {f"rsi{period}": rsi(closes, period)},
            "meta": {"period": period},
        }

    if kind == "macd":
        fast, slow, sig = (int_args + [12, 26, 9])[:3]
        return {
            "spec": spec, "kind": "macd",
            "lines": macd(closes, fast, slow, sig),
            "meta": {"fast": fast, "slow": slow, "signal": sig},
        }

    if kind == "boll":
        period = int_args[0] if int_args else 20
        num_std = args[1] if len(args) > 1 else 2.0
        return {
            "spec": spec, "kind": "boll",
            "lines": bollinger(closes, period, num_std),
            "meta": {"period": period, "num_std": num_std},
        }

    if kind == "atr":
        period = int_args[0] if int_args else 14
        return {
            "spec": spec, "kind": "atr",
            "lines": {f"atr{period}": atr(highs, lows, closes, period)},
            "meta": {"period": period},
        }

    if kind == "stoch":
        period = int_args[0] if int_args else 14
        smooth_d = int_args[1] if len(int_args) > 1 else 3
        return {
            "spec": spec, "kind": "stoch",
            "lines": stochastic(highs, lows, closes, period, smooth_d),
            "meta": {"period": period, "smooth_d": smooth_d},
        }

    raise ValueError(f"不支持的指标: {kind!r}（可用: {', '.join(SUPPORTED)}）")
