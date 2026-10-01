"""确定性规则后端 —— 不需要网络、不需要密钥的分析实现。

**它不是"假装有 AI"。** 它是 :class:`LlmProvider` 的另一个实现：读同样的工具
输出、遵守同样的输出段落契约、被同样一套护栏校验。区别只有一个——它不做自然
语言推理，而是把工具返回的真实数值套进按段落组织的规则模板里。

存在的理由很实际：

* **CI 要能跑**。一个只在配好 API key 的机器上才跑得通的系统，等于没有测试。
* **离线可用**。无网环境（现场演示、内网部署）同样能跑通全流程。
* **对照基线**。当 LLM 版本产出可疑结论时，规则版本给出"纯看数字该怎么判"，
  两者的差异正是 LLM 发挥（或犯错）的地方——这本身就是有价值的信息。

所以 ``provider='rule_based'`` 是配置里的**默认值**：装上就能跑，配了 key 再切。
"""

from __future__ import annotations

import json
import re
from typing import Any, Callable, Dict, List, Optional, Sequence

from finpulse_engine.agent.llm.base import (
    LlmProvider,
    LlmResponse,
    Message,
    ToolCall,
    Usage,
)
from finpulse_engine.agent.schemas import DIRECTIONS as _DIRECTIONS

#: 段落名 → 渲染函数。函数签名 (data, ctx) -> str。
#:
#: ``data`` 是所有工具输出的字典 ``{工具名: 结果}``；
#: ``ctx`` 携带跨角色的上下文（其它角色的结论等）。
FILLERS: Dict[str, Callable[[Dict[str, Any], Dict[str, Any]], str]] = {}


def filler(section: str):
    def deco(fn):
        FILLERS[section] = fn
        return fn
    return deco


# ── 数值格式化 ────────────────────────────────────────────────────


def _num(value: Any, decimals: int = 2, suffix: str = "") -> str:
    if value is None:
        return "数据未提供"
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        return f"{value:,.{decimals}f}{suffix}"
    return str(value)


def _pct(value: Any, decimals: int = 2) -> str:
    """渲染**已经是百分数**的值（22.46 → "22.46%"）。

    用之前必须先确认来源。引擎里比率有两种约定：

    * ``stats`` 的字段带 ``_pct`` 后缀，**已经乘过 100**（``ann_vol_pct``
      = 22.46）；C++ 传来的 ``change_pct`` 同样是百分数（2.55）。
      这些用 ``_pct`` 是对的。
    * ``flow`` 的 ``volatility_regime`` / ``percentile`` 是**小数**
      （0.2246 表示 22.46%）。这些必须用 ``_frac_pct``。

    两者混用的症状很隐蔽：报告读起来通顺，只是某个百分数小了 100 倍。
    """
    return _num(value, decimals, "%")


def _frac_pct(value: Any, decimals: int = 2) -> str:
    """渲染**小数形态**的比率（0.2246 → "22.46%"）。

    与 ``_pct`` 分开而不是合并成一个"自动判断量级"的函数：0.5 究竟表示
    50% 还是"0.5 倍"，只有**字段本身**知道。让格式化函数去猜，猜错的时候
    没人能看出来。这里靠调用点显式表达单位，猜的责任落回写代码的人身上。
    """
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return _num(value * 100.0, decimals, "%")
    return _num(value, decimals, "%")


def _ratio(value: Any, decimals: int = 2) -> str:
    return _num(value, decimals, " 倍")


def _get(data: Dict[str, Any], tool: str, *path: str) -> Any:
    """从工具输出里逐层取值，中途缺任何一层都返回 None 而不是抛异常。

    规则后端面对的是"工具可能失败、可能被裁剪"的现实。取不到就老实说取不到，
    不能因为一个字段缺失就让整份报告挂掉。
    """
    node: Any = data.get(tool)
    if node is None:
        return None
    for key in path:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
        if node is None:
            return None
    return node


# ── 技术面 ────────────────────────────────────────────────────────


def _trend_signals(data: Dict[str, Any]) -> List[tuple]:
    """返回 [(判据描述, '多'|'空'|'中')]。技术面与置信度共用这一份。"""
    ma5 = _get(data, "indicators", "ma", "ma5")
    ma20 = _get(data, "indicators", "ma", "ma20")
    close = _get(data, "indicators", "last_close")
    mid = _get(data, "indicators", "boll", "mid")
    hist = _get(data, "indicators", "macd", "hist")
    rsi = _get(data, "indicators", "rsi14")

    out: List[tuple] = []
    if ma5 is not None and ma20 is not None:
        if ma5 > ma20:
            out.append((f"MA5({_num(ma5)}) 位于 MA20({_num(ma20)}) 之上，均线多头排列", "多"))
        elif ma5 < ma20:
            out.append((f"MA5({_num(ma5)}) 位于 MA20({_num(ma20)}) 之下，均线空头排列", "空"))
        else:
            out.append(("MA5 与 MA20 重合，无方向", "中"))
    if close is not None and mid is not None:
        if close > mid:
            out.append((f"收盘 {_num(close)} 站上布林中轨 {_num(mid)}", "多"))
        elif close < mid:
            out.append((f"收盘 {_num(close)} 跌破布林中轨 {_num(mid)}", "空"))
        else:
            out.append(("收盘价等于布林中轨，无方向", "中"))
    if hist is not None:
        out.append((f"MACD 柱值 {_num(hist, 4)}，动量为{'正' if hist > 0 else '负'}"
                    if hist != 0 else "MACD 柱值为 0，动量中性",
                    "多" if hist > 0 else ("空" if hist < 0 else "中")))
    if rsi is not None:
        if rsi >= 70:
            out.append((f"RSI14 = {_num(rsi)}，进入超买区", "空"))
        elif rsi <= 30:
            out.append((f"RSI14 = {_num(rsi)}，进入超卖区", "多"))
        else:
            out.append((f"RSI14 = {_num(rsi)}，处于中性区间", "中"))
    return out


def _tally(signals: Sequence[tuple]) -> tuple:
    bull = sum(1 for _, d in signals if d == "多")
    bear = sum(1 for _, d in signals if d == "空")
    return bull, bear


@filler("趋势状态")
def _fill_trend(data: Dict[str, Any], ctx: Dict[str, Any]) -> str:
    signals = _trend_signals(data)
    if not signals:
        return "工具未返回可用的均线与布林数据，无法判断趋势状态。"
    bull, bear = _tally(signals)
    if bull > bear:
        label = "上升"
    elif bear > bull:
        label = "下降"
    else:
        label = "震荡"
    detail = "；".join(desc for desc, _ in signals)
    return f"**{label}**。判据：{detail}。（看多信号 {bull} 项 / 看空信号 {bear} 项）"


@filler("关键位")
def _fill_levels(data: Dict[str, Any], ctx: Dict[str, Any]) -> str:
    upper = _get(data, "indicators", "boll", "upper")
    lower = _get(data, "indicators", "boll", "lower")
    hi = _get(data, "stats", "max_high")
    lo = _get(data, "stats", "min_low")
    if upper is None and lower is None and hi is None and lo is None:
        return "工具未返回布林带与区间极值，无法给出关键位。"

    parts: List[str] = []
    if lo is not None:
        parts.append(f"区间低点 {_num(lo)}（来源：stats.min_low）")
    if lower is not None:
        parts.append(f"布林下轨 {_num(lower)}（来源：indicators.boll.lower）")
    support = " / ".join(parts) if parts else "数据未提供"

    parts = []
    if upper is not None:
        parts.append(f"布林上轨 {_num(upper)}（来源：indicators.boll.upper）")
    if hi is not None:
        parts.append(f"区间高点 {_num(hi)}（来源：stats.max_high）")
    resistance = " / ".join(parts) if parts else "数据未提供"

    return f"支撑：{support}。\n压力：{resistance}。"


@filler("动量")
def _fill_momentum(data: Dict[str, Any], ctx: Dict[str, Any]) -> str:
    rsi = _get(data, "indicators", "rsi14")
    macd = _get(data, "indicators", "macd", "macd")
    signal = _get(data, "indicators", "macd", "signal")
    hist = _get(data, "indicators", "macd", "hist")

    if rsi is None and macd is None:
        return "工具未返回 RSI 与 MACD，无法评估动量。"

    lines = [
        f"RSI14 = {_num(rsi)}。",
        f"MACD = {_num(macd, 4)}，signal = {_num(signal, 4)}，柱值 = {_num(hist, 4)}。",
    ]
    if macd is not None and signal is not None:
        if macd > signal:
            lines.append("MACD 位于信号线之上，短周期动量偏强。")
        else:
            lines.append("MACD 位于信号线之下，短周期动量偏弱。")
    lines.append("未做背离检测：本后端只报告数值与符号；背离判定需要人工看图或由 LLM 承担。")
    return " ".join(lines)


@filler("置信度")
def _fill_confidence(data: Dict[str, Any], ctx: Dict[str, Any]) -> str:
    # 三个角色的「置信度」问的不是同一件事，所以不能共用一个算法。
    #   aggregate（主席）  —— 不产生新信心，只能封顶在下级的最高值上
    #   sentiment（量价）  —— 评价的是「当前状态的描述」有多可靠，不是方向
    #   其它（技术面）      —— 判据的一致程度
    # 早先三个角色都走技术面那套 _trend_signals，量价角色因此会写
    # 「4 项判据中 3 项一致」——它根本没做趋势判断，这是错配。
    category = ctx.get("category")
    if category == "aggregate":
        return _fill_confidence_chair(data, ctx)
    if category == "sentiment":
        return _fill_confidence_flow(data, ctx)

    signals = _trend_signals(data)
    if not signals:
        return "LOW —— 没有可用的指标数据，无法形成判断。"
    bull, bear = _tally(signals)
    agree = max(bull, bear)
    conflict = min(bull, bear)
    total = len(signals)
    if agree >= 3 and conflict <= 1:
        level = "HIGH"
        why = f"{total} 项判据中 {agree} 项一致，反对仅 {conflict} 项"
    elif agree >= 2:
        level = "MEDIUM"
        why = f"{total} 项判据中 {agree} 项一致，但有 {conflict} 项明确相反"
    else:
        level = "LOW"
        why = f"{total} 项判据方向分散（多 {bull} / 空 {bear}），未形成合力"
    return f"**{level}** —— {why}。判 HIGH 需三项以上一致，此处按该门槛判定。"


def _fill_confidence_flow(data: Dict[str, Any], ctx: Dict[str, Any]) -> str:
    """量价角色的置信度：只评价「当前状态的描述」是否可靠。

    这个角色不做趋势判断、也没有预测工具，所以拿技术面的"判据一致性"
    来算它的置信度是错的。这里改成数**证据齐不齐**：量比、量能趋势、
    涨跌日量对比、波动率分位、波动聚集、背离检查——六项里有多少项真的
    拿到了工具输出。少了任何一项，就说明这一段写得不够实，置信度得降。
    """
    checks: List[tuple] = []

    ratio = _get(data, "flow", "volume_ratio")
    checks.append(("量比", ratio is not None))
    checks.append(("量能趋势", _get(data, "flow", "volume_trend") is not None))
    checks.append(("涨跌日量对比",
                   _get(data, "flow", "up_down_volume_ratio") is not None))

    regime = _get(data, "flow", "volatility_regime") or {}
    checks.append(("波动率分位", bool(regime.get("state"))))
    checks.append(("波动聚集",
                   _get(data, "flow", "autocorr_abs_ret") is not None))

    # 背离这一项特殊：查了但"未发现"也是有效结论，要算通过；
    # 只有工具压根没返回 divergence 字段才算缺证据。
    divergence = _get(data, "flow", "divergence")
    divergence_checked = "divergence" in (_get(data, "flow") or {})
    checks.append(("背离检查", divergence_checked))

    have = [name for name, ok in checks if ok]
    missing = [name for name, ok in checks if not ok]
    n = len(have)

    if n >= 5:
        level = "HIGH"
    elif n >= 3:
        level = "MEDIUM"
    else:
        level = "LOW"

    detail = f"6 项证据中取得 {n} 项（{('、'.join(have)) or '无'}）"
    if missing:
        detail += f"；缺 {('、'.join(missing))}"
    if isinstance(divergence, dict):
        detail += f"。背离检查有明确结论（{divergence.get('type', '未知')}）"
    elif divergence_checked:
        detail += "。背离检查已执行，结论为「未发现背离」"
    return (f"**{level}** —— {detail}。"
            f"**本角色没有预测工具，因此该置信度只针对「上述状态描述是否可靠」，"
            f"不针对未来方向。**")


def _fill_confidence_chair(data: Dict[str, Any], ctx: Dict[str, Any]) -> str:
    state = _chair_state(ctx)
    if state is None:
        return "LOW —— 没有下级分析师的结论，主席不产生新数据，因此无可继承的置信度。"
    def _names(seq):
        return "、".join(p.get("name", p.get("role_id", "?")) for p in seq)

    downgrade = {"HIGH": "MEDIUM", "MEDIUM": "LOW", "LOW": "LOW"}
    if state["tie"]:
        # 平票：置信度问的是「信息不足以定方向」这个判断有多可靠。
        # 两边都很确定却结论相反，恰恰说明"定不了方向"是可靠的判断，
        # 所以这里用全体最高值，再下调一档。
        cap = state["cap_all"]
        level = downgrade[cap]
        return (f"**{level}** —— 各分析师自报置信度最高为 {cap}"
                f"（{_names(ctx['peers'])}），方向分歧不构成多数、决议为 NEUTRAL，"
                f"主席据此在最高值上**下调一档**。"
                f"该置信度只反映「当前信息不足以定方向」这一判断的可靠程度。")

    if not state["adopted"]:
        return ("LOW —— 没有任何分析师的结论与决议方向一致，"
                "主席无法从下级继承信心，因此置信度取最低档。")

    cap = state["cap"]
    if state["minority"]:
        # 决议方向只被少数分析师支持。不下调的话会出现
        # "1 人看多、2 人不支持，置信度却是 HIGH"这种一眼就假的报告。
        level = downgrade[cap]
        return (f"**{level}** —— 支持决议方向的分析师中，自报置信度最高为 {cap}"
                f"（{_names(state['adopted'])}）；但支持该方向的只有 "
                f"{len(state['adopted'])} / {len(ctx['peers'])} 位，**不足半数**，"
                f"主席据此在最高值上**下调一档**。决议方向本身是少数意见，"
                f"置信度不应高于其支持面。")

    return (f"**{cap}** —— 取**支持决议方向**的分析师中自报置信度的最高者"
            f"（{_names(state['adopted'])}）：主席不产生新的信心，因此不超过此上限。"
            f"**注意：反对者的置信度不参与计算**——否则一个投反对票的分析师报 HIGH，"
            f"就会把「只有少数人支持」的决议写成高把握。")


# ── 风险 ──────────────────────────────────────────────────────────


@filler("下行风险")
def _fill_downside(data: Dict[str, Any], ctx: Dict[str, Any]) -> str:
    dd = _get(data, "stats", "max_drawdown_pct")
    peak = _get(data, "stats", "max_drawdown_peak_date")
    trough = _get(data, "stats", "max_drawdown_trough_date")
    rec = _get(data, "stats", "max_drawdown_recovery_date")
    vol = _get(data, "stats", "ann_vol_pct")
    if dd is None and vol is None:
        return "工具未返回回撤与波动率数据，无法评估下行风险。"

    recovery = f"已于 {rec} 收复" if rec else "**尚未收复**"
    return (f"最大回撤 {_pct(dd)}，区间 {peak or '数据未提供'} → {trough or '数据未提供'}，{recovery}。"
            f"当前年化波动率 {_pct(vol)}。")


@filler("尾部损失")
def _fill_tail(data: Dict[str, Any], ctx: Dict[str, Any]) -> str:
    var = _get(data, "stats", "var95_pct")
    cvar = _get(data, "stats", "cvar95_pct")
    skew = _get(data, "stats", "skew")
    kurt = _get(data, "stats", "excess_kurtosis")
    if var is None and cvar is None:
        return "工具未返回 VaR / CVaR，无法评估尾部损失。"

    lines = [f"95% 置信下的单日 VaR = {_pct(var)}，CVaR（超过 VaR 后的平均损失）= {_pct(cvar)}。",
             f"偏度 {_num(skew, 3)}，超额峰度 {_num(kurt, 3)}。"]
    if kurt is not None and kurt > 1.0:
        lines.append("**超额峰度显著为正，说明极端行情比正态假设更频繁，"
                     "基于正态分布计算的 VaR 会低估尾部风险，实际操作应按此上浮。**")
    elif kurt is not None:
        lines.append("超额峰度接近 0，正态假设在本样本期内基本成立。")
    if skew is not None and skew < -0.3:
        lines.append("偏度为负，下跌尾部比上涨尾部更厚。")
    return " ".join(lines)


@filler("模型可信度")
def _fill_skill(data: Dict[str, Any], ctx: Dict[str, Any]) -> str:
    skill = _get(data, "backtest", "skill")
    mae = _get(data, "backtest", "mae")
    base = _get(data, "backtest", "baseline_mae")
    if skill is None:
        return ("未取到回测技能分（backtest 工具未返回该字段）。"
                "**在拿到技能分之前，本项目不对任何预测类结论背书。**")

    verdict = ("**技能分为正，模型优于随机游走，其预测可作为弱证据引用。**"
               if skill > 0 else
               "**技能分不为正，模型不优于随机游走。任何基于该模型的看多或看空"
               "结论都必须视为不可信——随机游走不需要任何模型。**")
    return (f"技能分（1 − MSE_model / MSE_randomwalk）= {_num(skill, 4)}；"
            f"模型 MAE {_num(mae)} vs 随机游走 MAE {_num(base)}。{verdict}")


@filler("反对意见")
def _fill_objection(data: Dict[str, Any], ctx: Dict[str, Any]) -> str:
    others = [p for p in (ctx.get("peers") or [])
              if p.get("role_id") != ctx.get("role_id")]
    if not others:
        return ("本次为单角色运行，没有其它角色的结论可供质证。"
                "已检查的项：回撤口径、尾部假设、模型技能分——三项均已在本报告上方列出。")
    lines = ["对其它角色的逐条质疑："]
    for peer in others:
        direction = peer.get("direction") or "未给出方向"
        conf = (peer.get("confidence") or "").upper()
        name = peer.get("name", peer.get("role_id", "?"))
        if conf == "HIGH":
            lines.append(f"- {name}：给出 {direction} 且置信度 HIGH。"
                         f"要求其说明三项以上一致判据；若各判据实际互相矛盾，该置信度应下调。")
        else:
            lines.append(f"- {name}：给出 {direction}，置信度 {conf or '未标注'}。"
                         f"置信度未达 HIGH，其方向结论不应作为主要决策依据。")
    return "\n".join(lines)


# ── 终端证据（反向工具通道的落地） ─────────────────────────────────
#
# 声明了 terminal.* 工具的角色必须有这么一段。理由：工具调用链通了但报告
# 里一个字都不提它，等于这条通道只存在于调试日志里 —— 用户看不到"这份
# 结论用到了终端此刻的状态"。这正是"配置声明了工具却没有任何代码消费它"
# 那一类缺陷，量价分析师之前踩过一次（声明了三段却拿不到 flow 工具）。
#
# 反向通道本身（C++ 起 HTTP 服务端、Python 回调）已经实测可用；这一段
# 解决的是"用它"的问题。


@filler("终端证据")
def _fill_terminal_evidence(data: Dict[str, Any], ctx: Dict[str, Any]) -> str:
    lines: List[str] = []
    declared = False
    got_data = False

    # ── 终端进程内总线统计 ──
    bus = data.get("terminal.bus_stats")
    if isinstance(bus, dict):
        declared = True
        if bus.get("available") is False:
            lines.append(f"终端总线统计**不可用**：{bus.get('reason') or '未说明原因'}。")
        elif "error" in bus:
            lines.append(f"终端总线统计调用失败：{bus['error']}。")
        else:
            got_data = True
            lines.append(
                f"终端进程内总线：累计发布 {_num(bus.get('published'), 0)} 次、"
                f"投递 {_num(bus.get('delivered'), 0)} 次、"
                f"活跃订阅 {_num(bus.get('active_subscriptions'), 0)} 个、"
                f"无人订阅的发布 {_num(bus.get('unmatched'), 0)} 次。"
            )
            if bus.get("note"):
                lines.append(str(bus["note"]) + "。")

    # ── 终端实时行情快照 ──
    quote = data.get("terminal.live_quote")
    if isinstance(quote, dict):
        declared = True
        if quote.get("available") is False:
            lines.append(f"终端实时行情**不可用**：{quote.get('reason') or '未说明原因'}。")
        elif "error" in quote:
            lines.append(f"终端实时行情调用失败：{quote['error']}。")
        else:
            got_data = True
            lines.append(
                f"终端此刻正在接收的 {quote.get('symbol') or '（未命名）'} 最新价 "
                f"{_num(quote.get('last'))}，涨跌 {_pct(quote.get('change_pct'))}。"
            )

    # ── 终端已加载序列的数据质量 ──
    quality = data.get("terminal.data_quality")
    if isinstance(quality, dict):
        declared = True
        if quality.get("available") is False:
            lines.append(f"终端数据质量**不可用**：{quality.get('reason') or '未说明原因'}。")
        elif "error" in quality:
            lines.append(f"终端数据质量调用失败：{quality['error']}。")
        else:
            got_data = True
            issues = quality.get("issues") or []
            lines.append(
                f"终端已加载 {_num(quality.get('bars'), 0)} 根 K 线，"
                f"数据质量检查发现 {len(issues)} 个问题"
                + (f"（{'；'.join(str(i) for i in issues[:3])}）" if issues else "")
                + "。"
            )

    if not declared:
        return ("本角色未声明任何终端侧工具（terminal.*），"
                "因此这一段没有内容。这不是故障，是配置 —— "
                "想让结论用上终端此刻的状态，就在 config.tools 里声明它。")

    # 这一句只在**真的拿到数据**时才加。一个字符都没拿到的段落后面跟一句
    # "上述数据来自终端实时内存状态"，是在为不存在的东西背书。
    if got_data:
        lines.append(
            "上述数据来自 C++ 终端进程的**实时内存状态**，不是本引擎从历史序列"
            "推算出来的：它反映的是终端此刻在收什么、分发了多少，"
            "而历史序列只说明过去。"
        )
    else:
        lines.append(
            "**本段没有取到任何终端数据**，上述原因由终端侧给出。"
            "这一路旁证的缺失不构成对其它段落的反驳，也不应据此调整置信度。"
        )
    return " ".join(lines)


# ── 量价 ──────────────────────────────────────────────────────────

@filler("量能状态")
def _fill_volume(data: Dict[str, Any], ctx: Dict[str, Any]) -> str:
    ratio = _get(data, "flow", "volume_ratio")
    trend = _get(data, "flow", "volume_trend")
    up_down = _get(data, "flow", "up_down_volume_ratio")
    if ratio is None:
        return "工具未返回成交量数据，无法判断量能状态。"
    state = "放大" if ratio > 1.1 else ("萎缩" if ratio < 0.9 else "持平")
    lines = [f"最近窗口均量为全区间均量的 {_ratio(ratio)}，量能**{state}**。",
             f"最近窗口相对前一同长窗口为 {_ratio(trend)}。"]
    if up_down is not None:
        direction = "上涨日成交量高于下跌日" if up_down > 1 else "下跌日成交量高于上涨日"
        lines.append(f"上涨日均量 / 下跌日均量 = {_ratio(up_down)}，{direction}。")

    # 量价配合的方向判断放在这一段（本角色的第一段），因为方向抽取
    # 只认结论段——后面几段会被下游当成"过程描述"而不是结论。
    verdict = _volume_verdict(data, ratio)
    lines.append(f"量价配合方向：**{verdict[0]}** —— {verdict[1]}")
    return " ".join(lines)


def _volume_verdict(data: Dict[str, Any], ratio: float) -> tuple:
    """量价配合的方向判断。

    传统解读：放量上涨是资金推动（看多），缩量上涨是无量虚涨（看空），
    放量下跌是恐慌出逃（看空），缩量下跌是抛压枯竭（中性偏多）。
    还叠加一条背离覆盖：价格创新高/新低但量能萎缩时，背离优先于配合。
    """
    closes = _get(data, "indicators", "recent_closes") or []
    div = _get(data, "flow", "divergence")

    if isinstance(div, dict):
        t = div.get("type", "")
        if "新高" in t:
            return ("BEARISH", f"背离优先：{t}，价格形态未获量能确认。")
        if "新低" in t:
            return ("BULLISH", f"背离优先：{t}，下跌动能可能衰减，但需价格企稳确认。")

    if len(closes) >= 2:
        rising = closes[-1] > closes[0]
    else:
        total = _get(data, "stats", "total_return_pct")
        rising = bool(total is not None and total > 0)

    if rising and ratio > 1.0:
        return ("BULLISH", f"放量上涨（量比 {_num(ratio)}），量能确认价格方向。")
    if rising and ratio <= 1.0:
        return ("BEARISH", f"缩量上涨（量比 {_num(ratio)}），价格上行未获量能支撑，"
                           f"存在无量虚涨风险。")
    if not rising and ratio > 1.0:
        return ("BEARISH", f"放量下跌（量比 {_num(ratio)}），抛压增强。")
    return ("NEUTRAL", f"缩量下跌（量比 {_num(ratio)}），抛压未放大，暂不给出方向。")


@filler("波动率状态")
def _fill_vol_regime(data: Dict[str, Any], ctx: Dict[str, Any]) -> str:
    regime = _get(data, "flow", "volatility_regime") or {}
    ac = _get(data, "flow", "autocorr_abs_ret")
    if not regime.get("state"):
        return f"滚动波动率样本不足（{regime.get('note', '未知原因')}），无法判断所处位置。"
    lines = [f"当前年化波动率 {_frac_pct(regime.get('current'))}，"
             f"处于自身历史的 {_frac_pct(regime.get('percentile'))} 分位，"
             f"判定为 **{regime['state']}**"
             f"（区间 {_frac_pct(regime.get('min'))} ~ {_frac_pct(regime.get('max'))}）。"]
    if ac is not None:
        if ac > 0.15:
            lines.append(f"|收益| 的 lag-1 自相关为 {_num(ac, 3)}，存在明显波动聚集——"
                         f"大波动后面倾向跟着大波动。")
        else:
            lines.append(f"|收益| 的 lag-1 自相关为 {_num(ac, 3)}，未观察到明显波动聚集。")
    return " ".join(lines)


@filler("背离")
def _fill_divergence(data: Dict[str, Any], ctx: Dict[str, Any]) -> str:
    div = _get(data, "flow", "divergence")
    if isinstance(div, dict):
        t = div.get("type", "未知")
        return (f"发现背离：**{t}**。价格由 "
                f"{_num(div.get('price_prior_high', div.get('price_prior_low')))} 走到 "
                f"{_num(div.get('price_recent_high', div.get('price_recent_low')))}，"
                f"而同期量能比仅 {_ratio(div.get('volume_ratio'))}。"
                f"价格形态未获量能确认。")
    return ("未发现背离。已检查：最近窗口的价格新高/新低是否突破此前区间极值，"
            "以及同期均量是否放大。两项未同时成立。")


# ── 主席综合 ──────────────────────────────────────────────────────


def _peer_directions(peers: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    """按人数计票（用于展示"几位看多"）。方向由 :func:`_peer_weights` 定。"""
    tally = {"BULLISH": 0, "BEARISH": 0, "NEUTRAL": 0}
    for peer in peers:
        d = (peer.get("direction") or "").upper()
        if d in tally:
            tally[d] += 1
    return tally


def _peer_weights(peers: Sequence[Dict[str, Any]]) -> Dict[str, float]:
    """按权重计票。**方向由这个结果决定，人数只用于展示。**

    这是对 Fincept 的一处刻意改进：它的 IC 成员配了 2.0 / 1.5 / 1.5 / 1.0 / 1.0
    的权重，但计票函数收了 weight 参数却从不读取，实际是等权计数——
    也就是说那组权重是一份从未生效的配置。这里让它真的生效。
    """
    tally = {"BULLISH": 0.0, "BEARISH": 0.0, "NEUTRAL": 0.0}
    for peer in peers:
        d = (peer.get("direction") or "").upper()
        if d in tally:
            tally[d] += float(peer.get("weight", 1.0) or 1.0)
    return tally


_CONF_ORDER = {"LOW": 0, "MEDIUM": 1, "HIGH": 2}


def _chair_state(ctx: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """把各角色结论汇总成一份会议状态。四段渲染共用它，避免各算各的。

    这里要区分**三类**下级结论，混在一起会得出自相矛盾的报告：

    * **投票**（有 BULLISH / BEARISH / NEUTRAL 标签）—— 计数、定方向
    * **弃权**（没给方向标签，如风险官只评风险不定方向）—— 不计数，
      但人数要算进"参会人数"，否则会出现"3 位分析师中 1 位看多、0 位看空、
      1 位中性"这种加起来不等于 3 的句子（实测踩过）
    * **被否决**—— 有方向、但与决议相反。弃权者**不属于此列**：
      把"没表态"写成"与你相反"是在替对方编造立场
    """
    peers = ctx.get("peers") or []
    if not peers:
        return None

    tally = _peer_directions(peers)
    weights = _peer_weights(peers)
    voted = tally["BULLISH"] + tally["BEARISH"] + tally["NEUTRAL"]

    if weights["BULLISH"] > weights["BEARISH"]:
        direction = "BULLISH"
    elif weights["BEARISH"] > weights["BULLISH"]:
        direction = "BEARISH"
    else:
        direction = "NEUTRAL"

    # 方向缺失（None / 未知标签）都归为弃权，不参与方向统计。
    abstained = [p for p in peers
                 if (p.get("direction") or "").upper() not in _DIRECTIONS]
    voted_peers = [p for p in peers if p not in abstained]
    adopted = [p for p in voted_peers
               if (p.get("direction") or "").upper() == direction]
    rejected = [p for p in voted_peers
                if (p.get("direction") or "").upper() != direction]

    # 置信度上限的计算分两种情况，因为提问方式不同：
    #   有决议时——「这个方向有多可信？」只能由**支持方**回答。
    #              取全体最大值是错的：投反对票的分析师报 HIGH，会把
    #              "决议只有少数人支持"包装成高把握。
    #   平票时——「信息不足以定方向」这个判断有多可信？这时双方的高置信度
    #              恰恰是佐证（两边都很确定但结论相反）→ 取全体最大值。
    cap = "LOW"
    for peer in adopted:
        c = (peer.get("confidence") or "").upper()
        if _CONF_ORDER.get(c, 0) > _CONF_ORDER[cap]:
            cap = c
    cap_all = "LOW"
    for peer in peers:
        c = (peer.get("confidence") or "").upper()
        if _CONF_ORDER.get(c, 0) > _CONF_ORDER[cap_all]:
            cap_all = c

    tie = (direction == "NEUTRAL" and weights["BULLISH"] == weights["BEARISH"]
           and weights["BULLISH"] > 0)
    # 「少数决议」：支持决议的分析师不到全体的一半。方向虽由票数定出，
    # 但证据基础薄弱，报告必须自己说出来，不能靠置信度的高值掩盖。
    minority = bool(adopted) and len(adopted) * 2 < len(peers)

    # 权重是否真的影响过结果。都为 1.0（或都相等）时计票退化成人数，
    # 报告里就不必提权重，免得读者以为它在起作用。
    weighting_active = len(set(weights[k] for k in weights if weights[k] > 0)) > 1

    return {
        "peers": peers,
        "tally": tally,
        "weights": weights,
        "weighting_active": weighting_active,
        "voted": voted,
        "abstained": abstained,
        "abstain_count": len(abstained),
        "direction": direction,
        "cap": cap,
        "cap_all": cap_all,
        "tie": tie,
        "minority": minority,
        "adopted": adopted,
        "rejected": rejected,
    }


@filler("决议")
def _fill_verdict(data: Dict[str, Any], ctx: Dict[str, Any]) -> str:
    state = _chair_state(ctx)
    if state is None:
        return ("NEUTRAL / 短期 —— 本次没有下级分析师的结论可供综合。"
                "主席不产生新数据，也不凭现有数据自行形成方向，因此不形成方向性决议。"
                "请先运行分析师团队（agent.debate）。")
    tally = state["tally"]
    n = len(state["peers"])
    spread = (f"{n} 位分析师中 {state['voted']} 位给出了方向"
              f"（{tally['BULLISH']} 看多 / {tally['BEARISH']} 看空 / "
              f"{tally['NEUTRAL']} 中性）")
    if state["weighting_active"]:
        w = state["weights"]
        spread += (f"；按权重计票为 多 {_num(w['BULLISH'], 1)} / "
                   f"空 {_num(w['BEARISH'], 1)} / 中 {_num(w['NEUTRAL'], 1)}，"
                   f"**方向由加权结果决定**")
    if state["abstain_count"]:
        # 人数必须对得上：参会 n 人 = 给出方向的人 + 弃权的人。
        names = "、".join(p.get("name", p.get("role_id", "?"))
                          for p in state["abstained"])
        spread += f"，另有 {state['abstain_count']} 位未给出方向（{names}），不计入方向计票"

    if state["tie"]:
        core = (f"{spread}，看多与看空票数相同，**不构成多数**，"
                f"因此决议取 NEUTRAL 而非在两者间择一。")
    elif state["minority"]:
        core = (f"{spread}，据此形成方向性决议；但**支持该方向的分析师不足全体半数，"
                f"证据基础薄弱**——该决议是一个方向标签，不是一个高把握结论。")
    else:
        core = f"{spread}，据此形成方向性决议。"

    vol = _get(data, "stats", "ann_vol_pct")
    horizon_note = (f"年化波动率 {_pct(vol)}，" +
                    ("波动率不低，时间视野定为短期。" if (vol or 0) > 30 else
                     "波动率中等，时间视野定为短期至中期。"))
    return f"**{state['direction']}** / 短期。{core}{horizon_note}"


@filler("投入判断")
def _fill_investment(data: Dict[str, Any], ctx: Dict[str, Any]) -> str:
    """这场会到底给出了一个能拿去做投入决策的结论，还是没有？

    读者读完「决议」知道方向、读完「置信度」知道把握，但仍然不知道**所以呢**。
    这一段就是那个"所以呢"：把方向、证据强度、分歧代价三样收成一个明确的
    门槛判断。段名与三档措辞由 ``committee_chair.json`` 约束，这里只负责按
    会议状态填 —— 配置里那句"第一句就是明确结论，三选一"是给 LLM 看的，
    这里必须给出同样的形状，否则换个后端报告结构就变了。

    **它是判断，不是指令。** 不说仓位、不说价位：那需要资金约束与风险预算，
    本系统没有这两样输入，给了就是编。

    档位刻意只有三档，不再细分。三档之间是可分辨的（有方向且过半 / 没有方向
    或少半数 / 置信度最低），再多就只能靠拍阈值——而"拍出来的阈值"和"模型编
    的数字"一样不可复核。
    """
    state = _chair_state(ctx)
    if state is None:
        return ("**证据不足，暂不构成投入理由。** 本次没有下级分析师的结论可供综合；"
                "主席不产生新数据，也不凭现有数据自行形成方向，因此无从判断是否值得投入。")

    direction = state["direction"]
    tally = state["tally"]
    n = len(state["peers"])
    adopted_n = len(state["adopted"])
    rejected_n = len(state["rejected"])
    abstain = state["abstain_count"]
    cap = state["cap"]
    #: 全体的置信度上限。只有在"全体无方向"时才用它 —— 那时没有支持方，
    #: 而各方自报的置信度问的是「状态描述可不可靠」，不是方向的可信度。
    cap_all = state["cap_all"]

    # 「没有方向」有两种成因，措辞必须分开：票数打平（双方都有票、相抵）
    # 与全体中性（压根没有方向性判断）。混成一句会让读者以为有人看多。
    all_neutral = (not state["tie"]) and direction == "NEUTRAL"
    no_direction = state["tie"] or all_neutral

    if state["tie"]:
        head = (f"**不值得投入** —— 看多与看空各 {tally['BULLISH']} 票，票数相同，"
                f"**没有形成多数方向**，无法支撑方向性投入决策。")
    elif all_neutral:
        head = (f"**不值得投入** —— {n} 位分析师的结论均为 NEUTRAL，"
                f"**没有方向性结论**，无法支撑方向性投入决策。")
    elif state["minority"]:
        head = (f"**不值得投入** —— 方向虽定为 {direction}，支持者却只有 {adopted_n} 位，"
                f"不足全体 {n} 位的一半。这是一个方向标签，不足以支撑一次投入决策。")
    elif cap == "LOW":
        head = (f"**不值得投入** —— 方向为 {direction}，但支持方自报置信度最高仅 LOW，"
                f"证据强度不足以支撑一次投入决策。")
    else:
        # 必须写明"与决议方向一致"，否则 BEARISH 行情下这句会被读成"建议买入" ——
        # 「值得投入」本身不带方向，而方向才是这份报告最要紧的信息。
        head = (f"**值得投入** —— 支持一次与决议方向（{direction}）一致的投入决策："
                f"{n} 位分析师中 {adopted_n} 位支持，支持方置信度上限 {cap}，"
                f"证据强度达到可执行门槛。")

    # 三条依据与配置里要求模型逐行写的三条一一对应。两个后端给出同形状的
    # 报告，读者换后端时不用换脑子。
    if no_direction:
        d1 = "方向：无。本次没有多数指向。"
    elif state["minority"]:
        d1 = f"方向：{direction}，但支持面不足半数（{adopted_n}/{n}）。"
    else:
        d1 = f"方向：{direction}，{adopted_n}/{n} 位支持，超过半数。"

    if state["tie"]:
        # 打平时没有任何人支持"决议方向"，cap 只是初始值。写成"支持方上限
        # LOW"读起来像"支持者很没把握"，而事实是**没有支持者**。
        d2 = "证据强度：没有任何分析师的结论与决议方向一致，主席无从继承信心。"
    elif all_neutral:
        d2 = (f"证据强度：各方给的都是「无方向」判断，其自报置信度最高 {cap_all}；"
              f"但那个置信度衡量的是「状态描述是否可靠」，**不是方向的可信度**。")
    elif cap == "LOW":
        d2 = "证据强度：支持方自报置信度为最低档 LOW，说明其判据方向分散、未形成合力。"
    else:
        d2 = f"证据强度：支持方置信度上限 {cap}，决议置信度不超过该值。"

    # rejected 里既有反向票也有 NEUTRAL 票，所以措辞用"与决议不一致的结论"，
    # 不用"方向相反"——后者会把一个明确写了 NEUTRAL 的委员说成反对方。
    if rejected_n and abstain:
        d3 = (f"分歧的代价：{rejected_n} 位给出了与决议不一致的结论、"
              f"{abstain} 位未对方向表态，两者都在削减支持面。")
    elif rejected_n:
        d3 = (f"分歧的代价：{rejected_n} 位给出了与决议不一致的结论。"
              f"其风险项已原样保留，执行者须自行承担与之相反的风险。")
    elif abstain:
        d3 = (f"分歧的代价：{abstain} 位未对方向表态（**弃权不是反对**），"
              f"本决议的实际支持面比票数看起来更窄。")
    else:
        d3 = ("分歧的代价：无反对方、无弃权。但各角色用的是同一份数据源，"
              "**结论一致不等于相互印证**。")

    if no_direction and state["tie"]:
        change = ("判断会变的条件：补到新信息（更长样本、第三方数据源）后重开会议，"
                  "或任一位分析师改变方向使票数分出多数。")
    elif all_neutral:
        change = ("判断会变的条件：有分析师给出方向性结论。当前全体中性，"
                  "是数据未指向，不是分歧。")
    elif state["minority"]:
        change = ("判断会变的条件：支持面扩到全体半数以上 —— 补齐缺席委员，"
                  "或任一位弃权者给出与决议同向的方向。")
    elif cap == "LOW":
        change = ("判断会变的条件：支持方上调置信度（需补出三项以上同向判据），"
                  "或出现方向相反但证据更强的结论。")
    else:
        change = (f"判断会变的条件：支持方置信度下调、出现与 {direction} 相反且"
                  f"置信度不低于 {cap} 的结论，或模型技能分转负 —— "
                  f"任一出现即应推翻本判断。")

    return "\n".join([head, f"- {d1}", f"- {d2}", f"- {d3}", change])


@filler("采纳的意见")
def _fill_adopted(data: Dict[str, Any], ctx: Dict[str, Any]) -> str:
    state = _chair_state(ctx)
    if state is None:
        return "无（本次没有下级结论）。"
    if not state["adopted"]:
        return ("无。各角色方向分歧且不构成多数，没有任何一方可作为决议依据被采纳。"
                "这是本次会议最重要的结论——**没有结论本身就是一个结论**。")
    lines = []
    for peer in state["adopted"]:
        name = peer.get("name", peer.get("role_id", "?"))
        conf = peer.get("confidence") or "未标注"
        lines.append(f"- **{name}**（{state['direction']} / {conf}）：方向与最终决议一致，"
                     f"判据引用的是本报告可复核的工具输出，予以采纳。")
    return "\n".join(lines)


@filler("否决的意见")
def _fill_rejected(data: Dict[str, Any], ctx: Dict[str, Any]) -> str:
    state = _chair_state(ctx)
    if state is None:
        return "无（本次没有下级结论）。"
    rejected = state["rejected"]
    abstained = state["abstained"]
    if not rejected and not abstained:
        return "无。全部下级结论的方向与最终决议一致，不存在被否决的结论。"

    if rejected:
        lines = ["以下结论的方向与最终决议不同，未被采纳为决议依据："]
        for peer in rejected:
            name = peer.get("name", peer.get("role_id", "?"))
            direction = peer.get("direction") or "未给出方向"
            conf = peer.get("confidence") or "未标注"
            if conf.upper() == "LOW":
                reason = ("其自报置信度为 LOW —— 依角色定义，LOW 表示判据方向分散、"
                          "未形成合力，不足以支撑方向性决策。")
            elif state["tie"]:
                reason = ("其方向与另一方票数相同，不构成多数。两份结论的证据强度不足以"
                          "分出高下，故本次不做取舍（见「分歧记录」）。")
            else:
                reason = (f"其方向为 {direction}，与最终决议 {state['direction']} 相反。"
                          f"**注意：否决只表示方向不一致，不表示该结论错误**——"
                          f"其列出的风险项已在「分歧记录」中原样保留。")
            lines.append(f"- **{name}**（{direction} / {conf}）：{reason}")
    else:
        lines = ["无。给出方向的下级结论全部与最终决议一致。"]

    if abstained:
        # 弃权不是反对。这里与「方向相反」分开写，因为把"没表态"写成
        # "与你相反"等于替对方编造立场——报告的可信度就毁在这类细节上。
        lines.append("")
        lines.append("以下结论**未给出方向，因此既不支持也不反对**本次决议，"
                     "其列出的风险项与状态描述仍全量保留：")
        for peer in abstained:
            name = peer.get("name", peer.get("role_id", "?"))
            conf = peer.get("confidence") or "未标注"
            reason = ("该角色按定义不产出方向（其职责是评估风险或状态），"
                      "因此不参与方向计票。**把它记为反对是错的**——"
                      "它只是没有对方向表态。其数据支撑的结论见其报告原文。")
            lines.append(f"- **{name}**（未给出方向 / {conf}）：{reason}")
    return "\n".join(lines)


@filler("分歧记录")
def _fill_disagreement(data: Dict[str, Any], ctx: Dict[str, Any]) -> str:
    state = _chair_state(ctx)
    if state is None:
        return "本次没有下级结论，不存在分歧。"
    peers = state["peers"]
    if len(peers) < 2:
        return "本次有效角色少于 2 个，不存在分歧。"
    tally = state["tally"]
    by_dir: Dict[str, List[str]] = {}
    for peer in peers:
        # 未给方向的不能默认填成 NEUTRAL —— 那会把"弃权"写成"看中性"。
        raw = (peer.get("direction") or "").upper()
        key = raw if raw in _DIRECTIONS else "未给出方向"
        by_dir.setdefault(key, []).append(peer.get("name", peer.get("role_id", "?")))

    # 只有"只出现过一种方向"才叫一致；含弃权时不算一致（弃权者没背书）。
    active = {k: v for k, v in tally.items() if v > 0}
    if len(active) <= 1 and not state["abstain_count"]:
        only = next(iter(active), "NEUTRAL")
        names = "、".join(p.get("name", p.get("role_id", "?")) for p in peers)
        return (f"各角色结论一致（均为 {only}）：{names}。"
                f"一致不等于正确——各角色使用的是同一份数据，"
                f"**同一数据源上的多个结论不构成相互印证**。")

    detail = "；".join(f"{d}：{'、'.join(ns)}" for d, ns in sorted(by_dir.items()))
    if state["tie"]:
        handling = ("处置：票数相同，**不通过加权或措辞把分歧抹平**，决议取 NEUTRAL。"
                    "两位分析师各自的风险项与证据强度已在上面原样保留，"
                    "需要方向性判断时请补充新信息（更长样本、第三方数据）后重开会议。")
    else:
        handling = (f"处置：按多数方向形成决议（{state['direction']}），"
                    f"但**保留分歧不做平滑处理**。反对方的关切已在上方"
                    f"「否决的意见」中原样记录，决议的执行者需自行承担"
                    f"与反对方结论相反的风险。")
    if state["abstain_count"]:
        handling += (f"\n另有 {state['abstain_count']} 位分析师未对方向表态"
                     f"（已在「否决的意见」中单列）。弃权**不被计入任何一方**，"
                     f"因此本决议的实际支持面比票数看起来更窄。")
    lead = "**存在实质分歧**" if len(active) > 1 else "**方向票数一边倒，但存在未表态者**"
    return f"{lead}——{detail}。\n{handling}"


# ── 后端实现 ──────────────────────────────────────────────────────


class RuleBasedProvider(LlmProvider):
    """按工具输出的真实数值渲染结构化研判。

    ``sections`` 来自角色配置的 ``output_sections``：**段落由配置决定，
    不由代码决定**。新增一个角色只要在 JSON 里声明段落，并确保段落名在
    :data:`FILLERS` 里有实现；没实现的段落会被如实标记为"本后端无法生成"，
    而不是悄悄省略——省略会让护栏误判输出完整。
    """

    name = "rule_based"

    def __init__(
        self,
        *,
        role_id: str,
        sections: Sequence[str],
        category: str = "",
        tool_order: Sequence[str] = (),
        tool_defaults: Optional[Dict[str, Dict[str, Any]]] = None,
        peers: Optional[Sequence[Dict[str, Any]]] = None,
    ) -> None:
        self.role_id = role_id
        self.sections = list(sections)
        self.category = category
        self.tool_order = list(tool_order)
        self.tool_defaults = dict(tool_defaults or {})
        self.peers = list(peers or [])

    def describe(self) -> Dict[str, Any]:
        d = super().describe()
        d["role_id"] = self.role_id
        d["category"] = self.category
        d["sections"] = list(self.sections)
        d["note"] = "确定性规则后端：不做自然语言推理，按工具输出的真实数值填充段落"
        return d

    def complete(
        self,
        messages: Sequence[Message],
        *,
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: float = 0.3,
        max_tokens: int = 2048,
    ) -> LlmResponse:
        collected = self._collect_tool_results(messages)

        # 第一轮：把该角色的工具都调一遍。规则后端的策略是"先把证据取全"，
        # 这既是确定性的，也符合配置里 max_tool_calls 的预算约束。
        if tools and not collected:
            calls: List[ToolCall] = []
            for spec in tools:
                fn = spec.get("function") or {}
                name = fn.get("name") or ""
                if not name:
                    continue
                calls.append(ToolCall(
                    id=f"rb_{len(calls)}_{name}",
                    name=name,
                    arguments=dict(self.tool_defaults.get(name, {})),
                ))
            return LlmResponse(tool_calls=calls, finish_reason="tool_calls",
                               model=self.name, usage=Usage())

        # 第二轮：渲染。
        text = self.render(collected)
        return LlmResponse(text=text, finish_reason="stop", model=self.name, usage=Usage())

    # ── 内部 ─────────────────────────────────────────────────
    @staticmethod
    def _collect_tool_results(messages: Sequence[Message]) -> Dict[str, Any]:
        """从 role == 'tool' 的消息里把各工具的输出还原成字典。

        解析失败的工具会在结果里留下一个 ``_error`` 键，渲染阶段据此
        说出"该工具没给出数据"，而不是当成空结果继续编。
        """
        out: Dict[str, Any] = {}
        for msg in messages:
            if msg.role != "tool" or not msg.name:
                continue
            if msg.name in out:
                # 同名工具只认第一次的结果：规则后端的策略是一轮取全证据，
                # 出现重复说明上层多调了一次，保留首个即可。
                continue
            try:
                out[msg.name] = json.loads(msg.content)
            except (json.JSONDecodeError, TypeError):
                out[msg.name] = {"_error": "工具返回的不是合法 JSON"}
        return out

    def render(self, data: Dict[str, Any]) -> str:
        ctx = {"peers": self.peers, "role_id": self.role_id, "category": self.category}
        blocks: List[str] = []
        for section in self.sections:
            fn = FILLERS.get(section)
            if fn is None:
                body = (f"（本后端没有为「{section}」段落编写规则实现，"
                        f"该段在 provider='rule_based' 下留空。"
                        f"切换到 LLM 后端后此段会由模型填写。）")
            else:
                try:
                    body = fn(data, ctx)
                except Exception as exc:  # 单段渲染失败不能拖垮整份报告
                    body = f"（渲染失败：{type(exc).__name__}: {exc}）"
            blocks.append(f"## {section}\n{body}")
        return "\n\n".join(blocks)

    # ── 给编排层用：从渲染结果里抽方向与置信度 ──
    DIRECTION_RE = re.compile(r"\b(BULLISH|BEARISH|NEUTRAL)\b")
    CONF_RE = re.compile(r"\b(HIGH|MEDIUM|LOW)\b")
    SECTION_RE = re.compile(r"^##\s*(.+?)\s*$", re.MULTILINE)

    @classmethod
    def parse_sections(cls, text: str) -> Dict[str, str]:
        """把 ``## 段名`` 结构的文本切成 {段名: 正文}。"""
        out: Dict[str, str] = {}
        matches = list(cls.SECTION_RE.finditer(text))
        for i, m in enumerate(matches):
            start = m.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
            out[m.group(1).strip()] = text[start:end].strip()
        return out
