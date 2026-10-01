"""AR(p) 自回归模型。

模型形式::

    x_t = c + φ₁·x_{t-1} + φ₂·x_{t-2} + … + φ_p·x_{t-p} + e_t

参数用最小二乘估计：把滞后项拼成设计矩阵 X，解正规方程 ``β = (XᵀX)⁻¹Xᵀy``。

**为什么自己写 OLS 而不引 numpy / statsmodels**

    p 通常 ≤ 10，正规方程的系数矩阵规模是 (p+1)×(p+1)——最大 11×11。
    带部分主元的高斯消元解它，数值上完全够用，代码不到 40 行。
    而引入 numpy 会让引擎的发布包从几百 KB 涨到 30 MB 以上，
    并且给"用户机器上装不上依赖"增加一个失败点。
    在"引擎必须随桌面包一起分发"这个前提下，这个交换不划算。

    这是一个明确的工程取舍，不是能力上限：真要上大模型时，
    整个 forecaster 层就是替换点——``@register`` 一个新类即可，
    调用方一行都不用改。
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

from .base import Forecaster
from .registry import register

_MIN_OBS = 12


@register
class AutoRegressive(Forecaster):
    """AR(p)。``order=0`` 表示用 AIC 自动选阶。"""

    name = "ar"
    description = "AR(p) 自回归，最小二乘估计，支持 AIC 自动选阶"

    def __init__(self, order: int = 0, max_order: int = 8) -> None:
        self.order = int(order)
        self.max_order = max(1, int(max_order))
        self._selected = 0
        self._beta: List[float] = []
        self._sigma = 0.0
        self._aic: Optional[float] = None
        self._series: List[float] = []
        self._rss = 0.0

    # ── 拟合 ──────────────────────────────────────────────

    def fit(self, series: List[float]) -> None:
        xs = [float(v) for v in series]
        if len(xs) < _MIN_OBS:
            raise ValueError(f"AR 拟合至少需要 {_MIN_OBS} 个观测（当前 {len(xs)}）")

        self._series = xs

        if self.order > 0:
            p = min(self.order, self.max_order)
            fit = self._fit_order(xs, p)
            self._adopt(fit)
            self._aic = None
            return

        # AIC 自动选阶。上界除了 max_order，还要受样本量约束：
        # 每个待估参数至少要有几个观测，否则 AIC 会一路偏爱更高阶
        # （过拟合的经典表现）。
        upper = min(self.max_order, max(1, len(xs) // 6))
        best: Optional[Dict[str, Any]] = None
        best_aic = float("inf")

        for p in range(1, upper + 1):
            if len(xs) - p < p + 3:
                break
            try:
                fit = self._fit_order(xs, p)
            except ValueError:
                break
            aic = self._aic_of(fit)
            if aic < best_aic:
                best_aic, best = aic, fit

        if best is None:
            # 极端短序列（刚过 _MIN_OBS 门槛）救一下：退到 p=1
            best = self._fit_order(xs, 1)
            best_aic = self._aic_of(best)

        self._adopt(best)
        self._aic = best_aic

    def _adopt(self, fit: Dict[str, Any]) -> None:
        self._selected = int(fit["p"])
        self._beta = list(fit["beta"])
        self._sigma = float(fit["sigma"])
        self._rss = float(fit["rss"])

    def _fit_order(self, xs: List[float], p: int) -> Dict[str, Any]:
        X, y = self._design(xs, p)
        if len(y) <= len(X[0]) + 1:
            raise ValueError("样本不足以拟合该阶数")

        beta = self._ols(X, y)
        resid = [
            y[i] - sum(beta[j] * X[i][j] for j in range(len(beta)))
            for i in range(len(y))
        ]
        rss = sum(r * r for r in resid)
        dof = len(y) - len(beta)
        sigma = math.sqrt(rss / dof) if dof > 0 else float("inf")
        return {
            "p": p,
            "beta": beta,
            "rss": rss,
            "sigma": sigma,
            "n": len(y),
            "k": len(beta),
        }

    @staticmethod
    def _design(xs: List[float], p: int) -> Tuple[List[List[float]], List[float]]:
        """构造设计矩阵。第 i 行是 ``[1, x_{p+i-1}, …, x_i]``，对应目标 ``x_{p+i}``。

        注意下标方向：β₁ 配的是**最近**的一期 x_{t-1}，这与 predict() 里的
        取用顺序必须一致，写反了会让系数看起来"反着转"，而 MSE 居然还很正常
        —— 属于能跑但结论全错的那类 bug。
        """
        n = len(xs)
        X: List[List[float]] = []
        y: List[float] = []
        for t in range(p, n):
            X.append([1.0] + [xs[t - i] for i in range(1, p + 1)])
            y.append(xs[t])
        return X, y

    @staticmethod
    def _ols(X: List[List[float]], y: List[float]) -> List[float]:
        """解正规方程 XᵀX β = Xᵀy。"""
        k = len(X[0])
        A = [[0.0] * k for _ in range(k)]
        b = [0.0] * k

        for i, row in enumerate(X):
            yi = y[i]
            for a in range(k):
                ra = row[a]
                b[a] += ra * yi
                for c in range(a, k):
                    A[a][c] += ra * row[c]
        # 对称，补齐下三角
        for a in range(k):
            for c in range(a):
                A[a][c] = A[c][a]

        return AutoRegressive._gauss_solve(A, b)

    @staticmethod
    def _gauss_solve(A: List[List[float]], b: List[float]) -> List[float]:
        """带部分主元的高斯消元。

        部分主元（每列选绝对值最大的行交换）是必需的，不是讲究：
        滞后项之间高度相关，系数矩阵常常接近奇异，不做主元交换
        会累积出可观的数值误差。
        """
        n = len(b)
        M = [A[i][:] + [b[i]] for i in range(n)]

        for col in range(n):
            pivot = max(range(col, n), key=lambda r: abs(M[r][col]))
            if abs(M[pivot][col]) < 1e-12:
                raise ValueError("自回归设计矩阵奇异（序列可能近似常数，或阶数过高）")
            if pivot != col:
                M[col], M[pivot] = M[pivot], M[col]

            pv = M[col][col]
            for r in range(col + 1, n):
                factor = M[r][col] / pv
                if factor == 0.0:
                    continue
                for c in range(col, n + 1):
                    M[r][c] -= factor * M[col][c]

        x = [0.0] * n
        for r in range(n - 1, -1, -1):
            s = M[r][n] - sum(M[r][c] * x[c] for c in range(r + 1, n))
            x[r] = s / M[r][r]
        return x

    @staticmethod
    def _aic_of(fit: Dict[str, Any]) -> float:
        n, k, rss = fit["n"], fit["k"], fit["rss"]
        if rss <= 0.0:
            return float("-inf")  # 完美拟合（通常意味着数据有病）
        return n * math.log(rss / n) + 2.0 * k

    # ── 预测 ──────────────────────────────────────────────

    def predict(self, horizon: int) -> List[float]:
        if not self._beta:
            raise RuntimeError("predict 之前必须先 fit")
        return [p for p, _ in self._rolling_forecast(horizon)]

    def _rolling_forecast(self, horizon: int) -> List[Tuple[float, float]]:
        """逐点递推预测，同时返回每一步的预测误差方差。

        多步预测必须把**自己的预测值**填回历史再接下一步，
        不能用真实值（那是前视偏差，会把样本外表现伪造得极好）。
        """
        p = self._selected
        phi = self._beta[1:]
        hist = list(self._series)
        psi = self._psi_weights(phi, horizon)

        out: List[Tuple[float, float]] = []
        cum_psi2 = 0.0
        for h in range(1, horizon + 1):
            xhat = self._beta[0] + sum(phi[i] * hist[-(i + 1)] for i in range(p))
            # h 步预测误差 = Σ_{j=0}^{h-1} ψ_j · e_{t+h-j}，方差 = σ²·Σψ_j²
            cum_psi2 += psi[h - 1] ** 2
            var = (self._sigma ** 2) * cum_psi2
            out.append((xhat, var))
            hist.append(xhat)
        return out

    def interval(self, horizon: int, level: float = 0.95) -> List[Tuple[float, float]]:
        z = 1.96 if abs(level - 0.95) < 1e-9 else _z_for(level)
        out = []
        for xhat, var in self._rolling_forecast(horizon):
            band = z * math.sqrt(max(var, 0.0))
            out.append((xhat - band, xhat + band))
        return out

    @staticmethod
    def _psi_weights(phi: List[float], horizon: int) -> List[float]:
        """AR 过程 MA(∞) 表示的 ψ 权重。

        把 AR 写成 ``x_t = μ + Σ_{j≥0} ψ_j · e_{t-j}``，递推关系是::

            ψ₀ = 1
            ψ_j = Σ_{i=1..min(j,p)} φ_i · ψ_{j-i}

        于是 h 步预测误差恰好是 ``Σ_{j=0}^{h-1} ψ_j · e_{t+h-j}``，
        方差为 ``σ² · Σ_{j=0}^{h-1} ψ_j²``。

        很多实现图省事直接用 ``σ·√h``（随机游走的公式），
        但那只有在 φ 全为 0 时才正确。系数显著非零时，
        √h 会**明显高估**不确定性——比如 φ=0.8 的 AR(1)，h=10 时
        真实的标准差只有 √h 那个数的约 2/3。
        既然已经拟合出了系数，用精确公式没有任何额外代价。
        """
        psi = [1.0]
        for j in range(1, horizon):
            s = 0.0
            for i in range(1, min(j, len(phi)) + 1):
                s += phi[i - 1] * psi[j - i]
            psi.append(s)
        return psi

    # ── 自述 ──────────────────────────────────────────────

    def meta(self) -> Dict[str, Any]:
        return {
            "method_kind": "ar",
            "order": self._selected,
            "intercept": round(self._beta[0], 6) if self._beta else None,
            "phi": [round(v, 6) for v in self._beta[1:]],
            "sigma": round(self._sigma, 6),
            "rss": round(self._rss, 4),
            "aic": None if self._aic is None else round(self._aic, 3),
            "auto_selected": self.order <= 0,
            "n_obs": len(self._series),
        }

    def summary_phi(self) -> Dict[str, float]:
        """系数持久性：Σφ 接近 1 说明序列近似单位根（也就是随机游走）。"""
        return {"sum_phi": round(sum(self._beta[1:]), 6) if self._beta else 0.0}


def _z_for(level: float) -> float:
    table = {0.90: 1.645, 0.95: 1.960, 0.98: 2.326, 0.99: 2.576}
    best = min(table, key=lambda k: abs(k - level))
    return table[best]
