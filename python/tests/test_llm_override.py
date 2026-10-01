# -*- coding: utf-8 -*-
"""LLM 运行时覆盖与推理后端自述测试。

这一组测试对应一个具体的困惑：**"你这个 agent 到底拿什么在思考？"**

在此之前，这个问题的答案只存在于 ``configs/*.json`` 里 —— 而默认配置写的是
``rule_based``（一个不联网的规则后端）。于是从外面完全看不出它究竟是"没连模型"
还是"连了但你没配"。补了两样东西：

  1. :class:`LlmOverride` —— 命令行/界面**当场**传入 provider / model /
     base_url / api_key，不改文件、不设环境变量；
  2. ``agent.llm.status`` —— 把"有哪些后端、当前环境里哪些真能用、
     各角色实际会落到哪一个"变成一次可查的 RPC。

这里钉住三件事：**覆盖真的落到了后端构造上**、**缺密钥的降级能被看见**、
**密钥不会被回传出去**。
"""

from __future__ import annotations

import json
import math
import tempfile
import threading
import unittest
from pathlib import Path

from finpulse_engine.agent import api as agent_api
from finpulse_engine.agent.config import ModelConfig
from finpulse_engine.agent.guardrails import Pipeline
from finpulse_engine.agent.llm import registry as llm_registry
from finpulse_engine.agent.llm.override import LlmOverride, from_params
from finpulse_engine.agent.memory import DecisionMemory
from finpulse_engine.agent.orchestrator import Orchestrator
from finpulse_engine.agent.tools import default_registry
from finpulse_engine.rpc import Dispatcher
from finpulse_engine.stream import NullSink


def make_bars(n: int = 200) -> list:
    out = []
    for i in range(n):
        wave = 0.02 * math.sin(i / 11.0) + 0.008 * math.cos(i / 3.7)
        c = 100.0 * (1.0 + 0.0004 * i + wave)
        out.append({
            "ts": 1_700_000_000_000 + i * 86_400_000,
            "open": round(c * 0.999, 4),
            "high": round(c * 1.006, 4),
            "low": round(c * 0.994, 4),
            "close": round(c, 4),
            "volume": int(1_500_000 * (1.0 + 10.0 * abs(wave))),
        })
    return out


def make_service() -> agent_api.AgentService:
    mem = DecisionMemory(path=Path(tempfile.mkdtemp()) / "mem.jsonl")
    orch = Orchestrator(tools=default_registry(), guards=Pipeline(), memory=mem)
    return agent_api.AgentService(orchestrator=orch, memory=mem, sink=NullSink())


def build_backend(model: ModelConfig, override=None):
    """直接调后端工厂。返回 (provider, used_name, fallback_reason)。"""
    return llm_registry.build(
        model,
        override=override,
        role_id="test_role",
        sections=["结论"],
        category="technical",
        tool_order=[],
        tool_defaults={},
    )


# ── 覆盖对象 ──────────────────────────────────────────────────────


class OverrideObjectTests(unittest.TestCase):

    def test_全空缺省表示不覆盖(self):
        # 这一点很关键：前端提交一个全空的表单是常态，
        # 若被当成"要覆盖成空 provider"，每次研判都会莫名降级。
        self.assertFalse(LlmOverride().any())
        self.assertIsNone(from_params({}))

    def test_任意一项非空即为覆盖(self):
        for ov in (LlmOverride(provider="openai"),
                   LlmOverride(model_id="gpt-4o"),
                   LlmOverride(base_url="http://127.0.0.1:8000/v1"),
                   LlmOverride(api_key="sk-x")):
            self.assertTrue(ov.any())

    def test_空白字符不算填入(self):
        self.assertIsNone(from_params({"provider": "   ", "api_key": "\t"}))

    def test_自述里不含密钥(self):
        secret = "sk-do-not-leak-1234567890"
        ov = LlmOverride(provider="openai", api_key=secret)
        dumped = json.dumps(ov.describe(), ensure_ascii=False)
        self.assertNotIn(secret, dumped)
        self.assertNotIn("1234567890", dumped)     # 连片段都不能有
        self.assertTrue(ov.describe()["has_api_key"])

    def test_从rpc参数构造(self):
        ov = from_params({"provider": " deepseek ", "model": "deepseek-chat",
                          "base_url": " https://api.deepseek.com/v1 ",
                          "api_key": " sk-1 "})
        self.assertEqual("deepseek", ov.provider)
        self.assertEqual("deepseek-chat", ov.model_id)
        self.assertEqual("https://api.deepseek.com/v1", ov.base_url)
        self.assertEqual("sk-1", ov.api_key)


# ── 覆盖必须真的落到后端上 ────────────────────────────────────────


class BackendOverrideTests(unittest.TestCase):

    def test_覆盖provider换掉实际后端(self):
        # 配置里是 rule_based（默认），覆盖成 deepseek 并给密钥 →
        # 必须真的构造出 DeepSeek 后端，而不是"参数收下了但没用"。
        used_obj, used, reason = build_backend(
            ModelConfig(), LlmOverride(provider="deepseek", api_key="sk-test"))
        self.assertEqual("deepseek", used)
        self.assertIsNone(reason)
        self.assertEqual("deepseek", getattr(used_obj, "provider", ""))

    def test_只有运行时密钥时不算缺密钥(self):
        # 这是最容易搞错的一条：密钥只在命令行上给了，没设环境变量。
        # 若降级判断只看环境变量，用户会看到"缺少密钥"——而他明明填了。
        _, used, reason = build_backend(
            ModelConfig(provider="deepseek"), LlmOverride(api_key="sk-test"))
        self.assertEqual("deepseek", used)
        self.assertIsNone(reason)

    def test_没有密钥时降级并给出补救办法(self):
        import os
        old = os.environ.pop("DEEPSEEK_API_KEY", None)
        try:
            _, used, reason = build_backend(ModelConfig(provider="deepseek"))
            self.assertEqual("rule_based", used)
            self.assertIsNotNone(reason)
            # 降级信息里要写清楚"怎么才能用上"，只说"缺密钥"帮不到人
            self.assertIn("--api-key", reason)
        finally:
            if old is not None:
                os.environ["DEEPSEEK_API_KEY"] = old

    def test_覆盖base_url与模型名(self):
        used_obj, used, _ = build_backend(
            ModelConfig(),
            LlmOverride(provider="ollama", model_id="qwen2.5:7b",
                        base_url="http://127.0.0.1:11434/v1"))
        self.assertEqual("ollama", used)
        self.assertEqual("qwen2.5:7b", used_obj.model_id)
        self.assertTrue(used_obj.endpoint.endswith("/chat/completions"))

    def test_本地端点不需要密钥(self):
        # ollama / vLLM 这类本地部署通常不校验密钥，不该被判成"缺凭据"。
        _, used, reason = build_backend(
            ModelConfig(), LlmOverride(provider="ollama",
                                       base_url="http://127.0.0.1:11434/v1"))
        self.assertEqual("ollama", used)
        self.assertIsNone(reason)

    def test_不认识的provider不静默接受(self):
        # 静默退回规则后端最坏的结果是：用户一直以为自己在用大模型。
        _, used, reason = build_backend(
            ModelConfig(), LlmOverride(provider="gpt-5-turbo-ultra", api_key="k"))
        self.assertEqual("rule_based", used)
        self.assertIn("不在注册表", reason)

    def test_覆盖不影响未指定的字段(self):
        model = ModelConfig(provider="openai", model_id="gpt-4o-mini",
                            base_url="https://example.invalid/v1", temperature=0.7)
        used_obj, _, _ = build_backend(model, LlmOverride(api_key="sk-x"))
        self.assertEqual("gpt-4o-mini", used_obj.model_id)
        self.assertIn("example.invalid", used_obj.endpoint)


# ── 后端自述 ──────────────────────────────────────────────────────


class LlmStatusTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.svc = make_service()
        cls.d = Dispatcher()
        agent_api.install(cls.d, out=None, service=cls.svc,
                          write_lock=threading.Lock())

    def _call(self, method, params=None):
        resp = self.d.handle({"id": 1, "method": method, "params": params or {}})
        self.assertIsNotNone(resp)
        self.assertTrue(resp.get("ok"), resp)
        return resp["result"]

    def test_列出全部内置后端(self):
        out = self._call("agent.llm.status")
        names = {p["name"] for p in out["providers"]}
        for expected in ("rule_based", "openai", "deepseek", "moonshot",
                        "dashscope", "openrouter", "groq", "ollama", "openai_compat"):
            self.assertIn(expected, names)

    def test_每个角色都给出实际生效的后端(self):
        out = self._call("agent.llm.status")
        self.assertTrue(out["roles"])
        for r in out["roles"]:
            self.assertIn("configured_provider", r)
            self.assertIn("effective_provider", r)
            self.assertIn("degraded", r)

    def test_默认配置下不算用了外部后端(self):
        # 默认四个角色都是 rule_based。这里如实报 false，
        # 而不是含糊其辞 —— 用户需要一眼看出"现在没连模型"。
        out = self._call("agent.llm.status")
        status = {r["role"]: r for r in out["roles"]}
        self.assertEqual("rule_based", status["technical_analyst"]["effective_provider"])
        self.assertFalse(out["any_external"])

    def test_默认配置下没有降级(self):
        # 四个角色默认都是 rule_based —— 它是"配置选中的后端"，
        # 不是"降级的结果"。把 rule_based 一律标成 degraded 会让
        # 真正的降级（配了外部后端但缺密钥）淹没在噪音里。
        out = self._call("agent.llm.status")
        self.assertFalse(any(r["degraded"] for r in out["roles"]))

    def test_给出接入方式(self):
        out = self._call("agent.llm.status")
        blob = " ".join(out["how_to_connect"])
        self.assertIn("--api-key", blob)
        self.assertIn("configs/", blob)

    def test_后端自述里不含密钥(self):
        out = self._call("agent.llm.status")
        blob = json.dumps(out, ensure_ascii=False)
        # 环境变量的**名字**可以出现（那是使用说明），但形如真实密钥的
        # 字符串一个都不能有 —— 包括示例里的占位符：`sk-xxx` 这种写法
        # 既可能被人当成可用的值去试，也会被密钥扫描工具误报。
        self.assertNotIn("sk-", blob)
        for p in out["providers"]:
            self.assertNotIn("api_key", p)   # 只报"需不需要"，不报值

    def test_debate接受后端覆盖参数(self):
        # 参数名写错会被 Dispatcher 拦成 BadParams，所以这条同时验证了契约。
        out = self._call("agent.debate", {
            "bars": make_bars(200), "symbol": "TEST",
            "provider": "rule_based", "model": "", "base_url": "", "api_key": "",
            "include_text": False,
        })
        self.assertIn("direction", out)

    def test_单角色run也接受后端覆盖参数(self):
        out = self._call("agent.run", {
            "role": "technical_analyst", "bars": make_bars(200), "symbol": "TEST",
            "provider": "", "model": "", "base_url": "", "api_key": "",
            "include_text": False,
        })
        self.assertEqual("technical_analyst", out["role_id"])
        self.assertIn("provider", out)          # 每个角色都自报用了哪个后端


if __name__ == "__main__":
    unittest.main()
