"""Walk-forward 滚动回测。

**为什么不能"整段拟合 + 整段评估"**

    在一整段历史上拟合参数、再在同一段上算误差，等于让模型提前看到了答案。
    任何有点自由度的模型在这种评估下都会漂亮得不像话，而这个"漂亮"
    完全是幻觉。walk-forward 强制每个预测点只使用它**之前**的数据。

**一个很容易写错、写错后果又特别严重的细节**

    随机游走基线的预测值必须取**训练集最后一个**观测（即 `prices[t-1]`），
    而不是测试集里的任何一点。如果手滑写成用了测试集的价格，
    基线会变得异常强大，真正的模型会显得一无是处——
    反过来如果基线被写弱了，模型又会显得很厉害。
    这两种 bug 都不会报错，只会让你得出完全相反的结论。
    所以下面 FoldResult.anchor 是显式存下来的，并在测试里专门断言过。
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Sequence

from .base import Forecaster


@dataclass
class FoldResult:
    """一个折的预测结果。"""

    index: int
    train_size: int
    anchor: float                      # 训练集最后一点，也是随机游走基线的预测值
    predictions: List[float]
    actuals: List[float]
    band_low: List[float] = field(default_factory=list)
    band_high: List[float] = field(default_factory=list)

    @property
    def reported_coverage(self) -> float:
        """区间覆盖率：真实值落进预测区间的比例。"""
        if not self.band_low or not self.band_high:
            return float("nan")
        hits = sum(
            1
            for a, lo, hi in zip(self.actuals, self.band_low, self.band_high)
            if lo <= a <= hi
        )
        return hits / len(self.actuals)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "train_size": self.train_size,
            "anchor": round(self.anchor, 4),
            "predictions": [round(p, 4) for p in self.predictions],
            "actuals": [round(a, 4) for a in self.actuals],
        }


def walk_forward(
    prices: Sequence[float],
    make_model: Callable[[], Forecaster],
    folds: int = 5,
    horizon: int = 5,
    min_train: int = 60,
) -> List[FoldResult]:
    """扩张窗口的 walk-forward 回测。

    把序列末尾 ``folds × horizon`` 个点均匀切成 ``folds`` 段作为测试集，
    第 k 折用 ``prices[:test_start + k*horizon]`` 训练。

    ``make_model`` 是"造一个未拟合模型"的工厂，而不是模型实例本身——
    每折都必须拿全新的实例，复用同一个实例是最容易引入前视偏差的写法。
    """
    if folds < 1:
        raise ValueError("folds 必须 >= 1")
    if horizon < 1:
        raise ValueError("horizon 必须 >= 1")
    if min_train < 2:
        raise ValueError("min_train 必须 >= 2")

    n = len(prices)
    needed = min_train + folds * horizon
    if n < needed:
        raise ValueError(
            f"数据不足以做 walk-forward：需要至少 {needed} 个观测"
            f"（min_train={min_train} + folds={folds} × horizon={horizon}），当前 {n}"
        )

    test_start = n - folds * horizon
    results: List[FoldResult] = []

    for k in range(folds):
        t = test_start + k * horizon
        train = list(prices[:t])
        actual = [float(v) for v in prices[t:t + horizon]]

        model = make_model()
        model.fit(train)
        pred = [float(v) for v in model.predict(horizon)]

        low: List[float] = []
        high: List[float] = []
        try:
            bands = model.interval(horizon)
            low = [float(lo) for lo, _ in bands]
            high = [float(hi) for _, hi in bands]
        except Exception:  # noqa: BLE001 - 区间是增强项，没有不影响主流程
            low, high = [], []

        results.append(
            FoldResult(
                index=k,
                train_size=len(train),
                anchor=float(prices[t - 1]),
                predictions=pred,
                actuals=actual,
                band_low=low,
                band_high=high,
            )
        )

    return results


def evaluate(results: List[FoldResult], horizon: int) -> Dict[str, Any]:
    """把各折结果汇总成指标。所有误差都在**价格**原尺度上计算。"""
    if not results:
        raise ValueError("没有可评估的折")

    sq_m = sq_b = 0.0
    abs_m = abs_b = 0.0
    pct_sum = 0.0
    n = n_pct = 0
    dir_hits = dir_total = 0
    coverage_hits = coverage_total = 0
    per_fold: List[Dict[str, Any]] = []

    for f in results:
        fold_sq_m = fold_sq_b = 0.0
        fold_n = 0
        for p, a in zip(f.predictions, f.actuals):
            em = p - a
            eb = f.anchor - a
            sq_m += em * em
            sq_b += eb * eb
            abs_m += abs(em)
            abs_b += abs(eb)
            fold_sq_m += em * em
            fold_sq_b += eb * eb
            if a != 0.0:
                pct_sum += abs(em / a)
                n_pct += 1
            n += 1
            fold_n += 1
            # 方向命中：参照点是锚点（也就是"发布预测那一刻"的价格）
            if a != f.anchor:
                dir_total += 1
                if (p > f.anchor) == (a > f.anchor):
                    dir_hits += 1

        for a, lo, hi in zip(f.actuals, f.band_low, f.band_high):
            coverage_total += 1
            if lo <= a <= hi:
                coverage_hits += 1

        per_fold.append({
            "index": f.index,
            "train_size": f.train_size,
            "rmse": round(math.sqrt(fold_sq_m / fold_n), 4) if fold_n else None,
            "base_rmse": round(math.sqrt(fold_sq_b / fold_n), 4) if fold_n else None,
        })

    if n == 0:
        # 折存在但没有任何预测（例如模型返回了空列表）。
        # 静默产出 skill=0 会违背"样本不足绝不返回 0"的包级约定 —— 宁可报错。
        raise ValueError("折中没有可评估的预测结果（模型 predict 返回了空列表？）")

    mse_m = sq_m / n
    mse_b = sq_b / n
    skill = (1.0 - mse_m / mse_b) if mse_b > 0.0 else 0.0

    return {
        "folds": len(results),
        "horizon": horizon,
        "n_predictions": n,
        "mae": round(abs_m / n, 4),
        "rmse": round(math.sqrt(mse_m), 4),
        "mape": round(pct_sum / n_pct * 100.0, 4) if n_pct else None,
        "dir_acc": round(dir_hits / dir_total * 100.0, 2) if dir_total else None,
        "base_mae": round(abs_b / n, 4),
        "base_rmse": round(math.sqrt(mse_b), 4),
        # 随机游走预测"价格不变"，不携带任何方向信息。
        # 把它的方向命中率记成 0% 会人为抬高所有模型的技能分——
        # 按无信息处理成 50% 才诚实。
        "base_dir_acc": 50.0,
        "skill": round(skill, 4),
        "interval_coverage": (
            round(coverage_hits / coverage_total, 4) if coverage_total else None
        ),
        "per_fold": per_fold,
    }


def run(
    prices: Sequence[float],
    make_model: Callable[[], Forecaster],
    folds: int = 5,
    horizon: int = 5,
    min_train: int = 60,
) -> Dict[str, Any]:
    """walk_forward + evaluate 的便利入口。"""
    return evaluate(walk_forward(prices, make_model, folds, horizon, min_train), horizon)
