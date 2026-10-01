"""预测与回测测试。

三个最有分量的断言：
  1. AR(1) 的 OLS 能把已知系数**精确恢复**回来（数值正确性的硬证据）；
  2. ψ 权重递推与理论值一致（预测区间不是 σ√h 的糊弄版）；
  3. 随机游走对随机游走回测的技能分**恒等于 0**（基线没有被写强或写弱）。
"""

from __future__ import annotations

import math
import unittest

from finpulse_engine.forecast.ar import AutoRegressive
from finpulse_engine.forecast.backtest import evaluate, run, walk_forward, FoldResult
from finpulse_engine.forecast.randomwalk import RandomWalk, _z_for


class RandomWalkTests(unittest.TestCase):
    def test_样本不足抛错(self):
        with self.assertRaises(ValueError):
            RandomWalk().fit([1.0, 2.0])

    def test_无漂移预测等于最后观测(self):
        m = RandomWalk()
        m.fit([10.0, 11.0, 12.0])
        self.assertEqual(m.predict(3), [12.0, 12.0, 12.0])

    def test_带漂移按h线性外推(self):
        m = RandomWalk(drift=True)
        m.fit([1.0, 2.0, 3.0])          # 平均日变动 = 1
        self.assertEqual(m.predict(3), [4.0, 5.0, 6.0])

    def test_区间用精确的_z乘σ乘根号h(self):
        # diffs=[1,2,3] → m=2, 样本方差=((1)²+0+(1)²)/2=1 → σ=1
        m = RandomWalk()
        m.fit([1.0, 2.0, 4.0, 7.0])
        bands = m.interval(2)
        self.assertAlmostEqual(bands[0][0], 7.0 - 1.96 * 1.0 * 1.0)
        self.assertAlmostEqual(bands[0][1], 7.0 + 1.96 * 1.0 * 1.0)
        self.assertAlmostEqual(bands[1][0], 7.0 - 1.96 * 1.0 * math.sqrt(2))
        self.assertAlmostEqual(bands[1][1], 7.0 + 1.96 * 1.0 * math.sqrt(2))

    def test_sigma为零时区间退化为点(self):
        m = RandomWalk()
        m.fit([1.0, 2.0, 3.0])          # diffs 全为 1 → σ=0
        self.assertEqual(m.interval(3), [(3.0, 3.0)] * 3)

    def test_置信水平查表(self):
        self.assertEqual(_z_for(0.90), 1.645)
        self.assertEqual(_z_for(0.99), 2.576)

    def test_meta字段(self):
        m = RandomWalk(drift=True)
        m.fit([1.0, 2.0, 4.0])
        meta = m.meta()
        for key in ("method_kind", "last", "drift_per_bar", "sigma_per_bar",
                    "use_drift", "n_obs"):
            self.assertIn(key, meta)
        self.assertEqual(meta["method_kind"], "randomwalk")
        self.assertTrue(meta["use_drift"])


def make_ar1(n: int = 60, c: float = 2.0, phi: float = 0.5) -> list:
    """无噪声 AR(1)：x_t = c + phi·x_{t-1}。OLS 应当把它精确恢复。"""
    xs = [1.0]
    for _ in range(n - 1):
        xs.append(c + phi * xs[-1])
    return xs


class AutoRegressiveTests(unittest.TestCase):
    def test_样本不足抛错(self):
        with self.assertRaises(ValueError):
            AutoRegressive().fit([1.0] * 11)     # _MIN_OBS = 12

    def test_OLS精确恢复无噪声AR1系数(self):
        m = AutoRegressive(order=1)
        m.fit(make_ar1(60, c=2.0, phi=0.5))
        self.assertAlmostEqual(m._beta[0], 2.0, places=6)
        self.assertAlmostEqual(m._beta[1], 0.5, places=6)
        self.assertAlmostEqual(m._sigma, 0.0, places=6)

    def test_predict必须先fit(self):
        with self.assertRaises(RuntimeError):
            AutoRegressive().predict(3)

    def test_预测按递推公式走(self):
        m = AutoRegressive(order=1)
        xs = make_ar1(40)
        m.fit(xs)
        last = xs[-1]
        expect = [last]
        for _ in range(3):
            expect.append(2.0 + 0.5 * expect[-1])
        got = m.predict(3)
        for g, e in zip(got, expect[1:]):
            self.assertAlmostEqual(g, e, places=6)

    def test_多步预测把预测值填回历史而不是真实值(self):
        # 前视偏差的检验：递推只能用自己算出来的值
        m = AutoRegressive(order=1)
        xs = make_ar1(40, c=2.0, phi=0.5)
        m.fit(xs)
        h1 = m.predict(1)[0]
        # 第 2 步只能从 h1 递推，与"直接用最后真实值"的结果不同
        self.assertAlmostEqual(m.predict(2)[1], 2.0 + 0.5 * h1, places=6)

    def test_AIC自动选阶(self):
        m = AutoRegressive(order=0)      # 0 = 自动
        m.fit(make_ar1(80))
        self.assertEqual(m.meta()["auto_selected"], True)
        self.assertGreaterEqual(m._selected, 1)
        self.assertIsNotNone(m._aic)

    def test_design矩阵的方向(self):
        # 第 t 行是 [1, x_{t-1}, x_{t-2}] —— β₁ 配最近一期。写反了结论全错还不报错
        X, y = AutoRegressive._design([1.0, 2.0, 3.0, 4.0, 5.0], 2)
        self.assertEqual(X[0], [1.0, 2.0, 1.0])
        self.assertEqual(y[0], 3.0)
        self.assertEqual(X[2], [1.0, 4.0, 3.0])
        self.assertEqual(y[2], 5.0)

    def test_高斯消元解已知方程组(self):
        # 2x + y = 3 ; x + 3y = 5 → x=0.8, y=1.4
        got = AutoRegressive._gauss_solve([[2.0, 1.0], [1.0, 3.0]], [3.0, 5.0])
        self.assertAlmostEqual(got[0], 0.8, places=9)
        self.assertAlmostEqual(got[1], 1.4, places=9)

    def test_奇异矩阵抛错(self):
        with self.assertRaises(ValueError):
            AutoRegressive._gauss_solve([[1.0, 2.0], [2.0, 4.0]], [1.0, 2.0])

    def test_psi权重递推(self):
        # AR(1) φ=0.8 → ψ = [1, 0.8, 0.64, 0.512]（浮点上用近似比较）
        psi = AutoRegressive._psi_weights([0.8], 4)
        for got, want in zip(psi, [1.0, 0.8, 0.64, 0.512]):
            self.assertAlmostEqual(got, want)

    def test_psi权重_phi为空时归零(self):
        self.assertEqual(AutoRegressive._psi_weights([], 3), [1.0, 0.0, 0.0])

    def test_AR2的psi与理论一致(self):
        # ψ_j = φ₁·ψ_{j-1} + φ₂·ψ_{j-2}
        psi = AutoRegressive._psi_weights([0.5, 0.2], 4)
        self.assertAlmostEqual(psi[0], 1.0)
        self.assertAlmostEqual(psi[1], 0.5)
        self.assertAlmostEqual(psi[2], 0.5 * 0.5 + 0.2 * 1.0)   # 0.45
        self.assertAlmostEqual(psi[3], 0.5 * 0.45 + 0.2 * 0.5)  # 0.325

    def test_interval_随h先增后趋于平稳(self):
        # 平稳 AR 的区间宽度不应像 σ√h 那样无界增长。
        # 需要含噪序列：无噪声时 σ≈0，区间宽度全为 0，断言就没有意义了。
        import random

        rng = random.Random(42)
        xs = [1.0]
        for _ in range(199):
            xs.append(2.0 + 0.5 * xs[-1] + rng.gauss(0.0, 1.0))
        m = AutoRegressive(order=1)
        m.fit(xs)
        bands = m.interval(20)
        w = [hi - lo for lo, hi in bands]
        self.assertGreater(w[4], w[0])      # 初期变宽
        self.assertLess(w[19] / w[10], math.sqrt(19 / 10) * 1.05)  # 远慢于 √h

    def test_summary_phi(self):
        m = AutoRegressive(order=1)
        m.fit(make_ar1(60))
        self.assertAlmostEqual(m.summary_phi()["sum_phi"], 0.5, places=6)

    def test_meta在fit前不崩溃(self):
        meta = AutoRegressive().meta()
        self.assertEqual(meta["order"], 0)
        self.assertIsNone(meta["intercept"])


class WalkForwardTests(unittest.TestCase):
    def setUp(self):
        self.prices = [100.0 + 0.3 * i + math.sin(i / 9.0) * 8 for i in range(300)]

    def test_数据不足给出明确的数量说明(self):
        # min_train=60 + 5折×5步 = 85；80 根不够，报错要把这笔账算给人看
        with self.assertRaises(ValueError) as ctx:
            walk_forward(self.prices[:80], RandomWalk, folds=5, horizon=5, min_train=60)
        self.assertIn("需要至少 85", str(ctx.exception))

    def test_参数校验(self):
        for kw in ({"folds": 0}, {"horizon": 0}, {"min_train": 1}):
            with self.assertRaises(ValueError):
                walk_forward(self.prices, RandomWalk, **kw)

    def test_锚点必须是训练集最后一点(self):
        # backtest.py 文档里点名的那个"写错结论全反"的 bug，断言在这里兜底
        results = walk_forward(self.prices, RandomWalk, folds=5, horizon=5, min_train=60)
        n = len(self.prices)
        for f in results:
            t = n - 25 + f.index * 5
            self.assertAlmostEqual(f.anchor, self.prices[t - 1])

    def test_每折训练集扩张(self):
        results = walk_forward(self.prices, RandomWalk, folds=5, horizon=5, min_train=60)
        sizes = [f.train_size for f in results]
        self.assertEqual(sizes, sorted(sizes))
        self.assertEqual(len(set(sizes)), 5)

    def test_随机游走模型预测值等于锚点(self):
        results = walk_forward(self.prices, RandomWalk, folds=5, horizon=5, min_train=60)
        for f in results:
            for p in f.predictions:
                self.assertAlmostEqual(p, f.anchor)

    def test_区间存在(self):
        results = walk_forward(self.prices, RandomWalk, folds=3, horizon=5, min_train=60)
        self.assertTrue(results[0].band_low)
        self.assertEqual(len(results[0].band_low), 5)

    def test_reported_coverage(self):
        f = FoldResult(index=0, train_size=10, anchor=100.0,
                       predictions=[100, 100], actuals=[100, 200],
                       band_low=[90, 90], band_high=[110, 110])
        self.assertAlmostEqual(f.reported_coverage, 0.5)
        self.assertTrue(math.isnan(FoldResult(0, 10, 1, [1], [1]).reported_coverage))


class EvaluateTests(unittest.TestCase):
    def test_随机游走对随机游走技能分恒为0(self):
        # 基线既没被写强也没被写弱 —— 这是 evaluate 最重要的不变量
        prices = [100.0 + 0.3 * i + math.sin(i / 7.0) * 8 for i in range(200)]
        out = run(prices, RandomWalk, folds=5, horizon=5, min_train=60)
        self.assertAlmostEqual(out["skill"], 0.0)
        self.assertAlmostEqual(out["mae"], out["base_mae"])
        self.assertAlmostEqual(out["rmse"], out["base_rmse"])

    def test_base_dir_acc固定50(self):
        # 随机游走不带方向信息，记 0% 会人为抬高技能分 —— 固定成 50% 才诚实
        prices = [100.0 + 0.3 * i for i in range(150)]
        out = run(prices, RandomWalk, folds=3, horizon=5, min_train=60)
        self.assertEqual(out["base_dir_acc"], 50.0)

    def test_空折抛错(self):
        with self.assertRaises(ValueError):
            evaluate([], 5)

    def test_报告字段齐全(self):
        prices = [100.0 + 0.3 * i for i in range(150)]
        out = run(prices, RandomWalk, folds=3, horizon=5, min_train=60)
        for key in ("folds", "horizon", "n_predictions", "mae", "rmse", "mape",
                    "dir_acc", "base_mae", "base_rmse", "base_dir_acc", "skill",
                    "interval_coverage", "per_fold"):
            self.assertIn(key, out)
        self.assertEqual(out["n_predictions"], 15)
        self.assertEqual(len(out["per_fold"]), 3)

    def test_完美预测技能分为1(self):
        # 一个"永远预测对"的模型：skill 应该恰好是 1.0。
        # Oracle 通过闭包拿到完整序列 —— 它是刻意作弊的，用来校验 evaluate 的数学；
        # 注意 predict 必须按"训练集长度"切片，这正是 walk_forward 折切分的位置。
        prices = [100.0 + i for i in range(120)]

        class Oracle(RandomWalk):
            def fit(self, series):
                self._offset = len(series)
                super().fit(series)

            def predict(self, horizon):
                return prices[self._offset:self._offset + horizon]

        results = walk_forward(prices, Oracle, folds=4, horizon=5, min_train=60)
        out = evaluate(results, 5)
        self.assertAlmostEqual(out["skill"], 1.0)
        self.assertAlmostEqual(out["mae"], 0.0)


if __name__ == "__main__":
    unittest.main()
