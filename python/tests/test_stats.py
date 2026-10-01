"""统计与风险指标测试。

约定三条（每条都有测试锁住）：
  1. 用简单收益率（不是对数收益率）；
  2. 年化按 252 个交易日；
  3. 样本不足返回 None，绝不返回 0 —— 0 会被下游当成"波动率真的是零"。
"""

from __future__ import annotations

import math
import unittest

from finpulse_engine.analysis import stats


class ReturnsTests(unittest.TestCase):
    def test_简单收益率(self):
        rs = stats.returns([100, 110, 121])
        self.assertEqual(len(rs), 2)
        for r in rs:
            self.assertAlmostEqual(r, 0.1)

    def test_前值为0时跳过而不是除零(self):
        self.assertEqual(stats.returns([0, 5]), [])

    def test_对数收益率(self):
        self.assertTrue(abs(stats.log_returns([1.0, math.e])[0] - 1.0) < 1e-12)

    def test_长度为_n减一(self):
        self.assertEqual(len(stats.returns([1, 2, 3, 4])), 3)


class BasicStatsTests(unittest.TestCase):
    def test_mean空序列返回None(self):
        self.assertIsNone(stats.mean([]))

    def test_stdev样本不足返回None(self):
        self.assertIsNone(stats.stdev([1.0]))       # n-ddof = 0
        self.assertIsNone(stats.stdev([]))

    def test_stdev已知值(self):
        self.assertTrue(abs(stats.stdev([1, 2, 3, 4, 5]) - 1.5811388) < 1e-6)

    def test_stdev_ddof0_是总体标准差(self):
        self.assertTrue(abs(stats.stdev([1, 2, 3, 4], ddof=0) - math.sqrt(1.25)) < 1e-12)


class ReturnMetricsTests(unittest.TestCase):
    def test_total_return(self):
        self.assertTrue(abs(stats.total_return([100, 110, 121]) - 0.21) < 1e-12)
        self.assertIsNone(stats.total_return([100]))
        self.assertIsNone(stats.total_return([0, 5]))

    def test_cagr_常数序列为0(self):
        self.assertEqual(stats.cagr([100, 100]), 0.0)

    def test_cagr_单元素返回None(self):
        self.assertIsNone(stats.cagr([100.0]))

    def test_cagr_一年翻倍等于100pct(self):
        # 253 个点 = 252 个交易日间隔 = 1 年，翻倍 → 100%
        p = [100.0] * 252 + [200.0]
        self.assertTrue(abs(stats.cagr(p) - 1.0) < 1e-9)

    def test_annualized_volatility_常数为0不是None(self):
        # 收益全为 0 → 标准差为 0，这是"真的是零"，与"算不出来"不同
        self.assertEqual(stats.annualized_volatility([100.0] * 30), 0.0)

    def test_annualized_volatility_样本不足返回None(self):
        self.assertIsNone(stats.annualized_volatility([100.0]))

    def test_sharpe_零波动返回None(self):
        self.assertIsNone(stats.sharpe([100.0] * 30))

    def test_sortino_没有下行样本返回None(self):
        self.assertIsNone(stats.sortino([100, 101, 102, 103]))

    def test_sortino只计下行(self):
        # 涨 5% / 跌 2% 交替：均值为正，而下行偏差只由 -2% 贡献，
        # 所以 sortino（分母更小）必须显著大于 sharpe
        px = [100.0]
        for i in range(1, 61):
            px.append(px[-1] * (1.05 if i % 2 else 0.98))
        s = stats.sharpe(px)
        so = stats.sortino(px)
        self.assertIsNotNone(s)
        self.assertIsNotNone(so)
        self.assertGreater(so, s)


class MaxDrawdownTests(unittest.TestCase):
    def test_精确回撤区间(self):
        # 峰 120@1，谷 90@2，回撤 -25%，第 3 根收复
        out = stats.max_drawdown([100, 120, 90, 130])
        self.assertAlmostEqual(out["value"], -0.25)
        self.assertEqual(out["peak_index"], 1)
        self.assertEqual(out["trough_index"], 2)
        self.assertEqual(out["recovery_index"], 3)

    def test_未收复时recovery为None(self):
        out = stats.max_drawdown([100, 120, 90])
        self.assertIsNone(out["recovery_index"])

    def test_单边上涨没有回撤(self):
        out = stats.max_drawdown([100, 110, 120])
        self.assertEqual(out["value"], 0.0)
        self.assertIsNone(out["recovery_index"])

    def test_样本不足(self):
        self.assertIsNone(stats.max_drawdown([100])["value"])


class VarTests(unittest.TestCase):
    def test_空序列(self):
        self.assertEqual(stats.value_at_risk([]), {"var": None, "cvar": None})

    def test_var是左尾分位数_cvar更深(self):
        rs = [-0.05, -0.03, -0.01, 0.0, 0.01, 0.02] * 10
        out = stats.value_at_risk(rs, 0.95)
        self.assertIsNotNone(out["var"])
        self.assertLessEqual(out["var"], 0.0)
        self.assertLessEqual(out["cvar"], out["var"])  # 条件期望不浅于分位点

    def test_均匀分布下的精确分位数(self):
        # 0..99 → sorted 后 5% 分位 = 4.95（线性插值，与 numpy.percentile 一致）
        out = stats.value_at_risk([float(i) for i in range(100)], 0.95)
        self.assertAlmostEqual(out["var"], 4.95)


class ShapeTests(unittest.TestCase):
    def test_对称序列偏度为0(self):
        self.assertEqual(stats.skewness([1, 2, 3, 4, 5]), 0.0)

    def test_对称序列的修正峰度为负1点2(self):
        # 手算: m2=2, m4=6.8, g2=-1.3, 修正后 = (2/3)*(6*(-1.3)+6) = -1.2
        self.assertAlmostEqual(stats.kurtosis([1, 2, 3, 4, 5]), -1.2)

    def test_样本不足返回None(self):
        self.assertIsNone(stats.skewness([1, 2]))
        self.assertIsNone(stats.kurtosis([1, 2, 3]))

    def test_常数序列返回None(self):
        self.assertIsNone(stats.skewness([5.0] * 10))
        self.assertIsNone(stats.kurtosis([5.0] * 10))

    def test_交替序列的lag1自相关(self):
        # [1,-1]*3: m=0, denom=6, num=5*(-1) → -5/6
        self.assertAlmostEqual(stats.autocorrelation([1, -1] * 3, 1), -5.0 / 6.0)

    def test_lag过大返回None(self):
        self.assertIsNone(stats.autocorrelation([1, 2, 3], 5))


class SummaryTests(unittest.TestCase):
    def test_字段齐全且键名稳定(self):
        # C++ 侧的风险面板直接渲染这些键，改名就是兼容性事故
        out = stats.summary([100, 110, 121])
        for key in ("bars", "total_return_pct", "ann_vol_pct", "sharpe", "sortino",
                    "max_drawdown_pct", "skew", "excess_kurtosis",
                    "autocorr_lag1", "var95_pct", "best_day_pct", "positive_days_pct"):
            self.assertIn(key, out)

    def test_单调上涨序列的指标(self):
        out = stats.summary([100, 110, 121])
        self.assertEqual(out["bars"], 3)
        self.assertEqual(out["total_return_pct"], 21.0)
        self.assertEqual(out["best_day_pct"], 10.0)
        self.assertEqual(out["worst_day_pct"], 10.0)
        self.assertEqual(out["positive_days_pct"], 100.0)
        self.assertEqual(out["max_drawdown_pct"], 0.0)

    def test_时间戳透传(self):
        out = stats.summary([100, 110], timestamps=[1000, 2000])
        self.assertEqual(out["first_ts"], 1000)
        self.assertEqual(out["last_ts"], 2000)

    def test_全None序列不崩溃(self):
        # 单点序列：各指标应为 None 而不是抛异常或返回假的 0
        out = stats.summary([100.0])
        self.assertEqual(out["bars"], 1)
        self.assertIsNone(out["ann_vol_pct"])


if __name__ == "__main__":
    unittest.main()
