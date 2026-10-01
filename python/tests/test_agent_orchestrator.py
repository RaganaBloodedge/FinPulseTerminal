# -*- coding: utf-8 -*-
"""编排层测试 —— panel 配置 / 并行团队 / 投委会辩论 / 加权计票 / quorum。

三条最值得钉住的约束：

* **权重真的参与计票**。Fincept 的投委会配了成员权重却从不读取；
  这里必须有测试证明改权重会改变决议方向，否则我就重复了那个问题。
* **委员集体失效时不出决议**。"全员答不出来"和"大家认为该中性"是两件事，
  前者给一个 NEUTRAL 是把系统性故障伪装成决策意见。
* **交叉质证真的发生**。第二轮必须能拿到第一轮的结论，且改口会被记录。
"""

from __future__ import annotations

import unittest

from finpulse_engine.agent import config as agent_config
from finpulse_engine.agent.guardrails import Pipeline
from finpulse_engine.agent.memory import Decision, DecisionMemory
from finpulse_engine.agent.orchestrator import (
    OrchestrationError,
    Orchestrator,
    is_effective,
    summarize_debate,
)
from finpulse_engine.agent.roles import RoleRun
from finpulse_engine.agent.tools import ToolRegistry, default_registry

from tests.test_agent import _forecaster_factory, _synthetic_ctx, _inline_role


def _panel(**over):
    raw = {
        "id": "p", "name": "测试投委会", "description": "d",
        "chair": "committee_chair",
        "members": [
            {"role": "technical_analyst", "weight": 1.0},
            {"role": "flow_analyst", "weight": 1.0},
            {"role": "risk_officer", "weight": 1.0},
        ],
        "quorum": 2,
        "rounds": 2,
    }
    raw.update(over)
    return agent_config.parse_panel(raw, source="<inline>")


def _member(role_id, direction, confidence="HIGH", weight=1.0, ok=True):
    run = RoleRun(role_id=role_id, role_name=role_id, category="technical",
                  text=f"## 趋势状态\n{direction}\n## 置信度\n{confidence}\n",
                  direction=direction, confidence=confidence, weight=weight)
    if not ok:
        run.error = "模拟失败"
    return run


# ── panel 配置 ────────────────────────────────────────────────────


class TestPanelConfig(unittest.TestCase):
    def test_shipped_panel_loads(self):
        panels = agent_config.load_panels()
        self.assertIn("default_committee", panels)
        p = panels["default_committee"]
        self.assertEqual(p.chair, "committee_chair")
        self.assertEqual(len(p.members), 3)
        self.assertEqual(p.rounds, 2)

    def test_shipped_panel_has_no_problems(self):
        """仓库自带的那套投委会必须完全通过校验。"""
        roles = agent_config.load_all()
        for pid, panel in agent_config.load_panels().items():
            problems = agent_config.validate_panel(panel, roles)
            errors = [m for lv, m in problems if lv == agent_config.PANEL_ERROR]
            self.assertFalse(errors, msg=f"{pid}: {errors}")

    def test_unknown_field_rejected(self):
        raw = {"id": "p", "name": "n", "chair": "c", "members": [],
               "round": 2}   # 少了 s
        with self.assertRaises(agent_config.ConfigError) as ctx:
            agent_config.parse_panel(raw, source="<inline>")
        self.assertIn("round", str(ctx.exception))

    def test_empty_members_rejected(self):
        with self.assertRaises(agent_config.ConfigError):
            _panel(members=[])

    def test_duplicate_member_rejected(self):
        with self.assertRaises(agent_config.ConfigError) as ctx:
            _panel(members=[{"role": "a"}, {"role": "a"}])
        self.assertIn("重复", str(ctx.exception))

    def test_chair_cannot_be_a_member(self):
        """主席参与计票就等于"自己统计自己的票"。"""
        with self.assertRaises(agent_config.ConfigError) as ctx:
            _panel(chair="technical_analyst")
        self.assertIn("主席", str(ctx.exception))

    def test_non_positive_weight_rejected(self):
        with self.assertRaises(agent_config.ConfigError):
            _panel(members=[{"role": "a", "weight": 0}])

    def test_quorum_bounds(self):
        with self.assertRaises(agent_config.ConfigError):
            _panel(quorum=9)
        with self.assertRaises(agent_config.ConfigError):
            _panel(quorum=0)

    def test_rounds_bounds(self):
        with self.assertRaises(agent_config.ConfigError):
            _panel(rounds=99)

    def test_missing_member_role_is_an_error_level_problem(self):
        roles = agent_config.load_all()
        problems = agent_config.validate_panel(
            _panel(members=[{"role": "nope"}, {"role": "flow_analyst"}] + []), roles
        )
        levels = [lv for lv, _ in problems]
        self.assertIn(agent_config.PANEL_ERROR, levels)

    def test_unknown_member_is_error_but_no_warning_noise(self):
        roles = agent_config.load_all()
        problems = agent_config.validate_panel(
            _panel(members=[{"role": "nope"}, {"role": "flow_analyst"}]), roles)
        self.assertEqual(
            [lv for lv, _ in problems if lv == agent_config.PANEL_ERROR],
            [agent_config.PANEL_ERROR])
        self.assertFalse([lv for lv, _ in problems
                          if lv == agent_config.PANEL_WARNING])

    def test_all_risk_members_warns_about_no_direction(self):
        """只由风险类角色组成的会开不出方向，这是合法配置但要提醒。"""
        roles = agent_config.load_all()
        problems = agent_config.validate_panel(
            _panel(members=[{"role": "risk_officer"}], quorum=1), roles)
        warnings = [m for lv, m in problems if lv == agent_config.PANEL_WARNING]
        self.assertTrue(any("方向" in m for m in warnings), msg=str(problems))

    def test_normal_panel_has_no_warning(self):
        roles = agent_config.load_all()
        problems = agent_config.validate_panel(self._full_panel(), roles)
        self.assertEqual(problems, [], msg=str(problems))

    @staticmethod
    def _full_panel():
        return _panel()


# ── 单角色与并行团队 ──────────────────────────────────────────────


class TestRunRoleAndTeam(unittest.TestCase):
    def setUp(self):
        self.orch = Orchestrator(tools=default_registry(), guards=Pipeline())
        self.ctx = _synthetic_ctx()
        self.ctx.forecaster_factory = _forecaster_factory()

    def test_run_role(self):
        run = self.orch.run_role("technical_analyst", self.ctx)
        self.assertTrue(run.ok, msg=run.error)
        self.assertEqual(run.role_id, "technical_analyst")

    def test_run_role_unknown_reports_options(self):
        with self.assertRaises(agent_config.ConfigError) as ctx:
            self.orch.run_role("nope", self.ctx)
        self.assertIn("nope", str(ctx.exception))

    def test_run_team_runs_every_member(self):
        result = self.orch.run_team(
            ["technical_analyst", "flow_analyst", "risk_officer"], self.ctx)
        self.assertEqual(len(result.runs), 3)
        for rid, run in result.runs.items():
            self.assertTrue(run.ok, msg=f"{rid}: {run.error}")

    def test_run_team_applies_weights(self):
        result = self.orch.run_team(
            ["technical_analyst", "flow_analyst"], self.ctx,
            weights={"technical_analyst": 2.5, "flow_analyst": 0.5})
        self.assertEqual(result.runs["technical_analyst"].weight, 2.5)
        self.assertEqual(result.runs["flow_analyst"].weight, 0.5)

    def test_run_team_warns_on_unknown_tool_in_config(self):
        """角色声明了未注册的工具时要留痕，而不是悄悄少一个工具位。"""
        cfg = agent_config.parse_config(_inline_role(**{
            "config": {
                "model": {"provider": "rule_based"},
                "instructions": "你是一个用于测试的角色。" * 10,
                "tools": ["indicators", "not_a_tool"],
                "output_sections": ["趋势状态", "置信度"],
            }
        }))
        orch = Orchestrator(roles={"probe": cfg}, panels={},
                            tools=default_registry(), guards=Pipeline())
        result = orch.run_team(["probe"], self.ctx)
        self.assertTrue(any("not_a_tool" in i.detail for i in result.issues),
                        msg=str([i.to_json() for i in result.issues]))


# ── 投委会 ────────────────────────────────────────────────────────


class TestDebate(unittest.TestCase):
    def setUp(self):
        self.orch = Orchestrator(tools=default_registry(), guards=Pipeline())
        self.ctx = _synthetic_ctx()
        self.ctx.forecaster_factory = _forecaster_factory()

    def test_debate_end_to_end(self):
        result = self.orch.debate(self.ctx)
        self.assertTrue(result.valid, msg=str(result.problems))
        self.assertEqual(len(result.rounds), 2)
        self.assertIsNotNone(result.chair)
        self.assertTrue(result.chair.ok, msg=result.chair.error)
        for section in self.orch.roles["committee_chair"].output_sections:
            self.assertIn(section, result.chair.text)
        # 段落齐全还不够。规则后端遇到没写实现的段落会填一句占位说明 ——
        # 正文非空，缺段检查（只看"有没有内容"）完全看不出来。所以这里
        # 单独守一遍：配置里声明的每一段，都必须真的有人写过。
        self.assertNotIn("本后端没有为", result.chair.text,
                         msg="主席有段落没有规则实现，报告里留了占位句")
        self.assertIn(result.direction, ("BULLISH", "BEARISH", "NEUTRAL"))

    def test_round_index_recorded(self):
        result = self.orch.debate(self.ctx)
        self.assertTrue(all(r.round_index == 1 for r in result.rounds[0].runs.values()))
        self.assertTrue(all(r.round_index == 2 for r in result.rounds[1].runs.values()))

    def test_second_round_sees_first_round_peers(self):
        """第二轮里风险官要能引用别人的结论 —— 这是"质证真的发生了"的证据。"""
        result = self.orch.debate(self.ctx)
        risk = result.rounds[1].runs["risk_officer"]
        self.assertNotIn("单角色运行", risk.text)
        self.assertTrue(
            "技术面分析师" in risk.text or "量价分析师" in risk.text,
            msg=f"风险官没有看到其它委员的结论：{risk.text[-300:]}",
        )

    def test_first_round_has_no_peers(self):
        """第一轮必须是独立研判，不能给人看别人的结论。"""
        result = self.orch.debate(self.ctx)
        risk = result.rounds[0].runs["risk_officer"]
        self.assertIn("单角色运行", risk.text)

    def test_risk_officer_never_flips_to_a_quoted_direction(self):
        """真实回归：第二轮风险官的方向从 None（弃权）变成 BULLISH。

        根因是结论抽取扫了全部段落，把「反对意见」段里**引述**别人的方向
        当成了风险官自己的表态。后果是弃权票被算成看多票 —— 决议方向被
        一个从未对方向表态的角色带偏。这里钉住：两轮都必须保持弃权，
        且不能出现在"质证改口"名单里。
        """
        result = self.orch.debate(self.ctx)
        self.assertTrue(result.valid, msg=str(result.problems))

        seen = [rnd.runs["risk_officer"].direction for rnd in result.rounds]
        self.assertEqual(seen, [None, None],
                         msg=f"风险官在某一轮凭空表态了：{seen}")
        self.assertNotIn("risk_officer", result.flips(),
                         msg="弃权被误报成改口")

        # 前置条件核对：它确实在引述别人的方向，否则这条测试是空转。
        quoted = any(d in result.rounds[1].runs["risk_officer"].text
                     for d in ("BULLISH", "BEARISH"))
        self.assertTrue(quoted, msg="这个用例的前提不成立：报告里没有引述")
        for rnd in result.rounds:
            self.assertEqual(rnd.runs["risk_officer"].check.direction_sections,
                             ["下行风险"])

    def test_single_round_skips_cross_examination(self):
        result = self.orch.debate(self.ctx, rounds=1)
        self.assertEqual(len(result.rounds), 1)
        self.assertIn("单角色运行",
                      result.rounds[0].runs["risk_officer"].text)

    def test_quorum_not_met_produces_no_resolution(self):
        """全员拿不到数据时不能给方向 —— 那是把系统性故障伪装成决策意见。"""
        # 空工具注册表 = 模拟"数据源全挂了"。委员照样会产出文本
        # （通篇"工具未返回…"），run.ok 是 True，但没有形成任何判断。
        orch = Orchestrator(roles=self.orch.roles,
                            panels={"p": _panel(quorum=3)}, tools=ToolRegistry(),
                            guards=Pipeline())
        result = orch.debate(self.ctx, panel="p")
        self.assertFalse(result.valid)
        self.assertIsNone(result.chair)
        self.assertTrue(any("quorum" in p["message"] for p in result.problems),
                        msg=str(result.problems))
        # 问题说明里要指出真实原因，而不是只说"票不够"。
        joined = " ".join(p["message"] for p in result.problems)
        self.assertIn("工具数据", joined)

    def test_happy_path_members_are_effective(self):
        """反向验证：正常跑时三个委员都必须是"有效"的，否则上面那条闸门形同虚设。"""
        result = self.orch.debate(self.ctx)
        for rid, run in result.rounds[-1].runs.items():
            self.assertTrue(is_effective(run), msg=f"{rid} 未被判定为有效")

    def test_partial_failure_excludes_only_the_broken_member(self):
        """部分委员没拿到数据时，只把它排除，会照开。"""
        roles = dict(self.orch.roles)
        # 造一个声明了不存在工具的角色 —— 它调不到任何工具，因此拿不到
        # 任何 tool_results，会被判为"未形成有效判断"。
        from tests.test_agent import _inline_role
        broken = agent_config.parse_config(_inline_role(
            id="broken", name="坏的委员",
            config={
                "model": {"provider": "rule_based"},
                "instructions": "你是一个用于测试的角色。" * 10,
                "tools": ["no_such_tool"],
                "output_schema": "market_view",
                "output_sections": ["趋势状态", "置信度"],
            },
        ))
        roles["broken"] = broken

        orch = Orchestrator(roles=roles,
                            panels={"p": _panel(members=[
                                {"role": "technical_analyst"},
                                {"role": "flow_analyst"},
                                {"role": "broken"}], quorum=2)},
                            tools=default_registry(), guards=Pipeline())
        result = orch.debate(self.ctx, panel="p")
        self.assertTrue(result.valid, msg=str(result.problems))
        self.assertTrue(any(p["level"] == "warning" and "broken" in p["message"]
                            for p in result.problems), msg=str(result.problems))
        # 主席不能看到那个没拿到数据的委员。
        self.assertIn("2 位分析师", result.chair.text)

    def test_fatal_panel_problem_raises(self):
        orch = Orchestrator(roles=self.orch.roles,
                            panels={"p": _panel(members=[{"role": "nope"},
                                                         {"role": "flow_analyst"}])},
                            tools=default_registry(), guards=Pipeline())
        with self.assertRaises(agent_config.ConfigError) as ctx:
            orch.debate(self.ctx, panel="p")
        self.assertIn("nope", str(ctx.exception))

    def test_unknown_panel_name_lists_options(self):
        with self.assertRaises(OrchestrationError) as ctx:
            self.orch.debate(self.ctx, panel="nope")
        self.assertIn("nope", str(ctx.exception))

    def test_ambiguous_panel_requires_explicit_choice(self):
        """有多个投委会时必须显式指定 —— 猜错了代价比多敲一个参数高。"""
        orch = Orchestrator(roles=self.orch.roles,
                            panels={"a": _panel(id="a"), "b": _panel(id="b")},
                            tools=default_registry(), guards=Pipeline())
        with self.assertRaises(OrchestrationError):
            orch.debate(self.ctx)

    def test_single_panel_is_used_by_default(self):
        orch = Orchestrator(roles=self.orch.roles,
                            panels={"only": _panel(id="only")},
                            tools=default_registry(), guards=Pipeline())
        result = orch.debate(self.ctx)
        self.assertEqual(result.panel.id, "only")

    def test_summarize_debate_shape(self):
        result = self.orch.debate(self.ctx)
        s = summarize_debate(result)
        self.assertTrue(s["valid"])
        self.assertIn("direction", s)
        self.assertEqual(len(s["members"]), 3)
        self.assertIn("flips", s)

    def test_summarize_invalid_debate(self):
        result = self.orch.debate(self.ctx)
        result.valid = False
        s = summarize_debate(result)
        self.assertFalse(s["valid"])
        self.assertIn("problems", s)

    def test_json_is_serializable(self):
        import json
        result = self.orch.debate(self.ctx)
        payload = json.dumps(result.to_json(), ensure_ascii=False)
        self.assertIn("default_committee", payload)

    def test_议题随结果一起回传(self):
        """议题（本次运行参数）必须进 JSON —— C++ 侧靠它渲染报告头部。

        断言具体内容而不是"非空"：只查非空的话，引擎漏了标的、只回一句
        "样本 260 根"照样能绿，而用户看到的正是缺了标的那一版。
        """
        result = self.orch.debate(self.ctx)
        self.assertIn(self.ctx.symbol, result.intent)
        self.assertIn(str(self.ctx.bars), result.intent)
        self.assertIn(self.ctx.forecast_method, result.intent)
        self.assertEqual(result.to_json()["intent"], result.intent)

    def test_memory_is_shared_and_thread_safe(self):
        """并行跑委员时记忆是共享的，记录不能丢。"""
        import tempfile
        from pathlib import Path

        with tempfile.TemporaryDirectory() as tmp:
            mem = DecisionMemory(path=Path(tmp) / "mem.jsonl")
            orch = Orchestrator(tools=default_registry(), guards=Pipeline(),
                                memory=mem)
            orch.debate(self.ctx)
            # 3 位委员 × 2 轮 + 主席 = 7 条
            self.assertGreaterEqual(len(mem.all()), 6)
            on_disk = (Path(tmp) / "mem.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(on_disk), len(mem.all()))


# ── 加权计票（对 Fincept 的刻意改进） ──────────────────────────────


class TestWeightedVoting(unittest.TestCase):
    """Fincept 的 IC 配了成员权重却从不读取。这里必须证明权重真的生效。"""

    def _chair_with(self, peers):
        from finpulse_engine.agent.llm.rule_based import _chair_state
        return _chair_state({"peers": [
            {"role_id": r.role_id, "name": r.role_name,
             "direction": r.direction, "confidence": r.confidence,
             "weight": r.weight}
            for r in peers
        ]})

    def test_equal_weights_fall_back_to_headcount(self):
        state = self._chair_with([
            _member("a", "BULLISH"), _member("b", "BEARISH")])
        self.assertFalse(state["weighting_active"])
        self.assertEqual(state["direction"], "NEUTRAL")
        self.assertTrue(state["tie"])

    def test_weight_flips_the_decision(self):
        """同样两个人同样两个方向，只改权重，决议必须翻转。"""
        state = self._chair_with([
            _member("a", "BULLISH", weight=0.5),
            _member("b", "BEARISH", weight=2.0)])
        self.assertTrue(state["weighting_active"])
        self.assertEqual(state["direction"], "BEARISH")
        self.assertFalse(state["tie"])

        flipped = self._chair_with([
            _member("a", "BULLISH", weight=2.0),
            _member("b", "BEARISH", weight=0.5)])
        self.assertEqual(flipped["direction"], "BULLISH")

    def test_confidence_cap_ignores_dissenters(self):
        """投反对票的分析师报 HIGH，不能把决议置信度抬上去。"""
        state = self._chair_with([
            _member("a", "BULLISH", "LOW", weight=2.0),
            _member("b", "BEARISH", "HIGH", weight=1.0)])
        self.assertEqual(state["direction"], "BULLISH")
        self.assertEqual(state["cap"], "LOW")     # 只取支持方
        self.assertEqual(state["cap_all"], "HIGH")

    def test_tie_uses_all_peers_for_cap(self):
        """平票时问的是"信息不足以定方向"，两边的高置信度反而是佐证。"""
        state = self._chair_with([
            _member("a", "BULLISH", "HIGH"), _member("b", "BEARISH", "HIGH")])
        self.assertTrue(state["tie"])
        self.assertEqual(state["cap_all"], "HIGH")

    def test_abstention_excluded_from_vote(self):
        state = self._chair_with([
            _member("a", "BULLISH"), _member("b", None),
            _member("c", "NEUTRAL")])
        self.assertEqual(state["abstain_count"], 1)
        self.assertEqual(state["voted"], 2)
        self.assertEqual(state["direction"], "BULLISH")


if __name__ == "__main__":
    unittest.main()
