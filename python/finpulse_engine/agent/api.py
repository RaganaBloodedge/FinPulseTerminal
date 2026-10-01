"""智能体能力的 RPC 门面 —— 把编排层接到壳上。

这一层只做三件事，**不含任何研判逻辑**：

1. 把壳传来的 bars 组装成 :class:`~finpulse_engine.agent.tools.ToolContext`；
2. 调编排层，把结果转成可 JSON 化的字典；
3. 把执行过程中的进度转成事件推回壳。

## 为什么单独一个模块

``service.py`` 已经有 400 行基础分析的方法。智能体有它自己的配置体系
（角色 / 投委会 / 工具桥 / 记忆）和它自己的生命周期（配置加载一次、
记忆是跨请求的），塞进 service.py 会让那个文件同时承担两种职责。

## 一个刻意的取舍：编排器是**进程级单例**

角色配置与投委会配置在构造时读盘一次。每个请求重新读盘的话，会出现
"两次运行用了不同配置"这种在报告里根本看不出来的问题；而且读盘 + 校验
是几十毫秒量级，在秒级的研判里占比不小。

代价是改完配置要重启引擎才能生效。这是可接受的——配置是**部署**的一部分，
不是运行时状态。开发时用 ``reload()`` 显式刷新。

## 记忆为什么要跨请求

``DecisionMemory`` 记录历次决议。同一个标的连续几次研判的结论如果一直
在翻转，那本身是一条重要信息（"这个方向的判据不稳定"）。每次请求都新建
一个空的记忆，这条信息就永远看不到。所以它挂在单例上。
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Sequence

from finpulse_engine.datasource.base import Bar
from finpulse_engine.forecast import registry as fc_registry
from finpulse_engine.rpc import BadData, BadParams, NotFound

from . import config as agent_config
from .bridge import RemoteToolClient, connect as bridge_connect
from .config import ModelConfig
from .guardrails import Pipeline
from .memory import DecisionMemory
from .llm import discovery
from .llm import registry as llm_registry
from .llm.base import Message
from .llm.override import LlmOverride, from_params as override_from_params
from .orchestrator import DebateResult, OrchestrationError, Orchestrator, TeamResult
from .roles import describe_intent
from .tools import ToolContext, default_registry
from finpulse_engine.stream import EventSink, make_sink, null_sink

log = logging.getLogger("finpulse.agent.api")

#: 对话最多带多少条消息给模型。超过就只留最近的 —— 对话越长越贵，而早期
#: 的上下文对当前问题几乎没有帮助。截断会在返回值里标记出来（truncated），
#: 不做静默截断：用户有权知道自己这句话是在"没有完整上下文"下被回答的。
_CHAT_HISTORY_LIMIT = 24


def _llm_override(provider: str, model: str, base_url: str, api_key: str) -> Optional[LlmOverride]:
    """把四个 RPC 参数打包成覆盖对象。全空时返回 None（= 照配置来）。

    空字符串一律当"没填"处理：前端拿一个空的输入框提交上来是常态，
    如果空串被当成"要覆盖成空 provider"，那每次跑研判都会降级到规则后端。
    """
    return override_from_params({
        "provider": provider,
        "model": model,
        "base_url": base_url,
        "api_key": api_key,
    })

#: 保留多少次运行的轨迹供 ``agent.trace`` 回查。
#: 不做无限保留：轨迹里有完整的工具输出，是最占内存的部分。
TRACE_KEEP = 32


def _chat_system_prompt(persona: str, context: Dict[str, Any]) -> str:
    """拼对话用的系统提示词。

    两件事必须写死：**你现在看的是什么数据**，以及**不许编造数字**。
    一个可以自由回答的模型，在没有行情约束时最容易做的就是凭印象报一个
    "茅台大概一千六"——那正是这个终端最不该出现的输出：它看起来像数据，
    实际是模型的记忆，而记忆是会过期的。
    """
    lines = [
        persona.strip() or "你是一名证券分析助手，服务于本地金融终端 FinPulse Terminal。",
        "",
        "终端当前装载的数据：",
    ]
    if context:
        for key, label in (("symbol", "标的"), ("source", "数据源"),
                           ("rows", "K 线根数"), ("as_of", "数据截止"),
                           ("last_close", "最新收盘"), ("range_pct", "区间涨跌"),
                           ("ann_vol_pct", "年化波动率"), ("max_drawdown_pct", "最大回撤")):
            value = context.get(key)
            if value is None or value == "":
                continue
            lines.append(f"  · {label}: {value}")
        prov = context.get("provenance")
        if prov:
            lines.append(f"  · 数据出处: {prov}")
    else:
        lines.append("  （当前没有装载任何行情数据，用户可能在问一般性问题）")

    lines += [
        "",
        "回答要求：",
        "  1. 用中文，直接回答，先结论后依据；",
        "  2. **只使用上面给出的数字**。没有的就说没有，不要凭印象估算，"
        "也不要引用你记忆里的历史价格 —— 报价与日期必须来自终端；",
        "  3. 给判断时要带条件与风险，不要下「必涨必跌」式的确定性承诺；",
        "  4. 数据出处不是实时接口时，主动提醒用户；",
        "  5. 简短：几段之内说完。要结构化的研究报告，用户会去跑投委会研判。",
    ]
    return "\n".join(lines)


# ── 运行记录 ──────────────────────────────────────────────────────


class RunRecord:
    """一次调用的可回查记录。"""

    def __init__(self, run_id: str, kind: str, params: Dict[str, Any]) -> None:
        self.run_id = run_id
        self.kind = kind                      # role / team / debate
        self.params = params
        self.started_at = time.time()
        self.duration_ms = 0.0
        self.events: List[Dict[str, Any]] = []
        self.result: Optional[Dict[str, Any]] = None
        self.error: str = ""

    def add(self, event: str, data: Dict[str, Any]) -> None:
        # 事件全部留档：界面出问题时，"引擎到底推了什么"是第一个要问的问题。
        self.events.append({"t": round((time.time() - self.started_at) * 1000.0, 2),
                            "event": event, "data": data})

    def to_json(self, *, include_events: bool = True,
                include_result: bool = True) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "run_id": self.run_id,
            "kind": self.kind,
            "duration_ms": round(self.duration_ms, 3),
            "event_count": len(self.events),
            "params": self.params,
        }
        if self.error:
            d["error"] = self.error
        if include_events:
            d["events"] = list(self.events)
        if include_result and self.result is not None:
            d["result"] = self.result
        return d


# ── 服务 ──────────────────────────────────────────────────────────


class AgentService:
    """持有编排器与记忆，对 RPC 层暴露不抛异常的接口。"""

    def __init__(
        self,
        *,
        orchestrator: Optional[Orchestrator] = None,
        memory: Optional[DecisionMemory] = None,
        sink: Optional[EventSink] = None,
    ) -> None:
        if memory is None:
            memory = DecisionMemory()
        self.memory = memory
        self.orchestrator = orchestrator or Orchestrator(
            tools=default_registry(),
            guards=Pipeline(),
            memory=memory,
        )
        self.sink: EventSink = sink or null_sink()
        self._runs: "OrderedDict[str, RunRecord]" = OrderedDict()
        self._lock = threading.Lock()

    # ── 配置 ─────────────────────────────────────────────────
    def reload(self) -> Dict[str, Any]:
        """重新读盘加载角色与投委会配置。开发改配置时用。"""
        self.orchestrator.roles = agent_config.load_all()
        self.orchestrator.panels = agent_config.load_panels()
        return {"roles": sorted(self.orchestrator.roles),
                "panels": sorted(self.orchestrator.panels)}

    def list_roles(self) -> Dict[str, Any]:
        items = []
        for rid in sorted(self.orchestrator.roles):
            cfg = self.orchestrator.roles[rid]
            items.append({
                "id": cfg.id,
                "name": cfg.name,
                "description": cfg.description,
                "category": cfg.category,
                "version": cfg.version,
                "capabilities": list(cfg.capabilities),
                "tools": list(cfg.tools),
                "output_schema": cfg.output_schema,
                "output_sections": list(cfg.output_sections),
                "direction_sections": cfg.direction_scope,
                "provider": cfg.model.provider,
                "model_id": cfg.model.model_id,
                "memory": cfg.memory,
                "reasoning": cfg.reasoning,
                "max_tool_calls": cfg.max_tool_calls,
            })
        reg = self.orchestrator.tools
        return {
            "roles": items,
            "count": len(items),
            "categories": sorted({i["category"] for i in items}),
            "tools": reg.names(),
            "missing_tools": {
                i["id"]: reg.missing(i["tools"]) for i in items if reg.missing(i["tools"])
            },
        }

    def get_role(self, role_id: str) -> Dict[str, Any]:
        cfg = self._role(role_id)
        parts = [c for c in self.orchestrator.tools.catalog(cfg.tools)]
        d = cfg.to_json()
        d["instructions"] = cfg.instructions
        d["tools_detail"] = [
            {"name": p["function"]["name"],
             "description": p["function"]["description"],
             "remote": self.orchestrator.tools.get(p["function"]["name"]).remote}
            for p in parts
        ]
        return d

    def list_panels(self) -> Dict[str, Any]:
        items = []
        for pid in sorted(self.orchestrator.panels):
            p = self.orchestrator.panels[pid]
            d = p.to_json()
            problems = agent_config.validate_panel(p, self.orchestrator.roles)
            d["problems"] = [{"level": lv, "message": m} for lv, m in problems]
            d["valid"] = not any(lv == agent_config.PANEL_ERROR for lv, _ in problems)
            items.append(d)
        return {"panels": items, "count": len(items)}

    # ── 运行 ─────────────────────────────────────────────────
    def run_role(
        self,
        role_id: str,
        ctx: ToolContext,
        *,
        override: Optional[LlmOverride] = None,
        include_text: bool = True,
    ) -> Dict[str, Any]:
        # 先自己解析一次角色：编排层对未知角色抛的是 ConfigError（它不知道
        # 这是"RPC 调用方写错了"，还是"配置文件坏了"）。在门面上换成
        # NotFound，并顺手把可选项列出来 —— 前端最需要的就是这个列表。
        self._role(role_id)
        rec = self._begin("role", {"role": role_id})
        try:
            run = self.orchestrator.run_role(
                role_id, ctx, override=override,
                progress=self._hook(rec),
            )
        except Exception as exc:  # noqa: BLE001
            return self._fail(rec, exc)
        out = run.to_json(include_text=include_text)
        out["run_id"] = rec.run_id
        # 单角色路径没有 DebateResult 可挂，议题在这里补上。措辞与
        # build_task_prompt 共用 describe_intent —— 报告头部显示的必须是
        # 模型真正被告知的那句话，不是另写一句意思相近的。
        out["intent"] = describe_intent(ctx)
        return self._finish(rec, out)

    def run_team(
        self,
        role_ids: Sequence[str],
        ctx: ToolContext,
        *,
        panel_name: str = "自定义组合",
        override: Optional[LlmOverride] = None,
        weights: Optional[Dict[str, float]] = None,
        include_text: bool = True,
    ) -> Dict[str, Any]:
        if not role_ids:
            raise BadParams("role_ids 不能为空")
        if len(role_ids) > 12:
            raise BadParams("一次最多 12 个角色（再多上游会限流）")
        for rid in role_ids:
            self._role(rid)      # 未知角色一律 NotFound，理由同 run_role
        rec = self._begin("team", {"roles": list(role_ids)})
        try:
            result: TeamResult = self.orchestrator.run_team(
                role_ids, ctx, panel_name=panel_name,
                override=override, weights=weights,
                progress=self._hook(rec),
            )
        except Exception as exc:  # noqa: BLE001
            return self._fail(rec, exc)
        out = result.to_json(include_text=include_text)
        out["run_id"] = rec.run_id
        return self._finish(rec, out)

    def debate(
        self,
        ctx: ToolContext,
        *,
        panel: Optional[str] = None,
        override: Optional[LlmOverride] = None,
        rounds: Optional[int] = None,
        include_text: bool = True,
    ) -> Dict[str, Any]:
        rec = self._begin("debate", {"panel": panel, "rounds": rounds})
        try:
            result: DebateResult = self.orchestrator.debate(
                ctx, panel=panel, override=override,
                rounds=rounds, progress=self._hook(rec),
            )
        except Exception as exc:  # noqa: BLE001
            return self._fail(rec, exc)
        out = result.to_json(include_text=include_text)
        out["run_id"] = rec.run_id
        # 记忆的写入由 RoleRuntime 自己做（每个角色一份，主席那份就是会议决议）。
        # 这里不再单独记一遍：两次运行写两条同样的记录会让一致性统计翻倍。
        return self._finish(rec, out)

    # ── 回查 ─────────────────────────────────────────────────
    def trace(self, run_id: str) -> Dict[str, Any]:
        rec = self._run(run_id)
        return rec.to_json()

    def list_runs(self, limit: int = 10) -> Dict[str, Any]:
        with self._lock:
            items = list(self._runs.values())[-max(1, int(limit)):]
        return {"runs": [r.to_json(include_events=False, include_result=False)
                         for r in reversed(items)]}

    def recent_decisions(self, symbol: Optional[str] = None,
                         limit: int = 10) -> Dict[str, Any]:
        items = self.memory.recent(int(limit), symbol=symbol or None)
        return {
            "decisions": [d.to_json() for d in items],
            "count": len(items),
            "symbols": self.memory.symbols(),
        }

    def consistency(self, symbol: str, roles: Sequence[str]) -> Dict[str, Any]:
        reports = self.memory.consistency_report(symbol, roles) if roles else []
        return {
            "symbol": symbol,
            "reports": [r.to_json() for r in reports],
        }

    def stream_stats(self) -> Dict[str, Any]:
        getter = getattr(self.sink, "stats", None)
        return getter() if callable(getter) else {"enabled": False}

    # ── 内部 ─────────────────────────────────────────────────
    def _role(self, role_id: str):
        cfg = self.orchestrator.roles.get(role_id)
        if cfg is None:
            raise NotFound(
                f"未知角色: {role_id}（可用: {', '.join(sorted(self.orchestrator.roles))}）"
            )
        return cfg

    def _begin(self, kind: str, params: Dict[str, Any]) -> RunRecord:
        rec = RunRecord(uuid.uuid4().hex[:12], kind, params)
        with self._lock:
            self._runs[rec.run_id] = rec
            while len(self._runs) > TRACE_KEEP:
                self._runs.popitem(last=False)
        self.sink.emit("agent.run.start", {
            "run_id": rec.run_id, "kind": kind, "params": params,
            "started_at": rec.started_at,
        })
        return rec

    def _hook(self, rec: RunRecord):
        """把编排层的进度回调接到事件通道上。"""
        def hook(event: str, data: Dict[str, Any]) -> None:
            rec.add(event, data)
            self.sink.emit(f"agent.{event}", {"run_id": rec.run_id, **data})
        return hook

    def _finish(self, rec: RunRecord, out: Dict[str, Any]) -> Dict[str, Any]:
        rec.duration_ms = (time.time() - rec.started_at) * 1000.0
        rec.result = out
        self.sink.emit("agent.run.done", {
            "run_id": rec.run_id, "kind": rec.kind,
            "duration_ms": round(rec.duration_ms, 3),
            "valid": out.get("valid"),
            "direction": out.get("direction"),
            "confidence": out.get("confidence"),
        })
        return out

    def _fail(self, rec: RunRecord, exc: Exception) -> Dict[str, Any]:
        # 编排层抛的 ConfigError / OrchestrationError 是**配置问题**，
        # 不是引擎坏了。翻译成 BadParams 让壳能给出可操作的提示。
        rec.error = f"{type(exc).__name__}: {exc}"
        rec.duration_ms = (time.time() - rec.started_at) * 1000.0
        self.sink.emit("agent.run.error", {"run_id": rec.run_id,
                                           "kind": rec.kind, "error": rec.error})
        if isinstance(exc, (agent_config.ConfigError, OrchestrationError)):
            raise BadParams(rec.error) from exc
        raise

    def _run(self, run_id: str) -> RunRecord:
        with self._lock:
            rec = self._runs.get(run_id)
        if rec is None:
            raise NotFound(f"没有 run_id={run_id} 的记录（只保留最近 {TRACE_KEEP} 次）")
        return rec


# ── ToolContext 组装 ──────────────────────────────────────────────


def context_from(
    bars: Any,
    *,
    symbol: str = "",
    method: str = "ar",
    horizon: int = 5,
    folds: int = 5,
    min_train: int = 60,
    risk_free: float = 0.0,
    tool_bridge: Optional[Dict[str, Any]] = None,
) -> ToolContext:
    """把 RPC 传来的 bars 组装成 ToolContext。

    这里和 ``service.require_bars`` 重复了一点点校验，但不是复制粘贴：
    ``require_bars`` 面向"分析一批数据"，这里是"喂给智能体的证据集"，
    对样本量的要求和报错措辞都不一样（智能体的提示词里会点名样本量，
    样本太少时它写出来的东西会全是"数据不足"）。
    """
    if bars is None:
        raise BadParams("agent 方法需要 bars 参数（先用 source.load 取一段行情）")
    if not isinstance(bars, list):
        raise BadParams(f"bars 必须是数组，实际是 {type(bars).__name__}")
    if len(bars) < 60:
        # 60 是回测的最小样本量，也是指标全部有值的最低要求。低于这个数，
        # 每个角色都只能写出"数据不足"，跑一场会全是噪声。
        raise BadData(
            f"bars 只有 {len(bars)} 根，智能体研判至少需要 60 根"
            "（否则回测与长周期指标都没有值，角色的结论只能写「数据不足」）"
        )

    out: List[Bar] = []
    for i, item in enumerate(bars):
        if not isinstance(item, dict):
            raise BadData(f"bars[{i}] 不是对象")
        try:
            out.append(Bar.from_dict(item))
        except (KeyError, TypeError, ValueError) as exc:
            raise BadData(f"bars[{i}] 字段不合法: {exc}") from exc

    method = str(method or "ar")
    # 先补一次 discover()：引擎正常启动时 ``__main__`` 已经调用过，但
    # 单测/嵌入式调用未必。不做这一步的话，注册表为空会让下面那条
    # "未知道预测方法" 的报错变成纯粹误导 —— 列出的可用方法是空的。
    fc_registry.discover()
    if method not in fc_registry.names():
        raise NotFound(f"未知预测方法: {method}（可用: {', '.join(fc_registry.names())}）")

    horizon = int(horizon)
    if not 1 <= horizon <= 30:
        raise BadParams(f"horizon 应在 1..30，拿到 {horizon}")
    folds = int(folds)
    min_train = int(min_train)
    if not 1 <= folds <= 30:
        raise BadParams(f"folds 应在 1..30，拿到 {folds}")
    if min_train < 10:
        raise BadParams(f"min_train 至少为 10，拿到 {min_train}")

    def factory():
        return fc_registry.create(method)

    remote: RemoteToolClient = bridge_connect(
        (tool_bridge or {}).get("endpoint", ""),
        (tool_bridge or {}).get("token", ""),
    )

    return ToolContext(
        closes=[b.close for b in out],
        highs=[b.high for b in out],
        lows=[b.low for b in out],
        volumes=[b.volume for b in out],
        timestamps=[b.ts for b in out],
        symbol=symbol,
        forecaster_factory=factory,
        forecast_method=method,
        horizon=horizon,
        backtest_folds=folds,
        backtest_min_train=min_train,
        risk_free=float(risk_free),
        remote=remote,
    )


# ── RPC 注册 ──────────────────────────────────────────────────────


def install(dispatcher: Any, out: Optional[Any] = None,
            service: Optional[AgentService] = None,
            write_lock: Optional[Any] = None) -> AgentService:
    """把智能体方法注册到调度器上。

    ``out`` 是事件要写进去的二进制流（引擎里就是 stdout）。为 ``None``
    时事件通道关闭 —— 单测直接调这些函数时就是这种情况，不需要为此
    把调用逻辑分叉成两套。

    ``write_lock`` 必须与主循环写响应帧用的锁是**同一把**：事件帧和响应帧
    走同一条 stdout，各持一把锁挡不住交错，交错的字节在对面就是一个坏帧。
    """
    svc = service or AgentService(sink=make_sink(out, lock=write_lock))
    # 已经有人建好 service、但还没接上事件流时，把流补上。
    if out is not None and svc.sink is not None and not getattr(svc.sink, "enabled", False):
        svc.sink = make_sink(out, lock=write_lock)

    # ── 元信息 ────────────────────────────────────────────
    @dispatcher.method("agent.roles")
    def agent_roles() -> Dict[str, Any]:
        """列出所有分析角色及其工具与输出契约。"""
        return svc.list_roles()

    @dispatcher.method("agent.role.get")
    def agent_role_get(role_id: str = "") -> Dict[str, Any]:
        """取单个角色的完整定义（含 instructions 原文）。"""
        if not role_id:
            raise BadParams("需要 role_id")
        return svc.get_role(role_id)

    @dispatcher.method("agent.panels")
    def agent_panels() -> Dict[str, Any]:
        """列出所有投委会配置及其配置体检结果。"""
        return svc.list_panels()

    @dispatcher.method("agent.tools")
    def agent_tools(role_id: str = "") -> Dict[str, Any]:
        """列出工具目录。给 role_id 时只列该角色可见的那部分。"""
        reg = svc.orchestrator.tools
        names = reg.names() if not role_id else list(svc._role(role_id).tools)
        return {
            "tools": [
                {
                    "name": name,
                    "description": reg.get(name).description,
                    "remote": reg.get(name).remote,
                    "defaults": dict(reg.get(name).defaults),
                    "properties": dict(reg.get(name).properties),
                }
                for name in names if reg.get(name) is not None
            ],
            "count": len(names),
        }

    @dispatcher.method("agent.bridge.status")
    def agent_bridge_status(tool_bridge: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """探一次终端工具桥。前端可以在跑研判前先调这个，把不可用状态提前显示。"""
        client = bridge_connect((tool_bridge or {}).get("endpoint", ""),
                                (tool_bridge or {}).get("token", ""))
        desc = client.describe()
        if desc.get("available"):
            desc["tools"] = [t.get("name") for t in client.list_tools()]
        return desc

    @dispatcher.method("agent.llm.status")
    def agent_llm_status() -> Dict[str, Any]:
        """推理后端自述：有哪些可选、当前环境里哪些真的可用、各角色默认用哪个。

        **这个接口存在的理由**：一个声称"能接大模型"的智能体，如果回答不了
        "你现在到底拿什么在思考"，那它的推理过程就是不可信的。在这之前，
        想回答这个问题只能去翻 ``configs/*.json`` —— 门槛高到没人会去翻，
        于是默认的 ``rule_based`` 看起来就很像"它根本没连模型"。

        注意 ``effective_provider`` 与 ``configured_provider`` 的区别：前者是
        **在当前环境里真正会生效的**（缺密钥就降级），后者只是配置里写的。
        界面该显示前者，否则用户会以为自己在用大模型，其实一直在跑规则后端。
        """
        providers = llm_registry.list_providers()
        usable = {p["name"] for p in providers if p.get("available")}

        roles = []
        for item in svc.list_roles()["roles"]:
            configured = item.get("provider") or "rule_based"
            it_works = configured == "rule_based" or configured in usable
            roles.append({
                "role": item["id"],
                "name": item["name"],
                "configured_provider": configured,
                "model_id": item.get("model_id", ""),
                "effective_provider": configured if it_works else "rule_based",
                "degraded": not it_works,
                "reason": ("" if it_works else
                           f"provider='{configured}' 在当前环境不可用（缺密钥或未安装）"),
            })

        return {
            "providers": providers,
            # 给设置界面用的规格：默认端点、密钥环境变量、这家要不要密钥。
            # 有了它，界面才能把端点当"留空时的默认值"显示出来，而不是
            # 逼用户去查文档再手抄一遍。
            "provider_specs": discovery.describe_providers(),
            "roles": roles,
            "any_external": any(r["effective_provider"] != "rule_based" for r in roles),
            # 示例里刻意不写 "sk-xxx" 这种"像真密钥"的占位符：它和真密钥的
            # 形态一样，既容易被当成可用的值去试，也会让密钥扫描工具误报。
            "how_to_connect": [
                "界面（推荐）：右侧「AI 研判」页 →「设置…」→ 粘贴密钥 → 点「测试并获取模型」",
                "命令行：--agent --provider deepseek --api-key <你的密钥> --model deepseek-chat",
                "配置文件：改 python/finpulse_engine/agent/configs/<角色>.json 的 config.model",
                "环境变量：导出 DEEPSEEK_API_KEY / OPENAI_API_KEY / MOONSHOT_API_KEY 等",
                "本地模型：--provider ollama --base-url http://127.0.0.1:11434/v1",
            ],
        }

    @dispatcher.method("agent.llm.probe")
    def agent_llm_probe(api_key: str = "", provider: str = "",
                        base_url: str = "", timeout: float = 0.0) -> Dict[str, Any]:
        """用一把密钥问出「服务商是谁、有哪些可用模型」。

        **密钥只会发给 ``provider``/``base_url`` 指定的那一个端点。** 这里
        没有"依次试各家"的自动探测 —— 理由写在 ``llm/discovery.py`` 的文件头：
        拿用户的密钥去问无关的服务商，等于把它交给不该看到它的公司。

        两个用法：

        * ``provider`` 空 → 只做**本地**前缀推断，**不发任何请求**，返回
          ``needs_choice=True`` 与候选清单，请界面让用户点一下；
        * ``provider`` 给定 → 真的 ``GET {base}/v1/models``，返回模型列表。

        响应里永远不出现密钥本身，也不出现它的任何片段。
        """
        key = (api_key or "").strip()
        if not key and not base_url:
            raise BadParams(
                "需要 api_key；若是本地端点（Ollama / vLLM）不校验密钥，"
                "请同时给出 base_url。")

        name = (provider or "").strip().lower()
        if not name:
            guess = discovery.guess_provider(key)
            return {
                "ok": False,
                "needs_choice": True,
                "confident": guess["confident"],
                "provider": guess["provider"],
                "candidates": guess["candidates"],
                "note": guess["note"],
                "models": [],
                "total": 0,
                "filtered": 0,
                "error": "",
                "hint": "",
            }

        result = discovery.fetch_models(
            provider=name,
            api_key=key,
            base_url=base_url,
            timeout=timeout if timeout and timeout > 0 else discovery.DEFAULT_TIMEOUT,
        )
        result["needs_choice"] = False
        return result

    # ── 运行 ──────────────────────────────────────────────
    @dispatcher.method("agent.run")
    def agent_run(
        role: str = "",
        bars: Any = None,
        symbol: str = "",
        method: str = "ar",
        horizon: int = 5,
        folds: int = 5,
        min_train: int = 60,
        risk_free: float = 0.0,
        provider: str = "",
        model: str = "",
        base_url: str = "",
        api_key: str = "",
        tool_bridge: Optional[Dict[str, Any]] = None,
        include_text: bool = True,
    ) -> Dict[str, Any]:
        """单角色研判。

        ``provider`` / ``model`` / ``base_url`` / ``api_key`` 是**本次运行**的
        后端覆盖：填了就用填的，不填就照角色配置来。接入大模型不必改任何
        配置文件，也不必事先导出环境变量 —— 这四个参数就是那个入口。
        """
        if not role:
            raise BadParams("需要 role 参数")
        ctx = context_from(bars, symbol=symbol, method=method, horizon=horizon,
                           folds=folds, min_train=min_train, risk_free=risk_free,
                           tool_bridge=tool_bridge)
        return svc.run_role(role, ctx, override=_llm_override(provider, model, base_url, api_key),
                            include_text=bool(include_text))

    @dispatcher.method("agent.team")
    def agent_team(
        roles: Optional[List[str]] = None,
        bars: Any = None,
        symbol: str = "",
        method: str = "ar",
        horizon: int = 5,
        folds: int = 5,
        min_train: int = 60,
        risk_free: float = 0.0,
        provider: str = "",
        model: str = "",
        base_url: str = "",
        api_key: str = "",
        weights: Optional[Dict[str, float]] = None,
        tool_bridge: Optional[Dict[str, Any]] = None,
        include_text: bool = True,
    ) -> Dict[str, Any]:
        """多角色并行独立研判（互相看不到对方的结论）。

        后端覆盖参数同 :func:`agent_run`。
        """
        ctx = context_from(bars, symbol=symbol, method=method, horizon=horizon,
                           folds=folds, min_train=min_train, risk_free=risk_free,
                           tool_bridge=tool_bridge)
        return svc.run_team(list(roles or []), ctx, override=_llm_override(provider, model, base_url, api_key),
                            weights=weights, include_text=bool(include_text))

    @dispatcher.method("agent.debate")
    def agent_debate(
        bars: Any = None,
        panel: str = "",
        symbol: str = "",
        method: str = "ar",
        horizon: int = 5,
        folds: int = 5,
        min_train: int = 60,
        risk_free: float = 0.0,
        provider: str = "",
        model: str = "",
        base_url: str = "",
        api_key: str = "",
        rounds: int = 0,
        tool_bridge: Optional[Dict[str, Any]] = None,
        include_text: bool = True,
    ) -> Dict[str, Any]:
        """投委会：独立研判 → 交叉质证 → 主席综合。**这是智能体的主入口。**

        后端覆盖参数同 :func:`agent_run`。
        """
        ctx = context_from(bars, symbol=symbol, method=method, horizon=horizon,
                           folds=folds, min_train=min_train, risk_free=risk_free,
                           tool_bridge=tool_bridge)
        return svc.debate(ctx, panel=panel or None,
                          override=_llm_override(provider, model, base_url, api_key),
                          rounds=int(rounds) or None,
                          include_text=bool(include_text))

    @dispatcher.method("agent.chat")
    def agent_chat(
        messages: Optional[List[Dict[str, Any]]] = None,
        context: Optional[Dict[str, Any]] = None,
        role: str = "",
        provider: str = "",
        model: str = "",
        base_url: str = "",
        api_key: str = "",
        temperature: float = 0.3,
        max_tokens: int = 1200,
    ) -> Dict[str, Any]:
        """与终端对话。**这是"问答"，不是"出报告"** —— 区别很重要：

        ``agent.run`` / ``agent.team`` / ``agent.debate`` 是按契约产出结构化
        研判（段落必须齐、方向要从指定段抽取、护栏会检查）；这里不设契约，
        用户问什么就答什么，用自然语言。

        没连上大模型时**不假装回答**：返回 ``degraded=True`` 与一句能读懂
        的原因，让界面把"你还没接模型"直接说出来。规则后端填不出自由问答，
        它产出的模板和问题多半无关，那比承认没连更误导人。
        """
        raw_messages = list(messages or [])
        if not raw_messages:
            raise BadParams("messages 不能为空")
        truncated = len(raw_messages) > _CHAT_HISTORY_LIMIT
        history = raw_messages[-_CHAT_HISTORY_LIMIT:] if truncated else raw_messages

        turns: List[Message] = []
        for i, item in enumerate(history):
            if not isinstance(item, dict):
                raise BadParams(f"messages[{i}] 不是对象")
            who = str(item.get("role", "")).strip().lower()
            text = str(item.get("content", "")).strip()
            if who in ("system", "developer"):
                # 前端不能塞 system 提示词：那等于把"你是谁"交给调用方决定，
                # 而系统提示词里带着行情上下文与行为约束。
                raise BadParams(f"messages[{i}] 不接受 system 角色")
            if who not in ("user", "assistant"):
                raise BadParams(f"messages[{i}].role 必须是 user 或 assistant，得到 {who!r}")
            if not text:
                raise BadParams(f"messages[{i}].content 不能为空")
            turns.append(Message.user(text) if who == "user" else Message.assistant(text))
        if not any(m.role == "user" for m in turns):
            raise BadParams("对话里至少要有一条 user 消息")

        # 角色提示词：给了 role 就用那个角色的说明（人格一致），否则用通用分析师。
        persona = ""
        if role:
            try:
                persona = svc._role(role).instructions or ""
            except Exception:  # noqa: BLE001 —— 角色 id 写错不该让对话整个失败
                log.warning("对话请求的角色 %s 不存在，改用通用提示词", role)

        system_prompt = _chat_system_prompt(persona, context or {})

        cfg = ModelConfig(
            provider=provider or "rule_based",
            model_id=model,
            temperature=float(temperature),
            max_tokens=int(max_tokens),
            base_url=base_url,
        )
        backend, used, reason = llm_registry.build_chat(
            cfg, override=_llm_override(provider, model, base_url, api_key))

        if backend is None:
            return {
                "reply": (
                    "还没有连接大模型，所以我没法回答这个问题。\n\n"
                    f"原因：{reason}\n\n"
                    "接上之后这里就是真实模型的回答了。两种接法：\n"
                    "  1) 界面：「设置」里选 DeepSeek，填 API key（勾选记住可免每次重填）\n"
                    "  2) 命令行：finpulse-cli --agent --provider deepseek "
                    "--model deepseek-chat --api-key <你的密钥>"
                ),
                "provider": used,
                "degraded": True,
                "fallback_reason": reason,
                "truncated": truncated,
                "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            }

        replies = [Message.system(system_prompt)] + turns
        reply = backend.complete(replies, temperature=float(temperature),
                                 max_tokens=int(max_tokens))
        return {
            "reply": reply.text,
            "provider": used,
            "model": reply.model or cfg.model_id,
            "degraded": False,
            "fallback_reason": "",
            "finish_reason": reply.finish_reason,
            "truncated": truncated,
            "usage": reply.usage.to_json(),
        }

    # ── 回查 ──────────────────────────────────────────────
    @dispatcher.method("agent.trace")
    def agent_trace(run_id: str = "", include_events: bool = True,
                    include_result: bool = False) -> Dict[str, Any]:
        """回查一次运行的进度事件流（run_id 来自任何 agent.* 方法的返回）。"""
        if not run_id:
            raise BadParams("需要 run_id")
        rec = svc._run(run_id)
        return rec.to_json(include_events=bool(include_events),
                           include_result=bool(include_result))

    @dispatcher.method("agent.runs")
    def agent_runs(limit: int = 10) -> Dict[str, Any]:
        """最近几次运行。"""
        return svc.list_runs(int(limit))

    @dispatcher.method("agent.memory")
    def agent_memory(symbol: str = "", limit: int = 10) -> Dict[str, Any]:
        """历次决议记忆。用于看同一标的的方向是否稳定。"""
        return svc.recent_decisions(symbol or None, int(limit))

    @dispatcher.method("agent.consistency")
    def agent_consistency(symbol: str = "", roles: Optional[List[str]] = None) -> Dict[str, Any]:
        """各角色在最近若干次决议里的方向一致性。"""
        if not symbol:
            raise BadParams("需要 symbol")
        return svc.consistency(symbol, list(roles or []))

    @dispatcher.method("agent.reload")
    def agent_reload() -> Dict[str, Any]:
        """重新读盘加载角色/投委会配置（改完 JSON 不必重启引擎）。"""
        return svc.reload()

    @dispatcher.method("agent.stream.stats")
    def agent_stream_stats() -> Dict[str, Any]:
        """事件通道统计。推送数为 0 而壳说没收到事件时，先看这里。"""
        return svc.stream_stats()

    return svc
