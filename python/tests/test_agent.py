# -*- coding: utf-8 -*-
"""智能体层测试 —— 配置校验 / 工具注册表 / 输出契约 / 护栏 / 角色运行时。

这一份测试的重点不是"能跑通"，而是**把设计约束钉死**：

* 配置写错必须报错，不能静默用默认值顶上（少个 s 的 ``instruction``）。
* 工具失败必须变成结构化结果，不能让整轮分析崩掉。
* 降级必须可见（配了 openai 却没 key → 报告里要写清用了规则后端）。
* 方向抽取只在声明段落内进行，不能抽到"引述别人的方向"。
* 主席在平票时不能假装有结论。
"""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from finpulse_engine.agent import config as agent_config
from finpulse_engine.agent import schemas
from finpulse_engine.agent.guardrails import (
    ConfidencePresent,
    Guardrail,
    GuardrailContext,
    NumberProvenance,
    Pipeline,
    SectionCompleteness,
    summarize,
)
from finpulse_engine.agent.llm import LlmResponse, ToolCall, Usage
from finpulse_engine.agent.llm import registry as llm_registry
from finpulse_engine.agent.memory import Decision, DecisionMemory
from finpulse_engine.agent.roles import (
    CROSS_EXAM_PEER_LIMIT,
    PEER_TEXT_LIMIT,
    RoleRun,
    RoleRuntime,
    build_task_prompt,
    describe_intent,
    turn_budget,
)
from finpulse_engine.agent.tools import (
    DEFAULT_INDICATOR_SPECS,
    WIRE_NAME_MAX,
    WIRE_NAME_OK,
    ToolContext,
    ToolRegistry,
    ToolSpec,
    default_registry,
    to_wire_name,
)

# ── 测试夹具 ──────────────────────────────────────────────────────


def _synthetic_ctx(bars: int = 260, *, symbol: str = "TEST") -> ToolContext:
    """造一段可复现的行情。

    刻意用确定性函数（不是 random），这样每次跑测试拿到的指标数值完全一样，
    断言才能写死。
    """
    import math

    closes, highs, lows, volumes, ts = [], [], [], [], []
    for i in range(bars):
        drift = 0.0004 * i
        wave = 0.02 * math.sin(i / 11.0) + 0.008 * math.cos(i / 3.7)
        close = 100.0 * (1.0 + drift + wave)
        volumes.append(int(1_500_000 * (1.0 + 10.0 * abs(wave))))
        closes.append(round(close, 4))
        highs.append(round(close * 1.006, 4))
        lows.append(round(close * 0.994, 4))
        ts.append(1_700_000_000_000 + i * 86_400_000)
    return ToolContext(
        closes=closes, highs=highs, lows=lows, volumes=volumes,
        timestamps=ts, symbol=symbol,
        risk_free=0.0, forecast_method="ar", horizon=5,
        backtest_folds=3, backtest_min_train=60,
    )


def _forecaster_factory():
    from finpulse_engine.forecast import registry as fc
    return lambda: fc.create("ar")


def _inline_role(**over):
    """构造一份最小的合法角色配置（用于测校验与降级路径）。"""
    raw = {
        "id": "probe", "name": "探针角色", "description": "测试用",
        "category": "technical", "version": "1.0.0", "capabilities": [],
        "config": {
            "model": {"provider": "rule_based"},
            "instructions": "你是一个用于测试的角色。" * 10,
            "tools": ["indicators"],
            "output_schema": "market_view",
            "output_sections": ["趋势状态", "置信度"],
        },
    }
    raw.update(over)
    return raw


# ── 配置层 ────────────────────────────────────────────────────────


class TestRoleConfig(unittest.TestCase):
    def test_all_shipped_configs_load(self):
        """仓库里自带的角色配置必须全部合法。"""
        cfgs = agent_config.load_all()
        self.assertGreaterEqual(len(cfgs), 4)
        for role_id, cfg in cfgs.items():
            self.assertEqual(cfg.id, role_id)
            self.assertTrue(cfg.output_sections, f"{role_id} 没有声明输出段落")
            self.assertGreater(len(cfg.instructions), 80,
                               f"{role_id} 的 instructions 太短，无法承载约束")

    def test_shipped_roles_declare_tools_that_exist(self):
        """配置里声明的工具必须真的注册过 —— 写错工具名不能被静默忽略。"""
        reg = default_registry()
        for role_id, cfg in agent_config.load_all().items():
            missing = reg.missing(cfg.tools)
            self.assertFalse(missing, f"{role_id} 声明了不存在的工具 {missing}")

    def test_unknown_field_rejected(self):
        """未知字段必须报错。写错 ``instruction``（少个 s）不能被忽略。"""
        raw = _inline_role()
        raw["config"]["instruction"] = "少了个 s"   # 故意写错
        with self.assertRaises(agent_config.ConfigError) as ctx:
            agent_config.parse_config(raw, source="<inline>")
        self.assertIn("instruction", str(ctx.exception))

    def test_unknown_top_level_field_rejected(self):
        raw = _inline_role()
        raw["configx"] = {}
        with self.assertRaises(agent_config.ConfigError) as ctx:
            agent_config.parse_config(raw, source="<inline>")
        self.assertIn("configx", str(ctx.exception))

    def test_short_instructions_rejected(self):
        raw = _inline_role()
        raw["config"]["instructions"] = "太短了"
        with self.assertRaises(agent_config.ConfigError) as ctx:
            agent_config.parse_config(raw, source="<inline>")
        self.assertIn("instructions", str(ctx.exception))

    def test_missing_confidence_section_rejected(self):
        """非 risk 角色必须声明"置信度"段，否则下游无法抽取。"""
        raw = _inline_role()
        raw["config"]["output_sections"] = ["趋势状态", "关键位"]
        with self.assertRaises(agent_config.ConfigError) as ctx:
            agent_config.parse_config(raw, source="<inline>")
        self.assertIn("置信度", str(ctx.exception))

    def test_risk_schema_may_omit_confidence(self):
        """risk_assessment 契约用「模型可信度」承担同样职责，允许不写「置信度」。"""
        raw = _inline_role()
        raw["config"]["output_schema"] = "risk_assessment"
        raw["config"]["output_sections"] = ["下行风险", "模型可信度"]
        cfg = agent_config.parse_config(raw)
        self.assertNotIn("置信度", cfg.output_sections)

    def test_duplicate_sections_rejected(self):
        raw = _inline_role()
        raw["config"]["output_sections"] = ["趋势状态", "趋势状态", "置信度"]
        with self.assertRaises(agent_config.ConfigError):
            agent_config.parse_config(raw)

    # ── 方向来源段落 ─────────────────────────────────────────────
    def test_direction_scope_defaults_to_conclusion_section(self):
        """不声明 direction_sections 时，只认第一段。"""
        cfg = agent_config.parse_config(_inline_role())
        self.assertEqual(cfg.direction_scope, ["趋势状态"])

    def test_direction_sections_declared_are_honoured(self):
        raw = _inline_role()
        raw["config"]["output_sections"] = ["趋势状态", "关键位", "置信度"]
        raw["config"]["direction_sections"] = ["关键位"]
        cfg = agent_config.parse_config(raw)
        self.assertEqual(cfg.direction_scope, ["关键位"])

    def test_direction_sections_must_be_own_sections(self):
        """方向来源必须是本角色声明的段落 —— 否则就是"从别人的话里抄方向"。"""
        raw = _inline_role()
        raw["config"]["direction_sections"] = ["风险官的反对意见"]
        with self.assertRaises(agent_config.ConfigError) as ctx:
            agent_config.parse_config(raw, source="<inline>")
        self.assertIn("direction_sections", str(ctx.exception))

    def test_chair_declares_an_investment_verdict(self):
        """主席必须有一段正面回答「这次到底值不值得投入」。

        读者读完「决议」知道方向、读完「置信度」知道把握，仍然不知道**所以呢**。
        没有这一段，报告就是一份把判断工作推回给读者的会议纪要——各段都正确，
        却没有任何一处可以拿去做决策。
        """
        cfg = agent_config.load_all()["committee_chair"]
        self.assertIn("投入判断", cfg.output_sections)
        # 紧跟「决议」：答案要出现在读者开始跳读之前，而不是压在整篇论证之后。
        self.assertEqual(cfg.output_sections[:2], ["决议", "投入判断"])
        # 三档措辞写死在指令里。留给模型自由发挥，它就会写成
        # 「建议关注」这类没有操作含义的词——那正是这段要消灭的东西。
        self.assertIn("## 投入判断", cfg.instructions)
        for level in ("**值得投入**", "**不值得投入**", "**证据不足"):
            self.assertIn(level, cfg.instructions,
                          f"主席指令里没有写死「{level}」这一档")

    def test_shipped_roles_declare_a_direction_scope(self):
        """每个内置角色的方向来源都必须是它自己的段落，且非空。"""
        for role_id, cfg in agent_config.load_all().items():
            scope = cfg.direction_scope
            self.assertTrue(scope, f"{role_id} 的方向来源段落为空")
            for section in scope:
                self.assertIn(section, cfg.output_sections,
                              f"{role_id} 的方向来源 {section} 不在它自己的段落里")

    def test_shipped_direction_scope_is_serialized(self):
        cfg = agent_config.load_all()["committee_chair"]
        self.assertIn("direction_sections", cfg.to_json())
        self.assertEqual(cfg.to_json()["direction_sections"], cfg.direction_scope)

    # ── 契约一致性（防止"声明了但没人用"） ────────────────────────
    #
    # 这一组是**预防性**的：量价分析师曾经声明了三段却拿不到 flow 工具、
    # 三个角色曾经声明了 terminal.* 工具却没有任何代码消费它的输出。
    # 两次都是"配置说了一件事、代码在做另一件事"，而单侧的单元测试全绿。

    def test_instructions_document_every_declared_section(self):
        """提示词里必须写出每一个声明的段落。

        程序按 ``output_sections`` 校验输出完整性。如果提示词没告诉模型要写
        哪几段，换成 LLM 后端时模型就会漏段，护栏报"缺段"，而根因是配置里
        两处描述不同步 —— 规则后端下这个问题看不出来。
        """
        for role_id, cfg in agent_config.load_all().items():
            for section in cfg.output_sections:
                self.assertIn(
                    f"## {section}", cfg.instructions,
                    f"{role_id} 的 instructions 没有写「## {section}」这一段，"
                    f"但 output_sections 里声明了它",
                )

    def test_roles_declaring_terminal_tools_have_terminal_section(self):
        """声明了 terminal.* 工具的角色，必须有一段专门写终端证据。

        工具调用链通了但报告里一个字都不提，等于这条反向通道只存在于调试
        日志里 —— 用户看不到"这份结论用到了终端此刻的状态"。
        """
        for role_id, cfg in agent_config.load_all().items():
            terminal_tools = [t for t in cfg.tools if t.startswith("terminal.")]
            if not terminal_tools:
                continue
            self.assertIn(
                "终端证据", cfg.output_sections,
                f"{role_id} 声明了 {terminal_tools} 却没有「终端证据」段落，"
                f"工具结果拿回来了也没地方用",
            )

    def test_panel_member_serialization_uses_config_key(self):
        """panel 成员的键名必须与配置 JSON 里人写的那份**同名**。

        曾经这里发的是 ``role_id``（Python 属性名），而配置文件写 ``role``；
        C++ 侧照配置文件解析，于是委员列表解析成空、界面一片空白。
        跨语言契约只能有一个名字。
        """
        panel = agent_config.load_panels()["default_committee"]
        members = panel.to_json()["members"]
        self.assertTrue(members)
        for m in members:
            self.assertIn("role", m)
            self.assertNotIn("role_id", m)
            self.assertIn(m["role"], panel.role_ids)

    def test_network_provider_needs_model_id(self):
        """声明了需要联网的 provider，就必须给出 model_id。"""
        raw = _inline_role()
        raw["config"]["model"] = {"provider": "openai"}   # 没有 model_id
        with self.assertRaises(agent_config.ConfigError) as ctx:
            agent_config.parse_config(raw, source="<inline>")
        self.assertIn("model_id", str(ctx.exception))

    def test_bad_category_rejected(self):
        raw = _inline_role(category="magic")
        with self.assertRaises(agent_config.ConfigError) as ctx:
            agent_config.parse_config(raw, source="<inline>")
        self.assertIn("category", str(ctx.exception))

    def test_bool_is_not_a_number(self):
        """True 不能被当成 1.0 悄悄通过。"""
        raw = _inline_role()
        raw["config"]["model"] = {"provider": "rule_based", "temperature": True}
        with self.assertRaises(agent_config.ConfigError):
            agent_config.parse_config(raw)

    def test_select_roles_reports_missing_id(self):
        cfgs = agent_config.load_all()
        with self.assertRaises(agent_config.ConfigError) as ctx:
            agent_config.select_roles(cfgs, ["nope"])
        self.assertIn("nope", str(ctx.exception))


# ── 工具层 ────────────────────────────────────────────────────────


class TestToolRegistry(unittest.TestCase):
    def test_default_registry_names(self):
        reg = default_registry()
        names = reg.names()
        for expected in ("indicators", "stats", "flow", "forecast", "backtest"):
            self.assertIn(expected, names)
        # 远程工具一律带 terminal. 前缀，这条约定要在测试里钉住。
        remote = [n for n in names if n.startswith("terminal.")]
        self.assertEqual(len(remote), 3)

    def test_unknown_tool_is_reported_not_raised(self):
        reg = default_registry()
        self.assertEqual(reg.missing(["indicators", "nope"]), ["nope"])
        result = reg.call("nope", {}, _synthetic_ctx())
        self.assertFalse(result["available"])
        self.assertIn("nope", result["reason"])

    def test_handler_exception_becomes_result(self):
        """工具里抛异常必须变成结构化结果，而不是向上抛。"""
        reg = ToolRegistry()
        reg.register(ToolSpec(
            name="boom", description="d", properties={}, required=[],
            handler=lambda ctx, args: (_ for _ in ()).throw(RuntimeError("炸了")),
        ))
        result = reg.call("boom", {}, _synthetic_ctx())
        self.assertFalse(result["available"])
        self.assertIn("RuntimeError", result["error"])

    def test_bad_argument_range_is_a_result(self):
        reg = default_registry()
        result = reg.call("flow", {"window": 9999}, _synthetic_ctx())
        self.assertFalse(result["available"])

    def test_indicators_returns_named_latest_values(self):
        """工具层把 250 长度的数组改写成"最新值 + 显式命名"。"""
        reg = default_registry()
        ctx = _synthetic_ctx()
        result = reg.call("indicators", {"specs": list(DEFAULT_INDICATOR_SPECS)}, ctx)
        self.assertTrue(result["available"])
        self.assertEqual(result["bars"], ctx.bars)
        self.assertIn("ma", result)
        self.assertIn("macd", result)
        # 区间最近 5 根收盘价要在，模型靠它判断方向变化。
        self.assertEqual(len(result["recent_closes"]), 5)
        # 单线指标用线名做顶层键。
        self.assertIn("rsi14", result)

    def test_stats_carries_price_extremes(self):
        """模块只管收益率，价格极值由工具层补上 —— 否则说不出"关键位"。"""
        reg = default_registry()
        ctx = _synthetic_ctx()
        result = reg.call("stats", {}, ctx)
        self.assertTrue(result["available"])
        self.assertAlmostEqual(result["max_high"], max(ctx.highs), places=3)
        self.assertAlmostEqual(result["min_low"], min(ctx.lows), places=3)

    def test_flow_returns_volume_ratio(self):
        reg = default_registry()
        result = reg.call("flow", {}, _synthetic_ctx())
        self.assertTrue(result["available"])
        self.assertIn("volume_ratio", result)
        self.assertIn("volatility_regime", result)

    def test_remote_tool_without_terminal_is_graceful(self):
        """没有终端（NullToolClient）是正常状态，要给出明确原因。"""
        reg = default_registry()
        result = reg.call("terminal.bus_stats", {}, _synthetic_ctx())
        self.assertFalse(result["available"])
        self.assertTrue(result.get("reason"))

    def test_forecast_without_factory_is_reported(self):
        reg = default_registry()
        ctx = _synthetic_ctx()
        self.assertIsNone(ctx.forecaster_factory)
        result = reg.call("forecast", {}, ctx)
        self.assertFalse(result["available"])
        self.assertIn("预测器", result["reason"])

    def test_backtest_refuses_when_sample_too_small(self):
        reg = default_registry()
        ctx = _synthetic_ctx(bars=40)
        ctx.forecaster_factory = _forecaster_factory()
        result = reg.call("backtest", {"folds": 5, "horizon": 5, "min_train": 60}, ctx)
        self.assertFalse(result["available"])
        self.assertIn("样本", result["reason"])

    def test_backtest_skill_is_reported(self):
        """技能分是整条决策链的地基，必须真的算得出来。"""
        reg = default_registry()
        ctx = _synthetic_ctx()
        ctx.forecaster_factory = _forecaster_factory()
        result = reg.call("backtest", {}, ctx)
        self.assertTrue(result["available"], msg=str(result))
        self.assertIn("skill", result)


class TestToolWireNames(unittest.TestCase):
    """注册表里的名字 → 真正发到网上去的名字。

    背景是一个真实故障：接上 DeepSeek 跑投委会，三个委员全部"未产出结论"，
    报告里写 ``Invalid 'tools[2].function.name'`` —— ``tools[2]`` 就是
    ``terminal.live_quote``，点号不在协议允许的 ``[a-zA-Z0-9_-]`` 里。
    服务端因此**拒掉整次请求**，不是"某个工具用不了"，是那一场研判全废。

    修法是边界上翻译：注册表保留点号（人类读配置一眼分辨本地/远程，
    C++ 桥也一直这么叫），发给模型时换成下划线，回程再翻回来。
    这一个类把这条约定钉死。
    """

    def test_每个工具都能生成合法的线上名(self):
        reg = default_registry()
        for name in reg.names():
            wire = to_wire_name(name)
            self.assertRegex(wire, WIRE_NAME_OK,
                             msg=f"'{name}' 的线上名 '{wire}' 发不出去")

    def test_远程工具名的点号换成下划线(self):
        self.assertEqual(to_wire_name("terminal.live_quote"),
                         "terminal_live_quote")
        # 本地工具本来就是合法标识符，不该被改动。
        self.assertEqual(to_wire_name("indicators"), "indicators")

    def test_目录里发出去的是线上名_展示用的是原名(self):
        """两个入口的名字**必须**不一样，各有各的用途。

        ``catalog()`` 用原名：``api.get_role`` 靠 ``function.name`` 反查 spec，
        界面也照原样显示给用户。``wire_catalog()`` 用线上名：那是真正
        要 POST 出去的字段。
        """
        reg = default_registry()
        shown = [c["function"]["name"] for c in reg.catalog(["terminal.live_quote"])]
        sent = [c["function"]["name"]
                for c in reg.wire_catalog(["terminal.live_quote"]).schemas]
        self.assertEqual(shown, ["terminal.live_quote"])
        self.assertEqual(sent, ["terminal_live_quote"])

    def test_回程翻译把线上名翻回注册表名(self):
        reg = default_registry()
        wire = reg.wire_catalog(["indicators", "terminal.bus_stats"])
        self.assertEqual(wire.resolve("terminal_bus_stats"), "terminal.bus_stats")
        self.assertEqual(wire.resolve("indicators"), "indicators")

    def test_认不出的名字原样返回(self):
        """模型编了个工具名时，要让它走到 ``call()`` 的"未知工具"分支。

        在这里静默丢掉的话，一次幻觉调用会表现成"这一轮什么都没调"，
        轨迹里看不出发生过什么。
        """
        reg = default_registry()
        wire = reg.wire_catalog(["indicators"])
        self.assertEqual(wire.resolve("make_me_rich"), "make_me_rich")
        result = reg.call(wire.resolve("make_me_rich"), {}, _synthetic_ctx())
        self.assertFalse(result["available"])
        self.assertIn("未知工具", result["reason"])

    def test_两个工具撞同一个线上名时报错(self):
        """``a.b`` 与 ``a_b`` 会撞成 ``a_b`` —— 发出去就分不清该调哪个了。

        这是**配置错误**，不是运行期偶发，所以要在构造目录时当场抛，
        而不是等到某次研判的 HTTP 400 里才暴露。
        """
        reg = ToolRegistry()
        for name in ("a.b", "a_b"):
            reg.register(ToolSpec(name=name, description="d", properties={},
                                  required=[], handler=lambda c, a: {}))
        with self.assertRaises(ValueError) as ctx:
            reg.wire_catalog(["a.b", "a_b"])
        self.assertIn("a.b", str(ctx.exception))
        self.assertIn("a_b", str(ctx.exception))

    def test_超长工具名会报错(self):
        reg = ToolRegistry()
        long_name = "x" * (WIRE_NAME_MAX + 1)
        reg.register(ToolSpec(name=long_name, description="d", properties={},
                              required=[], handler=lambda c, a: {}))
        with self.assertRaises(ValueError) as ctx:
            reg.wire_catalog([long_name])
        self.assertIn(str(WIRE_NAME_MAX), str(ctx.exception))

    def test_重复声明同一个工具只发一条(self):
        reg = default_registry()
        wire = reg.wire_catalog(["indicators", "indicators"])
        self.assertEqual([c["function"]["name"] for c in wire.schemas], ["indicators"])

    def test_配置里所有角色的工具都能转成线上名(self):
        """拿真实配置跑一遍 —— 谁往角色里加了个名字不合法的工具，这里就红。"""
        reg = default_registry()
        for role_id, cfg in agent_config.load_all().items():
            try:
                reg.wire_catalog(cfg.tools)
            except ValueError as exc:  # pragma: no cover - 只在配置写错时走到
                self.fail(f"角色 {role_id} 的工具发不出去：{exc}")


# ── 输出契约 ──────────────────────────────────────────────────────


class TestSchemas(unittest.TestCase):
    SECTIONS = ["趋势状态", "关键位", "动量", "置信度"]

    def test_parse_sections(self):
        text = (
            "## 趋势状态\n价格在均线上方，方向 BULLISH。\n"
            "## 关键位\n上方压力 120，下方支撑 100。\n"
            "## 动量\nRSI 为 58，MACD 柱为正。\n"
            "## 置信度\nMEDIUM\n"
        )
        parsed = schemas.parse_sections(text)
        self.assertEqual(len(parsed), 4)
        self.assertIn("BULLISH", parsed["趋势状态"])

    def test_parse_sections_strips_bold_title(self):
        """有些模型会写 "## **趋势状态**"，标题要去掉粗体包装才对得上。"""
        parsed = schemas.parse_sections("## **趋势状态**\nBULLISH\n")
        self.assertIn("趋势状态", parsed)

    def test_direction_only_from_declared_sections(self):
        """方向只能来自声明的结论段，不能抽到"引述别人的方向"。"""
        text = (
            "## 趋势状态\n本角色结论：BEARISH。\n"
            "## 关键位\n风险官此前给出 BULLISH，但我认为其判据不足。\n"
            "## 动量\nMACD 柱转负。\n"
            "## 置信度\nHIGH\n"
        )
        sections = schemas.parse_sections(text)
        # 只把第一段声明为方向来源。
        direction = schemas.extract_direction(text, sections, ["趋势状态"])
        self.assertEqual(direction, "BEARISH")

    def test_direction_order_matters(self):
        """多段都含方向标签时，取声明顺序里第一个。"""
        text = (
            "## 决议\nNEUTRAL\n"
            "## 分歧记录\n技术面 BULLISH，风险面 BEARISH。\n"
            "## 置信度\nMEDIUM\n"
        )
        sections = schemas.parse_sections(text)
        d = schemas.extract_direction(text, sections, ["决议", "分歧记录"])
        self.assertEqual(d, "NEUTRAL")

    def test_direction_chinese_fallback(self):
        """技术面角色习惯写"上升/下降"，要能兜底成方向标签。"""
        text = "## 趋势状态\n**上升**。判据：均线多头排列。\n## 置信度\nHIGH\n"
        sections = schemas.parse_sections(text)
        self.assertEqual(schemas.extract_direction(text, sections, ["趋势状态"]),
                         "BULLISH")

    def test_confidence_extraction_prefers_confidence_section(self):
        """「未达 HIGH」这类解释文字不能被误当成置信度。"""
        text = ("## 趋势状态\nBULLISH\n"
                "## 置信度\n**MEDIUM** —— 未达 HIGH 的门槛。\n")
        self.assertEqual(schemas.extract_confidence(text), "MEDIUM")

    def test_numbers_in_text_keeps_sign(self):
        nums = schemas.numbers_in_text("涨了 3.14%，回撤 -2.55%")
        self.assertIn(3.14, nums)
        self.assertIn(-2.55, nums)

    def test_number_pool_is_flat_and_skips_field_names(self):
        """数字池必须能在任意嵌套的工具结果里找到出处。"""
        result = {"ma": {"ma5": 101.25}, "macd": {"macd": -0.5}}
        pool = schemas.number_pool(result)
        self.assertIn(101.25, pool)
        self.assertIn(-0.5, pool)

    def test_dates_are_not_treated_as_numbers(self):
        """2026-09-30 会被拆成 2026 / 09 / 30 三个"找不到出处的数字"。"""
        nums = schemas.numbers_in_text("截至 2026-09-30，价格为 100。")
        self.assertNotIn(2026.0, nums)
        self.assertIn(100.0, nums)

    def test_check_output_reports_missing_and_empty(self):
        text = "## 趋势状态\nBULLISH\n## 关键位\n\n## 置信度\nHIGH\n"
        check = schemas.check_output(text, self.SECTIONS)
        self.assertIn("动量", check.missing_sections)
        self.assertIn("关键位", check.empty_sections)
        self.assertFalse(check.complete)

    def test_check_output_ok(self):
        # 每段正文都要超过 8 个字符，否则会被判成"段落内容过短"。
        text = (
            "## 趋势状态\nBULLISH，均线多头排列\n"
            "## 关键位\n上方压力 120.5，下方支撑 100.2\n"
            "## 动量\nRSI 为 58.3，MACD 柱为正\n"
            "## 置信度\nMEDIUM，两条判据一致\n"
        )
        check = schemas.check_output(text, self.SECTIONS)
        self.assertTrue(check.complete, msg=str(check.to_json()))
        self.assertEqual(check.direction, "BULLISH")
        self.assertEqual(check.confidence, "MEDIUM")

    # ── 方向来源段落（回归防护） ──────────────────────────────────
    #
    # 这一组是为了钉死一个**反复复发**的缺陷：``check_output`` 早期把全部
    # output_sections 都当成方向来源，于是风险官「反对意见」段里引述的
    # BULLISH 被当成它自己的立场 —— 弃权被误算成看多。第一次是"第一段没
    # 方向标签所以往后扫"，第二轮交叉质证之后它真的开始引用别人的结论了，
    # 于是同一个 bug 换条路又回来了一次。

    def test_direction_scope_defaults_to_first_section(self):
        """不声明时应只认第一段（结论段）。"""
        text = (
            "## 趋势状态\n本角色没有对方向表态。\n"
            "## 关键位\n参考风险官此前的 BULLISH。\n"
            "## 动量\n量能中性。\n"
            "## 置信度\nHIGH\n"
        )
        check = schemas.check_output(text, self.SECTIONS)
        self.assertIsNone(check.direction,
                          msg="引述别人的方向被当成了自己的立场")
        self.assertEqual(check.direction_sections, ["趋势状态"])

    def test_direction_scope_excludes_quoting_section(self):
        """风险官式布局：前三段讲风险，第四段引述别人的方向。"""
        text = (
            "## 下行风险\n最大回撤 12.4%，尾部风险中等。\n"
            "## 尾部损失\nCVaR 为 -3.8%。\n"
            "## 模型可信度\nMEDIUM，技能分接近 0。\n"
            "## 反对意见\n"
            "- 技术面分析师：给出 BULLISH，但未说明样本外表现。\n"
            "- 量价分析师：给出 NEUTRAL。\n"
        )
        sections = ["下行风险", "尾部损失", "模型可信度", "反对意见"]
        check = schemas.check_output(text, sections, direction_sections=["下行风险"])
        self.assertIsNone(check.direction, msg="反对意见段的引述不应成为方向")
        # 只允许"下行风险"段承载方向，哪怕它是空的。
        self.assertEqual(check.direction_sections, ["下行风险"])

    def test_direction_scope_can_be_widened_explicitly(self):
        """显式放宽到两段时，第二段里的方向就合法了。"""
        text = (
            "## 量能状态\n量价配合，日线看多。\n"
            "## 波动率状态\nBULLISH，波动率处于低位。\n"
            "## 置信度\nHIGH\n"
        )
        check = schemas.check_output(
            text, ["量能状态", "波动率状态", "置信度"],
            direction_sections=["量能状态", "波动率状态"],
        )
        self.assertEqual(check.direction, "BULLISH")
        self.assertEqual(check.direction_sections, ["量能状态", "波动率状态"])

    def test_direction_from_is_serialized(self):
        """留档：事后要能复盘"这个方向是从哪个段抽出来的"。"""
        check = schemas.check_output("## 趋势状态\nBULLISH，均线多头\n",
                                     ["趋势状态"], direction_sections=["趋势状态"])
        self.assertEqual(check.to_json()["direction_from"], ["趋势状态"])


# ── 护栏 ──────────────────────────────────────────────────────────


class TestRuleBackendUnits(unittest.TestCase):
    """规则后端的**单位**。这类 bug 不会让任何东西崩溃，只是把数字写错 100 倍。"""

    def test_波动率按小数渲染成百分数(self):
        """``flow.volatility_regime`` 里的比率是**小数**（0.1531 = 15.31%）。

        曾经这四处直接套了给"已是百分数"的值用的格式化函数，于是报告里
        出现「年化波动率 0.15%」—— 一个懂行的人看到这个数字就会开始怀疑
        整份报告。同时因为写进文本的 22.09 与工具输出里的 0.2209 对不上，
        护栏还会每次误报「22.09% 无法溯源」。
        """
        from finpulse_engine.agent.llm.rule_based import _fill_vol_regime

        data = {"flow": {"volatility_regime": {
            "state": "偏低", "current": 0.1531, "percentile": 0.2209,
            "min": 0.0537, "max": 0.4135, "samples": 200,
        }}}
        text = _fill_vol_regime(data, {})
        self.assertIn("15.31%", text)
        self.assertIn("22.09%", text)
        self.assertIn("5.37%", text)
        self.assertIn("41.35%", text)
        # 少乘 100 的原症状
        self.assertNotIn("0.15%", text)
        self.assertNotIn("0.22%", text)

    def test_波动率样本不足时不编数字(self):
        from finpulse_engine.agent.llm.rule_based import _fill_vol_regime

        text = _fill_vol_regime(
            {"flow": {"volatility_regime": {"state": None, "note": "窗口太少"}}}, {})
        self.assertIn("样本不足", text)
        self.assertNotIn("%", text)

    # ── 投入判断的三档 ────────────────────────────────────────────
    #
    # 这一段是"这场会到底给出了什么能拿去做决策的结论"的正面回答。档位必须
    # 与会议状态严格对应：把「值得投入」发给一场连方向都没定出来的会，
    # 比不写这一段更糟 —— 它把"信息不足"包装成了结论。

    @staticmethod
    def _peers(*spec):
        return [{"role_id": r, "name": n, "direction": d, "confidence": c}
                for r, n, d, c in spec]

    def test_投入判断_方向过半且置信度够(self):
        from finpulse_engine.agent.llm.rule_based import _fill_investment

        peers = self._peers(
            ("technical_analyst", "技术面分析师", "BULLISH", "HIGH"),
            ("flow_analyst", "量价分析师", "BULLISH", "MEDIUM"),
            ("risk_officer", "风险官", "BEARISH", "MEDIUM"),
        )
        text = _fill_investment({}, {"peers": peers})
        self.assertTrue(text.startswith("**值得投入**"), text)
        # 反向证据必须写出来，不能只说"值得"
        self.assertIn("分歧的代价", text)
        self.assertIn("判断会变的条件", text)

    def test_投入判断_看空方向必须写明投的是什么(self):
        """BEARISH 时报「值得投入」，不写方向就会被读成「建议买入」。

        「值得投入」这四个字本身不带方向。第一版就是这么写的，实跑看空报告
        时读起来像在劝人买 —— 而方向恰恰是这份报告最要紧的信息。
        """
        from finpulse_engine.agent.llm.rule_based import _fill_investment

        peers = self._peers(
            ("technical_analyst", "技术面分析师", "BEARISH", "HIGH"),
            ("flow_analyst", "量价分析师", "BEARISH", "HIGH"),
            ("risk_officer", "风险官", "BEARISH", "MEDIUM"),
        )
        text = _fill_investment({}, {"peers": peers})
        self.assertIn("**值得投入**", text)
        self.assertIn("与决议方向（BEARISH）一致", text)

    def test_投入判断_少数派不算值得投入(self):
        """支持者不足半数时，方向标签成立但结论不成立。

        这正是本项目最该防的那种报告：五段齐全、方向明确、看起来很有底气，
        而实际上只有一位分析师支撑它。
        """
        from finpulse_engine.agent.llm.rule_based import _fill_investment

        peers = self._peers(
            ("technical_analyst", "技术面分析师", "BEARISH", "HIGH"),
            ("flow_analyst", "量价分析师", "NEUTRAL", "MEDIUM"),
            ("risk_officer", "风险官", None, "HIGH"),
        )
        text = _fill_investment({}, {"peers": peers})
        self.assertTrue(text.startswith("**不值得投入**"), text)
        self.assertIn("不足全体 3 位的一半", text)

    def test_投入判断_票数打平不算值得投入(self):
        from finpulse_engine.agent.llm.rule_based import _fill_investment

        peers = self._peers(
            ("technical_analyst", "技术面分析师", "BULLISH", "HIGH"),
            ("flow_analyst", "量价分析师", "BEARISH", "HIGH"),
            ("risk_officer", "风险官", None, "MEDIUM"),
        )
        text = _fill_investment({}, {"peers": peers})
        self.assertTrue(text.startswith("**不值得投入**"), text)
        self.assertIn("没有形成多数方向", text)
        # 打平时没有任何人支持"决议方向"，不能写成"支持方置信度上限 LOW"——
        # 那读起来像"支持者很没把握"，而事实是没有支持者。
        self.assertNotIn("支持方置信度上限", text)

    def test_投入判断_全体中性不算值得投入(self):
        """全员 NEUTRAL 是**没有方向**，不是"高置信度地认为中性"。

        这里有个只靠"置信度高低"判断就会踩的坑：三位分析师都自报 HIGH，
        按置信度看是最强的一档，可他们谁都没给出方向。旧写法会把它判成
        「值得投入」——一个凭空出现的结论。
        """
        from finpulse_engine.agent.llm.rule_based import _fill_investment

        peers = self._peers(
            ("technical_analyst", "技术面分析师", "NEUTRAL", "HIGH"),
            ("flow_analyst", "量价分析师", "NEUTRAL", "HIGH"),
            ("risk_officer", "风险官", "NEUTRAL", "MEDIUM"),
        )
        text = _fill_investment({}, {"peers": peers})
        self.assertTrue(text.startswith("**不值得投入**"), text)
        self.assertIn("没有方向性结论", text)

    def test_投入判断_没有下级结论时不给档位(self):
        from finpulse_engine.agent.llm.rule_based import _fill_investment

        text = _fill_investment({}, {"peers": []})
        self.assertIn("证据不足", text)
        self.assertNotIn("**值得投入**", text)


class TestGuardrails(unittest.TestCase):
    def _ctx(self, text, sections, role=None, **kw):
        check = schemas.check_output(text, sections)
        return GuardrailContext(
            role=role, text=text, check=check,
            tool_results=kw.get("tool_results", {}),
            tool_calls=kw.get("tool_calls", []),
            prompt_text=kw.get("prompt_text", ""),
            provider=kw.get("provider", "rule_based"),
            fallback_reason=kw.get("fallback_reason"),
        )

    def test_section_completeness_is_error(self):
        sections = ["趋势状态", "关键位", "置信度"]
        role = agent_config.parse_config(_inline_role())
        gctx = self._ctx("## 趋势状态\nBULLISH\n", sections, role=role)
        issues = SectionCompleteness().check(gctx)
        self.assertTrue(any(i.severity == "error" for i in issues))

    def test_chair_missing_investment_verdict_is_error(self):
        """主席漏写「投入判断」必须判 error，不能悄悄放行。

        这一段的存在意义就是正面回答值不值得投入。一旦它可以缺省，
        需求就退回原点：报告照样读完，照样说不出所以然。
        """
        cfg = agent_config.load_all()["committee_chair"]
        text = "## 决议\nBEARISH / 中期。\n" + "".join(
            f"## {s}\n" + "内容足够长的一段话\n"
            for s in cfg.output_sections if s != "投入判断")
        issues = Pipeline().run(self._ctx(text, cfg.output_sections, role=cfg))
        self.assertTrue(Pipeline.has_errors(issues), "缺段没有被判 error")
        detail = " ".join(i.detail for i in issues)
        self.assertIn("投入判断", detail)

    def test_confidence_present_is_error(self):
        sections = ["趋势状态", "置信度"]
        role = agent_config.parse_config(_inline_role())
        gctx = self._ctx("## 趋势状态\nBULLISH\n## 置信度\n\n", sections, role=role)
        issues = ConfidencePresent().check(gctx)
        self.assertTrue(any(i.severity == "error" for i in issues))

    def test_confidence_present_skips_risk_schema(self):
        """风险官没有「置信度」段，这条护栏不该对它报错。"""
        sections = ["下行风险", "模型可信度"]
        role = agent_config.parse_config(_inline_role(**{
            "config": {
                "model": {"provider": "rule_based"},
                "instructions": "你是一个用于测试的风险角色。" * 8,
                "tools": ["stats"],
                "output_schema": "risk_assessment",
                "output_sections": sections,
            }
        }))
        gctx = self._ctx("## 下行风险\n回撤 -30%\n## 模型可信度\n技能分为负\n",
                         sections, role=role)
        self.assertEqual(ConfidencePresent().check(gctx), [])

    def test_number_provenance_flags_fabricated_number(self):
        """报告里 999.99 在工具结果里找不到出处 → 必须报警。"""
        sections = ["趋势状态", "置信度"]
        text = "## 趋势状态\n价格将涨到 999.99。\n## 置信度\nHIGH\n"
        gctx = self._ctx(text, sections, tool_results={"stats": {"last": 100.0}})
        issues = NumberProvenance().check(gctx)
        # 数字出现在 detail 里（message 只给条数），两处都算命中。
        self.assertTrue(any("999.99" in (i.message + i.detail) for i in issues),
                        msg=f"未报出编造的数字：{[i.to_json() for i in issues]}")

    def test_number_provenance_accepts_real_number(self):
        sections = ["趋势状态", "置信度"]
        text = "## 趋势状态\n均线 101.25 在上方。\n## 置信度\nHIGH\n"
        gctx = self._ctx(text, sections,
                         tool_results={"indicators": {"ma": {"ma5": 101.25}}})
        self.assertEqual(NumberProvenance().check(gctx), [])

    def test_number_provenance_allows_rounding(self):
        """工具给 143.5725，报告写 143.57 应该算对得上。"""
        sections = ["趋势状态", "置信度"]
        text = "## 趋势状态\n现值 143.57。\n## 置信度\nHIGH\n"
        gctx = self._ctx(text, sections, tool_results={"x": {"v": 143.5725}})
        self.assertEqual(NumberProvenance().check(gctx), [])

    # ── 第 4 次真跑暴露的两类误报（都会"每次都报"，必须堵死）──

    def test_number_provenance_accepts_非ASCII负号(self):
        """报告写 "−14.2606"（U+2212），工具输出里是 -14.2606 —— 同一个数。

        中文语境下模型经常吐数学减号，而 ``_NUMBER_RE`` 的 ``[-+]?`` 只认
        ASCII。不归一化的后果不是"漏一个边角情况"，而是**每次真跑**都把负值
        判成编造 —— 第 4 次真跑里主席那份完全正确的报告就这么被判了三处。
        """
        sections = ["回撤", "置信度"]
        text = "## 回撤\ntotal_return_pct=\u221214.2606。\n## 置信度\nHIGH\n"
        gctx = self._ctx(text, sections,
                         tool_results={"stats": {"total_return_pct": -14.2606}})
        self.assertEqual(NumberProvenance().check(gctx), [])

    def test_number_provenance_accepts_负值的幅度写法(self):
        """工具给 -24.8469，报告写 "24.85%" —— 这是**对**的说法。

        "最大回撤 24.85%" 说的是回撤的幅度，负号在括号里（-24.8469）。
        要求符号一致，会把这类正确引用整批判成"编数字"，而风险官每次都会
        这么写。
        """
        sections = ["风险", "置信度"]
        text = "## 风险\n最大回撤 24.85%。\n## 置信度\nHIGH\n"
        gctx = self._ctx(text, sections,
                         tool_results={"stats": {"max_drawdown_pct": -24.8469}})
        self.assertEqual(NumberProvenance().check(gctx), [])

    def test_非ASCII负号必须被当成符号断言(self):
        """归一化的**真正**作用：让 U+2212 成为符号，而不是被无声丢掉。

        报告写 "−14.2606"（U+2212）、工具给的却是 **+14.2606** —— 方向反了。
        不做归一化，U+2212 会在正则那一步被丢掉，这个数就变成"没写符号"，
        随后被"按幅度匹配"放行，符号断言就此消失。

        **这条不能和上面那条合并**：上面"接受 U+2212"的用例同时被幅度匹配
        兜住，把归一化退回旧行为它照样绿 —— 拿它验证归一化就是在空转。
        """
        sections = ["收益", "置信度"]
        text = "## 收益\ntotal_return_pct=\u221214.2606。\n## 置信度\nHIGH\n"
        gctx = self._ctx(text, sections,
                         tool_results={"stats": {"total_return_pct": 14.2606}})
        issues = NumberProvenance().check(gctx)
        self.assertTrue(
            issues,
            msg="U+2212 被当成了「没写符号」，模型的符号断言消失了",
        )

    def test_抽取时把非ASCII负号归一成ASCII(self):
        """归一化做在抽取入口：token 带 ASCII 负号，池子也装成负数。"""
        self.assertEqual(schemas.extract_numbers("回撤 \u221224.8469"), ["-24.8469"])
        self.assertEqual(schemas.number_pool({"a": "回撤 \u221224.8469"}), {-24.8469})

    def test_符号写反了仍然要报(self):
        """放宽幅度匹配 ≠ 不看符号。

        工具给 +14.2606，报告**显式**写成 -14.2606：符号在这里是模型的断言，
        而且方向整个反了，必须拦。上面那条放宽的是"模型没表态"，
        不是"模型说了算"。
        """
        sections = ["收益", "置信度"]
        text = "## 收益\ntotal_return_pct=-14.2606。\n## 置信度\nHIGH\n"
        gctx = self._ctx(text, sections,
                         tool_results={"stats": {"total_return_pct": 14.2606}})
        issues = NumberProvenance().check(gctx)
        self.assertTrue(any("-14.2606" in (i.message + i.detail) for i in issues),
                        msg=f"显式写反的符号没被拦住：{[i.to_json() for i in issues]}")

    def test_number_provenance_without_tools_is_honest(self):
        """没有工具输出时说"无法溯源"，而不是默默放行。"""
        gctx = self._ctx("## 趋势状态\nBULLISH\n## 置信度\nHIGH\n",
                         ["趋势状态", "置信度"])
        issues = NumberProvenance().check(gctx)
        self.assertTrue(any("无法" in i.message for i in issues))

    def test_number_provenance_accepts_percent_form_of_a_ratio(self):
        """工具给小数 0.2246，报告写 "22.46%" —— 同一个比率的两种写法。

        不做这层换算的后果不是"漏掉一个边角情况"，而是**每次**跑量价类
        角色都误报一条。每次都出现的告警会被用户学会忽略，等于废掉整条
        溯源检查。
        """
        sections = ["波动率状态", "置信度"]
        text = "## 波动率状态\n处于 22.46% 分位。\n## 置信度\nHIGH\n"
        gctx = self._ctx(text, sections,
                         tool_results={"flow": {"volatility_regime": {
                             "percentile": 0.2246}}})
        self.assertEqual(NumberProvenance().check(gctx), [])

    def test_number_provenance_percent_form_only_applies_to_percent_literals(self):
        """容差不能顺手放宽：写成 "5046"（不带百分号）不该去匹配池子里的 50.46。"""
        sections = ["趋势状态", "置信度"]
        text = "## 趋势状态\n成交量 5046。\n## 置信度\nHIGH\n"
        gctx = self._ctx(text, sections, tool_results={"x": {"ratio": 50.46}})
        issues = NumberProvenance().check(gctx)
        self.assertTrue(any("5046" in (i.message + i.detail) for i in issues),
                        msg="不带百分号的字面量不该享受 ÷100 的换算")

    # ── 池子的边界：模型"读过什么" vs 它"调了什么工具" ──
    #
    # 这一组是照着线上现场写的。主席的配置里只有 stats 一个工具，但它的
    # 提示词写着"你手上只有各人的结论与他们的工具输出"，真实模型于是引用
    # 技术面的均线、量价的量比 —— 完全正确，却被判「26 个数字无法溯源」。

    def test_number_provenance_accepts_numbers_from_the_task_prompt(self):
        """提示词里的数字（运行参数、别人的工具输出）也是合法出处。"""
        sections = ["决议", "置信度"]
        text = ("## 决议\n采纳技术面：ma20 1273.42。本次最小训练样本 60 根。\n"
                "## 置信度\nMEDIUM\n")
        gctx = self._ctx(
            text, sections,
            # 主席自己只调了 stats
            tool_results={"stats": {"last_price": 1258.62}},
            prompt_text=("本次分析的运行参数：\n- 样本：250 根 K 线\n"
                         "- 预测设置：方法 ar，步长 5，回测 5 折、最小训练样本 60\n"
                         "### 技术面分析师（BEARISH / MEDIUM）\nma20 1273.42\n"),
        )
        self.assertEqual(NumberProvenance().check(gctx), [])

    def test_number_provenance_still_flags_numbers_the_prompt_never_had(self):
        """放宽池子不等于放行编造 —— 提示词里没有的数字照样要抓。"""
        sections = ["决议", "置信度"]
        text = "## 决议\n压力位 1988.88，据此看多。\n## 置信度\nMEDIUM\n"
        gctx = self._ctx(
            text, sections,
            tool_results={"stats": {"last_price": 1258.62}},
            prompt_text="### 技术面分析师\nma20 1273.42\n- 最小训练样本 60\n",
        )
        issues = NumberProvenance().check(gctx)
        self.assertTrue(any("1988.88" in (i.message + i.detail) for i in issues),
                        msg=f"编造的价位没被抓住：{[i.to_json() for i in issues]}")

    def test_number_provenance_accepts_a_ratio_computed_from_the_pool(self):
        """由池中两数相除算出的比值算履职，不算编造。

        提示词本来就要求分析师做这类换算（量比、占比、倍数）。不认它，
        每次跑都会多报两三条 —— 又回到"每次都误报等于没有护栏"。
        """
        sections = ["量价状态", "置信度"]
        text = ("## 量价状态\n上涨日均量 4319967，下跌日 3805282，比 1.135。\n"
                "## 置信度\nMEDIUM\n")
        gctx = self._ctx(text, sections, tool_results={"flow": {
            "up_day_avg_volume": 4319967.0, "down_day_avg_volume": 3805282.0}})
        self.assertEqual(NumberProvenance().check(gctx), [])

    def test_number_provenance_accepts_percent_form_of_a_computed_ratio(self):
        """自行换算的比值写成百分数（62.5% = 5000000 / 8000000），同样算对得上。

        刻意用大于 :data:`NumberProvenance.SMALL_INT_MAX` 的值：写成 16.0%
        时会被"小整数一律放行"那条白名单兜住，用例就变成空转 —— 无论池子
        怎么建它都不会红。
        """
        sections = ["量能状态", "置信度"]
        text = ("## 量能状态\n上涨日成交 5000000，全样本 8000000，占比 62.5%。\n"
                "## 置信度\nMEDIUM\n")
        gctx = self._ctx(text, sections, tool_results={"flow": {
            "up_volume": 5000000.0, "total_volume": 8000000.0}})
        self.assertEqual(NumberProvenance().check(gctx), [])

    def test_衍生比值不接受量纲不同的两个数相除(self):
        """成交量 ÷ 价格 ≈ 3194 —— 这种商没人会写进报告，不能当成出处。

        不设这道界，池子里任意两数相除会铺开一片数值区间，编造的绝对值
        就能藏在里面，护栏等于失效。
        """
        sections = ["关键位", "置信度"]
        text = "## 关键位\n预期阻力 3194.70。\n## 置信度\nMEDIUM\n"
        gctx = self._ctx(text, sections, tool_results={"flow": {
            "avg_volume": 4019941.0}, "stats": {"last_price": 1258.62}})
        issues = NumberProvenance().check(gctx)
        self.assertTrue(any("3194.70" in (i.message + i.detail) for i in issues),
                        msg=f"量纲不同的除法被当成了出处：{[i.to_json() for i in issues]}")

    def test_无法溯源时把具体是哪个数字写进message(self):
        """报告渲染只显示 message。只给条数，用户没法复核是哪一个。"""
        sections = ["趋势状态", "置信度"]
        text = "## 趋势状态\n价格将涨到 999.99。\n## 置信度\nHIGH\n"
        gctx = self._ctx(text, sections, tool_results={"stats": {"last": 100.0}})
        issues = NumberProvenance().check(gctx)
        self.assertTrue(issues)
        self.assertIn("999.99", issues[0].message,
                      msg=f"message 里没有数字，用户看不到是哪一处：{issues[0].message}")

    def test_forbidden_phrases_is_error(self):
        from finpulse_engine.agent.guardrails import ForbiddenPhrases
        sections = ["趋势状态", "置信度"]
        text = "## 趋势状态\n强烈建议买入，必涨。\n## 置信度\nHIGH\n"
        issues = ForbiddenPhrases().check(self._ctx(text, sections))
        self.assertTrue(issues)

    def test_tool_budget_is_error(self):
        from finpulse_engine.agent.guardrails import ToolBudget
        role = agent_config.parse_config(_inline_role(**{
            "config": {
                "model": {"provider": "rule_based"},
                "instructions": "你是一个用于测试的角色。" * 10,
                "tools": ["indicators"],
                "output_sections": ["趋势状态", "置信度"],
                "max_tool_calls": 2,
            }
        }))
        gctx = self._ctx("## 趋势状态\nBULLISH\n## 置信度\nHIGH\n",
                         role.output_sections, role=role,
                         tool_calls=["indicators"] * 5)
        issues = ToolBudget().check(gctx)
        self.assertTrue(any(i.severity == "error" for i in issues))

    def test_summarize_counts_by_level(self):
        sections = ["趋势状态", "置信度"]
        role = agent_config.parse_config(_inline_role())
        gctx = self._ctx("## 趋势状态\nBULLISH\n", sections, role=role)
        summary = summarize(Pipeline().run(gctx))
        self.assertGreaterEqual(summary["errors"], 1)
        self.assertIn("items", summary)

    def test_broken_guardrail_does_not_crash_pipeline(self):
        """护栏自己崩了不能把分析拖垮，但必须留痕。"""
        from finpulse_engine.agent.guardrails import Guardrail, Issue

        class Boom(Guardrail):
            name = "boom"

            def check(self, ctx):
                raise RuntimeError("护栏内部错误")

        gctx = self._ctx("## 趋势状态\nBULLISH\n## 置信度\nHIGH\n",
                         ["趋势状态", "置信度"])
        issues = Pipeline([Boom()]).run(gctx)
        self.assertTrue(any("护栏自身执行失败" in i.message for i in issues))


# ── 角色运行时 ────────────────────────────────────────────────────


class _Capture(Guardrail):
    """只记录护栏上下文、不报任何问题 —— 用来检查"传进去的到底是什么"。"""

    name = "capture"

    def __init__(self) -> None:
        self.seen = []

    def check(self, ctx: GuardrailContext):
        self.seen.append(ctx)
        return []


class TestRoleRuntime(unittest.TestCase):
    def setUp(self):
        self.cfgs = agent_config.load_all()
        self.reg = default_registry()
        self.ctx = _synthetic_ctx()
        self.ctx.forecaster_factory = _forecaster_factory()

    def _run(self, role_id, **kw):
        cfg = self.cfgs[role_id]
        runtime = RoleRuntime(cfg, self.reg, guards=Pipeline())
        return runtime.run(self.ctx, **kw)

    # ── 技术面 ──
    def test_technical_analyst_end_to_end(self):
        """技术分析师：工具取数 → 渲染 → 契约解析 → 护栏，全链路。"""
        run = self._run("technical_analyst")
        self.assertTrue(run.ok, msg=f"error={run.error}")
        self.assertEqual(run.provider, "rule_based")
        self.assertIsNone(run.fallback_reason)
        self.assertGreater(run.turns, 0)
        for section in self.cfgs["technical_analyst"].output_sections:
            self.assertIn(section, run.text, msg=f"缺少段落 {section}")
        called = {i.name for i in run.invocations}
        self.assertTrue(called & {"indicators", "stats"}, msg=f"只调了 {called}")
        self.assertFalse(Pipeline.has_errors(run.issues), msg=str(run.issues))

    def test_direction_and_confidence_present(self):
        run = self._run("technical_analyst")
        self.assertIn(run.direction, ("BULLISH", "BEARISH", "NEUTRAL"))
        self.assertIn(run.confidence, ("LOW", "MEDIUM", "HIGH"))

    def test_tool_result_carried_for_provenance(self):
        """护栏要能追溯数字，工具结果必须挂回 RoleRun。"""
        run = self._run("technical_analyst")
        self.assertTrue(run.tool_results)
        self.assertTrue(any(k in run.tool_results for k in ("indicators", "stats")))

    def test_task_prompt_reaches_the_guardrails(self):
        """提示词原文必须送到护栏 —— 数字溯源的池子靠它。

        主席的配置里只有 stats 一个工具，可它的提示词里装着各分析师的结论
        与运行参数，而这两样都是它写报告时的**合法出处**。传不到护栏，
        一份完全正确的主席报告就会被判二十多处"无法溯源"—— 线上就是这样。
        """
        peer = RoleRun(role_id="technical_analyst", role_name="技术面分析师",
                       category="technical",
                       text="## 趋势状态\nBULLISH，压力位 1500.25\n")
        cap = _Capture()
        runtime = RoleRuntime(self.cfgs["committee_chair"], self.reg,
                             guards=Pipeline([cap]))
        runtime.run(self.ctx, peers=[peer])

        self.assertTrue(cap.seen, msg="护栏没有被调用")
        prompt_text = cap.seen[0].prompt_text
        # 对等角色的结论原文
        self.assertIn("1500.25", prompt_text)
        # 本次运行的参数。断言整行而不是一个关键词：措辞以后还会改，
        # 写死"最小训练样本 60"的话，改一次文案就红一次，而红的理由
        # 看起来会像"提示词没传到护栏"—— 那是完全不同的一件事。
        self.assertIn(describe_intent(self.ctx), prompt_text)
        # 而这两个数字都不在主席自己的工具输出里 —— 这正是池子必须并上
        # 提示词的理由。少了这一步，测试会绿得毫无意义。
        pool = schemas.number_pool(cap.seen[0].tool_results)
        self.assertNotIn(1500.25, pool)
        self.assertNotIn(float(self.ctx.backtest_min_train), pool)

    def test_technical_report_numbers_are_all_traceable(self):
        """技术面报告的每个数字都应能在工具输出里找到出处。"""
        run = self._run("technical_analyst")
        self.assertEqual(NumberProvenance().check(GuardrailContext(
            role=self.cfgs["technical_analyst"], text=run.text, check=run.check,
            tool_results=run.tool_results)), [])

    # ── 量价 ──
    def test_flow_analyst_sections_present(self):
        run = self._run("flow_analyst")
        self.assertTrue(run.ok, msg=f"error={run.error}")
        for section in self.cfgs["flow_analyst"].output_sections:
            self.assertIn(section, run.text, msg=f"缺少段落 {section}")

    # ── 终端证据（反向工具通道的落地） ──
    def test_terminal_evidence_says_unavailable_without_bridge(self):
        """没有终端工具桥时，报告要如实写出"不可用"及其原因。

        这是"降级必须可见"的一次具体体现：读报告的人得能分辨
        "终端说没有这只票" 和 "这台机器上根本没有终端在监听"。
        """
        run = self._run("technical_analyst")
        self.assertTrue(run.ok, msg=f"error={run.error}")
        body = schemas.parse_sections(run.text)["终端证据"]
        self.assertIn("不可用", body)
        # 原因由终端侧（这里是 NULL 客户端）给出，必须原样带出来。
        self.assertIn("未连接终端工具桥", body)
        # 一条数据都没拿到时，不能再写"上述数据来自终端实时内存状态"
        # —— 那是在为不存在的东西背书。
        self.assertNotIn("上述数据来自", body)
        self.assertIn("本段没有取到任何终端数据", body)

    def test_terminal_evidence_renders_bridge_data(self):
        """终端工具返回真数据时，这一段必须真的把数值写出来。

        否则反向工具通道就只是"调用成功"而已 —— 数据流进了上下文，
        报告里却看不见，用户无法判断结论用没用上终端状态。
        """
        class StubBridge:
            """冒充 C++ 工具桥。只实现被用到的那两个方法。"""

            enabled = True

            def __init__(self):
                self.calls = []

            def available(self):
                return True

            def describe(self):
                return {"available": True, "endpoint": "http://127.0.0.1:1",
                        "tool_count": 4}

            def call(self, name, arguments):
                self.calls.append(name)
                if name == "terminal.live_quote":
                    return {"available": True, "tool": name, "symbol": "600519",
                            "last": 1712.5, "change_pct": -0.83, "source": "C++"}
                if name == "terminal.data_quality":
                    return {"available": True, "tool": name, "symbol": "600519",
                            "bars": 260, "issues": []}
                if name == "terminal.bus_stats":
                    return {"available": True, "tool": name, "published": 1234,
                            "delivered": 1234, "active_subscriptions": 3,
                            "unmatched": 0}
                raise AssertionError(f"不该调用 {name}")

            def list_tools(self):
                return []

        stub = StubBridge()
        self.ctx.remote = stub
        run = self._run("technical_analyst")
        self.assertTrue(run.ok, msg=f"error={run.error}")
        self.assertIn("terminal.live_quote", stub.calls)

        body = schemas.parse_sections(run.text)["终端证据"]
        self.assertNotIn("不可用", body)
        self.assertIn("600519", body)
        self.assertIn("1,712.50", body)          # 价格要按数值格式化出来
        self.assertIn("-0.83%", body)
        # 必须点明数据性质：实时内存状态 ≠ 历史序列推算。
        self.assertIn("实时", body)

    def test_tool_evidence_numbers_are_traceable(self):
        """终端段里的数字也要能追溯到工具输出 —— 它没有豁免权。"""

        class StubBridge:
            enabled = True

            def available(self):
                return True

            def describe(self):
                return {"available": True, "endpoint": "x", "tool_count": 4}

            def call(self, name, arguments):
                if name == "terminal.live_quote":
                    return {"available": True, "tool": name, "symbol": "600519",
                            "last": 1712.5, "change_pct": -0.83}
                return {"available": True, "tool": name}

            def list_tools(self):
                return []

        self.ctx.remote = StubBridge()
        run = self._run("technical_analyst")
        self.assertTrue(run.ok, msg=f"error={run.error}")
        issues = NumberProvenance().check(GuardrailContext(
            role=self.cfgs["technical_analyst"], text=run.text, check=run.check,
            tool_results=run.tool_results, tool_calls=[i.name for i in run.invocations],
        ))
        self.assertEqual([i for i in issues if i.severity == "error"], [],
                         msg=str([i.to_json() for i in issues]))

    def test_risk_officer_cites_data_quality(self):
        """风险官要拿终端的数据质量结论给自己验料。"""

        class StubBridge:
            enabled = True

            def available(self):
                return True

            def describe(self):
                return {"available": True, "endpoint": "x", "tool_count": 4}

            def call(self, name, arguments):
                if name == "terminal.data_quality":
                    return {"available": True, "tool": name, "symbol": "600519",
                            "bars": 260, "issues": ["第 7 根 high < low"]}
                return {"available": True, "tool": name}

            def list_tools(self):
                return []

        self.ctx.remote = StubBridge()
        run = self._run("risk_officer")
        self.assertTrue(run.ok, msg=f"error={run.error}")
        body = schemas.parse_sections(run.text)["终端证据"]
        self.assertIn("260", body)
        self.assertIn("high < low", body)

    def test_flow_analyst_verdict_in_first_section(self):
        """量价分析师的第一段必须带量价配合方向 —— 主席要能统计到它的票。"""
        run = self._run("flow_analyst")
        sections = self.cfgs["flow_analyst"].output_sections
        first = schemas.parse_sections(run.text)[sections[0]]
        self.assertIn("量价配合方向", first,
                      msg=f"第一段没有方向标签，主席统计不到它的票：{first[:200]}")
        self.assertIn(run.direction, ("BULLISH", "BEARISH", "NEUTRAL"))

    # ── 风险 ──
    def test_risk_officer_does_not_critique_itself(self):
        """风险官只质疑别人，不能把自己也列进去。"""
        run = self._run("risk_officer")
        self.assertTrue(run.ok, msg=f"error={run.error}")
        self.assertIn("单角色运行", run.text)

    def test_risk_officer_with_peers(self):
        tech = self._run("technical_analyst")
        run = self._run("risk_officer", peers=[tech])
        self.assertTrue(run.ok, msg=f"error={run.error}")
        self.assertIn("技术面分析师", run.text)
        # 不能出现"质疑：风险官"这种自我质证。
        cfg = self.cfgs["risk_officer"]
        idx = run.text.find("反对意见")
        tail = run.text[idx:] if idx >= 0 else ""
        self.assertNotIn(f"- {cfg.name}：", tail)

    def test_risk_officer_never_adopts_a_quoted_direction(self):
        """风险官「反对意见」段会引述别人的方向，那不能变成它自己的立场。

        真实回归：第二轮交叉质证之后，风险官的方向标签从 ``None``（弃权）
        变成 ``BULLISH``，因为 ``check_output`` 扫了全部段落、把引述当成了
        表态。后果是弃权票被算成看多票，决议方向被它带偏。
        """
        tech = self._run("technical_analyst")
        flow = self._run("flow_analyst")
        run = self._run("risk_officer", peers=[tech, flow])
        self.assertTrue(run.ok, msg=f"error={run.error}")

        # 前置条件：报告里**确实**出现了别人方向标签的引述。
        quotes = [d for d in ("BULLISH", "BEARISH")
                  if d in run.text and d != run.direction]
        self.assertTrue(quotes, msg=f"这个用例的前提不成立，报告里没有引述：{run.text[:300]}")

        # 结论：风险官按定义不产出方向，抽出来的必须是 None。
        self.assertIsNone(run.direction,
                          msg=f"引述的 {quotes} 被当成了风险官自己的方向")
        self.assertEqual(run.check.direction_sections, ["下行风险"])
        # 弃权不能被当成"已表态"，否则主席的计票分母就错了。
        self.assertTrue(self.cfgs["risk_officer"].direction_scope == ["下行风险"])

    def test_risk_officer_direction_is_none_in_debate(self):
        """投委会两轮里风险官都应保持弃权 —— 第一轮和第二轮不能给出不同答案。

        ``peers`` 正是第二轮的条件（能看到别人的结论）。完整的编排层验证见
        ``test_agent_orchestrator.TestDebate.test_risk_officer_never_flips_to_a_quoted_direction``。
        """
        tech = self._run("technical_analyst")
        flow = self._run("flow_analyst")
        first = self._run("risk_officer")                       # 第一轮：独立
        second = self._run("risk_officer", peers=[tech, flow])  # 第二轮：交叉质证
        self.assertIsNone(first.direction, msg="第一轮风险官凭空表态")
        self.assertIsNone(second.direction, msg="第二轮被引述的方向带偏")

    def test_risk_officer_flags_negative_skill(self):
        """技能分不为正时，风险官必须把预测类结论判为不可信。"""
        run = self._run("risk_officer")
        skill = run.tool_results.get("backtest", {}).get("skill")
        if skill is not None and skill <= 0:
            self.assertIn("不可信", run.text)
            self.assertIn("随机游走", run.text)

    # ── 主席 ──
    def test_chair_tally_and_verdict(self):
        """主席综合三个分析师的意见，必须把声明的段落都写出来。"""
        peers = [self._run(r) for r in
                 ("technical_analyst", "risk_officer", "flow_analyst")]
        cfg = self.cfgs["committee_chair"]
        run = self._run("committee_chair", peers=peers)
        self.assertTrue(run.ok, msg=f"error={run.error}")
        for section in cfg.output_sections:
            self.assertIn(section, run.text, msg=f"缺少段落 {section}")
        self.assertIn(run.direction, ("BULLISH", "BEARISH", "NEUTRAL"))

    def test_chair_tally_matches_peer_votes(self):
        """决议方向必须与票数一致。"""
        peers = [self._run(r) for r in
                 ("technical_analyst", "risk_officer", "flow_analyst")]
        run = self._run("committee_chair", peers=peers)
        bull = sum(1 for p in peers if p.direction == "BULLISH")
        bear = sum(1 for p in peers if p.direction == "BEARISH")
        if bull > bear:
            self.assertEqual(run.direction, "BULLISH")
        elif bear > bull:
            self.assertEqual(run.direction, "BEARISH")
        else:
            self.assertEqual(run.direction, "NEUTRAL")

    def test_chair_tie_is_declared_not_papered_over(self):
        """平票时主席必须说出来，不能靠措辞把分歧抹平。"""
        def fake(role_id, name, direction, confidence):
            return RoleRun(role_id=role_id, role_name=name, category="technical",
                           text=f"## 趋势状态\n{direction}\n## 置信度\n{confidence}\n",
                           direction=direction, confidence=confidence)

        peers = [fake("a", "甲分析师", "BULLISH", "HIGH"),
                 fake("b", "乙分析师", "BEARISH", "HIGH")]
        run = self._run("committee_chair", peers=peers)
        self.assertTrue(run.ok, msg=f"error={run.error}")
        # 必须把"票数相同、不构成多数"说出来，而不是挑一边写。
        self.assertIn("票数相同", run.text)
        self.assertIn("不构成多数", run.text)
        self.assertEqual(run.direction, "NEUTRAL")
        # 平票时不得给出 HIGH：分歧意味着证据不支持任何一方。
        self.assertNotEqual(run.confidence, "HIGH")
        # 没有一方可被采纳，这一点也要写出来。
        self.assertIn("没有结论本身就是一个结论", run.text)

    def test_chair_confidence_uses_own_algorithm(self):
        """主席置信度取分析师最高值封顶，不能沿用分析师的判据计数法。"""
        def fake(role_id, direction, confidence):
            return RoleRun(role_id=role_id, role_name=role_id, category="technical",
                           text=f"## 趋势状态\n{direction}\n## 置信度\n{confidence}\n",
                           direction=direction, confidence=confidence)

        peers = [fake("a", "BULLISH", "MEDIUM"), fake("b", "BULLISH", "LOW")]
        run = self._run("committee_chair", peers=peers)
        self.assertEqual(run.direction, "BULLISH")
        self.assertEqual(run.confidence, "MEDIUM")   # 封顶在最高值，不上浮

    def test_chair_without_peers_refuses_to_invent(self):
        """没有下级结论时，主席不能自己编一个方向。"""
        run = self._run("committee_chair")
        self.assertIn("没有下级", run.text)

    # ── 降级可见性 ──
    def test_provider_fallback_is_visible(self):
        """配了 deepseek 但没 key → 降级到规则后端，且必须写明原因。"""
        cfg = agent_config.parse_config(_inline_role(**{
            "config": {
                "model": {"provider": "deepseek", "model_id": "deepseek-chat"},
                "instructions": "你是一个用于测试降级的角色。" * 8,
                "tools": ["indicators"],
                "output_sections": ["趋势状态", "置信度"],
            }
        }))
        old = os.environ.pop("DEEPSEEK_API_KEY", None)
        try:
            run = RoleRuntime(cfg, self.reg, guards=Pipeline()).run(self.ctx)
        finally:
            if old is not None:
                os.environ["DEEPSEEK_API_KEY"] = old
        self.assertEqual(run.provider, "rule_based")
        self.assertIsNotNone(run.fallback_reason)
        self.assertIn("DEEPSEEK_API_KEY", run.fallback_reason)
        # 降级要能穿过护栏被看见。
        self.assertTrue(any(i.guardrail == "provider_fallback" for i in run.issues))

    def test_unknown_provider_name_is_rejected_visibly(self):
        """provider 名拼错不能静默退回规则后端 —— 那会让人以为自己在用 LLM。"""
        cfg = agent_config.parse_config(_inline_role(**{
            "config": {
                "model": {"provider": "opemai", "model_id": "x"},   # 拼错
                "instructions": "你是一个用于测试拼错的角色。" * 8,
                "tools": ["indicators"],
                "output_sections": ["趋势状态", "置信度"],
            }
        }))
        run = RoleRuntime(cfg, self.reg, guards=Pipeline()).run(self.ctx)
        self.assertEqual(run.provider, "rule_based")
        self.assertIn("opemai", run.fallback_reason)

    # ── 轨迹与提示词 ──
    def test_trace_records_spans(self):
        run = self._run("technical_analyst")
        self.assertIsNotNone(run.trace)
        kinds = {s.kind for s in run.trace.children} | {run.trace.kind}
        self.assertIn("role", kinds)
        self.assertIn("backend", kinds)

    def test_task_prompt_states_run_parameters(self):
        """任务提示词必须写明标的/样本量/预测设置，否则模型会去猜。"""
        cfg = self.cfgs["technical_analyst"]
        prompt = build_task_prompt(cfg, self.ctx)
        self.assertIn(self.ctx.symbol, prompt)
        self.assertIn(str(self.ctx.bars), prompt)
        self.assertIn(cfg.name, prompt)
        for section in cfg.output_sections:
            self.assertIn(section, prompt)

    def test_议题与提示词说的是同一句话(self):
        """报告头部给用户看的「议题」，必须就是喂给模型的那句运行参数。

        两处各写一遍，迟早会漂移成两件事 —— 而"模型被告知的"和"用户以为
        它被问的"不一致，是这套系统里最难查的一类 bug。所以这里比对的是
        **同一函数的输出**，不是各写一遍关键词。
        """
        cfg = self.cfgs["technical_analyst"]
        prompt = build_task_prompt(cfg, self.ctx)
        self.assertIn(describe_intent(self.ctx), prompt)

        intent = describe_intent(self.ctx)
        self.assertIn(self.ctx.symbol, intent)
        self.assertIn(str(self.ctx.bars), intent)
        self.assertIn(self.ctx.forecast_method, intent)
        self.assertIn(self.ctx.as_of(), intent)

    def test_task_prompt_truncates_peer_text(self):
        """对等结论过长要截断并显式标注，不能悄悄塞爆上下文。"""
        peer = RoleRun(role_id="a", role_name="话多的分析师", category="technical",
                       text="很长的一段话。" * 400, direction="BULLISH",
                       confidence="HIGH")
        prompt = build_task_prompt(self.cfgs["committee_chair"], self.ctx, peers=[peer])
        self.assertIn("已截断", prompt)
        self.assertLess(len(prompt), PEER_TEXT_LIMIT + 4000)

    def test_run_json_is_serializable(self):
        """结果要能直接进 RPC —— 不能有不可 JSON 化的字段。"""
        import json
        run = self._run("technical_analyst")
        payload = json.dumps(run.to_json(), ensure_ascii=False)
        self.assertIn("role_id", payload)


# ── 决策记忆 ──────────────────────────────────────────────────────


class TestDecisionMemory(unittest.TestCase):
    def test_consistency_flags_flip_without_evidence(self):
        """方向横跳但技能分没变 → 记忆层要报出来。"""
        mem = DecisionMemory()
        mem.record(Decision(ts_ms=1_000, symbol="X", role_id="r", role_name="R",
                            direction="BULLISH", confidence="HIGH",
                            skill=0.05, ann_vol_pct=20.0))
        mem.record(Decision(ts_ms=2_000, symbol="X", role_id="r", role_name="R",
                            direction="BEARISH", confidence="HIGH",
                            skill=0.05, ann_vol_pct=20.0))
        report = mem.consistency("X", "r")
        self.assertEqual(report.flips, 1)
        self.assertFalse(report.evidence_changed)
        self.assertIn("缺乏新的证据支撑", report.message)

    def test_consistency_accepts_flip_with_evidence(self):
        """技能分显著变化时，方向改变是有依据的，不该被判为不稳定。"""
        mem = DecisionMemory()
        mem.record(Decision(ts_ms=1, symbol="X", role_id="r", role_name="R",
                            direction="BULLISH", confidence="HIGH", skill=-0.05))
        mem.record(Decision(ts_ms=2, symbol="X", role_id="r", role_name="R",
                            direction="BEARISH", confidence="HIGH", skill=0.30))
        report = mem.consistency("X", "r")
        self.assertTrue(report.evidence_changed)
        self.assertIn("有证据支撑", report.message)

    def test_single_sample_is_not_judged(self):
        mem = DecisionMemory()
        mem.record(Decision(ts_ms=1, symbol="X", role_id="r", role_name="R",
                            direction="BULLISH", confidence="LOW"))
        report = mem.consistency("X", "r")
        self.assertEqual(report.flips, 0)
        self.assertIn("不足以判断", report.message)

    def test_memory_roundtrip_jsonl(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mem.jsonl"
            mem = DecisionMemory(path=path)
            mem.record(Decision(ts_ms=1, symbol="X", role_id="r", role_name="R",
                                direction="BULLISH", confidence="LOW"))
            mem.record(Decision(ts_ms=2, symbol="X", role_id="r", role_name="R",
                                direction="BEARISH", confidence="LOW"))
            self.assertTrue(path.is_file())
            again = DecisionMemory(path=path)
            self.assertEqual(len(again.all()), 2)
            self.assertEqual(again.symbols(), ["X"])

    def test_corrupt_line_is_skipped_not_fatal(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "mem.jsonl"
            path.write_text('{"ts_ms": 1, "symbol": "X", "role_id": "r",\n'
                            '{"ts_ms": 2, "symbol": "X", "role_id": "r", '
                            '"role_name": "R", "direction": "BULLISH", '
                            '"confidence": "LOW"}\n', encoding="utf-8")
            mem = DecisionMemory(path=path)
            self.assertEqual(len(mem.all()), 1)


# ── 输出预算：轮数与截断 ──────────────────────────────────────────
#
# 这一组用例来自一次真实故障。接上 DeepSeek 跑投委会，三个委员全部报
# "未产出结论（第 2 轮输出被 max_tokens=1600 截断）"，整场作废，还烧掉
# 三万多 token。根因不是一个，是两个叠在一起：
#
#   1. **轮数预算（写死 4）比工具预算（6）还小。** 真实 LLM 常常一轮只调
#      一个工具，于是它花了 4 轮取数、轮数用光，报告根本没机会写；
#   2. **截断被当成"整份报告不可用"。** 一份五段齐全、只是末句被切掉的
#      报告照样被丢，于是一个"写满"的报告和一个"没写"的报告被一视同仁。
#
# 两条都必须钉住 —— 它们都长得不像 bug，只有从"这一轮到底发生了什么"
# 才看得出来。


class _ScriptedProvider:
    """按调用序号返回预置回复的假后端。

    ``respond(n, tools, max_tokens)`` 里 ``n`` 从 1 开始；``tools`` 是这次
    请求实际收到的工具目录（``None`` 表示已经被撤下）。
    """

    name = "scripted"

    def __init__(self, respond):
        self._respond = respond
        self.tools_seen = []
        self.users = []

    def complete(self, messages, *, tools=None, temperature=0.3, max_tokens=2048):
        self.tools_seen.append(tools)
        self.users.append([m.content for m in messages if m.role == "user"])
        return self._respond(len(self.tools_seen), tools, max_tokens)


def _resp(text=None, *, finish="stop", calls=()):
    return LlmResponse(
        text=text or "",
        tool_calls=[ToolCall(id=f"c{i}", name=n) for i, n in enumerate(calls)],
        usage=Usage(prompt_tokens=100, completion_tokens=50),
        finish_reason=finish,
    )


def _report(sections, *, first_only=False):
    """按声明段落拼一份报告。``first_only`` 用来模拟"被切在半路"。"""
    out = []
    for i, name in enumerate(sections):
        if first_only and i > 0:
            break
        if "置信度" in name:
            out.append(f"## {name}\nMEDIUM —— 依据上述工具输出。")
        elif name in ("趋势状态", "量能状态", "决议"):
            out.append(f"## {name}\n**BULLISH** —— 依据上述工具输出。")
        else:
            out.append(f"## {name}\n依据工具输出，本段结论如上。")
    return "\n\n".join(out)


class TestOutputBudget(unittest.TestCase):
    """轮数预算与截断处理 —— 真实故障的回归。"""

    def setUp(self):
        self.cfgs = agent_config.load_all()
        self.reg = default_registry()
        self.ctx = _synthetic_ctx()
        self.ctx.forecaster_factory = _forecaster_factory()

    def _run(self, role_id, provider, **kw):
        from unittest.mock import patch
        runtime = RoleRuntime(self.cfgs[role_id], self.reg, guards=Pipeline())
        with patch.object(llm_registry, "build",
                          return_value=(provider, "scripted", None)):
            return runtime.run(self.ctx, **kw)

    # ── 轮数预算 ──
    def test_轮数预算必须容得下工具预算(self):
        """写死 4 而工具预算是 6 —— 这就是那场故障的直接原因。

        最坏情况是"一轮只调一个工具"，所以轮数必须够它把每个工具各调一次、
        再留一轮写报告。
        """
        for rid in ("technical_analyst", "flow_analyst", "risk_officer",
                    "committee_chair"):
            cfg = self.cfgs[rid]
            budget = turn_budget(cfg.max_tool_calls)
            self.assertGreater(
                budget, len(cfg.tools),
                msg=f"{rid}: 声明了 {len(cfg.tools)} 个工具，"
                    f"轮数预算只有 {budget} —— 报告没机会写")

    def test_模型一轮只调一个工具也写得完报告(self):
        """量价分析师有 4 个工具。写死 4 轮时，它会正好把轮数花在取数上。

        修好之后：4 轮取数 + 第 5 轮出报告。
        """
        cfg = self.cfgs["flow_analyst"]
        provider = _ScriptedProvider(
            lambda n, tools, mt: (
                _resp(finish="tool_calls",
                      calls=[tools[n - 1]["function"]["name"]])
                if tools and n <= len(tools)
                else _resp(_report(cfg.output_sections))
            ))
        run = self._run("flow_analyst", provider)

        self.assertEqual(len(provider.tools_seen), 5,
                         msg=f"一共问了 {len(provider.tools_seen)} 轮")
        self.assertTrue(run.ok, msg=f"error={run.error}")
        self.assertIsNone(run.error)
        for section in cfg.output_sections:
            self.assertIn(section, run.text, msg=f"缺少段落 {section}")

    def test_工具预算用完后撤下工具并催收尾(self):
        """预算用完了还递工具，模型只会继续要 —— 而每一轮都真金白银。

        所以那一轮要**真的把工具撤下来**，并明确告诉它"现在写报告"。
        """
        cfg = self.cfgs["flow_analyst"]
        provider = _ScriptedProvider(
            lambda n, tools, mt: (
                _resp(finish="tool_calls",
                      calls=[tools[0]["function"]["name"]])
                if tools else _resp(_report(cfg.output_sections))
            ))
        run = self._run("flow_analyst", provider)

        self.assertIsNone(provider.tools_seen[-1],
                          msg="工具预算用完后仍然把工具目录递了过去")
        self.assertTrue(run.ok, msg=f"error={run.error}")
        self.assertEqual(len(run.invocations), cfg.max_tool_calls)
        # 撤下工具的同时要说明白，否则模型只会写一段"我需要更多数据"。
        self.assertTrue(any("工具预算已用完" in (u or "")
                            for texts in provider.users for u in texts),
                        msg="没有告诉模型工具预算已经用完")

    # ── 截断 ──
    def test_截断但段落齐全的报告仍然可用(self):
        """这正是被误杀的那种报告。

        五段全都写了，只是末句撞上 max_tokens 被切掉。上一版直接把它
        写成 run.error，于是"未产出结论"—— 一场投委会因此全军覆没。
        """
        cfg = self.cfgs["technical_analyst"]
        text = _report(cfg.output_sections)
        provider = _ScriptedProvider(
            lambda n, tools, mt: (
                _resp(finish="tool_calls",
                      calls=[tools[i]["function"]["name"] for i in range(len(tools))])
                if tools else _resp(text, finish="length")
            ))
        run = self._run("technical_analyst", provider)

        self.assertTrue(run.truncated, msg="截断没有被记录下来")
        self.assertIsNone(run.error, msg=f"截断又被当成错误了：{run.error}")
        self.assertTrue(run.ok)
        self.assertEqual(run.direction, "BULLISH")
        self.assertFalse(Pipeline.has_errors(run.issues),
                         msg=f"齐全的报告被判了 error：{[i.to_json() for i in run.issues]}")
        # 但也不能什么都不说：读报告的人有权知道末段可能不完整。
        kinds = {i.guardrail for i in run.issues}
        self.assertIn("output_truncated", kinds, msg=str(kinds))

    def test_截断到缺段才算不可用(self):
        """截断与缺段是两件事，判罚也不同。

        缺段由 SectionCompleteness 判 error —— 那份报告下游真的读不到
        东西。这才是"不可用"的定义。
        """
        from finpulse_engine.agent.orchestrator import is_effective
        cfg = self.cfgs["technical_analyst"]
        provider = _ScriptedProvider(
            lambda n, tools, mt: (
                _resp(finish="tool_calls",
                      calls=[tools[i]["function"]["name"] for i in range(len(tools))])
                if tools else _resp(_report(cfg.output_sections, first_only=True),
                                    finish="length")
            ))
        run = self._run("technical_analyst", provider)

        self.assertTrue(run.truncated)
        self.assertTrue(run.text.strip(), msg="正文不为空，所以 run.ok 仍是 True")
        self.assertTrue(Pipeline.has_errors(run.issues),
                        msg="缺段必须判 error")
        self.assertFalse(is_effective(run), msg="缺段的报告不该进计票")

    # ── 输出预算与质证注入量 ──
    def test_提示词里写明了输出预算(self):
        """输出越界是最贵的一种失败：截断要么丢段，要么逼我们重跑一遍。

        把预算写进提示词，比事后补救便宜得多。
        """
        prompt = build_task_prompt(self.cfgs["technical_analyst"], self.ctx)
        self.assertIn("输出预算", prompt)
        self.assertIn(str(len(self.cfgs["technical_analyst"].output_sections) * 200),
                      prompt)
        self.assertIn("同时调用多个工具", prompt, msg="没告诉模型可以一次点多个工具")

    def test_交叉质证注入的对等结论比主席那份短(self):
        """委员只要"回应结论"，主席才需要"通读全文"。

        给委员全文，换来的只会是一段同样长的回话 —— 那正是输出撞上
        max_tokens 被截断的由来。
        """
        peer = RoleRun(role_id="flow_analyst", role_name="量价分析师",
                       category="sentiment", text="甲" * 3000,
                       direction="BULLISH", confidence="MEDIUM")

        member = build_task_prompt(self.cfgs["technical_analyst"], self.ctx,
                                   peers=[peer])
        chair = build_task_prompt(self.cfgs["committee_chair"], self.ctx,
                                  peers=[peer])

        for who, prompt in (("委员", member), ("主席", chair)):
            self.assertIn("……（原文过长已截断）", prompt, msg=f"{who}那份没截断")
        self.assertEqual(member.count("甲"), CROSS_EXAM_PEER_LIMIT)
        self.assertEqual(chair.count("甲"), PEER_TEXT_LIMIT)
        self.assertLess(member.count("甲"), chair.count("甲"))


if __name__ == "__main__":
    unittest.main()
