"""编排层 —— 把角色配置组织成一场会议。

三种编排，按复杂度递增：

    run_role()   一个角色独立研判
    run_team()   多个角色并行独立研判（互不可见）
    debate()     投委会：独立研判 → 交叉质证 → 主席综合

## 为什么要单独一层

:class:`~finpulse_engine.agent.roles.RoleRuntime` 只负责"一个角色怎么跑"。
谁先跑、谁看得到谁的结论、票怎么算、主席在什么情况下应该拒绝出结论——
这些是**会议层面的决策**，混进角色运行时会让它同时承担两件事。

## 与 Fincept 的两处刻意偏离

读它的 ``reasoning/ic_deliberation.py`` 之后确认：那个"投委会"实际只做到
**各成员拿到同一份 prompt、彼此独立作答、然后等权计票**。没有交叉质证，
也没有用到它自己配置的成员权重。所以这里的实现是**有意做得比它完整**：

1. **交叉质证真的发生**。第二轮把各委员第一轮的结论互相可见地交付回去
   （``RoleRuntime.run(peers=...)``），让它们在自己的职责范围内作出回应。
   第一轮与第二轮的方向若不同，会被显式记录下来——这是"质证起没起作用"
   的证据。
2. **权重要真的参与计票**（见 ``rule_based._peer_weights``）。

保留的部分是它的核心思路：角色声明化、结论结构化、程序只读声明过的段落。

## 一个不做的取舍

不做"多轮直到收敛"。收敛条件在 LLM 场景下很难给出可靠定义，而无限轮次
会把延迟和成本变成不可预测的量。两轮（独立 + 质证）+ 主席综合是一个
**可解释的有限过程**——每一轮为什么存在都能说清楚。
"""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

from finpulse_engine.agent.config import (
    PANEL_ERROR,
    ConfigError,
    PanelConfig,
    RoleConfig,
    load_all,
    load_panels,
    validate_panel,
)
from finpulse_engine.agent.guardrails import Issue, Pipeline, summarize
from finpulse_engine.agent.llm.override import LlmOverride
from finpulse_engine.agent.memory import DecisionMemory
from finpulse_engine.agent.roles import RoleRun, RoleRuntime, describe_intent
from finpulse_engine.agent.tools import ToolContext, ToolRegistry, default_registry
from finpulse_engine.agent.tracing import Tracer

#: 并行委员数上限。委员之间互相不可见，所以并发跑是安全的；
#: 上限存在的理由是别把上游 API 打限流。
MAX_PARALLEL = 4

log = logging.getLogger("finpulse.agent.orchestrator")

#: 进度回调：``(事件名, 数据)``。
#:
#: 刻意用**回调**而不是直接依赖 :mod:`finpulse_engine.stream`：编排层不该
#: 知道事件最终是写 stdout、进总线还是什么都不做。它只负责"发生了什么"。
ProgressHook = Callable[[str, Dict[str, Any]], None]


def emit_progress(hook: Optional[ProgressHook], event: str,
                  **data: Any) -> None:
    """推一条进度。**回调抛异常绝不能打断研判。**

    进度是给界面看的东西。它坏了顶多是界面少一个转圈动画，而让一个已经
    算了 8 秒的研判因为"推送失败"整个丢掉，是把装饰当成了正事。
    """
    if hook is None:
        return
    try:
        hook(event, data)
    except Exception as exc:  # noqa: BLE001 — 见上面
        log.warning("进度回调在处理 %s 时失败（已忽略）: %s", event, exc)


class OrchestrationError(RuntimeError):
    """编排本身不成立（配置错、票数不足等）。"""


def is_effective(run: RoleRun) -> bool:
    """这个委员的结论算不算"有效表态"。

    比 ``run.ok`` 严格得多，而 quorum 必须建在这里。原因：

    ``run.ok`` 只表示"产出了非空文本"。一个**什么工具数据都没拿到**的角色
    照样会产出文本——通篇写"工具未返回…，无法判断"。它没有失败，但也没有
    形成任何判断。如果 quorum 建在 ``ok`` 上，那么"整台终端的数据源全挂了"
    这种情况会以 quorum 通过、主席基于一堆"数据未提供"给出方向收场——
    正是 :meth:`Orchestrator.debate` 声称要防的那件事。

    所以有效的定义是三条同时成立：
    1. 产出了正文（``ok``）
    2. **至少有一个工具真的返回了可用结果**（有证据）
    3. 护栏没有 error 级问题（这份报告可用）
    """
    if not run.ok:
        return False
    if not run.tool_results:
        return False
    return not Pipeline.has_errors(run.issues)


# ── 结果对象 ──────────────────────────────────────────────────────


@dataclass
class RoundResult:
    """一轮里各委员的结论。"""

    index: int
    runs: Dict[str, RoleRun] = field(default_factory=dict)
    #: 本轮相对上一轮改变过方向/置信度的委员。
    changed: List[str] = field(default_factory=list)
    #: 本轮没产出可用结论、因而**沿用了上一轮立场**的委员。
    #: 单列出来是因为"沿用"必须可见：一份第二轮截断的报告如果悄悄被
    #: 第一轮的结论顶替，读报告的人会以为那三位真的质证过了。
    carried: List[str] = field(default_factory=list)
    #: ``{role_id: 为什么本轮不用它的结论}``，跟着 carried 一起讲清楚。
    carry_reason: Dict[str, str] = field(default_factory=dict)
    duration_ms: float = 0.0

    @property
    def ok_runs(self) -> List[RoleRun]:
        """产出了正文的委员。注意这不等于"形成了有效判断"——见 is_effective。"""
        return [r for r in self.runs.values() if r.ok]

    @property
    def effective_runs(self) -> List[RoleRun]:
        """真正形成了有效判断的委员（有正文 + 有工具证据 + 护栏无 error）。"""
        return [r for r in self.runs.values() if is_effective(r)]

    def directions(self) -> Dict[str, Optional[str]]:
        return {rid: run.direction for rid, run in self.runs.items()}

    def to_json(self, *, include_text: bool = True) -> Dict[str, Any]:
        return {
            "round": self.index,
            "duration_ms": round(self.duration_ms, 3),
            "changed": list(self.changed),
            # 沿用上一轮的委员。界面不一定要显示，但**必须**能被查到 ——
            # "这一轮到底谁真的表了态"是复盘一场会议时第一个要问的问题。
            "carried": list(self.carried),
            "carry_reason": dict(self.carry_reason),
            # 「形成了有效判断」的判定规则**只在这里实现一份**。壳侧再算一遍
            # 必然会和这里漂移（少判一个条件、多算一个字段），然后两边对
            # "这一轮算不算够人数"给出不同答案。
            "effective": sorted(r.role_id for r in self.effective_runs),
            "ineffective": sorted(rid for rid, r in self.runs.items()
                                  if not is_effective(r)),
            "members": {rid: run.to_json(include_text=include_text)
                        for rid, run in self.runs.items()},
        }


@dataclass
class TeamResult:
    """一次并行独立研判的结果（没有主席）。"""

    panel_name: str
    runs: Dict[str, RoleRun] = field(default_factory=dict)
    duration_ms: float = 0.0
    issues: List[Issue] = field(default_factory=list)

    @property
    def directions(self) -> Dict[str, Optional[str]]:
        return {rid: run.direction for rid, run in self.runs.items()}

    def to_json(self, *, include_text: bool = True) -> Dict[str, Any]:
        effective = sorted(r.role_id for r in self.runs.values() if is_effective(r))
        return {
            "panel": self.panel_name,
            "duration_ms": round(self.duration_ms, 3),
            # 团队没有主席，所以"有效"的定义就是"至少有一位委员形成了
            # 有效判断"。全员拿不到数据时把它报成有效，等于给了用户一个
            # 看起来像结论的东西 —— 和投委会 quorum 不足同一个道理。
            "valid": bool(effective),
            "effective": effective,
            "ineffective": sorted(rid for rid, r in self.runs.items()
                                  if not is_effective(r)),
            "direction": None,
            "members": {rid: run.to_json(include_text=include_text)
                        for rid, run in self.runs.items()},
            "guardrails": summarize(self.issues),
        }


@dataclass
class DebateResult:
    """一场投委会的完整产物。"""

    panel: PanelConfig
    rounds: List[RoundResult] = field(default_factory=list)
    chair: Optional[RoleRun] = None
    #: 这场会在**议什么** —— 本次运行的参数（标的 / 样本区间 / 预测设置），
    #: 由 :func:`describe_intent` 生成，与喂给模型的提示词同源。
    #:
    #: 单列出来是因为报告头部此前只有「决议 / 置信度 / 问题」，读者看不到
    #: 结论是在什么口径下得出的。这个字段不参与任何计算，只负责回答
    #: "它到底被问的是什么" —— 少了它，一份正确的报告也无法被复核。
    intent: str = ""
    #: 会议是否有效。委员成功数达不到 quorum 时为 False，此时不产出决议。
    valid: bool = False
    #: 会议为什么无效 / 有什么系统性问题。每项是 ``{level, message}``，
    #: level 取 ``error`` / ``warning``。
    problems: List[Dict[str, str]] = field(default_factory=list)
    issues: List[Issue] = field(default_factory=list)
    duration_ms: float = 0.0

    @property
    def final_round(self) -> Optional[RoundResult]:
        return self.rounds[-1] if self.rounds else None

    @property
    def direction(self) -> Optional[str]:
        return self.chair.direction if self.chair else None

    @property
    def confidence(self) -> Optional[str]:
        return self.chair.confidence if self.chair else None

    def flips(self) -> Dict[str, tuple]:
        """质证改变了哪些委员的方向。``{role_id: (轮1方向, 轮2方向)}``"""
        if len(self.rounds) < 2:
            return {}
        first, last = self.rounds[0], self.rounds[-1]
        out: Dict[str, tuple] = {}
        for rid, run in last.runs.items():
            before = first.runs.get(rid)
            if before is None:
                continue
            if before.direction != run.direction:
                out[rid] = (before.direction, run.direction)
        return out

    def to_json(self, *, include_text: bool = True) -> Dict[str, Any]:
        d: Dict[str, Any] = {
            "panel": self.panel.to_json(),
            "valid": self.valid,
            "intent": self.intent,
            "problems": list(self.problems),
            "direction": self.direction,
            "confidence": self.confidence,
            "duration_ms": round(self.duration_ms, 3),
            "rounds": [r.to_json(include_text=include_text) for r in self.rounds],
            "flips": {k: list(v) for k, v in self.flips().items()},
            "guardrails": summarize(self.issues),
        }
        if self.chair:
            d["chair"] = self.chair.to_json(include_text=include_text)
        return d


# ── 编排器 ────────────────────────────────────────────────────────


class Orchestrator:
    """把角色配置组织成会议。

    ``roles`` 与 ``panels`` 都在构造时加载一次：它们在一次会话里不会变，
    每个请求都重新读盘既慢又会让"两次运行用了不同配置"变得可能。
    """

    def __init__(
        self,
        roles: Optional[Dict[str, RoleConfig]] = None,
        panels: Optional[Dict[str, PanelConfig]] = None,
        tools: Optional[ToolRegistry] = None,
        *,
        guards: Optional[Pipeline] = None,
        memory: Optional[DecisionMemory] = None,
        max_parallel: int = MAX_PARALLEL,
    ) -> None:
        self.roles = roles if roles is not None else load_all()
        self.panels = panels if panels is not None else load_panels()
        self.tools = tools if tools is not None else default_registry()
        self.guards = guards if guards is not None else Pipeline()
        self.memory = memory
        self.max_parallel = max(1, max_parallel)

    # ── 单角色 ───────────────────────────────────────────────
    def run_role(
        self,
        role_id: str,
        ctx: ToolContext,
        *,
        override: Optional[LlmOverride] = None,
        progress: Optional[ProgressHook] = None,
    ) -> RoleRun:
        role = self._role(role_id)
        emit_progress(progress, "role.start", role=role.id, role_name=role.name)
        runtime = RoleRuntime(role, self.tools, guards=self.guards, memory=self.memory)
        run = runtime.run(ctx, override=override)
        emit_progress(progress, "role.done", role=role.id, role_name=role.name,
                      ok=run.ok, direction=run.direction,
                      confidence=run.confidence, duration_ms=run.duration_ms)
        return run

    # ── 并行团队 ─────────────────────────────────────────────
    def run_team(
        self,
        role_ids: Sequence[str],
        ctx: ToolContext,
        *,
        panel_name: str = "自定义组合",
        override: Optional[LlmOverride] = None,
        weights: Optional[Dict[str, float]] = None,
        progress: Optional[ProgressHook] = None,
    ) -> TeamResult:
        """多个角色**并行**独立研判。互相看不到对方的结论。

        顺序重要吗？不重要——每个角色读的是同一份工具输出，彼此不通信。
        所以用线程池并发，把 N 个角色的耗时从"相加"变成"取最大"。
        """
        t0 = time.perf_counter()
        result = TeamResult(panel_name=panel_name)

        # 先做一遍配置校验，避免跑到一半才发现角色名写错。
        roles = [self._role(rid) for rid in role_ids]
        for role in roles:
            missing = self.tools.missing(role.tools)
            if missing:
                result.issues.append(Issue(
                    "tool_missing", "warning",
                    f"角色 {role.id} 声明了未注册的工具",
                    f"缺失：{missing}；已注册：{self.tools.names()}",
                ))

        emit_progress(progress, "team.start", count=len(roles),
                      roles=[r.id for r in roles])

        def _one(role: RoleConfig) -> RoleRun:
            emit_progress(progress, "role.start", role=role.id, role_name=role.name)
            runtime = RoleRuntime(role, self.tools, guards=self.guards,
                                  memory=self.memory)
            run = runtime.run(ctx, override=override)
            if weights and role.id in weights:
                run.weight = weights[role.id]
            emit_progress(progress, "role.done", role=role.id, role_name=role.name,
                          ok=run.ok, direction=run.direction,
                          confidence=run.confidence, duration_ms=run.duration_ms)
            return run

        workers = min(self.max_parallel, max(1, len(roles)))
        with ThreadPoolExecutor(max_workers=workers,
                                thread_name_prefix="fp-agent") as pool:
            for run in pool.map(_one, roles):
                result.runs[run.role_id] = run

        result.duration_ms = (time.perf_counter() - t0) * 1000.0
        emit_progress(progress, "team.done", duration_ms=result.duration_ms,
                      directions=result.directions)
        return result

    # ── 投委会 ───────────────────────────────────────────────
    def debate(
        self,
        ctx: ToolContext,
        *,
        panel: Optional[str | PanelConfig] = None,
        override: Optional[LlmOverride] = None,
        rounds: Optional[int] = None,
        progress: Optional[ProgressHook] = None,
    ) -> DebateResult:
        """开一场投委会。

        流程：
            第一轮  各委员独立研判（并行），互不可见
            第二轮  把第一轮结论互相交付，各委员在自己的职责范围内回应
            主席    综合最终一轮的结论，显式记录分歧

        委员成功数低于 ``panel.quorum`` 时 **不产出决议**：全员失败的会议
        如果还给出一个方向，那是把系统性故障伪装成决策意见。

        **但"失败"只算它自己那一轮。** 后一轮没跑出可用结论的委员，如果
        前一轮的结论仍然成立，就沿用前者——理由见 :meth:`_one_round`。
        否则一次 max_tokens 截断就能把整场会追溯地否掉，而两轮之间并没有
        出现任何新事实。沿用会在 ``problems`` 里显式写出来。
        """
        p = self._panel(panel)
        problems = validate_panel(p, self.roles)
        # 角色不存在是硬错误（跑不起来）；职责搭配不当只是告警，
        # 会跟着结果一起返回，让使用者自己判断要不要在意。
        fatal = [msg for level, msg in problems if level == PANEL_ERROR]
        if fatal:
            raise ConfigError(f"投委会 '{p.id}' 配置无效：" + "；".join(fatal))

        t0 = time.perf_counter()
        result = DebateResult(panel=p, intent=describe_intent(ctx), problems=[
            {"level": level, "message": msg} for level, msg in problems
        ])

        total_rounds = rounds if rounds is not None else p.rounds
        weights = {m.role_id: m.weight for m in p.members}

        emit_progress(progress, "debate.start", panel=p.id, panel_name=p.name,
                      rounds=total_rounds, chair=p.chair,
                      members=[m.role_id for m in p.members], quorum=p.quorum)

        # ── 第一轮：独立研判 ──
        first = self._one_round(1, p, ctx, peers=None, override=override,
                                weights=weights, cross_only=False, progress=progress)
        result.rounds.append(first)
        emit_progress(progress, "round.done", round=1, changed=[],
                      directions=first.directions(),
                      effective=len(first.effective_runs), quorum=p.quorum,
                      duration_ms=first.duration_ms)

        # ── 第二轮：交叉质证 ──
        # 只有声明了 cross_examine 的委员参与。参与者的 peers 是**别人**的
        # 第一轮结论——把委员自己的结论再喂回去没有意义。
        for idx in range(2, total_rounds + 1):
            prev = result.rounds[-1]
            nxt = self._one_round(
                idx, p, ctx,
                peers=prev, override=override,
                weights=weights, cross_only=True, progress=progress,
            )
            nxt.changed = _diff(prev, nxt)
            result.rounds.append(nxt)
            emit_progress(progress, "round.done", round=idx, changed=list(nxt.changed),
                          directions=nxt.directions(),
                          effective=len(nxt.effective_runs), quorum=p.quorum,
                          carried=list(nxt.carried),
                          duration_ms=nxt.duration_ms)
            # 沿用必须留痕。一份"第二轮没跑出来、拿第一轮结论顶上"的会议
            # 记录，如果不说清楚，读起来就像三位委员真的质证过了。
            if nxt.carried:
                result.problems.append({"level": "warning", "message": (
                    f"第 {idx} 轮以下委员未产出可用结论，已沿用其第 "
                    f"{prev.index} 轮立场："
                    + "；".join(f"{rid}（{nxt.carry_reason.get(rid, '')}）"
                                for rid in sorted(nxt.carried))
                )})

        final = result.rounds[-1]
        ok = final.effective_runs
        ineffective = [rid for rid, r in final.runs.items() if not is_effective(r)]

        # ── quorum 校验 ──
        if len(ok) < p.quorum:
            result.valid = False
            reasons = []
            for rid, run in final.runs.items():
                if is_effective(run):
                    continue
                # 原因必须具体到可操作：是截断、缺段，还是根本没取到数据，
                # 该改的东西完全不同。判据只在这里实现一份（why_unusable），
                # 免得"运行告警"和"沿用理由"两处说法漂移。
                why = (f"未产出结论（{run.error or '正文为空'}）"
                       if not run.ok else why_unusable(run))
                reasons.append(f"{rid}：{why}")
            result.problems.append({"level": "error", "message": (
                f"有效委员 {len(ok)} 位（{sorted(r.role_id for r in ok)}），"
                f"未达 quorum {p.quorum} 位。未形成有效判断的委员：" +
                "；".join(reasons) +
                "。**委员集体无法形成判断时不出决议**——给一个方向会把"
                "系统性故障（数据源不可用、密钥失效、后端故障）"
                "包装成决策意见。"
            )})
            result.duration_ms = (time.perf_counter() - t0) * 1000.0
            emit_progress(progress, "debate.done", valid=False, direction=None,
                          confidence=None, quorum_met=False,
                          problems=list(result.problems),
                          duration_ms=result.duration_ms)
            return result

        if ineffective:
            result.problems.append({"level": "warning", "message": (
                f"以下委员未形成有效判断，已从计票中排除：{ineffective}。"
                f"最终票数只统计了 {sorted(r.role_id for r in ok)}。"
            )})

        # ── 主席综合 ──
        # 主席只看得到**成功**的委员：把失败的委员塞进去会让它统计到空票。
        emit_progress(progress, "chair.start", role=p.chair,
                      peers=sorted(r.role_id for r in ok))
        chair = RoleRuntime(self._role(p.chair), self.tools,
                            guards=self.guards, memory=self.memory)
        result.chair = chair.run(ctx, peers=ok, override=override)

        result.valid = result.chair.ok
        if not result.valid:
            result.problems.append({"level": "error", "message": (
                f"主席未能产出结论：{result.chair.error or '正文为空'}"
            )})

        # 汇总各角色的护栏问题，会议层面也留一份。
        for run in list(final.runs.values()) + [result.chair]:
            for issue in run.issues:
                result.issues.append(Issue(
                    f"{run.role_id}.{issue.guardrail}", issue.severity,
                    issue.message, issue.detail,
                ))

        result.duration_ms = (time.perf_counter() - t0) * 1000.0
        emit_progress(progress, "debate.done", valid=result.valid,
                      direction=result.direction, confidence=result.confidence,
                      quorum_met=True, flips={k: list(v) for k, v in result.flips().items()},
                      chair_role=p.chair, duration_ms=result.duration_ms)
        return result

    # ── 内部 ─────────────────────────────────────────────────
    def _one_round(
        self,
        index: int,
        panel: PanelConfig,
        ctx: ToolContext,
        *,
        peers: Optional[RoundResult],
        override: Optional[LlmOverride],
        weights: Dict[str, float],
        cross_only: bool,
        progress: Optional[ProgressHook] = None,
    ) -> RoundResult:
        """跑一轮，并行。

        ``cross_only=True`` 时只跑声明了 ``cross_examine`` 的委员，其余角色
        沿用上一轮的结论——风控类角色不需要对方向发表第二次意见，重复跑一遍
        只会增加成本并可能引入不一致。
        """
        round_result = RoundResult(index=index)
        t0 = time.perf_counter()

        selected = [m for m in panel.members
                    if m.cross_examine or not cross_only]

        # 每个委员看到的 peers 都是**上一轮**的结论，不含本轮其他人的输出。
        # 因此本轮内部无依赖，可以并发跑；也避免了结果取决于谁先跑完。
        def _peers_for(role_id: str) -> Optional[List[RoleRun]]:
            if peers is None:
                return None
            return [run for rid, run in peers.runs.items()
                    if rid != role_id and run.ok]

        # 交叉质证轮里，**没有别人的结论可看**的委员不重跑。
        #
        # 它拿到的提示词与上一轮一字不差（对等结论块是空的），工具输出也是
        # 同一份，再问一遍只会得到同一个答案 —— 而每一轮都是真金白银。
        # 上一轮全员失败时，这条规则会把整个第二轮省掉，而不是把它原样重放。
        skipped: List[str] = []
        if cross_only and peers is not None:
            runnable = []
            for m in selected:
                (runnable if _peers_for(m.role_id) else skipped).append(m)
            selected = runnable

        with ThreadPoolExecutor(
            max_workers=min(self.max_parallel, max(1, len(selected))),
            thread_name_prefix="fp-agent",
        ) as pool:
            futures = [
                pool.submit(self._run_member, m.role_id, ctx, override,
                            _peers_for(m.role_id), weights.get(m.role_id, 1.0), index,
                            progress)
                for m in selected
            ]
            for f in futures:
                run = f.result()
                round_result.runs[run.role_id] = run

        # ── 沿用上一轮 ──
        #
        # 本轮没跑、或本轮没能产出**可用**结论的委员，只要上一轮的结论还能用，
        # 就把它带下来。判据是 is_effective 而不是"有没有正文"：一份被截断、
        # 少了段落的报告确实有正文，但它连计票都不该进。
        #
        # 不这么做的代价在这一版之前一直在付：第二轮因为 max_tokens 被截断，
        # 于是第一轮明明成立的结论**一起作废**，整场会议报"有效委员 0 位"。
        # 而两轮之间并没有出现任何新事实 —— 第二轮的失败不该追溯地否定
        # 第一轮的结论。
        #
        # 沿用必须可见：round_result.carried 会被 debate() 写进运行告警，
        # 读报告的人有权知道"哪几位其实没有真的参与这一轮"。
        for member in panel.members:
            rid = member.role_id
            cur = round_result.runs.get(rid)
            if cur is not None and is_effective(cur):
                continue
            prev = peers.runs.get(rid) if peers is not None else None
            if prev is None:
                continue
            if is_effective(prev):
                if rid in skipped:
                    why = "本轮无对等结论可质证"
                else:
                    # 只放原因本身。外层那句话说过了"未产出可用结论"，
                    # 这里再写一遍就成了「未产出可用结论（未产出可用结论（…））」——
                    # 用户贴回来的报告里就是这么套着的。
                    why = why_unusable(cur)
                prev.round_index = peers.index
                round_result.runs[rid] = prev
                round_result.carried.append(rid)
                round_result.carry_reason[rid] = why
            elif cur is None:
                # 本轮没跑、上一轮也没产出可用结论：把那条记录原样带下来。
                # 否则最终一轮会是空的，"为什么没有决议"就无从解释 ——
                # quorum 不足的理由是照着最终一轮的 runs 生成的。
                round_result.runs[rid] = prev

        round_result.duration_ms = (time.perf_counter() - t0) * 1000.0
        return round_result

    def _run_member(
        self,
        role_id: str,
        ctx: ToolContext,
        override: Optional[LlmOverride],
        peers: Optional[List[RoleRun]],
        weight: float,
        round_index: int,
        progress: Optional[ProgressHook] = None,
    ) -> RoleRun:
        role = self._role(role_id)
        emit_progress(progress, "role.start", role=role_id, role_name=role.name,
                      round=round_index, weight=weight,
                      cross_examining=bool(peers))
        runtime = RoleRuntime(role, self.tools, guards=self.guards, memory=self.memory)
        run = runtime.run(ctx, peers=peers, override=override)
        run.weight = weight
        run.round_index = round_index
        emit_progress(progress, "role.done", role=role_id, role_name=role.name,
                      round=round_index, weight=weight, ok=run.ok,
                      direction=run.direction, confidence=run.confidence,
                      error=run.error, duration_ms=run.duration_ms,
                      tool_calls=[i.name for i in run.invocations])
        return run

    def _role(self, role_id: str) -> RoleConfig:
        if role_id not in self.roles:
            raise ConfigError(f"没有名为 '{role_id}' 的角色；可选 {sorted(self.roles)}")
        return self.roles[role_id]

    def _panel(self, panel: Optional[str | PanelConfig]) -> PanelConfig:
        if isinstance(panel, PanelConfig):
            return panel
        if panel is None:
            if not self.panels:
                raise OrchestrationError(
                    "没有任何投委会配置。请在 configs/panels/ 下放一份 panel JSON，"
                    "或显式传入一个 PanelConfig。"
                )
            # 只有一个 panel 时就用它，省掉调用方必须记住名字的负担；
            # 有多个时要求显式指定——猜错了代价比多敲一个参数高。
            if len(self.panels) == 1:
                return next(iter(self.panels.values()))
            raise OrchestrationError(
                f"有多个投委会配置，请显式指定：{sorted(self.panels)}"
            )
        if panel not in self.panels:
            raise OrchestrationError(
                f"没有名为 '{panel}' 的投委会；可选 {sorted(self.panels)}"
            )
        return self.panels[panel]

    # ── 自述 ─────────────────────────────────────────────────
    def list_panels(self) -> List[Dict[str, Any]]:
        out = []
        for p in self.panels.values():
            d = p.to_json()
            d["problems"] = [
                {"level": level, "message": msg}
                for level, msg in validate_panel(p, self.roles)
            ]
            out.append(d)
        return out


def why_unusable(run: RoleRun) -> str:
    """这个角色的结论为什么进不了计票。**要具体到可操作。**

    "报告不可用"这四个字对排查毫无帮助 —— 上一版报告里就是这么写的，
    用户拿到的现场信息是"某处护栏报了错"。这里把真原因写出来：是截断、
    是缺段、还是根本没取到数据，各自该改的东西完全不同。
    """
    if run.error:
        return run.error
    if run.truncated:
        missing = list(run.check.missing_sections) if run.check else []
        return ("输出被 max_tokens 截断，段落没写完"
                + (f"，缺：「{'、'.join(missing)}」" if missing else ""))
    if not run.tool_results:
        return "没有取得任何工具数据，通篇只能是「数据未提供」"
    kinds = sorted({i.guardrail for i in run.issues if i.severity == "error"})
    return "护栏报了 error 级问题" + (f"（{'、'.join(kinds)}）" if kinds else "")


def _diff(before: RoundResult, after: RoundResult) -> List[str]:
    """比较两轮之间哪些委员改了口。只在**参与本轮**的委员之间比较。"""
    changed: List[str] = []
    for rid, run in after.runs.items():
        prev = before.runs.get(rid)
        if prev is None:
            continue
        if prev.direction != run.direction:
            changed.append(rid)
    return changed


def summarize_debate(result: DebateResult) -> Dict[str, Any]:
    """给 CLI / RPC 用的一句话摘要 + 关键字段，不含全文。"""
    if not result.valid:
        return {
            "valid": False,
            "panel": result.panel.name,
            "problems": result.problems,
        }
    final = result.final_round
    return {
        "valid": True,
        "panel": result.panel.name,
        "direction": result.direction,
        "confidence": result.confidence,
        "members": {
            rid: {"direction": run.direction, "confidence": run.confidence,
                  "weight": run.weight, "round": run.round_index,
                  "ok": run.ok, "effective": is_effective(run),
                  "errors": len(
                      [i for i in run.issues if i.severity == "error"])}
            for rid, run in (final.runs.items() if final else [])
        },
        "flips": {k: list(v) for k, v in result.flips().items()},
        "duration_ms": round(result.duration_ms, 1),
    }
