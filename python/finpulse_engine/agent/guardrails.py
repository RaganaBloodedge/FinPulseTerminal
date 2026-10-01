"""护栏 —— 在输出离开系统之前拦住它。

复刻 Fincept ``modules/guardrails_module.py`` 的思路（金融 PII、注入防护、
输出校验），但把重点放在这一类系统**真正会出的问题**上，而不是清单式地
堆规则。按重要性排序，本项目最担心的三件事：

1. **编数字**。模型很擅长生成一个"看起来对"的收益率。而这份报告会被
   用来做决策。所以 :class:`NumberProvenance` 会逐个核对：报告里出现的
   每个数字，能不能在本次工具输出里找到出处。
2. **不履职**。少写一段、置信度段为空，从文本上看不出来，但下游（主席、
   风控）依赖这些段。所以缺段是 **error** 而不是 warning。
3. **越权表述**。「强烈建议买入」「必涨」这类词一出现，报告的性质就从
   "分析"变成了"荐股"。角色的 DO-NOT 清单里明确禁止，这里做机器兜底。

严重级别只有两档，刻意不分更多：**error 表示这份报告不可用，
warning 表示可用但要在轨迹里留痕**。多分档只会让人忽略。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from finpulse_engine.agent.config import RoleConfig
from finpulse_engine.agent.schemas import (
    DIRECTIONS,
    CONFIDENCE_LEVELS,
    OutputCheck,
    decimals_of,
    extract_numbers,
    number_pool,
    numbers_in_text,
)

ERROR = "error"
WARNING = "warning"


@dataclass
class Issue:
    guardrail: str
    severity: str
    message: str
    detail: str = ""

    def to_json(self) -> Dict[str, Any]:
        d = {"guardrail": self.guardrail, "severity": self.severity, "message": self.message}
        if self.detail:
            d["detail"] = self.detail
        return d


@dataclass
class GuardrailContext:
    """护栏能看到的一切。刻意把工具输出也放进来——没有它就没法做数字溯源。"""

    role: RoleConfig
    text: str
    check: OutputCheck
    tool_results: Dict[str, Any] = field(default_factory=dict)
    tool_calls: List[str] = field(default_factory=list)
    #: 该角色这一轮收到的**任务提示词原文**（运行参数 + 其它角色的结论）。
    #:
    #: 数字溯源必须按"模型能读到什么"来判，而不是按"它调了哪些工具"。
    #: 主席只声明了 ``stats``，可它的提示词里写着"你手上只有各人的结论与
    #: 他们的工具输出"，于是它引用技术面的均线、量价的量比**完全合法** ——
    #: 池子里不装这些，就会把一整份正确的报告判成"26 个数字无法溯源"。
    prompt_text: str = ""
    provider: str = ""
    fallback_reason: Optional[str] = None
    #: 正文被 max_tokens 截断（``finish_reason == "length"``）。
    truncated: bool = False
    #: 截断发生在第几轮。与 "缺段" 分开记：一个是没写完，一个是没写。
    turn: int = 0


class Guardrail(ABC):
    name: str = "guardrail"
    severity: str = WARNING

    @abstractmethod
    def check(self, ctx: GuardrailContext) -> List[Issue]:
        ...

    def _issue(self, message: str, detail: str = "", severity: Optional[str] = None) -> Issue:
        return Issue(self.name, severity or self.severity, message, detail)


# ── 具体护栏 ──────────────────────────────────────────────────────


class SectionCompleteness(Guardrail):
    """声明的段落一个都不能少。少段等于没履职，下游会读不到东西。"""

    name = "section_completeness"
    severity = ERROR

    def check(self, ctx: GuardrailContext) -> List[Issue]:
        out: List[Issue] = []
        if ctx.check.missing_sections:
            out.append(self._issue(
                f"缺少 {len(ctx.check.missing_sections)} 个声明段落",
                "缺失：" + "、".join(ctx.check.missing_sections)
                + f"；应包含：{'、'.join(ctx.role.output_sections)}",
            ))
        if ctx.check.empty_sections:
            out.append(self._issue(
                f"{len(ctx.check.empty_sections)} 个段落内容过短（少于 8 字）",
                "过短：" + "、".join(ctx.check.empty_sections),
            ))
        return out


class ConfidencePresent(Guardrail):
    """置信度必须落在三档之内。

    这条护栏的存在理由是下游逻辑：主席靠置信度封顶、风控靠它决定是否
    采信。抽不到置信度时，主席会把该角色的上限当成 LOW——看起来是"保守"，
    实际上是**因为解析失败而降级**，必须报出来。
    """

    name = "confidence_present"
    severity = ERROR

    def check(self, ctx: GuardrailContext) -> List[Issue]:
        # 风险官这类 schema 不输出置信度段，其"模型可信度"承担同样职责。
        if "置信度" not in ctx.role.output_sections:
            return []
        if ctx.check.confidence is None:
            return [self._issue(
                "未能从输出中抽到置信度",
                f"期望 {list(CONFIDENCE_LEVELS)} 之一；下游（主席封顶、风控采信）"
                f"依赖该字段，缺失会被当作 LOW 处理",
            )]
        return []


class DirectionPresent(Guardrail):
    """方向标签缺失只是警告，不是错误。

    风控与量价角色本来就可以不给方向（它们评价的是风险与状态）。
    """

    name = "direction_present"

    def check(self, ctx: GuardrailContext) -> List[Issue]:
        if ctx.role.category in ("risk", "sentiment"):
            return []
        if ctx.check.direction is None:
            return [self._issue(
                "输出中没有出现方向标签",
                f"期望 {list(DIRECTIONS)} 之一，编排层靠它统计票数；"
                f"缺失时该角色的票不计入",
            )]
        return []


class ForbiddenPhrases(Guardrail):
    """越权表述的机器兜底。

    每个角色的 instructions 里都有 DO-NOT 清单，但提示词是"软约束"。
    这里做硬兜底，同时**不试图穷举**——只拦那些一出现就改变报告性质的词。
    """

    name = "forbidden_phrases"
    severity = ERROR

    PHRASES = (
        "强烈建议买入", "强烈建议卖出", "强烈推荐", "必涨", "必跌", "稳赚",
        "保证收益", "包赚", "无风险收益", "翻倍", "满仓", "梭哈",
        "保证盈利", "稳赚不赔", "保本保收益",
    )

    def check(self, ctx: GuardrailContext) -> List[Issue]:
        hits = [p for p in self.PHRASES if p in ctx.text]
        if not hits:
            return []
        return [self._issue(
            f"出现 {len(hits)} 处越权表述",
            "命中：" + "、".join(hits)
            + "。本系统的产出是分析而非荐股，此类表述会把报告性质改变，必须重写。",
        )]


class NumberProvenance(Guardrail):
    """数字溯源：报告里的数字必须能在**模型读过的东西**里找到出处。

    **这是本项目最有价值的一条护栏。** LLM 编造看起来合理的数字是
    最常见的失败模式，而且从文本上完全看不出来。做法很朴素：
    收集所有工具输出里出现过的数字构成"池子"，再逐个核对报告里的数字。

    容差按"模型写了几位小数"来定：工具给 143.5725，报告写 143.57，
    应该算对得上；写 143.58 就算对不上。允许的常量（95、100、252 这类
    口径常量与步数）单独列白名单，避免噪音淹没真问题。

    **池子的边界是"模型读过什么"，不是"它调了什么工具"。** 这两者差得
    很远，而且是踩了坑才知道的：主席只声明了 ``stats``，提示词却明确要求
    它引用各分析师的判据并交叉核对。真实 DeepSeek 照做了，报告完全正确，
    却被判「26 个数字无法溯源」—— 池子里只有 stats，它引用的均线、量比、
    成交量当然找不到。**一条每次都误报的告警，等于把这整条护栏废掉。**
    所以池子 = 自己的工具输出 ∪ 任务提示词（运行参数 + 其它角色结论）。
    """

    name = "number_provenance"
    severity = WARNING

    #: 口径常量、步数索引、角色人数这类"不是从数据里算出来的"数字。
    ALLOWED = frozenset({0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 12.0, 20.0, 26.0, 95.0, 100.0, 252.0})

    #: 整数里 ≤ 该值的一律放行（步数、折数、人数、天数）。
    SMALL_INT_MAX = 32.0

    #: 认可为"衍生比值"的商的取值范围。
    #:
    #: 比值类数字（量比、占比、倍数、分位）都落在这一段里；超出它的商
    #: 多半是量纲不同的两个数相除（成交量 ÷ 价格 ≈ 3000），那种商不是
    #: 分析里会引用的数，却会给编造的绝对值提供藏身处。
    DERIVED_MIN = 0.001
    DERIVED_MAX = 100.0

    def check(self, ctx: GuardrailContext) -> List[Issue]:
        pool = number_pool(ctx.tool_results)
        # 提示词里的数字也算出处：运营参数（样本量、步长、折数）与其它
        # 角色的结论都在里面，模型引用它们不是编造。
        pool |= number_pool(ctx.prompt_text)
        if not pool:
            # 没有任何工具输出可以比对。这时候报"无法溯源"是诚实的做法，
            # 而不是默默放行。
            return [self._issue(
                "本次没有工具输出，无法对报告中的数字做溯源检查",
                "该角色若引用了数字，请人工复核",
            )]

        cleaned = numbers_in_text(ctx.text)
        # 保留原始字面量以便按精度做容差。
        raw_tokens = extract_numbers(_strip_dates(ctx.text))

        unmatched: List[str] = []
        for idx, value in enumerate(cleaned):
            if value in self.ALLOWED:
                continue
            if value == int(value) and abs(value) <= self.SMALL_INT_MAX:
                continue
            # 原始字面量要一起传下去：比较时得知道这个数后面跟没跟 "%"。
            token = raw_tokens[idx] if idx < len(raw_tokens) else ""
            digits = decimals_of(token) if token else 2
            if self._matches(value, digits, pool, token=token):
                continue
            if self._is_derived(value, digits, pool, token=token):
                continue
            unmatched.append(token or str(value))

        if not unmatched:
            return []
        # 条数写在 message 里、字面量也放进去：报告渲染只显示 message，
        # 只说"8 个数字"用户没法复核是这 8 个中的哪一个。
        shown = unmatched[:4]
        more = len(unmatched) - len(shown)
        head = f"{len(unmatched)} 个数字无法溯源：{'、'.join(shown)}"
        if more > 0:
            head += f" 等 {len(unmatched)} 处"
        detail = ("未能在工具输出与提示词中找到出处：" + "、".join(unmatched[:8]))
        if len(unmatched) > 8:
            detail += f"（另有 {len(unmatched) - 8} 个）"
        detail += "。模型编造看似合理的数字是最常见的失败模式，请人工复核这几处。"
        return [self._issue(head, detail)]

    @staticmethod
    def _matches(value: float, digits: int, pool: set, *, token: str = "") -> bool:
        """按精度比较：把池子里的每个数也截到同样位数再看是否相等。

        ``token`` 是原文里的字面量，用来判断两件事：带没带百分号、
        **有没有显式写符号**。

        **没写符号时按幅度比。** 工具返回 ``max_drawdown_pct = -24.8469``，
        报告写「最大回撤 24.85%」是完全正确的表达 —— 回撤是负的、幅度是正
        的，人话里说的就是后者。要求符号一致，会把这类正确引用整批判成
        "编数字"，而这正是第 4 次真跑里风险官那两条告警的由来。

        **显式写了符号就按符号比。** 这时候符号是模型的**断言**，翻了号是
        真错误：工具给 −24.8469，报告自己加个负号写成 ``-24.8469`` 就该拦。
        幅度匹配只在"模型没表态"时才放宽，不替它把符号偷偷改对。

        注意池子里的数字**带符号原样存**（工具输出是 JSON 数值，``float``
        保号），所以负值在这一步必须用 ``abs`` 去对；而正文里的负号在
        :func:`extract_numbers` 入口已经归一成 ASCII —— 那一步解决的是
        "U+2212 压根没被当成符号"，这一步解决的是"符号对不上"。
        """
        signed = token[:1] in ("-", "+")
        target = round(value, digits)
        for candidate in pool:
            if round(candidate, digits) == target:
                return True
            if not signed and round(abs(candidate), digits) == target:
                return True

        # 百分比字面量的换算：报告里写 "22.46%"，而 flow 工具输出里存的是
        # 0.2246 —— 同一个比率的两种写法，不是两个数。
        #
        # 不做这一步的代价不是"漏掉一个边角情况"，而是**每次**跑量价类角色
        # 都误报一条"数字无法溯源"。一条每次都出现的告警，用户很快会学会
        # 忽略，等于把整条溯源检查废掉 —— 那比不检查更糟。
        #
        # 只对**带 % 的字面量**做换算：写成 "50" 时不会去匹配池子里的 0.5，
        # 所以没有把普通数字的容差一起放宽。
        if token.endswith("%"):
            # 除以 100 会多出两位有效小数（0.2246 → 4 位），容差跟着放大。
            scaled = round(value / 100.0, digits + 2)
            for candidate in pool:
                if round(candidate, digits + 2) == scaled:
                    return True
                if not signed and round(abs(candidate), digits + 2) == scaled:
                    return True
        return False

    @classmethod
    def _is_derived(cls, value: float, digits: int, pool: set, *, token: str = "") -> bool:
        """这个数是不是由池子里的**两个数相除**得到的？

        提示词本来就要求分析师做这类换算（量比、占比、相对强弱、倍数），
        所以"自己算出来的比值"是履职，不是编造。不认它的话，每次跑都会
        多报两三条 —— 又回到"每次都误报 = 没有护栏"。

        **只认除法，不认加减乘。** 这不是省事：池子里任意两个数相乘或相加，
        会覆盖掉极大一片数值区间，随便编一个价位都能撞上其中一个，护栏就
        等于删掉了。除法的商落在有限的几个量级上，既覆盖了真正会被引用的
        比值，又给编造的绝对值留不下多少藏身处。

        商限定在 :data:`DERIVED_MIN` ~ :data:`DERIVED_MAX`：量纲不同的两个数
        相除（成交量 ÷ 价格）会得到几千几万，那种商没人会写进报告。
        """
        targets = [(round(value, digits), digits)]
        if token.endswith("%"):
            targets.append((round(value / 100.0, digits + 2), digits + 2))

        values = [v for v in pool if v != 0.0]
        for a in values:
            for b in values:
                if a == b:
                    continue
                q = a / b
                if not (cls.DERIVED_MIN <= abs(q) <= cls.DERIVED_MAX):
                    continue
                for target, d in targets:
                    if round(q, d) == target:
                        return True
        return False


class ToolBudget(Guardrail):
    """工具调用不能超出角色声明的预算。

    超出通常意味着循环没有正确收敛（模型反复调同一个工具），
    这类问题在文本上看不出来，只能从调用记录发现。
    """

    name = "tool_budget"
    severity = ERROR

    def check(self, ctx: GuardrailContext) -> List[Issue]:
        used = len(ctx.tool_calls)
        if used <= ctx.role.max_tool_calls:
            return []
        return [self._issue(
            f"工具调用 {used} 次，超出该角色预算 {ctx.role.max_tool_calls} 次",
            "调用序列：" + " → ".join(ctx.tool_calls),
        )]


class OutputTruncated(Guardrail):
    """输出被 max_tokens 截断 —— 留痕，但**不等于报告不可用**。

    截断与"缺段"是两件事，严重级别也不同：

    * **缺段**由 :class:`SectionCompleteness` 判 error —— 那份报告下游
      真的读不到东西；
    * **截断**只是"话没说完"。五段齐全、最后一句被切掉的报告照样能读、
      能计票。

    这条区分是踩过坑才加的：上一版把截断直接写进 ``run.error``，于是一份
    写满了五个段落、只在末尾被切掉的报告被当成"未产出结论"丢掉，三个委员
    同时出局，整场投委会废掉。**"正好写满"被升级成了"整场作废"。**

    这里只给警告，把判断权交给缺段护栏 —— 它才是负责回答"这份报告能不能
    用"的那一条。
    """

    name = "output_truncated"

    def check(self, ctx: GuardrailContext) -> List[Issue]:
        if not ctx.truncated:
            return []
        return [self._issue(
            "输出被 max_tokens 截断，末段可能不完整",
            f"发生在第 {ctx.turn} 轮。若声明的段落仍然齐全，本报告按可用处理；"
            f"否则请上调该角色的 max_tokens，或收紧提示词里的输出预算。",
        )]


class ProviderFallback(Guardrail):
    """发生了后端降级时必须留痕。

    配了 ``openai`` 却因为没有密钥退回规则后端，如果不说出来，
    使用者会以为自己看到的是 LLM 的判断。**降级本身可接受，静默降级不可接受。**
    """

    name = "provider_fallback"

    def check(self, ctx: GuardrailContext) -> List[Issue]:
        if not ctx.fallback_reason:
            return []
        return [self._issue(
            f"分析后端已降级：{ctx.provider}",
            ctx.fallback_reason,
        )]


# ── 管线 ──────────────────────────────────────────────────────────


def _strip_dates(text: str) -> str:
    from finpulse_engine.agent.schemas import _DATE_RE
    return _DATE_RE.sub(" ", text or "")


class Pipeline:
    """按固定顺序跑完所有护栏。

    顺序不重要（各护栏互不依赖），固定只是为了输出稳定、便于对比两次运行的差异。
    """

    def __init__(self, guards: Optional[Sequence[Guardrail]] = None) -> None:
        self.guards: List[Guardrail] = list(guards) if guards is not None else [
            SectionCompleteness(),
            ConfidencePresent(),
            DirectionPresent(),
            ForbiddenPhrases(),
            NumberProvenance(),
            ToolBudget(),
            OutputTruncated(),
            ProviderFallback(),
        ]

    def run(self, ctx: GuardrailContext) -> List[Issue]:
        out: List[Issue] = []
        for guard in self.guards:
            try:
                out.extend(guard.check(ctx))
            except Exception as exc:  # noqa: BLE001
                # 护栏自己崩了不能把分析拖垮，但必须留下痕迹——
                # 静默吞掉异常会让"护栏失效"变成没人知道的事。
                out.append(Issue(
                    guard.name, WARNING,
                    f"护栏自身执行失败: {type(exc).__name__}",
                    str(exc),
                ))
        return out

    @staticmethod
    def has_errors(issues: Sequence[Issue]) -> bool:
        return any(i.severity == ERROR for i in issues)

    @staticmethod
    def errors(issues: Sequence[Issue]) -> List[Issue]:
        return [i for i in issues if i.severity == ERROR]

    @staticmethod
    def warnings(issues: Sequence[Issue]) -> List[Issue]:
        return [i for i in issues if i.severity == WARNING]


def summarize(issues: Sequence[Issue]) -> Dict[str, Any]:
    """给 RPC 回传用的紧凑摘要。"""
    return {
        "total": len(issues),
        "errors": len(Pipeline.errors(issues)),
        "warnings": len(Pipeline.warnings(issues)),
        "items": [i.to_json() for i in issues],
    }
