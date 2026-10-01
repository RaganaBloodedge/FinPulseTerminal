"""service 层测试：输入校验 + 通过 Dispatcher 的端到端 RPC 流。

这里不伪造进程 —— 直接 build 一个 Dispatcher 然后 handle() 消息，
这正是 service 与 rpc 分层设计想要达成的可测性。
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from finpulse_engine.datasource import registry as ds_registry
from finpulse_engine.forecast import registry as fc_registry
from finpulse_engine.rpc import BadData, BadParams, Dispatcher, NotFound
from finpulse_engine.service import (
    build,
    make_forecaster,
    median_step_ms,
    normalize_bars,
    require_bars,
    validate_bars,
)
from finpulse_engine.datasource.base import Bar, DataSource


def make_bars(n: int = 60, start: float = 100.0) -> list:
    """构造 n 根合法 K 线，价格缓慢上行。"""
    out = []
    px = start
    for i in range(n):
        o = px
        c = px * 1.005
        out.append(Bar(ts=86_400_000 * (i + 1),
                       open=round(o, 2), high=round(c * 1.01, 2),
                       low=round(o * 0.99, 2), close=round(c, 2), volume=1000 + i).to_dict())
        px = c
    return out


class RequireBarsTests(unittest.TestCase):
    def test_缺参报BadParams(self):
        with self.assertRaises(BadParams):
            require_bars(None)

    def test_非数组报BadParams(self):
        with self.assertRaises(BadParams):
            require_bars({"ts": 1})

    def test_根数不足报BadData(self):
        with self.assertRaises(BadData) as ctx:
            require_bars([{}, {}], min_count=5)
        self.assertIn("至少需要 5", str(ctx.exception))

    def test_元素不是对象报BadData(self):
        with self.assertRaises(BadData):
            require_bars([1, 2])

    def test_字段缺失报BadData(self):
        with self.assertRaises(BadData):
            require_bars([{"ts": 1, "open": 1.0}])

    def test_合法输入返回Bar列表(self):
        bars = require_bars(make_bars(3))
        self.assertEqual(len(bars), 3)
        self.assertIsInstance(bars[0], Bar)


class ValidateBarsTests(unittest.TestCase):
    def test_干净数据零问题(self):
        self.assertEqual(validate_bars(require_bars(make_bars(10))), [])

    def test_high低于low被发现(self):
        bars = require_bars(make_bars(2))
        bars[1].high = 1.0
        bars[1].low = 5.0
        issues = validate_bars(bars)
        self.assertTrue(any("high" in i for i in issues))

    def test_价格非正被发现(self):
        bars = require_bars(make_bars(2))
        bars[1].open = 0.0
        self.assertTrue(any("非正" in i for i in validate_bars(bars)))

    def test_close脱离区间被发现(self):
        bars = require_bars(make_bars(2))
        bars[1].close = bars[1].high * 10
        self.assertTrue(any("之外" in i for i in validate_bars(bars)))

    def test_时间戳未递增被发现(self):
        bars = require_bars(make_bars(3))
        bars[2].ts = bars[1].ts
        self.assertTrue(any("递增" in i for i in validate_bars(bars)))

    def test_问题列表有上限(self):
        bad = [{"ts": i, "open": 0, "high": 0, "low": 0, "close": 0} for i in range(50)]
        issues = validate_bars([Bar.from_dict(b) for b in bad])
        self.assertLessEqual(len(issues), 11)      # 10 条 + 省略提示
        self.assertIn("省略", issues[-1])


class NormalizeTests(unittest.TestCase):
    def test_排序(self):
        out = normalize_bars([Bar(ts=3, open=1, high=1, low=1, close=1),
                              Bar(ts=1, open=1, high=1, low=1, close=1)])
        self.assertEqual([b.ts for b in out], [1, 3])

    def test_同时间戳保留后出现的(self):
        # CSV 追加写覆盖旧行的语义 —— 保留最后一条才是对的
        first = Bar(ts=1, open=1, high=1, low=1, close=1)
        second = Bar(ts=1, open=2, high=2, low=2, close=2)
        out = normalize_bars([first, second])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].close, 2.0)


class MedianStepTests(unittest.TestCase):
    def test_不足两个返回默认一天(self):
        self.assertEqual(median_step_ms([1]), 86_400_000)

    def test_日线混入周末后中位数仍是一天(self):
        # 差值 [1天,3天,1天,1天] → 中位数 1 天；平均值会被周末拖偏
        day = 86_400_000
        self.assertEqual(median_step_ms([0, day, 4 * day, 5 * day, 6 * day]), day)

    def test_非正差值回退默认(self):
        self.assertEqual(median_step_ms([5, 5, 5]), 86_400_000)


class MakeForecasterTests(unittest.TestCase):
    def test_未知方法报NotFound并列出可用项(self):
        with self.assertRaises(NotFound) as ctx:
            make_forecaster("lstm", {})
        self.assertIn("randomwalk", str(ctx.exception))

    def test_合法选项透传(self):
        m = make_forecaster("randomwalk", {"drift": True})
        self.assertTrue(m.use_drift)

    def test_未知选项被忽略而不是TypeError(self):
        # 前端迭代时几乎必然多传字段 —— 直接 **options 会炸，这里按签名过滤
        m = make_forecaster("randomwalk", {"drift": True, "future_option": 1})
        self.assertTrue(m.use_drift)

    def test_ar的order透传(self):
        self.assertEqual(make_forecaster("ar", {"order": 3}).order, 3)


class EndToEndTests(unittest.TestCase):
    """build() 出来的 Dispatcher 走一遍全部 RPC 方法。"""

    #: 基础分析层的方法。智能体那一组单独断言 —— 写死"总数等于 N"
    #: 每加一个方法就得改一次，而且改的时候很容易把断言改成瞎写一个数，
    #: 它实际什么都没验证。
    BASE_METHODS = (
        "handshake", "ping", "engine.info",
        "source.list", "source.load", "source.pull",
        "analysis.indicators", "analysis.stats",
        "forecast.list", "forecast.run", "forecast.backtest",
    )

    @classmethod
    def setUpClass(cls):
        ds_registry.discover()
        fc_registry.discover()
        cls.d = Dispatcher()
        build(cls.d)
        cls.bars = make_bars(300)

    def handle(self, method, params=None, rid=1):
        return self.d.handle({"id": rid, "method": method,
                              "params": params if params is not None else {}})

    def test_基础方法全部注册(self):
        registered = set(self.d.methods)
        missing = [m for m in self.BASE_METHODS if m not in registered]
        self.assertFalse(missing, msg=f"缺少基础方法 {missing}")

    def test_智能体方法全部注册(self):
        """智能体那一组必须齐全 —— CLI 和 GUI 都按这些名字调。"""
        agent_methods = {
            "agent.roles", "agent.role.get", "agent.panels", "agent.tools",
            "agent.bridge.status", "agent.llm.status",
            "agent.run", "agent.team", "agent.debate", "agent.chat",
            "agent.trace", "agent.runs", "agent.memory", "agent.consistency",
            "agent.reload", "agent.stream.stats",
        }
        registered = set(self.d.methods)
        self.assertFalse(agent_methods - registered,
                         msg=f"缺少 {sorted(agent_methods - registered)}")

    def test_agent命名空间不与基础方法重名(self):
        """两个命名空间不能互相盖住：重名时后注册的会静默顶掉先注册的。"""
        names = [m for m in self.d.methods]
        self.assertEqual(len(names), len(set(names)))

    def test_handshake成功并上报能力(self):
        r = self.handle("handshake", {"client": "pytest", "protocol": 1})
        self.assertTrue(r["ok"])
        body = r["result"]
        self.assertEqual(body["name"], "finpulse-engine")
        self.assertIn("synthetic", body["sources"])
        self.assertIn("ar", body["forecasters"])

    def test_handshake协议不匹配被拒(self):
        r = self.handle("handshake", {"protocol": 99})
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"]["code"], "ProtocolMismatch")

    def test_ping心跳回显(self):
        r = self.handle("ping", {"nonce": 42})
        self.assertEqual(r["result"], {"nonce": 42, "alive": True})

    def test_engine_info与source_list(self):
        info = self.handle("engine.info")["result"]
        self.assertIn("synthetic", [s["name"] for s in info["sources"]])
        lst = self.handle("source.list")["result"]
        self.assertEqual(len(lst["sources"]), len(ds_registry.names()))

    def test_forecast_list带参数自省(self):
        r = self.handle("forecast.list")["result"]["forecasters"]
        ar = next(x for x in r if x["name"] == "ar")
        self.assertIn("order", ar["params"])

    def test_source_load_synthetic(self):
        r = self.handle("source.load", {"source": "synthetic", "symbol": "SYNTH",
                                        "bars": 80})
        self.assertTrue(r["ok"])
        self.assertEqual(r["result"]["count"], 80)
        self.assertEqual(len(r["result"]["bars"]), 80)

    def test_source_load未知源报NotFound(self):
        r = self.handle("source.load", {"source": "bogus"})
        self.assertEqual(r["error"]["code"], "NotFound")

    def test_analysis_stats(self):
        r = self.handle("analysis.stats", {"bars": self.bars[:50]})
        self.assertTrue(r["ok"])
        self.assertEqual(r["result"]["bars"], 50)
        # 回撤区间被翻译成了日期
        self.assertIn("max_drawdown_trough_date", r["result"])

    def test_analysis_indicators默认两条(self):
        r = self.handle("analysis.indicators", {"bars": self.bars[:40]})
        self.assertEqual(len(r["result"]["results"]), 2)

    def test_analysis_indicators坏规格报BadParams(self):
        r = self.handle("analysis.indicators",
                        {"bars": self.bars[:40], "specs": ["nope"]})
        self.assertEqual(r["error"]["code"], "BadParams")

    def test_forecast_run(self):
        r = self.handle("forecast.run", {"bars": self.bars, "method": "ar",
                                         "horizon": 5})
        self.assertTrue(r["ok"])
        pts = r["result"]["points"]
        self.assertEqual(len(pts), 5)
        # 预测点时间戳按中位步长外推且递增
        self.assertLess(pts[0]["ts"], pts[-1]["ts"])
        # 区间包住点估计
        for p in pts:
            self.assertLessEqual(p["lower"], p["upper"])
        # meta 带回模型自述与请求参数
        self.assertEqual(r["result"]["meta"]["horizon"], 5)

    def test_forecast_run_horizon越界(self):
        r = self.handle("forecast.run", {"bars": self.bars, "horizon": 999})
        self.assertEqual(r["error"]["code"], "BadParams")

    def test_forecast_run样本不足(self):
        r = self.handle("forecast.run", {"bars": self.bars[:10]})
        self.assertEqual(r["error"]["code"], "BadData")

    def test_forecast_backtest(self):
        r = self.handle("forecast.backtest", {"bars": self.bars, "method": "ar",
                                              "horizon": 5, "folds": 4,
                                              "min_train": 100})
        self.assertTrue(r["ok"])
        body = r["result"]
        for key in ("skill", "mae", "base_mae", "dir_acc", "interval_coverage"):
            self.assertIn(key, body)
        self.assertEqual(body["folds"], 4)
        self.assertEqual(body["method"], "ar")

    def test_forecast_backtest数据不足(self):
        r = self.handle("forecast.backtest", {"bars": self.bars[:50],
                                              "folds": 5, "min_train": 100})
        self.assertEqual(r["error"]["code"], "BadData")

    def test_error响应都能被再次编码为合法帧(self):
        # 引擎最终要把响应写回管道 —— 错误对象里不能混进不可序列化的东西
        from finpulse_engine.protocol import encode_frame
        for params in ({"source": "bogus"},
                       {"bars": self.bars, "horizon": 999},
                       {"bars": self.bars[:3]}):
            resp = self.d.handle({"id": 7, "method": "forecast.run", "params": params})
            if not resp["ok"]:
                encode_frame(resp)          # 不抛即通过


# ── source.pull：批量拉取 ─────────────────────────────────────────


@ds_registry.register
class FakePullSource(DataSource):
    """测试用的"需要网络"数据源。

    **为什么不直接拿 tushare 来测**：它要 token、要网络。把网络状态当成
    断言的前提，结果就是"在家绿、在公司红"，最后没人看这些用例。
    这里要验的是 ``source.pull`` **自己的**逻辑 —— 去重、进度事件、
    回落判定、落盘、单只失败不拖垮整批 —— 用一个确定的假源才验得准。
    """

    name = "fakepull"
    description = "测试用假实时源"
    requires_network = True

    #: 用**类属性**而不是实例属性：``ds_registry.create()`` 每次都新建实例，
    #: 在测试里给某个实例赋的值到不了 RPC 内部新建的那个对象上。
    mode = "live_api"
    #: 这些代码一律抛异常，用来验"单只失败不中断整批"。
    boom: tuple = ("BOOM",)

    def load(self, symbol: str, bars: int, **kwargs) -> list:
        if symbol in type(self).boom:
            raise RuntimeError("模拟取数失败")
        n = max(1, min(int(bars), 5))
        px = 100.0
        out = []
        for i in range(n):
            out.append(Bar(ts=1_700_000_000_000 + i * 86_400_000,
                           open=round(px, 4), high=round(px * 1.01, 4),
                           low=round(px * 0.99, 4), close=round(px * 1.002, 4),
                           volume=1000))
            px *= 1.002
        return out

    def provenance(self) -> dict:
        return {"mode": type(self).mode, "detail": "fakepull"}


class _Events:
    """最小的事件收集器。接口只有 emit —— 和 FrameSink 一样。"""

    def __init__(self) -> None:
        self.items: list = []

    def emit(self, name: str, data=None) -> None:
        self.items.append((name, data))

    def names(self) -> list:
        return [n for n, _ in self.items]


class SourcePullTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        ds_registry.discover()

    def setUp(self):
        # 每个用例从干净状态开始：这两个是**类属性**，上一条用例的改动
        # 会留给下一条（测试之间通过全局状态互相影响时，失败会表现为
        # "单独跑能过、一起跑不行"）。
        FakePullSource.mode = "live_api"
        FakePullSource.boom = ("BOOM",)
        self.events = _Events()
        self.d = Dispatcher()
        build(self.d, event_out=self.events)

    def pull(self, **params):
        return self.d.handle({"id": 1, "method": "source.pull", "params": params})

    # ── 输入校验 ──

    def test_缺symbols报BadParams(self):
        r = self.pull(source="fakepull")
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"]["code"], "BadParams")

    def test_symbols不是数组报BadParams(self):
        r = self.pull(symbols="AAA.SH", source="fakepull")
        self.assertEqual(r["error"]["code"], "BadParams")

    def test_bars非正报BadParams(self):
        r = self.pull(symbols=["AAA.SH"], source="fakepull", bars=0)
        self.assertEqual(r["error"]["code"], "BadParams")

    def test_全是空白代码报BadParams(self):
        # 粘贴时多出来的空行不该变成"一只叫 '' 的股票"。
        r = self.pull(symbols=["", "   "], source="fakepull")
        self.assertEqual(r["error"]["code"], "BadParams")

    def test_未知数据源报NotFound(self):
        r = self.pull(symbols=["AAA.SH"], source="no-such-source")
        self.assertEqual(r["error"]["code"], "NotFound")

    def test_本地数据源拒绝拉取(self):
        """csv / synthetic 没有"实时数据"可拉。

        不拦的话，行为是"把缓存读出来再写回缓存" —— 除了刷新一下 mtime
        什么也没干，却会让用户以为刚才真的更新了行情。
        """
        for name in ("csv", "synthetic"):
            r = self.pull(symbols=["AAA.SH"], source=name)
            self.assertEqual(r["error"]["code"], "BadParams", msg=name)
            self.assertIn("本地数据源", r["error"]["message"], msg=name)

    # ── 成功路径 ──

    def test_成功拉取并落盘(self):
        out = tempfile.mkdtemp(prefix="finpulse-pull-")
        r = self.pull(symbols=["AAA.SH", "BBB.SH"], source="fakepull",
                      bars=3, out_dir=out)
        self.assertTrue(r["ok"], msg=str(r.get("error")))
        res = r["result"]

        self.assertEqual(res["source"], "fakepull")
        self.assertEqual(res["total"], 2)
        self.assertEqual(res["ok"], 2)
        self.assertEqual(res["failed"], 0)
        self.assertEqual(Path(res["dir"]).resolve(), Path(out).resolve())

        for it in res["items"]:
            self.assertTrue(it["ok"], msg=str(it))
            self.assertEqual(it["mode"], "live_api")
            self.assertEqual(it["rows"], 3)
            self.assertGreater(it["as_of"], 0)
            self.assertTrue(Path(it["path"]).is_file())
            self.assertEqual(Path(it["path"]).name, f"{it['symbol']}.csv")

    def test_落盘的文件能被CSV源原样读回(self):
        """读写必须共用同一套定位逻辑。

        这条断言守的是一个非常具体的症状：**"我刚拉完却说读不到"**。
        拉取走 write_bars、读取走 CsvSource._resolve，两边一旦各写一份
        路径推导，就会出现"文件明明在那儿、程序说没有"。
        """
        from finpulse_engine.datasource.csvfile import CsvSource
        out = tempfile.mkdtemp(prefix="finpulse-pull-")
        res = self.pull(symbols=["CCC.SH"], source="fakepull", bars=4,
                        out_dir=out)["result"]

        item = res["items"][0]
        self.assertTrue(item["ok"], msg=str(item))

        bars = CsvSource().load(symbol="CCC.SH", bars=10, path=item["path"])
        self.assertEqual(len(bars), 4)
        self.assertEqual(bars[-1].ts, item["as_of"])

    def test_落盘是原子替换不留临时文件(self):
        out = tempfile.mkdtemp(prefix="finpulse-pull-")
        self.pull(symbols=["DDD.SH"], source="fakepull", bars=3, out_dir=out)
        leftovers = [p.name for p in Path(out).iterdir() if p.name.endswith(".tmp")]
        self.assertEqual(leftovers, [])

    def test_重复代码被去重且保序(self):
        # 手抖粘两遍同一只股票，不该让汇总里出现两行、也不该白拉两次。
        out = tempfile.mkdtemp(prefix="finpulse-pull-")
        res = self.pull(symbols=["BBB.SH", "AAA.SH", "BBB.SH"],
                        source="fakepull", bars=2, out_dir=out)["result"]
        self.assertEqual(res["total"], 2)
        self.assertEqual([it["symbol"] for it in res["items"]],
                         ["BBB.SH", "AAA.SH"])

    def test_代码两端空白被去掉(self):
        out = tempfile.mkdtemp(prefix="finpulse-pull-")
        res = self.pull(symbols=["  AAA.SH  "], source="fakepull",
                        bars=2, out_dir=out)["result"]
        self.assertEqual(res["items"][0]["symbol"], "AAA.SH")

    # ── 进度事件 ──

    def test_进度事件按顺序推出且有总数(self):
        """界面上的「第 3/14 只」就是靠这里。没有它，批量任务只剩转圈。"""
        out = tempfile.mkdtemp(prefix="finpulse-pull-")
        self.pull(symbols=["AAA.SH", "BBB.SH", "CCC.SH"],
                  source="fakepull", bars=2, out_dir=out)

        names = self.events.names()
        self.assertEqual(names[0], "data.pull.start")
        self.assertEqual(names[-1], "data.pull.done")
        self.assertEqual(names.count("data.pull.symbol"), 3)

        start = dict(self.events.items)["data.pull.start"]
        self.assertEqual(start["total"], 3)
        self.assertEqual(start["source"], "fakepull")
        self.assertEqual(start["bars"], 2)

        steps = [d for n, d in self.events.items if n == "data.pull.symbol"]
        self.assertEqual([s["index"] for s in steps], [1, 2, 3])
        self.assertEqual([s["symbol"] for s in steps], ["AAA.SH", "BBB.SH", "CCC.SH"])
        self.assertTrue(all(s["total"] == 3 for s in steps))

        done = dict(self.events.items)["data.pull.done"]
        self.assertEqual((done["ok"], done["failed"]), (3, 0))

    def test_没有事件通道也能跑完(self):
        # 单测与离线调用就是这种情况。业务代码只管 emit，不该到处分叉。
        d = Dispatcher()
        build(d, event_out=None)
        out = tempfile.mkdtemp(prefix="finpulse-pull-")
        r = d.handle({"id": 1, "method": "source.pull",
                      "params": {"symbols": ["AAA.SH"], "source": "fakepull",
                                 "bars": 2, "out_dir": out}})
        self.assertTrue(r["ok"], msg=str(r.get("error")))
        self.assertEqual(r["result"]["ok"], 1)

    # ── 失败与回落 ──

    def test_单只失败不中断整批(self):
        """为一只写错的代码放弃另外九只没有道理。"""
        out = tempfile.mkdtemp(prefix="finpulse-pull-")
        res = self.pull(symbols=["AAA.SH", "BOOM", "CCC.SH"],
                        source="fakepull", bars=2, out_dir=out)["result"]

        self.assertEqual(res["total"], 3)
        self.assertEqual(res["ok"], 2)
        self.assertEqual(res["failed"], 1)

        by_symbol = {it["symbol"]: it for it in res["items"]}
        self.assertTrue(by_symbol["AAA.SH"]["ok"])
        self.assertTrue(by_symbol["CCC.SH"]["ok"])
        self.assertFalse(by_symbol["BOOM"]["ok"])
        self.assertIn("模拟取数失败", by_symbol["BOOM"]["error"])
        # 失败项必须带上原因类型，否则用户无从下手
        self.assertIn("RuntimeError", by_symbol["BOOM"]["error"])

    def test_单只失败仍然走完全部标的(self):
        out = tempfile.mkdtemp(prefix="finpulse-pull-")
        self.pull(symbols=["BOOM", "AAA.SH", "BBB.SH"],
                  source="fakepull", bars=2, out_dir=out)
        steps = [d for n, d in self.events.items if n == "data.pull.symbol"]
        self.assertEqual([s["symbol"] for s in steps],
                         ["BOOM", "AAA.SH", "BBB.SH"])

    def test_回落数据不算拉取成功(self):
        """拿到缓存/演示数据**不算成功**。

        这条是这儿最要紧的一条。批量拉取的目的是把实时数据落到本地；
        拿缓存覆盖缓存毫无意义，却会让摘要里写"成功 14 只"—— 而用户
        明天断网时读到的还是那份旧缓存，他以为早上刚刷新过。
        """
        out = tempfile.mkdtemp(prefix="finpulse-pull-")
        FakePullSource.mode = "local_cache"
        res = self.pull(symbols=["AAA.SH"], source="fakepull",
                        bars=2, out_dir=out)["result"]

        self.assertEqual(res["ok"], 0)
        self.assertEqual(res["failed"], 1)
        item = res["items"][0]
        self.assertFalse(item["ok"])
        self.assertEqual(item["mode"], "local_cache")
        self.assertIn("未取到实时数据", item["error"])
        self.assertIn("local_cache", item["error"])
        # 更重要的是：**什么都没有落盘**。
        self.assertEqual(list(Path(out).iterdir()), [])

    def test_演示数据同样不算成功(self):
        out = tempfile.mkdtemp(prefix="finpulse-pull-")
        FakePullSource.mode = "demo_synthetic"
        res = self.pull(symbols=["AAA.SH"], source="fakepull",
                        bars=2, out_dir=out)["result"]
        self.assertEqual(res["ok"], 0)
        self.assertIn("demo_synthetic", res["items"][0]["error"])

    def test_回落时摘要里dir为空(self):
        # dir 用来告诉用户"文件在哪儿"。一只都没落盘时给出一个目录，
        # 用户会去那个目录找根本不存在的文件。
        out = tempfile.mkdtemp(prefix="finpulse-pull-")
        FakePullSource.mode = "local_cache"
        res = self.pull(symbols=["AAA.SH", "BBB.SH"], source="fakepull",
                        bars=2, out_dir=out)["result"]
        self.assertEqual(res["dir"], "")


if __name__ == "__main__":
    unittest.main()
