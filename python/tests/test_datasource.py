"""数据源与注册表测试。

synthetic 的重点：
  * 同参数必须**逐字节可复现**（回测与演示的地基）；
  * annual_vol 必须**名副其实** —— 第一版在这里出过 0.28 → 实测 0.54 的 bug，
    下面的锚定测试就是为它设的回归防线。

csv 的重点：列名别名、紧凑日期、坏行处理、排序与截断。
"""

from __future__ import annotations

import math
import tempfile
import unittest
from pathlib import Path

from finpulse_engine.datasource import registry as ds_registry
from finpulse_engine.datasource.base import Bar
from finpulse_engine.datasource.csvfile import CsvSource
from finpulse_engine.datasource.synthetic import SyntheticSource
from finpulse_engine.rpc import BadData, BadParams, NotFound


class RegistryTests(unittest.TestCase):
    def test_discover后两个内置源都已注册(self):
        ds_registry.discover()
        names = ds_registry.names()
        self.assertIn("synthetic", names)
        self.assertIn("csv", names)

    def test_create未知源抛KeyError(self):
        with self.assertRaises(KeyError):
            ds_registry.create("no-such-source")

    def test_create返回新实例(self):
        a = ds_registry.create("synthetic")
        b = ds_registry.create("synthetic")
        self.assertIsNot(a, b)

    def test_重复注册同名源报错(self):
        from finpulse_engine.datasource.registry import register

        class Dup(SyntheticSource):
            name = "synthetic"

        with self.assertRaises(ValueError):
            register(Dup)


class SyntheticTests(unittest.TestCase):
    def setUp(self):
        self.src = SyntheticSource()

    def test_参数校验(self):
        with self.assertRaises(BadParams):
            self.src.load(bars=0)
        with self.assertRaises(BadParams):
            self.src.load(bars=99999)
        with self.assertRaises(BadParams):
            self.src.load(annual_vol=0.0)

    def test_根数与符号(self):
        bars = self.src.load(bars=100, seed=7)
        self.assertEqual(len(bars), 100)
        self.assertEqual(self.src.symbols(), ["SYNTH", "DEMO-A", "DEMO-B", "DEMO-C"])

    def test_同参数完全可复现(self):
        a = self.src.load(bars=120, seed=3)
        b = self.src.load(bars=120, seed=3)
        self.assertEqual(a, b)

    def test_不同种子结果不同(self):
        a = self.src.load(bars=120, seed=3)
        b = self.src.load(bars=120, seed=4)
        self.assertNotEqual([x.close for x in a], [x.close for x in b])

    def test_时间戳严格递增(self):
        bars = self.src.load(bars=200)
        for prev, cur in zip(bars, bars[1:]):
            self.assertLess(prev.ts, cur.ts)

    def test_OHLC关系自洽(self):
        for b in self.src.load(bars=300, seed=11):
            self.assertGreaterEqual(b.high, max(b.open, b.close))
            self.assertLessEqual(b.low, min(b.open, b.close))
            self.assertGreater(b.close, 0.0)
            self.assertGreaterEqual(b.volume, 0)

    def test_annual_vol锚定_回归防线(self):
        # 关键回归测试：annual_vol=0.28 必须真的给出 ~28% 的年化波动率。
        # 第一版漏了跳变项和跳空项，实测分别是 53.9% 和 37.6%。
        bars = self.src.load(bars=4000, seed=42, annual_vol=0.28)
        logs = [math.log(b.close / p.close) for p, b in zip(bars, bars[1:])]
        n = len(logs)
        m = sum(logs) / n
        sd = math.sqrt(sum((x - m) ** 2 for x in logs) / (n - 1))
        realized = sd * math.sqrt(252)
        self.assertTrue(0.20 <= realized <= 0.40,
                        f"年化波动率 {realized:.3f} 偏离锚定值 0.28 过远")

    def test_符号名影响起始价但不影响可复现性(self):
        a = self.src.load(symbol="SYNTH", bars=50)[0]
        b = self.src.load(symbol="DEMO-B", bars=50)[0]
        self.assertNotEqual(a.open, b.open)
        # 不能用内置 hash()：字符串哈希带随机盐，跨进程不稳定 —— 实现里有注释
        self.assertEqual(self.src.load(symbol="DEMO-B", bars=50)[0].open, b.open)

    def test_info结构(self):
        info = self.src.info()
        self.assertEqual(info["name"], "synthetic")
        self.assertFalse(info["requires_network"])
        self.assertTrue(info["available"])


def write_csv(text: str) -> Path:
    f = tempfile.NamedTemporaryFile("w", suffix=".csv", delete=False, encoding="utf-8")
    f.write(text)
    f.close()
    return Path(f.name)


class CsvTests(unittest.TestCase):
    def setUp(self):
        self.src = CsvSource()

    def tearDown(self):
        pass  # 临时文件交给系统临时目录清理

    def test_available恒真(self):
        self.assertTrue(self.src.available())

    def test_显式路径不存在报NotFound(self):
        with self.assertRaises(NotFound):
            self.src.load(path="/no/such/file.csv")

    def test_既无路径也无符号报BadData(self):
        with self.assertRaises(BadData):
            self.src.load(symbol="")

    def test_标准英文表头(self):
        p = write_csv(
            "date,open,high,low,close,volume\n"
            "2024-03-01,10,11,9,10.5,1000\n"
            "2024-03-04,10.5,12,10,11.8,1200\n"
        )
        bars = self.src.load(path=str(p))
        self.assertEqual(len(bars), 2)
        self.assertEqual(bars[0].close, 10.5)
        self.assertEqual(bars[1].volume, 1200)

    def test_中文表头别名(self):
        p = write_csv(
            "日期,开盘,最高,最低,收盘,成交量\n"
            "2024-03-01,10,11,9,10.5,1000\n"
        )
        bars = self.src.load(path=str(p))
        self.assertEqual(len(bars), 1)
        self.assertEqual(bars[0].open, 10.0)

    def test_紧凑日期格式(self):
        p = write_csv(
            "date,open,high,low,close\n"
            "20240301,10,11,9,10.5\n"
        )
        bars = self.src.load(path=str(p))
        from finpulse_engine.timeutil import format_date
        self.assertEqual(format_date(bars[0].ts), "2024-03-01")

    def test_千分位与缺失成交量(self):
        p = write_csv(
            "date,open,high,low,close,vol\n"
            "2024-03-01,1,234.5,1,233.0,\"1,234,567\"\n"
            "2024-03-04,1,235.0,1,234.0,-\n"
        )
        bars = self.src.load(path=str(p))
        self.assertEqual(bars[0].volume, 1234567)
        self.assertEqual(bars[1].volume, 0)

    def test_坏行默认丢弃(self):
        p = write_csv(
            "date,open,high,low,close\n"
            "2024-03-01,10,11,9,10.5\n"
            "2024-03-04,0,0,0,0\n"          # 停牌：价格全 0 → 丢弃
            "2024-03-05,abc,11,9,10.5\n"    # 解析失败 → 丢弃
            "2024-03-06,10,11,9,10.6\n"
        )
        bars = self.src.load(path=str(p))
        self.assertEqual(len(bars), 2)

    def test_缺必需列报BadData并列出缺哪些(self):
        p = write_csv("date,open,close\n2024-03-01,10,10.5\n")
        with self.assertRaises(BadData) as ctx:
            self.src.load(path=str(p))
        self.assertIn("high", str(ctx.exception))

    def test_空文件报BadData(self):
        p = write_csv("")
        with self.assertRaises(BadData):
            self.src.load(path=str(p))

    def test_无表头报BadData(self):
        p = write_csv("2024-03-01,10,11,9,10.5\n")
        with self.assertRaises(BadData):
            self.src.load(path=str(p))

    def test_乱序输入按时间升序返回(self):
        p = write_csv(
            "date,open,high,low,close\n"
            "2024-03-04,10,11,9,11.0\n"
            "2024-03-01,10,11,9,10.0\n"
        )
        bars = self.src.load(path=str(p))
        self.assertLess(bars[0].ts, bars[1].ts)
        self.assertEqual(bars[0].close, 10.0)

    def test_bars参数只保留最近若干根(self):
        rows = "\n".join(f"2024-03-{d:02d},{d},{d+1},{d-1},{d}.5"
                         for d in range(1, 11))
        p = write_csv("date,open,high,low,close\n" + rows + "\n")
        bars = self.src.load(path=str(p), bars=3)
        self.assertEqual(len(bars), 3)
        self.assertEqual(bars[-1].close, 10.5)   # 3月10日

    def test_制表符分隔自动嗅探(self):
        p = write_csv(
            "date\topen\thigh\tlow\tclose\n"
            "2024-03-01\t10\t11\t9\t10.5\n"
        )
        bars = self.src.load(path=str(p))
        self.assertEqual(len(bars), 1)

    def test_BOM不破坏表头识别(self):
        p = write_csv("﻿date,open,high,low,close\n2024-03-01,10,11,9,10.5\n")
        bars = self.src.load(path=str(p))
        self.assertEqual(len(bars), 1)


class BarTests(unittest.TestCase):
    def test_to_from_dict往返(self):
        b = Bar(ts=123, open=1.0, high=2.0, low=0.5, close=1.5, volume=99)
        self.assertEqual(Bar.from_dict(b.to_dict()), b)

    def test_volume缺省为0(self):
        self.assertEqual(Bar.from_dict({"ts": 1, "open": 1, "high": 1,
                                        "low": 1, "close": 1}).volume, 0)


if __name__ == "__main__":
    unittest.main()
