"""Tushare 数据源测试。

这个数据源要真的发 HTTP，而**测试不能真的联网** —— 依赖网络和 token 的
测试必然变成"有时候红"，然后被人加个 skip 标记，最后没人再看它。

所以传输层是可注入的（``TushareSource(transport=...)``），这里注入一个
记录调用的假实现，于是三条路径都能离线钉死：

    1. 请求体构造得对不对（api_name / token / params / fields）；
    2. 响应解析得对不对（竖表 → Bar、降序 → 升序、去重、单位换算）；
    3. 失败时回落得对不对（无 token / 接口报错 / 传输异常 / 禁用缓存）。

第 3 条尤其重要：回落是"静默地换了一个数据源"，如果不把出处写清楚，
用户会拿缓存数据当成实时行情看。
"""

from __future__ import annotations

import datetime as _dt
import unittest
from typing import Any, Dict, List

from finpulse_engine.datasource.base import Bar, DataSource
from finpulse_engine.datasource.tushare import TOKEN_ENV, TushareSource
from finpulse_engine.rpc import BadData

_FIELDS = ["ts_code", "trade_date", "open", "high", "low", "close", "vol", "amount"]


def _row(code: str, date: str, close: float, vol: Any = 1234.0) -> List[Any]:
    return [code, date, close - 0.5, close + 1.0, close - 1.0, close, vol, 1000.0]


def _ok(items: List[List[Any]]) -> Dict[str, Any]:
    return {"code": 0, "msg": "", "data": {"fields": list(_FIELDS), "items": items}}


class RecordingTransport:
    """假的传输层：记下每次调用的参数，返回预置响应或抛预置异常。"""

    def __init__(self, response: Any) -> None:
        self.response = response
        self.calls: List[Dict[str, Any]] = []

    def __call__(self, url: str, payload: Dict[str, Any], timeout: float) -> Dict[str, Any]:
        self.calls.append({"url": url, "payload": payload, "timeout": timeout})
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


class FakeCache(DataSource):
    """假的本地缓存：返回固定的一小段 K 线，并记下是否被用到。"""

    name = "fake-cache"

    def __init__(self, bars: int = 3) -> None:
        self._bars = [
            Bar(ts=1_600_000_000_000 + i * 86_400_000,
                open=10.0 + i, high=11.0 + i, low=9.0 + i, close=10.5 + i, volume=100 + i)
            for i in range(bars)
        ]
        self.loaded = 0

    def load(self, symbol: str = "", bars: int = 250, **kwargs: Any) -> List[Bar]:
        self.loaded += 1
        return list(self._bars[-bars:])


class FakeDemo(DataSource):
    """假的"演示数据源"。

    注入它而不是让测试去调真正的 synthetic 源：这一条测的是**回落链的
    走向与出处标注**，不该顺带把合成源的生成逻辑也拖进来 ——
    那样合成源一改，这里就会以一种看不懂的方式红掉。
    """

    name = "fake-demo"
    requires_network = False

    def load(self, symbol: str = "", bars: int = 250, **kwargs: Any) -> List[Bar]:
        return [Bar(ts=1_600_000_000_000 + i * 86_400_000,
                    open=20.0, high=21.0, low=19.0, close=20.5, volume=1)
                for i in range(int(bars))]


class TushareRequestTests(unittest.TestCase):
    """请求体构造：Tushare 的契约就写在这一次 POST 里。"""

    def test_请求体带上接口名与token(self):
        tr = RecordingTransport(_ok([_row("000001.SZ", "20240102", 10.0)]))
        src = TushareSource(transport=tr, token="tok-123", cache=FakeCache())
        src.load(symbol="000001.SZ", bars=10)

        self.assertEqual(1, len(tr.calls))
        payload = tr.calls[0]["payload"]
        self.assertEqual("daily", payload["api_name"])
        self.assertEqual("tok-123", payload["token"])
        self.assertEqual("000001.SZ", payload["params"]["ts_code"])

    def test_请求字段包含ohlcv(self):
        tr = RecordingTransport(_ok([_row("000001.SZ", "20240102", 10.0)]))
        TushareSource(transport=tr, token="t", cache=FakeCache()).load(bars=10)
        fields = tr.calls[0]["payload"]["fields"]
        for name in ("trade_date", "open", "high", "low", "close", "vol"):
            self.assertIn(name, fields)

    def test_时间窗口随根数放大(self):
        # 要取 250 根日线，窗口必须明显长于 250 个自然日 ——
        # 只给 250 天的话，扣掉周末和长假实际拿不到 250 根。
        tr = RecordingTransport(_ok([_row("000001.SZ", "20240102", 10.0)]))
        TushareSource(transport=tr, token="t", cache=FakeCache()).load(bars=250)
        params = tr.calls[0]["payload"]["params"]
        start = _dt.datetime.strptime(params["start_date"], "%Y%m%d").date()
        end = _dt.datetime.strptime(params["end_date"], "%Y%m%d").date()
        self.assertGreater((end - start).days, 250)


class TushareParseTests(unittest.TestCase):
    """响应解析：竖表转 K 线。"""

    def _load(self, items, **kw):
        tr = RecordingTransport(_ok(items))
        src = TushareSource(transport=tr, token="t", cache=FakeCache())
        return src.load(symbol="000001.SZ", bars=100, **kw)

    def test_响应按时间升序返回(self):
        # Tushare 的 daily 默认**降序**（最新在前），本项目一律要求升序。
        # 反了的话 K 线图会整个倒过来，而且所有基于时间顺序的指标都错。
        bars = self._load([
            _row("000001.SZ", "20240103", 12.0),
            _row("000001.SZ", "20240102", 11.0),
            _row("000001.SZ", "20240101", 10.0),
        ])
        closes = [b.close for b in bars]
        self.assertEqual([10.0, 11.0, 12.0], closes)

    def test_同一天重复行只保留一条(self):
        bars = self._load([
            _row("000001.SZ", "20240102", 11.0),
            _row("000001.SZ", "20240101", 10.0),
            _row("000001.SZ", "20240102", 99.0),   # 复权/更正后的重复行
        ])
        self.assertEqual(2, len(bars))
        self.assertEqual(99.0, bars[-1].close)   # 后出现的覆盖先出现的

    def test_成交量从手换算成股(self):
        # Tushare 的 vol 单位是"手"，本项目按"股"存。差 100 倍的话，
        # 量价分析师拿到的实时数据与历史 CSV 会对不上。
        bars = self._load([_row("000001.SZ", "20240102", 10.0, vol=1234.0)])
        self.assertEqual(123_400, bars[0].volume)

    def test_停牌日空成交量不丢整根k线(self):
        # 停牌当天 Tushare 的 vol 是空值 —— 价格还在，只是没成交，
        # 不该因此把这一根 K 线整条丢掉。
        bars = self._load([_row("000001.SZ", "20240102", 10.0, vol=None)])
        self.assertEqual(1, len(bars))
        self.assertEqual(0, bars[0].volume)

    def test_坏行被跳过而不是整批失败(self):
        bars = self._load([
            _row("000001.SZ", "20240102", 11.0),
            ["000001.SZ", "20240103", "not-a-number", None, None, None, None, None],
            _row("000001.SZ", "20240104", 12.0),
        ])
        self.assertEqual(2, len(bars))

    def test_接口非零code抛出并可回落(self):
        tr = RecordingTransport({"code": 2002, "msg": "抱歉，您没有权限", "data": None})
        cache = FakeCache()
        src = TushareSource(transport=tr, token="t", cache=cache)
        bars = src.load(symbol="000001.SZ", bars=10)
        self.assertEqual(1, cache.loaded)          # 确实回落了
        self.assertEqual("local_cache", src.provenance()["mode"])
        self.assertIn("2002", src.provenance()["reason"])
        self.assertTrue(bars)

    def test_缺少必需字段时报错(self):
        tr = RecordingTransport({"code": 0, "msg": "", "data": {"fields": ["ts_code"], "items": []}})
        src = TushareSource(transport=tr, token="t", cache=FakeCache())
        with self.assertRaises(BadData) as ctx:
            src._parse(tr.response, "000001.SZ")
        self.assertIn("trade_date", str(ctx.exception))

    def test_顶层不是对象时报错(self):
        src = TushareSource(transport=RecordingTransport({}), token="t", cache=FakeCache())
        with self.assertRaises(BadData):
            src._parse("not a dict", "000001.SZ")  # type: ignore[arg-type]


class TushareFallbackTests(unittest.TestCase):
    """回落路径：不能因为没网/没 token 就整个用不了，但回落必须是**显式**的。"""

    def test_没有token时回落本地缓存(self):
        tr = RecordingTransport(_ok([]))
        cache = FakeCache()
        src = TushareSource(transport=tr, token="", cache=cache)
        bars = src.load(symbol="000001.SZ", bars=10)

        self.assertEqual(0, len(tr.calls))          # 压根没发请求
        self.assertEqual(1, cache.loaded)
        self.assertTrue(bars)
        prov = src.provenance()
        self.assertEqual("local_cache", prov["mode"])
        self.assertIn(TOKEN_ENV, prov["reason"])

    def test_环境变量提供token(self):
        import os

        old = os.environ.get(TOKEN_ENV)
        os.environ[TOKEN_ENV] = "tok-from-env"
        try:
            tr = RecordingTransport(_ok([_row("000001.SZ", "20240102", 10.0)]))
            src = TushareSource(transport=tr, token="", cache=FakeCache())
            src.load(symbol="000001.SZ", bars=10)
            self.assertEqual("tok-from-env", tr.calls[0]["payload"]["token"])
        finally:
            if old is None:
                os.environ.pop(TOKEN_ENV, None)
            else:
                os.environ[TOKEN_ENV] = old

    def test_传输异常时回落本地缓存(self):
        tr = RecordingTransport(OSError("网络不可达"))
        cache = FakeCache()
        src = TushareSource(transport=tr, token="t", cache=cache)
        bars = src.load(symbol="000001.SZ", bars=10)

        self.assertTrue(bars)
        self.assertEqual(1, cache.loaded)
        self.assertEqual("local_cache", src.provenance()["mode"])
        self.assertIn("网络不可达", src.provenance()["reason"])

    def test_禁用缓存时取数失败直接报错(self):
        # 有些场景（例如对账）宁可失败也不能悄悄换数据源。
        # 注意要把**两级回落**都禁掉：只禁缓存，等于允许它退到演示数据，
        # 那对"对账"这种用途是更糟的结果 —— 一份看起来正常的假数据。
        tr = RecordingTransport(OSError("网络不可达"))
        src = TushareSource(transport=tr, token="t", cache=FakeCache())
        with self.assertRaises(BadData):
            src.load(symbol="000001.SZ", bars=10,
                     allow_cache=False, allow_demo=False)

    def test_只禁缓存但允许演示时仍然给得出数据(self):
        # 两级的语义是独立的：禁缓存 ≠ 禁演示。
        tr = RecordingTransport(OSError("网络不可达"))
        src = TushareSource(transport=tr, token="t", cache=FakeCache(),
                            demo=FakeDemo())
        bars = src.load(symbol="000001.SZ", bars=10, allow_cache=False)

        self.assertEqual(10, len(bars))
        self.assertEqual("demo_synthetic", src.provenance()["mode"])

    def test_缓存也读不到且禁用演示时报错信息同时给出两条出路(self):
        class BrokenCache(DataSource):
            name = "broken"

            def load(self, symbol: str = "", bars: int = 250, **kwargs: Any):
                raise BadData("文件不存在")

        src = TushareSource(transport=RecordingTransport(_ok([])),
                            token="", cache=BrokenCache())
        with self.assertRaises(BadData) as ctx:
            src.load(symbol="000001.SZ", bars=10, allow_demo=False)
        msg = str(ctx.exception)
        self.assertIn(TOKEN_ENV, msg)     # 出路一：配 token
        self.assertIn("data/", msg)       # 出路二：放缓存文件

    def test_缓存也读不到时回落到演示数据并标明出处(self):
        class BrokenCache(DataSource):
            name = "broken"

            def load(self, symbol: str = "", bars: int = 250, **kwargs: Any):
                raise BadData("文件不存在")

        src = TushareSource(transport=RecordingTransport(_ok([])),
                            token="", cache=BrokenCache(), demo=FakeDemo())
        bars = src.load(symbol="000001.SZ", bars=7)

        self.assertEqual(7, len(bars))
        prov = src.provenance()
        # 出处必须一眼看出不是真实行情 —— 否则这就是"静默造假数据"。
        self.assertEqual("demo_synthetic", prov["mode"])
        self.assertIn("演示", prov["detail"])
        self.assertIn("文件不存在", prov["reason"])

    def test_成功时出处标明是实时接口(self):
        tr = RecordingTransport(_ok([
            _row("000001.SZ", "20240102", 10.0),
            _row("000001.SZ", "20240103", 11.0),
        ]))
        src = TushareSource(transport=tr, token="t", cache=FakeCache())
        src.load(symbol="000001.SZ", bars=10)

        prov = src.provenance()
        self.assertEqual("live_api", prov["mode"])
        self.assertEqual("000001.SZ", prov["symbol"])
        self.assertEqual(2, prov["rows"])
        self.assertTrue(prov["as_of"] > 0)   # 数据日期，供"这份数据新不新"判断

    def test_返回根数不超过请求量(self):
        items = [_row("000001.SZ", f"2024010{i}", 10.0 + i) for i in range(1, 8)]
        src = TushareSource(transport=RecordingTransport(_ok(items)),
                            token="t", cache=FakeCache())
        bars = src.load(symbol="000001.SZ", bars=3)
        self.assertEqual(3, len(bars))
        # 截取的必须是**最近**的几根，而不是最早的几根
        self.assertEqual(17.0, bars[-1].close)


class TushareSelfDescriptionTests(unittest.TestCase):
    def test_没有token时仍然可用但说明里写清回落(self):
        src = TushareSource(cache=FakeCache())
        import os
        old = os.environ.pop(TOKEN_ENV, None)
        try:
            self.assertTrue(src.available())   # 可用：会走本地缓存
            info = src.info()
            self.assertFalse(info["token_configured"])
            self.assertIn("回落", info["detail"])
        finally:
            if old is not None:
                os.environ[TOKEN_ENV] = old

    def test_有token时说明里写明直连(self):
        src = TushareSource(token="t", cache=FakeCache())
        info = src.info()
        self.assertTrue(info["token_configured"])
        self.assertIn("api.tushare.pro", info["detail"])

    def test_注册表中能找到tushare(self):
        from finpulse_engine.datasource import registry as ds_registry

        ds_registry.discover()
        self.assertIn("tushare", ds_registry.names())


if __name__ == "__main__":
    unittest.main()
