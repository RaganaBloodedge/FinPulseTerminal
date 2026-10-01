"""角色运行时 —— 配置 → 可执行的分析角色。

这里包含本项目自己实现的 **tool-calling 循环**。因为不依赖 Agno 之类的框架，
循环的每一步都得自己负责，也因此每一步都能讲清楚：

    组装消息(系统提示词 + 任务) 
        ↓
    问后端 → 要工具？──是──→ 执行工具 → 把结果作为 tool 消息追加 ──┐
        │                                                            │
        否                                                           │
        ↓                                                            │
    拿到正文 ←───────────────────────────────────────────────────────┘
        ↓
    契约解析 → 护栏检查 → 记录结论 → 产出轨迹

三个刻意的约束：

* **轮数上限跟着工具预算走**。模型可能反复调同一个工具，所以要有硬上限；
  但这个上限**不能写死**——真实 LLM 常常一轮只调一个工具，写死成 4 会让
  它在取完数之前就把轮数花光，报告根本没机会写。见 :func:`turn_budget`。
* **工具异常不中断**。任何工具失败都转成结构化结果交给模型看，
  让它在报告里如实写"该数据不可用"——这比让整轮分析崩掉有用得多。
* **护栏在正文产出之后、返回之前**。问题项随结果一起返回，
  不阻塞生成（error 级由调用方决定是否采用），但一定留痕。
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from finpulse_engine.agent.config import RoleConfig
from finpulse_engine.agent.guardrails import GuardrailContext, Issue, Pipeline, summarize
from finpulse_engine.agent.llm import LlmError, Message, Usage
from finpulse_engine.agent.llm import registry as llm_registry
from finpulse_engine.agent.llm.override import LlmOverride
from finpulse_engine.agent.memory import Decision, DecisionMemory
from finpulse_engine.agent.schemas import OutputCheck, check_output
from finpulse_engine.agent.tools import ToolContext, ToolRegistry, serialize
from finpulse_engine.agent.tracing import Span, Tracer

#: 一次角色运行里"问后端"的绝对轮数上限。
#:
#: 真正的轮数由 :func:`turn_budget` 按角色的工具预算算出来，这里只是兜底。
TURN_HARD_CAP = 10


def turn_budget(max_tool_calls: int) -> int:
    """这个角色最多能"问后端"几轮。

    **这个数必须跟着工具预算走，不能写死。** 上一版是常量 4，理由是
    "2 轮是常态（取工具、出结论）"—— 那是照规则后端推的：它一轮就把
    所有工具都调完。真实 LLM 不这么做，它常常**一轮只调一个工具**。
    角色的工具预算是 6，于是它花掉 4 轮取数，轮到该写报告时轮数已经用完，
    收尾一句"（达到最大轮数仍未产出结论）"—— 而它自己的每一步都没错。
    现场表现是"委员集体弃权"，看不出是预算写错了。

    ``max_tool_calls + 2``：工具预算用完还留一轮写报告，再多一轮给
    "模型先回一句没有工具的话"这种情况垫底。
    """
    return min(TURN_HARD_CAP, max(2, int(max_tool_calls)) + 2)


#: 注入到提示词里的对等角色结论的最大长度。主席需要通读各人的意见来做
#: 综合，给它全文；四个人各 2000 字塞进去会挤爆上下文——截断，并明确
#: 标注已截断。
PEER_TEXT_LIMIT = 1600

#: **交叉质证**时注入的对等结论上限，比主席那份小得多。
#:
#: 两个理由，都不是省事：
#:
#: * 委员要做的是"回应别人的结论"，一份被截到 1600 字的完整报告只会
#:   换来一段同样长的回话 —— 那正是输出撑爆 ``max_tokens`` 被截断的由来；
#: * 这一段在每个委员的提示词里占最大头，而它每一轮、每一次调用都要重发。
#:
#: 900 字足够装下"方向 + 置信度 + 关键判据"，继续加长的边际信息很低。
CROSS_EXAM_PEER_LIMIT = 900

#: 每段的目标字数。乘上段落数就是写进提示词的全文预算。
SECTION_CHAR_BUDGET = 200


@dataclass
class ToolInvocation:
    """一次工具调用的记录。报告里引用的数字靠它追溯。"""

    name: str
    arguments: Dict[str, Any]
    ok: bool
    duration_ms: float
    #: 结果摘要（不存全量，避免轨迹膨胀）。
    digest: str = ""
    error: str = ""

    def to_json(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "name": self.name, "ok": self.ok,
            "duration_ms": round(self.duration_ms, 3),
        }
        if self.arguments:
            d["arguments"] = self.arguments
        if self.digest:
            d["digest"] = self.digest
        if self.error:
            d["error"] = self.error
        return d


@dataclass
class RoleRun:
    """一个角色一次完整运行的产物。"""

    role_id: str
    role_name: str
    category: str
    text: str = ""
    check: Optional[OutputCheck] = None
    issues: List[Issue] = field(default_factory=list)
    direction: Optional[str] = None
    confidence: Optional[str] = None
    #: 该角色在本次会议里的计票权重，由编排层按 panel 配置写入。
    #: 这是对 Fincept 的一处刻意改进：它的 IC 成员配了 weight 却从未使用
    #: （`_get_agent_opinion` 收了参数但不读），计票是等权的。
    weight: float = 1.0
    #: 本次结论是第几轮产出（1 = 独立研判，2 = 交叉质证后）。
    round_index: int = 1
    invocations: List[ToolInvocation] = field(default_factory=list)
    tool_results: Dict[str, Any] = field(default_factory=dict)
    provider: str = ""
    fallback_reason: Optional[str] = None
    usage: Usage = field(default_factory=Usage)
    duration_ms: float = 0.0
    turns: int = 0
    error: Optional[str] = None
    #: 正文是被 max_tokens 截断的（``finish_reason == "length"``）。
    #: **这不是错误**，只是一条必须留痕的事实：它决定了
    #: "这份报告的末段可能不完整"该由谁负责（见 guardrails.OutputTruncated）。
    truncated: bool = False
    trace: Optional[Span] = None

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.text.strip())

    def to_json(self, *, include_text: bool = True) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "role_id": self.role_id,
            "role_name": self.role_name,
            "category": self.category,
            "direction": self.direction,
            "confidence": self.confidence,
            "weight": self.weight,
            "round": self.round_index,
            "provider": self.provider,
            "duration_ms": round(self.duration_ms, 3),
            "turns": self.turns,
            "tool_calls": [i.to_json() for i in self.invocations],
            "guardrails": summarize(self.issues),
            "ok": self.ok,
        }
        if self.fallback_reason:
            d["fallback_reason"] = self.fallback_reason
        if self.check:
            d["output_check"] = self.check.to_json()
        if self.usage.total_tokens:
            d["usage"] = self.usage.to_json()
        if self.error:
            d["error"] = self.error
        if self.truncated:
            d["truncated"] = True
        if include_text:
            d["text"] = self.text
        return d


# ── 提示词组装 ────────────────────────────────────────────────────


def describe_intent(ctx: ToolContext) -> str:
    """这次研判到底在**议什么** —— 运行参数就是议题。

    报告头部一直只有「决议 / 置信度 / 告警」，读者看不到这场会是被什么问题
    召集的。而运行参数就是那个问题：同一组均线，套在 250 根日线上和 60 根
    30 分钟线上是两个结论。只给数字不给口径，读者无法判断该不该采信。

    **措辞只在这里写一份**：提示词里给模型看的、报告头部给用户看的，必须
    是同一句话。两处各写一遍，迟早会说成两件事 —— 而"模型被告知的"和
    "用户以为它被问的"不一致，是这套系统里最难查的一类 bug。
    """
    bits = [f"标的 {ctx.symbol or '未命名'}"]
    sample = f"样本 {ctx.bars} 根 K 线"
    if ctx.timestamps:
        sample += f"，截至 {ctx.as_of()}"
    bits.append(sample)
    bits.append(f"预测方法 {ctx.forecast_method}、步长 {ctx.horizon}、"
                f"回测 {ctx.backtest_folds} 折、最小训练 {ctx.backtest_min_train}")
    return "；".join(bits)


def build_task_prompt(
    role: RoleConfig,
    ctx: ToolContext,
    *,
    peers: Optional[Sequence["RoleRun"]] = None,
    instruction: str = "",
) -> str:
    """拼出这一轮的"任务"消息。

    刻意把这次运行的**参数**写清楚（标的、样本量、截至日期、预测设置），
    因为工具返回的是"当前状态"，模型需要知道这个状态对应什么设置。
    少了这几行，模型会去猜样本量，然后写出"基于约 200 根 K 线"这种
    看起来合理但毫无依据的话。

    参数那一行复用 :func:`describe_intent`，与报告头部同源。
    """
    n_sections = max(1, len(role.output_sections))
    lines = [
        "本次分析的运行参数（议题）：",
        f"- {describe_intent(ctx)}",
        f"- 你的角色：{role.name}（{role.description}）",
        f"- 必须输出的段落：{'、'.join(role.output_sections)}",
        # 输出预算是**硬提示**，不是客套。正文一旦撞上 max_tokens 被截断，
        # 末段就没了 —— 那份报告直接作废，整场研判还要重跑一遍。所以
        # "写多长"这件事必须在提示词里说清楚，而不是留给模型自己把握。
        f"- 输出预算：全文 {n_sections * SECTION_CHAR_BUDGET} 字以内"
        f"（共 {n_sections} 段，每段 2-4 句）。写满不等于写透，"
        "超出预算会被截断，反而丢掉最后一段。",
    ]
    if instruction:
        lines.append("")
        lines.append(instruction)

    if peers:
        # 主席要通读全文做综合；委员只需要回应结论 —— 后者给全文，
        # 换来的只会是一段同样长的回话。理由见 CROSS_EXAM_PEER_LIMIT。
        limit = PEER_TEXT_LIMIT if role.category == "aggregate" else CROSS_EXAM_PEER_LIMIT
        lines.append("")
        lines.append("以下是其它分析师本轮已给出的独立结论。"
                     "请在你的职责范围内对其作出回应（质疑、补充或指出无法反驳）。"
                     "回应要简短，不要复述原文：")
        for peer in peers:
            body = peer.text.strip()
            if len(body) > limit:
                body = body[:limit] + "\n……（原文过长已截断）"
            lines.append("")
            lines.append(f"### {peer.role_name}（{peer.direction or '未给方向'} / "
                         f"{peer.confidence or '未标置信度'}）")
            lines.append(body)

    lines.append("")
    lines.append("请先调用你需要的工具取证，再按上面要求的段落输出结论。"
                 "一次回复里可以同时调用多个工具，不必一轮只调一个；"
                 "工具没有提供的数据，写「数据未提供」，不要估算。")
    return "\n".join(lines)


# ── 运行时 ────────────────────────────────────────────────────────


class RoleRuntime:
    """把一份 :class:`RoleConfig` 变成可执行的角色。"""

    def __init__(
        self,
        config: RoleConfig,
        tools: ToolRegistry,
        *,
        guards: Optional[Pipeline] = None,
        memory: Optional[DecisionMemory] = None,
    ) -> None:
        self.config = config
        self.tools = tools
        self.guards = guards or Pipeline()
        self.memory = memory

    # ── 主流程 ───────────────────────────────────────────────
    def run(
        self,
        ctx: ToolContext,
        *,
        peers: Optional[Sequence[RoleRun]] = None,
        instruction: str = "",
        override: Optional[LlmOverride] = None,
        tracer: Optional[Tracer] = None,
    ) -> RoleRun:
        t0 = time.perf_counter()
        run = RoleRun(
            role_id=self.config.id,
            role_name=self.config.name,
            category=self.config.category,
        )
        tracer = tracer or Tracer(self.config.id)
        unknown = self.tools.missing(self.config.tools)
        # 把各工具的固定参数交给后端。规则后端"不做推理"，靠这份默认值
        # 决定每个工具用什么参数调——工具自己声明 defaults，比在后端里
        # 硬编码参数更符合"配置即接口"。
        tool_defaults = {
            name: dict(self.tools.get(name).defaults)
            for name in self.config.tools
            if self.tools.get(name) is not None
        }

        override = override or LlmOverride()
        with tracer.span(self.config.id, kind="role",
                         provider=override.provider or self.config.model.provider) as span:
            run.trace = span

            # 工具目录用**注册表原名**。改名（``terminal.live_quote`` →
            # ``terminal_live_quote``）是传输层的事：只有"要 POST 出去"
            # 这一个场景下点号才非法，规则后端拿到原名才好按名字查参数。
            # 见 tools 的"线上名"一节。
            catalog = self.tools.catalog(self.config.tools)
            provider, used, fallback = llm_registry.build(
                self.config.model,
                override=override,
                role_id=self.config.id,
                sections=self.config.output_sections,
                category=self.config.category,
                tool_order=list(self.config.tools),
                tool_defaults=tool_defaults,
                peers=[p.to_json(include_text=False) | {"name": p.role_name,
                                                        "role_id": p.role_id}
                        for p in (peers or [])],
            )
            run.provider = used
            run.fallback_reason = fallback
            span.attrs["resolved_provider"] = used
            if fallback:
                span.attrs["fallback"] = fallback
            if unknown:
                # 配置里写了不存在的工具名：不静默丢弃，记进 span 供排查。
                span.attrs["unknown_tools"] = unknown

            # 任务提示词要先算出来、单独留一份：数字溯源要按"模型读过什么"
            # 判，这份文本里的运行参数与其它角色的结论都算合法出处。
            task_prompt = build_task_prompt(self.config, ctx, peers=peers,
                                            instruction=instruction)
            messages: List[Message] = [
                Message.system(self.config.instructions),
                Message.user(task_prompt),
            ]

            try:
                self._loop(provider, messages, catalog, ctx, run, tracer)
            except LlmError as exc:
                # 后端失败是可预期的（没 key、网络断、限流）。记成错误结果返回，
                # 让上层决定是重试、换后端还是跳过这个角色——而不是抛穿整场辩论。
                run.error = f"后端调用失败：{exc}"
                span.error = run.error

            with tracer.span("guardrails", kind="check"):
                self._check(run, ctx, prompt_text=task_prompt)

        run.duration_ms = (time.perf_counter() - t0) * 1000.0
        # 把 span 自身的耗时也补上（它在 with 块里被 finally 赋值）。
        if run.trace is not None and run.trace.duration_ms == 0.0:
            run.trace.duration_ms = run.duration_ms

        self._remember(ctx, run)
        return run

    # ── 循环 ─────────────────────────────────────────────────
    def _loop(
        self,
        provider: Any,
        messages: List[Message],
        catalog: List[Dict[str, Any]],
        ctx: ToolContext,
        run: RoleRun,
        tracer: Tracer,
    ) -> None:
        budget = turn_budget(self.config.max_tool_calls)
        max_calls = int(self.config.max_tool_calls)
        # 工具预算用完之后仍然把工具目录递过去，模型只会继续要工具 ——
        # 而每一轮都是一次真实的计费调用。所以那一轮改为明确告诉它
        # "预算用完了，现在写报告"，并且**真的把工具撤下来**：模型不会
        # 去点一个它没被告知的工具。（规则后端也吃这条消息，它本来就
        # 忽略 user 消息，工具撤下来之后正好进入渲染分支。）
        exhausted_notice = False

        for turn in range(1, budget + 1):
            run.turns = turn

            if not exhausted_notice and len(run.invocations) >= max_calls:
                exhausted_notice = True
                messages.append(Message.user(
                    f"（工具预算已用完：你已调用 {len(run.invocations)} 次，上限 {max_calls} 次。"
                    "请立即基于已经拿到的工具输出，按要求输出剩余段落并收尾，"
                    "不要再调用任何工具。）"
                ))

            with tracer.span(f"backend.turn{turn}", kind="backend",
                             provider=run.provider) as bspan:
                response = provider.complete(
                    messages,
                    # 每一轮都把工具目录递过去：真实 LLM 可能"取一次不够、
                    # 想再补一个工具"，而预算由 max_tool_calls 护栏兜底。
                    tools=None if exhausted_notice else (catalog or None),
                    temperature=self.config.model.temperature,
                    max_tokens=self.config.model.max_tokens,
                )
                bspan.attrs["finish"] = response.finish_reason
                bspan.attrs["tool_calls"] = len(response.tool_calls)
                if response.usage.total_tokens:
                    bspan.attrs["tokens"] = response.usage.total_tokens

            run.usage = Usage(
                prompt_tokens=run.usage.prompt_tokens + response.usage.prompt_tokens,
                completion_tokens=run.usage.completion_tokens + response.usage.completion_tokens,
            )

            if not response.tool_calls:
                run.text = response.text
                if response.finish_reason == "length":
                    # 被 max_tokens 截断。**这里不判死刑**：截断只说明
                    # "没写完"，不说明"不能用"。五段齐全、只是最后一句被
                    # 切掉的报告照样可读可计票；真正缺段的情况由
                    # SectionCompleteness 护栏报 error —— 那是它该管的。
                    #
                    # 上一版在这里直接设 run.error，等于把"正好写满"升级成
                    # "整场作废"：一场投委会因此三个委员同时出局。它同时
                    # 掩盖了真正的线索 —— 报告里只说了一句后端截断，
                    # 完全看不出该调的是 max_tokens 还是提示词预算。
                    run.truncated = True
                return

            # 记下模型这次要调什么，然后真的去调。
            messages.append(Message(role="assistant", content=response.text or "",
                                    tool_calls=response.tool_calls))
            for call in response.tool_calls:
                t_call = time.perf_counter()
                result = self.tools.call(call.name, call.arguments, ctx)
                elapsed = (time.perf_counter() - t_call) * 1000.0
                ok = bool(result.get("available", True)) and "error" not in result
                inv = ToolInvocation(
                    name=call.name,
                    arguments=call.arguments,
                    ok=ok,
                    duration_ms=elapsed,
                    error=str(result.get("error") or result.get("reason") or ""),
                )
                inv.digest = _digest(result)
                run.invocations.append(inv)
                if ok:
                    run.tool_results[call.name] = result
                messages.append(Message.tool_result(call.id, call.name, serialize(result)))

        # 轮数用尽仍要工具：用现有内容收尾，并记录这一事实。
        # 走到这里说明"撤下工具 + 明说写报告"都没能让它收尾（例如它一直在
        # 报同一个不存在的工具名）。这是真失败，判 error 是对的。
        run.text = run.text or "（达到最大轮数仍未产出结论）"
        run.error = (run.error or "") + \
            f"（达到最大轮数 {budget}，可能存在工具循环）"

    # ── 契约与护栏 ───────────────────────────────────────────
    def _check(self, run: RoleRun, ctx: ToolContext, *, prompt_text: str = "") -> None:
        # direction_scope 只含"本角色自己的结论段"。传全部 output_sections 会
        # 把引述别人的方向（风险官「反对意见」段）当成它自己的立场——第二轮
        # 交叉质证后尤其明显，那时它真的在引用别人的结论。
        run.check = check_output(
            run.text,
            self.config.output_sections,
            direction_sections=self.config.direction_scope,
        )
        run.direction = run.check.direction
        run.confidence = run.check.confidence

        gctx = GuardrailContext(
            role=self.config,
            text=run.text,
            check=run.check,
            tool_results=run.tool_results,
            tool_calls=[i.name for i in run.invocations],
            # 数字溯源的池子 = 自己的工具输出 ∪ 这份提示词。主席被要求
            # "引用各分析师的判据并交叉核对"，它引用的数字来自别人的工具，
            # 不是它自己调的那些 —— 池子里不装提示词就必然全判成"无法溯源"。
            prompt_text=prompt_text,
            provider=run.provider,
            fallback_reason=run.fallback_reason,
            truncated=run.truncated,
            turn=run.turns,
        )
        run.issues = self.guards.run(gctx)

    # ── 记忆 ─────────────────────────────────────────────────
    def _remember(self, ctx: ToolContext, run: RoleRun) -> None:
        if self.memory is None or not run.text:
            return
        skill = run.tool_results.get("backtest", {}).get("skill") \
            if isinstance(run.tool_results.get("backtest"), dict) else None
        vol = run.tool_results.get("stats", {}).get("ann_vol_pct") \
            if isinstance(run.tool_results.get("stats"), dict) else None
        self.memory.record(Decision(
            ts_ms=int(time.time() * 1000),
            symbol=ctx.symbol,
            role_id=run.role_id,
            role_name=run.role_name,
            direction=run.direction,
            confidence=run.confidence,
            skill=skill if isinstance(skill, (int, float)) else None,
            ann_vol_pct=vol if isinstance(vol, (int, float)) else None,
            provider=run.provider,
            digest=_first_line(run.text),
        ))


def _digest(result: Dict[str, Any], limit: int = 160) -> str:
    """工具结果的一句话摘要，用于轨迹。"""
    if "error" in result:
        return f"失败：{result['error']}"[:limit]
    if result.get("available") is False:
        return f"不可用：{result.get('reason', '未说明')}"[:limit]
    keys = [k for k in result if k not in ("available", "tool")]
    return "字段：" + "、".join(keys[:10]) + ("…" if len(keys) > 10 else "")


def _first_line(text: str, limit: int = 120) -> str:
    for line in (text or "").splitlines():
        line = line.strip().lstrip("#").strip()
        if line and len(line) > 4:
            return line[:limit]
    return ""
