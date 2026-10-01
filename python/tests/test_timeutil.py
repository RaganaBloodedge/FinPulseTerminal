"""时间工具测试。

两边（C++ ``Types.cpp`` / Python ``timeutil.py``）的时间约定必须一致，
否则时间轴会整体错位 —— 测试围绕这个约定展开。
"""

from __future__ import annotations

import datetime as dt
import unittest

from finpulse_engine.timeutil import (
    format_date,
    format_datetime,
    from_ms,
    parse_datetime_ms,
    recent_trading_days,
    to_ms,
)

UTC = dt.timezone.utc


class RoundTripTests(unittest.TestCase):
    def test_epoch(self):
        self.assertEqual(to_ms(dt.datetime(1970, 1, 1, tzinfo=UTC)), 0)
        self.assertEqual(from_ms(0), dt.datetime(1970, 1, 1, tzinfo=UTC))

    def test_naive_datetime按UTC处理(self):
        naive = dt.datetime(2024, 3, 15, 12, 0, 0)
        aware = naive.replace(tzinfo=UTC)
        self.assertEqual(to_ms(naive), to_ms(aware))

    def test_日期格式化(self):
        ms = to_ms(dt.datetime(2024, 3, 5, tzinfo=UTC))
        self.assertEqual(format_date(ms), "2024-03-05")
        self.assertEqual(format_datetime(ms), "2024-03-05 00:00:00")

    def test_毫秒保留(self):
        ms = to_ms(dt.datetime(2024, 3, 5, 7, 8, 9, tzinfo=UTC)) + 123
        self.assertEqual(from_ms(ms).microsecond, 123_000)

    def test_解析与格式化互逆(self):
        cases = {
            "2024-03-15": "2024-03-15 00:00:00",
            "2024-03-15 09:30": "2024-03-15 09:30:00",
            "2024-03-15 09:30:45": "2024-03-15 09:30:45",
        }
        for text, expected in cases.items():
            self.assertEqual(format_datetime(parse_datetime_ms(text)), expected)

    def test_支持_T分隔符(self):
        self.assertEqual(parse_datetime_ms("2024-03-15T09:30:00"),
                         parse_datetime_ms("2024-03-15 09:30:00"))

    def test_非法输入抛_ValueError(self):
        for bad in ("", "2024/03/15", "15-03-2024", "2024-13-01", "abc"):
            with self.assertRaises(ValueError):
                parse_datetime_ms(bad)


class RecentTradingDaysTests(unittest.TestCase):
    def test_只跳过周末不处理节假日(self):
        # 2024-03-15 是周五。往前数 5 个交易日应该是 3/11(一) ~ 3/15(五)，
        # 跳过 3/9(六) 和 3/10(日)
        end = to_ms(dt.datetime(2024, 3, 15, tzinfo=UTC))
        days = recent_trading_days(5, end_ms=end)
        self.assertEqual([format_date(d) for d in days],
                         ["2024-03-11", "2024-03-12", "2024-03-13", "2024-03-14", "2024-03-15"])

    def test_升序返回(self):
        end = to_ms(dt.datetime(2024, 3, 15, tzinfo=UTC))
        days = recent_trading_days(10, end_ms=end)
        self.assertEqual(days, sorted(days))

    def test_全部是工作日(self):
        end = to_ms(dt.datetime(2024, 3, 15, tzinfo=UTC))
        for ms in recent_trading_days(30, end_ms=end):
            self.assertLess(from_ms(ms).weekday(), 5)

    def test_count为0返回空表(self):
        self.assertEqual(recent_trading_days(0), [])

    def test_跨周末取数正确(self):
        # 结束日是交易日时会被包含在内：3/18(一) 往前 3 个交易日
        # = 3/18, 3/15, 3/14（3/16、3/17 是周末）
        end = to_ms(dt.datetime(2024, 3, 18, tzinfo=UTC))  # 周一
        days = recent_trading_days(3, end_ms=end)
        self.assertEqual([format_date(d) for d in days],
                         ["2024-03-14", "2024-03-15", "2024-03-18"])


class ParseFormatTests(unittest.TestCase):
    """日期解析接受的写法。

    紧凑格式这条是**补上的**：它原先只作为一段正则活在 CSV 数据源里，
    于是新加的 Tushare 数据源拿到 ``trade_date="20240102"`` 时整批解析失败，
    而且失败得很安静 —— 坏行被逐条跳过、结果为空、然后静默回落到本地缓存。
    格式解析该收在时间工具里，所以这里也钉一条测试。
    """

    def test_标准日期(self):
        self.assertEqual(parse_datetime_ms("2024-01-02"),
                         parse_datetime_ms("2024-01-02 00:00:00"))

    def test_带时间的日期时间(self):
        a = parse_datetime_ms("2024-01-02 15:30:00")
        b = parse_datetime_ms("2024-01-02 15:30")
        self.assertEqual(a, b)

    def test_紧凑八位日期(self):
        # Tushare 的 trade_date、通达信/同花顺的导出都是这个格式
        self.assertEqual(parse_datetime_ms("20240102"),
                         parse_datetime_ms("2024-01-02"))

    def test_紧凑格式与标准格式跨年一致(self):
        self.assertEqual(parse_datetime_ms("19991231"),
                         parse_datetime_ms("1999-12-31"))

    def test_无法解析时抛valueerror(self):
        for bad in ("", "2024/01/02", "202401", "abcdefgh"):
            with self.assertRaises(ValueError):
                parse_datetime_ms(bad)


if __name__ == "__main__":
    unittest.main()
