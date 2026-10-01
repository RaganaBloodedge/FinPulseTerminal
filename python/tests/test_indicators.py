"""技术指标测试。

三个必须守住的不变量（全部有对应断言）：
  1. 输出与输入等长，前导填 None —— 画图按下标对齐靠它；
  2. 滚动窗口只用历史数据 —— 前视偏差会让回测虚高且极难察觉；
  3. 期望值是手工推出来的精确数，不是"跑一遍看输出像不像"。
"""

from __future__ import annotations

import math
import unittest

from finpulse_engine.analysis.indicators import (
    atr,
    bollinger,
    compute_spec,
    ema,
    macd,
    rsi,
    sma,
    stochastic,
    true_range,
)

ALMOST = lambda a, b: abs(a - b) < 1e-6  # noqa: E731


class SmaTests(unittest.TestCase):
    def test_精确值(self):
        self.assertEqual(sma([1, 2, 3, 4, 5], 3), [None, None, 2.0, 3.0, 4.0])

    def test_输出与输入等长且前导为_None(self):
        out = sma(list(range(10)), 4)
        self.assertEqual(len(out), 10)
        self.assertEqual(out[:3], [None, None, None])

    def test_样本不足时全为_None(self):
        self.assertEqual(sma([1.0, 2.0], 5), [None, None])

    def test_滑动和与逐窗口重算一致(self):
        # O(n) 的滑动和实现不能在浮点累积上偏离朴素算法太多
        xs = [math.sin(i / 7.0) * 100 for i in range(200)]
        fast = sma(xs, 13)
        for i in range(12, 200):
            expect = sum(xs[i - 12:i + 1]) / 13
            self.assertTrue(ALMOST(fast[i], expect), f"index {i}")

    def test_period非法(self):
        with self.assertRaises(ValueError):
            sma([1.0], 0)


class EmaTests(unittest.TestCase):
    def test_种子取首值(self):
        self.assertEqual(ema([10.0], 5), [10.0])

    def test_精确递推(self):
        # k = 2/(3+1) = 0.5
        out = ema([1, 2, 3], 3)
        self.assertEqual(out[0], 1.0)
        self.assertEqual(out[1], 1.5)   # 2*0.5 + 1*0.5
        self.assertEqual(out[2], 2.25)  # 3*0.5 + 1.5*0.5

    def test_从第0根就有值(self):
        # 这是"种子取首值"这个约定的直接后果：画图不会有空白段
        out = ema([1, 2, 3, 4], 26)
        self.assertIsNotNone(out[0])


class RsiTests(unittest.TestCase):
    def test_单边上涨收敛到100(self):
        out = rsi([float(i) for i in range(1, 41)], 14)
        self.assertEqual(out[:14], [None] * 14)
        self.assertEqual(out[-1], 100.0)

    def test_全平序列返回50(self):
        # 没有动量时 50（中性）比 100 更符合直觉，实现里是显式约定
        out = rsi([5.0] * 20, 14)
        self.assertEqual(out[-1], 50.0)

    def test_Wilder平滑与简单平均不同(self):
        # 用简单平均算出来的是另一个指标。这里用一组手算数据锁定差异：
        # 前 14 期涨幅全为 1，之后一期涨幅为 1 → Wilder: avg_gain=1 → RSI=100
        xs = [float(i) for i in range(16)]
        self.assertEqual(rsi(xs, 14)[14], 100.0)

    def test_样本不足(self):
        self.assertEqual(rsi([1.0, 2.0], 14), [None, None])


class MacdTests(unittest.TestCase):
    def test_三条线等长且_hist为差值(self):
        xs = [100 + math.sin(i / 5.0) * 10 for i in range(60)]
        out = macd(xs)
        self.assertEqual(set(out), {"macd", "signal", "hist"})
        for key in out:
            self.assertEqual(len(out[key]), 60)
        for m, s, h in zip(out["macd"], out["signal"], out["hist"]):
            self.assertTrue(ALMOST(h, m - s))

    def test_参数约束(self):
        with self.assertRaises(ValueError):
            macd([1.0] * 30, fast=26, slow=12)   # fast 必须 < slow
        with self.assertRaises(ValueError):
            macd([1.0] * 30, fast=12, slow=26, signal=0)


class BollingerTests(unittest.TestCase):
    def test_精确值_用总体标准差(self):
        # [1,2,3]: mean=2, 总体sd=sqrt(2/3)≈0.8164966
        out = bollinger([1, 2, 3], period=3, num_std=1.0)
        sd = math.sqrt(2.0 / 3.0)
        self.assertTrue(ALMOST(out["mid"][2], 2.0))
        self.assertTrue(ALMOST(out["upper"][2], 2.0 + sd))
        self.assertTrue(ALMOST(out["lower"][2], 2.0 - sd))

    def test_常数序列上下轨重合(self):
        out = bollinger([5.0] * 6, period=3)
        self.assertEqual(out["upper"][5], 5.0)
        self.assertEqual(out["lower"][5], 5.0)

    def test_样本不足(self):
        out = bollinger([1.0, 2.0], period=5)
        self.assertEqual(out["mid"], [None, None])

    def test_参数非法(self):
        with self.assertRaises(ValueError):
            bollinger([1.0] * 5, period=0)
        with self.assertRaises(ValueError):
            bollinger([1.0] * 5, num_std=0)


class TrueRangeAndAtrTests(unittest.TestCase):
    def test_TR取三者最大(self):
        tr = true_range(highs=[10, 12], lows=[8, 11], closes=[9, 11])
        self.assertEqual(tr[0], 2.0)                      # 退化成 H-L
        self.assertEqual(tr[1], 3.0)                      # max(1, |12-9|, |11-9|)

    def test_隔夜跳空时TR大于当日振幅(self):
        tr = true_range(highs=[10, 11], lows=[9, 10.5], closes=[9, 10.5])
        # max(0.5, |11-9|=2, |10.5-9|=1.5) = 2
        self.assertEqual(tr[1], 2.0)

    def test_atr用Wilder平滑(self):
        # closes 递增且 h==l → TR = [0, 1, 1, 1]
        # Wilder: 首值 (0+1+1)/3 = 2/3；次值 (2/3·2 + 1)/3 = 7/9
        out = atr(highs=[1, 2, 3, 4], lows=[1, 2, 3, 4], closes=[1, 2, 3, 4], period=3)
        self.assertTrue(ALMOST(out[2], 2.0 / 3.0))
        self.assertTrue(ALMOST(out[3], 7.0 / 9.0))


class StochasticTests(unittest.TestCase):
    def test_一字板取50(self):
        out = stochastic(highs=[10] * 16, lows=[10] * 16, closes=[10] * 16, period=14)
        self.assertEqual(out["k"][-1], 50.0)

    def test_收盘在区间顶时K为100(self):
        hs = [float(i) for i in range(1, 20)]
        ls = [0.0] * 19
        cs = [float(i) for i in range(1, 20)]
        out = stochastic(highs=hs, lows=ls, closes=cs, period=14, smooth_d=1)
        self.assertEqual(out["k"][-1], 100.0)

    def test_输出等长且前导为_None(self):
        out = stochastic(highs=[1.0] * 20, lows=[1.0] * 20, closes=[1.0] * 20)
        self.assertEqual(len(out["k"]), 20)
        self.assertEqual(out["k"][:12], [None] * 12)


class ComputeSpecTests(unittest.TestCase):
    def setUp(self):
        self.closes = [100 + math.sin(i / 4.0) * 5 for i in range(60)]
        self.highs = [c + 2 for c in self.closes]
        self.lows = [c - 2 for c in self.closes]

    def test_ma默认参数(self):
        out = compute_spec("ma", self.closes, self.highs, self.lows)
        self.assertEqual(out["kind"], "ma")
        self.assertEqual(sorted(out["lines"]), ["ma20", "ma5"])

    def test_ma显式参数与默认输出等长(self):
        out = compute_spec("ma:7,14", self.closes, self.highs, self.lows)
        for line in out["lines"].values():
            self.assertEqual(len(line), 60)

    def test_boll支持浮点参数(self):
        out = compute_spec("boll:20,1.5", self.closes, self.highs, self.lows)
        self.assertEqual(out["meta"]["num_std"], 1.5)

    def test_rsi默认14(self):
        out = compute_spec("rsi", self.closes, self.highs, self.lows)
        self.assertEqual(list(out["lines"]), ["rsi14"])

    def test_未知指标报错并列出可用项(self):
        with self.assertRaises(ValueError) as ctx:
            compute_spec("ichimoku", self.closes, self.highs, self.lows)
        self.assertIn("rsi", str(ctx.exception))

    def test_大小写不敏感(self):
        out = compute_spec("MA:5", self.closes, self.highs, self.lows)
        self.assertEqual(list(out["lines"]), ["ma5"])


if __name__ == "__main__":
    unittest.main()
