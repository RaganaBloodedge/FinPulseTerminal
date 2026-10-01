"""工具注册表 —— 智能体可以调用的东西。

两类工具，边界在配置里是可见的（远程工具一律带 ``terminal.`` 前缀）：

* **本地工具**：跑在引擎进程里，直接调用 :mod:`finpulse_engine.analysis`
  与 :mod:`finpulse_engine.forecast`。指标、统计、量价、预测、回测。
* **远程工具**：通过 :mod:`finpulse_engine.agent.bridge` 反向回调 C++ 终端。
  它们回答的是只有终端才知道的问题（实时行情、总线统计、数据质量）。

## 另一件事：注册表里的名字 ≠ 发给模型的名字

远程工具的名字里有点号（``terminal.live_quote``）。**这对 function-calling
协议是非法的** —— OpenAI 系的 ``function.name`` 只允许 ``[A-Za-z0-9_-]``，
DeepSeek 更是直接 400 拒掉整次请求：

    Invalid 'tools[2].function.name': string does not match pattern
    '^[a-zA-Z0-9_-]+$'

点号是我们自己挑的（人类读配置时一眼能看出"这个工具跑在终端里"），
不该为一个别人的格式限制把它改掉 —— 那要连带动配置、C++ 桥、文档、
记忆里的既有结论。所以做法是**在边界上翻译**：注册表保持点号，
发出去时换成下划线，模型回传时再翻回来。见 :func:`to_wire_name`
与 :meth:`ToolRegistry.wire_catalog`。

## 一个刻意的设计决定：工具输出要为"读"设计

分析模块返回的是**给程序用**的结构：``indicators.compute_spec()`` 返回与
输入等长的 250 元素数组，因为 C++ 侧要靠下标跟 K 线对齐画图。

但把 250 个数原样塞进提示词，既浪费上下文，模型也读不出重点。所以工具层
负责**改写形状**：只保留最新值、显式命名（``ma.ma5`` 而不是 ``lines["ma5"][-1]``）、
补上模型需要的派生字段（区间高低点）。

一句话：**模块不为模型改，工具层负责翻译。**
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from finpulse_engine.agent.bridge import NullToolClient, RemoteToolClient
from finpulse_engine.analysis import flow, indicators, stats

#: 默认算哪几条指标。选这五条是因为它们覆盖了趋势、动量、波动、通道四类信息，
#: 再多就是重复了。
DEFAULT_INDICATOR_SPECS = ("ma:5,20", "rsi:14", "macd:12,26,9", "boll:20,2", "atr:14")


def _last(series: Optional[Sequence[Any]]) -> Any:
    """取序列里最后一个非 None 值。指标前导都是 None，不能直接取 [-1]。"""
    if not series:
        return None
    for value in reversed(series):
        if value is not None:
            return value if isinstance(value, (int, float)) else None
    return None


def _round(value: Any, digits: int = 4) -> Any:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return value
    return round(float(value), digits)


# ── 上下文 ────────────────────────────────────────────────────────


@dataclass
class ToolContext:
    """工具执行所需的一切。

    ``forecaster_factory`` 由上层注入而不是在这里 import ——
    :mod:`finpulse_engine.service` 要 import 本模块来注册 RPC，
    本模块再回头 import service 就成环了。注入同时还带来一个好处：
    换预测器实现不需要动工具层。
    """

    closes: List[float] = field(default_factory=list)
    highs: List[float] = field(default_factory=list)
    lows: List[float] = field(default_factory=list)
    volumes: List[float] = field(default_factory=list)
    timestamps: List[int] = field(default_factory=list)
    symbol: str = ""

    forecaster_factory: Optional[Callable[[], Any]] = None
    forecast_method: str = "ar"
    horizon: int = 5
    backtest_folds: int = 5
    backtest_min_train: int = 60
    risk_free: float = 0.0

    remote: RemoteToolClient = field(default_factory=NullToolClient)

    @property
    def bars(self) -> int:
        return len(self.closes)

    def as_of(self) -> str:
        """最后一根 K 线的日期。报告里每个数字都要能说清"截至什么时候"。"""
        from finpulse_engine.timeutil import format_date
        return format_date(self.timestamps[-1]) if self.timestamps else ""


# ── 线上名（wire name）────────────────────────────────────────────
#
# function-calling 协议对 ``function.name`` 的约束是 ``[A-Za-z0-9_-]{1,64}``。
# 我们的注册表里远程工具带点号（``terminal.live_quote``），必须换掉。
#
# **换名发生在传输层**（:mod:`finpulse_engine.agent.llm.openai_compat`），
# 不在注册表、也不在角色运行时：点号对我们自己毫无问题，只有"要 POST
# 出去"这一个场景才非法。规则后端（``rule_based``）同样会收到这份目录，
# 而它是按**原来的名字**查固定参数、按名字取工具结果的 —— 若在角色层就
# 把名字改掉，规则后端会表现为"这一段没有内容"，而不是报错。

#: 协议允许的最大长度。超了不是"可能出问题"，而是服务端直接 400。
WIRE_NAME_MAX = 64

#: 协议不允许的字符。
_WIRE_NAME_ILLEGAL = re.compile(r"[^A-Za-z0-9_-]")

#: 协议的合法形态。测试拿它做断言，别处也用得到。
WIRE_NAME_OK = re.compile(rf"^[A-Za-z0-9_-]{{1,{WIRE_NAME_MAX}}}$")


def to_wire_name(name: str) -> str:
    """注册表里的工具名 → 能安全放进 ``function.name`` 的名字。

    只做一件事：把非法字符换成下划线。刻意**不做**更聪明的映射
    （比如加前缀、编码成 ``terminal__live_quote``）—— 换出来的名字会
    原样出现在模型的视野和它的 tool_call 里，越接近原名的越好读、越难被
    模型改写错。

    这个映射不保证可逆（``a.b`` 与 ``a_b`` 撞同一个名字），所以回程不靠
    它反推，而是用 :func:`to_wire_catalog` 建的那张对照表 —— 一个纯函数
    没法知道全局有哪些名字。
    """
    return _WIRE_NAME_ILLEGAL.sub("_", name or "")


@dataclass(frozen=True)
class WireCatalog:
    """能发出去的工具目录，外加"名字翻回来"的对照表。"""

    schemas: List[Dict[str, Any]] = field(default_factory=list)
    #: 线上名 → 原来的名字。
    by_name: Dict[str, str] = field(default_factory=dict)

    def resolve(self, wire_name: str) -> str:
        """模型回传的工具名 → 原来的名字。

        认不出来就**原样返回**：那时该由 :meth:`ToolRegistry.call` 给出
        "未知工具"的结构化结果。在这里静默丢掉的话，模型的一次幻觉调用
        会表现成"这一轮什么都没调"，排查时看不出到底发生过什么。
        """
        return self.by_name.get(wire_name, wire_name)


def to_wire_catalog(schemas: Sequence[Dict[str, Any]]) -> WireCatalog:
    """把一份工具目录改成能发出去的样子，并给出回程对照表。

    两个副作用必须在这里挡住，因为**它们都是配置错误，不是运行期偶发**：

    * 两个不同的工具名撞成同一个线上名（``a.b`` 与 ``a_b``）——
      发出去之后模型只能回一个名字，我们无法知道它要调哪个；
    * 线上名超过协议上限 64 字符。

    当场抛异常，好过等到某次研判的 HTTP 400 里才暴露 —— 那时现场只剩
    一句"服务端拒绝了这个请求"，与真正的原因隔着好几层。
    """
    out: List[Dict[str, Any]] = []
    by_name: Dict[str, str] = {}
    for schema in schemas:
        fn = (schema or {}).get("function") or {}
        original = fn.get("name") or ""
        if not original:
            continue
        wire = to_wire_name(original)
        taken = by_name.get(wire)
        if taken is not None:
            if taken != original:
                raise ValueError(
                    f"工具名冲突：'{taken}' 与 '{original}' 的线上名都是 "
                    f"'{wire}'，发给 LLM 后无法区分该调哪个"
                )
            continue          # 目录里重复了同一个工具，去重
        if len(wire) > WIRE_NAME_MAX:
            raise ValueError(
                f"工具名 '{original}' 的线上名 '{wire}' 有 {len(wire)} 字符，"
                f"超过 function-calling 协议上限 {WIRE_NAME_MAX}"
            )
        by_name[wire] = original
        out.append({"type": schema.get("type", "function"),
                    "function": {**fn, "name": wire}})
    return WireCatalog(schemas=out, by_name=by_name)


# ── 工具规格 ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    properties: Dict[str, Any]
    required: List[str]
    handler: Callable[[ToolContext, Dict[str, Any]], Any]
    remote: bool = False
    #: 规则后端用的固定参数（它不做推理，直接把该角色的工具按这个参数调一遍）。
    defaults: Dict[str, Any] = field(default_factory=dict)

    def to_openai_schema(self) -> Dict[str, Any]:
        """转成 function-calling 目录项。这里用的是**注册表原名**。

        改名是传输层的事（见本文件"线上名"一节）：这份目录交给规则后端
        时也必须保持原名，否则它按名字查固定参数会全部查空。
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": dict(self.properties),
                    "required": list(self.required),
                },
            },
        }


# ── 本地工具实现 ──────────────────────────────────────────────────


def _tool_indicators(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    specs = args.get("specs") or list(DEFAULT_INDICATOR_SPECS)
    if not isinstance(specs, list):
        raise ValueError("specs 必须是字符串数组")
    if len(specs) > 16:
        raise ValueError("一次最多计算 16 条指标")

    out: Dict[str, Any] = {
        "symbol": ctx.symbol,
        "bars": ctx.bars,
        "as_of": ctx.as_of(),
        "last_close": _round(ctx.closes[-1]) if ctx.closes else None,
        "specs": list(specs),
    }
    for spec in specs:
        result = indicators.compute_spec(str(spec), ctx.closes, ctx.highs, ctx.lows)
        kind = result.get("kind")
        lines = result.get("lines") or {}
        if kind in ("ma", "ema"):
            bucket = out.setdefault("ma", {})
            for key, series in lines.items():
                bucket[key] = _round(_last(series))
        elif kind == "macd":
            out["macd"] = {k: _round(_last(v)) for k, v in lines.items()}
        elif kind == "boll":
            out["boll"] = {k: _round(_last(v)) for k, v in lines.items()}
        else:
            # rsi / atr / tr / kdj 这类单线指标：直接用线名做顶层键，
            # 于是 "rsi14" 就是 "rsi14"，不需要调用方去记数组下标。
            for key, series in lines.items():
                out[key] = _round(_last(series))

    # 最近 5 根收盘价：让模型能看出"最近是在往上还是往下"，
    # 否则它只有一堆均线数值，判断不出方向变化。
    out["recent_closes"] = [_round(v) for v in ctx.closes[-5:]]
    return out


def _tool_stats(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    risk_free = float(args.get("risk_free", ctx.risk_free) or 0.0)
    report = stats.summary(ctx.closes, ctx.timestamps, risk_free)
    # 模块只管收益率，不管价格水平的极值；而"关键位"这种事必须用到它们。
    # 在工具层补上，不改模块——模块的字段名被 C++ 侧的风险面板依赖着。
    if ctx.highs:
        report["max_high"] = _round(max(ctx.highs))
    if ctx.lows:
        report["min_low"] = _round(min(ctx.lows))
    report["symbol"] = ctx.symbol
    return report


def _tool_flow(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    window = int(args.get("window", 20) or 20)
    if not (3 <= window <= 120):
        raise ValueError("window 应在 3..120 之间")
    return flow.volume_profile(ctx.closes, ctx.volumes, ctx.highs, ctx.lows, window=window)


def _tool_forecast(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    if ctx.forecaster_factory is None:
        return {"available": False,
                "reason": "本次运行未注入预测器工厂，无法执行预测"}
    horizon = int(args.get("horizon", ctx.horizon) or ctx.horizon)
    if not (1 <= horizon <= 60):
        raise ValueError("horizon 应在 1..60 之间")

    model = ctx.forecaster_factory()
    model.fit(ctx.closes)
    preds = list(model.predict(horizon))
    try:
        bands = list(model.interval(horizon))
    except Exception:  # noqa: BLE001 —— 区间算不出不该拖垮点预测
        bands = [(v, v) for v in preds]

    points = []
    for i, value in enumerate(preds):
        lo, hi = bands[i] if i < len(bands) else (value, value)
        points.append({"step": i + 1, "value": _round(value),
                       "lower": _round(lo), "upper": _round(hi)})
    return {
        "method": ctx.forecast_method,
        "symbol": ctx.symbol,
        "last_close": _round(ctx.closes[-1]),
        "horizon": horizon,
        "points": points,
        # meta 里的 AIC / 阶数 / 漂移这些是模型自述，原样带上便于追责。
        "meta": model.meta(),
    }


def _tool_backtest(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    from finpulse_engine.forecast import backtest as bt

    if ctx.forecaster_factory is None:
        return {"available": False,
                "reason": "本次运行未注入预测器工厂，无法执行回测"}
    folds = int(args.get("folds", ctx.backtest_folds) or ctx.backtest_folds)
    horizon = int(args.get("horizon", ctx.horizon) or ctx.horizon)
    min_train = int(args.get("min_train", ctx.backtest_min_train) or ctx.backtest_min_train)
    if not (1 <= folds <= 30):
        raise ValueError("folds 应在 1..30 之间")
    if not (1 <= horizon <= 30):
        raise ValueError("horizon 应在 1..30 之间")
    if ctx.bars < min_train + folds * horizon:
        return {"available": False,
                "reason": f"样本 {ctx.bars} 根不足以完成 {folds} 折 × {horizon} 步回测"
                          f"（需要至少 {min_train + folds * horizon} 根）"}

    report = bt.run(ctx.closes, ctx.forecaster_factory,
                    folds=folds, horizon=horizon, min_train=min_train)
    report["method"] = ctx.forecast_method
    return report


# ── 远程工具实现 ──────────────────────────────────────────────────
# 三个薄封装：真正的执行发生在 C++ 侧。这里不做任何加工，
# 因为 C++ 返回什么就是什么——加工会让"这个数字来自终端"这件事变得不可追溯。


def _tool_live_quote(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    payload = {"symbol": args.get("symbol") or ctx.symbol}
    return ctx.remote.call("terminal.live_quote", payload)


def _tool_bus_stats(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    return ctx.remote.call("terminal.bus_stats", {})


def _tool_data_quality(ctx: ToolContext, args: Dict[str, Any]) -> Dict[str, Any]:
    payload = {"symbol": args.get("symbol") or ctx.symbol}
    return ctx.remote.call("terminal.data_quality", payload)


# ── 注册表 ────────────────────────────────────────────────────────


class ToolRegistry:
    """名字 → 工具实现。查找失败返回 ``None`` 而不是抛异常："""

    def __init__(self) -> None:
        self._specs: Dict[str, ToolSpec] = {}

    def register(self, spec: ToolSpec) -> None:
        if spec.name in self._specs:
            raise ValueError(f"工具名重复: {spec.name}")
        self._specs[spec.name] = spec

    def get(self, name: str) -> Optional[ToolSpec]:
        return self._specs.get(name)

    def names(self) -> List[str]:
        return sorted(self._specs)

    def catalog(self, names: Sequence[str]) -> List[Dict[str, Any]]:
        """把角色声明的工具名转成 OpenAI function-calling 目录。

        名字写得不对时**不报错也不静默丢弃**，而是记进 ``missing`` —— 由角色
        运行时把它写进轨迹。静默丢弃会让"配置里写错了工具名"变成一个
        没人发现的空工具位。

        这里用的是**注册表原名**（点上号的点号照旧）。真正要 POST 出去的
        那份由传输层用 :func:`to_wire_catalog` 现改。
        """
        out: List[Dict[str, Any]] = []
        for name in names:
            spec = self._specs.get(name)
            if spec is not None:
                out.append(spec.to_openai_schema())
        return out

    def wire_catalog(self, names: Sequence[str]) -> WireCatalog:
        """把角色声明的工具名直接转成"能发出去"的形态。

        传输层与这几条自检逻辑（撞名、超长）都收在 :func:`to_wire_catalog`
        里，这里只是省掉调用方先取 :meth:`catalog` 再转一次。装机自检与
        测试用它，比手工拼一份目录更省事。
        """
        return to_wire_catalog(self.catalog(names))

    def missing(self, names: Sequence[str]) -> List[str]:
        return [n for n in names if n not in self._specs]

    def call(self, name: str, arguments: Dict[str, Any], ctx: ToolContext) -> Dict[str, Any]:
        """执行一个工具。任何异常都转成结构化结果，**不向上抛**。

        理由：一次工具失败不应该让整轮分析作废。把失败如实交给模型看，
        它在报告里会写"该数据不可用"——这正是我们想要的行为。
        """
        spec = self._specs.get(name)
        if spec is None:
            return {"available": False, "tool": name,
                    "reason": f"未知工具 '{name}'；已注册: {self.names()}"}
        try:
            result = spec.handler(ctx, dict(arguments or {}))
        except Exception as exc:  # noqa: BLE001 —— 见上
            return {"available": False, "tool": name,
                    "error": f"{type(exc).__name__}: {exc}"}
        if not isinstance(result, dict):
            return {"available": True, "tool": name, "value": result}
        result.setdefault("available", True)
        result.setdefault("tool", name)
        return result


def default_registry() -> ToolRegistry:
    """构造本项目的标准工具集。"""
    reg = ToolRegistry()

    reg.register(ToolSpec(
        name="indicators",
        description=("计算技术指标的最新数值。返回均线(ma)、相对强弱(rsi*)、"
                     "指数平滑异同(macd)、布林带(boll)、真实波幅(atr*) 以及最近 5 根收盘价。"
                     "所有数值截至 as_of 日期。"),
        properties={
            "specs": {
                "type": "array", "items": {"type": "string"},
                "description": ('指标规格，如 "ma:5,20"、"rsi:14"、"macd:12,26,9"、'
                                '"boll:20,2"、"atr:14"。省略则计算默认的五条。'),
            },
        },
        required=[],
        handler=_tool_indicators,
        defaults={},
    ))

    reg.register(ToolSpec(
        name="stats",
        description=("描述性统计与风险指标：区间涨跌、年化收益(CAGR)、年化波动率、"
                     "夏普/索提诺、最大回撤及其区间与是否收复、VaR/CVaR(95%)、"
                     "偏度、超额峰度、自相关、涨跌日占比、区间最高/最低价。"),
        properties={
            "risk_free": {"type": "number", "description": "年化无风险利率，默认 0"},
        },
        required=[],
        handler=_tool_stats,
        defaults={},
    ))

    reg.register(ToolSpec(
        name="flow",
        description=("量价与波动状态：成交量相对均量的放大/萎缩倍数、上涨日与下跌日"
                     "成交量对比、|收益| 自相关（波动聚集）、当前波动率在自身历史的"
                     "分位、以及价格创新高/新低但量能未跟上的背离。"),
        properties={
            "window": {"type": "integer", "description": "滚动窗口，默认 20"},
        },
        required=[],
        handler=_tool_flow,
        defaults={},
    ))

    reg.register(ToolSpec(
        name="forecast",
        description="按指定方法预测未来若干步，含置信区间与模型自述信息（阶数、AIC 等）。",
        properties={
            "horizon": {"type": "integer", "description": "预测步数，默认取本次运行设置"},
        },
        required=[],
        handler=_tool_forecast,
        defaults={},
    ))

    reg.register(ToolSpec(
        name="backtest",
        description=("滚动回测（walk-forward，扩张窗口无前视），并给出随机游走基线的对照。"
                     "**技能分 = 1 − MSE_model / MSE_randomwalk**，小于等于 0 表示模型"
                     "还不如随机游走。任何引用预测结论的分析都必须先看这个数字。"),
        properties={
            "folds": {"type": "integer", "description": "折数，默认取本次运行设置"},
            "horizon": {"type": "integer", "description": "每折预测步数"},
            "min_train": {"type": "integer", "description": "首折最小训练样本数"},
        },
        required=[],
        handler=_tool_backtest,
        defaults={},
    ))

    # ── 远程：需要 C++ 终端在监听 ──
    reg.register(ToolSpec(
        name="terminal.live_quote",
        description=("【远程·终端】取终端此刻正在接收的最新行情快照。"
                     "回放或实时模式下才有数据——它反映的是**终端当前状态**，"
                     "不是历史 K 线，不要把它当作另一份历史数据使用。"),
        properties={"symbol": {"type": "string", "description": "标的代码，省略则用当前标的"}},
        required=[],
        handler=_tool_live_quote,
        remote=True,
        defaults={},
    ))

    reg.register(ToolSpec(
        name="terminal.bus_stats",
        description=("【远程·终端】行情总线的投递统计（发布数 / 投递数 / 未匹配订阅 / "
                     "失败数 / 活跃订阅数）。可用来判断终端是否真的在收行情，"
                     "以及是否存在订阅掉帧。"),
        properties={},
        required=[],
        handler=_tool_bus_stats,
        remote=True,
        defaults={},
    ))

    reg.register(ToolSpec(
        name="terminal.data_quality",
        description=("【远程·终端】由终端对当前标的做数据质量检查"
                     "（非正价格、high<low、时间倒序等），返回问题清单。"),
        properties={"symbol": {"type": "string", "description": "标的代码，省略则用当前标的"}},
        required=[],
        handler=_tool_data_quality,
        remote=True,
        defaults={},
    ))

    # 装机自检：所有工具名都要能安全地发给 LLM。放在这里而不是等发请求时
    # 才发现 —— wire 名冲突/超长是**配置错误**，构造注册表时就该炸出来。
    reg.wire_catalog(reg.names())

    return reg


def serialize(result: Any) -> str:
    """工具结果转成塞进 ``role='tool'`` 消息的文本。

    ``ensure_ascii=False`` 让中文工具名与原因保持可读；
    ``default=str`` 兜住 datetime 之类的非 JSON 类型，避免因为一个
    边缘字段把整轮分析打断。
    """
    try:
        return json.dumps(result, ensure_ascii=False, default=str)
    except (TypeError, ValueError) as exc:
        return json.dumps({"available": False, "error": f"工具结果无法序列化: {exc}"},
                          ensure_ascii=False)
