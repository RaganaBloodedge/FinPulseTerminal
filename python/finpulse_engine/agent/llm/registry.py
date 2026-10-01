"""后端注册表 —— 按配置里的 ``model.provider`` 选中实现。

复刻 Fincept ``registries/models_registry.py`` 的核心行为，并加了一条它没有
而我这里必须有的规则：**降级必须可见**。

Fincept 在工具/向量库不可用时静默返回 ``None``（::

    except ImportError: return None

），对产品是可接受的——少一个工具而已。但在这里，如果配了 ``openai`` 却因为
没有 key 悄悄退回规则后端，用户会以为自己看到的是 LLM 的判断。所以
:func:`resolve` 会同时返回"实际用了谁"和"为什么换人"，由上层写进报告与轨迹。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional

from finpulse_engine.agent.config import ModelConfig
from finpulse_engine.agent.llm.base import LlmError, LlmProvider
from dataclasses import replace

from finpulse_engine.agent.llm.openai_compat import OpenAiCompatProvider
from finpulse_engine.agent.llm.override import LlmOverride
from finpulse_engine.agent.llm.rule_based import RuleBasedProvider

#: 各 provider 的默认密钥环境变量。与 C++ 侧注入的变量名保持一致。
API_KEY_ENV: Dict[str, str] = {
    "openai": "OPENAI_API_KEY",
    "deepseek": "DEEPSEEK_API_KEY",
    "moonshot": "MOONSHOT_API_KEY",
    "dashscope": "DASHSCOPE_API_KEY",
    "openrouter": "OPENROUTER_API_KEY",
    "groq": "GROQ_API_KEY",
    "ollama": "",  # 本地端点通常不校验密钥
    "openai_compat": "FINPULSE_LLM_API_KEY",
    "rule_based": "",
}


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    description: str
    needs_network: bool
    default_key_env: str = ""


SPECS: Dict[str, ProviderSpec] = {
    "rule_based": ProviderSpec(
        "rule_based",
        "确定性规则后端：不联网、不需密钥，按工具输出的真实数值渲染结构化研判",
        needs_network=False,
    ),
    "openai": ProviderSpec("openai", "OpenAI 官方 /chat/completions", True, "OPENAI_API_KEY"),
    "deepseek": ProviderSpec("deepseek", "DeepSeek 开放平台", True, "DEEPSEEK_API_KEY"),
    "moonshot": ProviderSpec("moonshot", "月之暗面 Kimi", True, "MOONSHOT_API_KEY"),
    "dashscope": ProviderSpec("dashscope", "阿里云百炼（OpenAI 兼容模式）", True, "DASHSCOPE_API_KEY"),
    "openrouter": ProviderSpec("openrouter", "OpenRouter 聚合网关", True, "OPENROUTER_API_KEY"),
    "groq": ProviderSpec("groq", "Groq 推理服务", True, "GROQ_API_KEY"),
    "ollama": ProviderSpec("ollama", "本地 Ollama（默认 127.0.0.1:11434）", False, ""),
    "openai_compat": ProviderSpec(
        "openai_compat",
        "任意 OpenAI 兼容端点：必须显式给出 model.base_url",
        True,
        "FINPULSE_LLM_API_KEY",
    ),
}

#: 所有走 HTTP 的 provider 都共用同一个实现类，差别只在端点与密钥。
_HTTP_PROVIDERS = frozenset({
    "openai", "deepseek", "moonshot", "dashscope", "openrouter", "groq",
    "ollama", "openai_compat",
})

#: ``model.provider`` 写成这些别名时，转成内部名。
_ALIASES = {"kimi": "moonshot", "qwen": "dashscope", "aliyun": "dashscope"}


def resolve(requested: str, model: ModelConfig, api_key: str = "") -> tuple:
    """决定实际用哪个后端。

    返回 ``(provider, fallback_reason)``。``fallback_reason`` 为 ``None``
    表示按配置执行；非 ``None`` 表示发生了降级，调用方**必须**把它写出来。

    ``api_key`` 是**运行时**给的密钥（命令行 ``--api-key`` / 界面输入框）。
    它必须参与"密钥够不够"的判断：只看环境变量的话，用户明明在命令行上
    填了 key，却会被判成"缺少密钥"然后降级 —— 这正是那种"我填了啊"的困惑。
    """
    name = _ALIASES.get((requested or "").strip().lower(), (requested or "").strip().lower())
    if not name:
        name = "rule_based"

    if name not in SPECS:
        # 不认识的 provider 名不静默接受：那通常意味着配置里拼错了，
        # 静默退回规则后端会让人一直以为自己在用 LLM。
        return "rule_based", (
            f"配置里的 provider='{requested}' 不在注册表中（可选 {sorted(SPECS)}），"
            f"已降级到规则后端"
        )

    if name == "rule_based":
        return name, None

    spec = SPECS[name]
    if spec.needs_network:
        env = API_KEY_ENV.get(name, "")
        base_url = model.base_url or ""
        local = "127.0.0.1" in base_url or "localhost" in base_url
        # 本地端点（ollama / 自建网关）不需要密钥，但**必须显式给 base_url**，
        # 否则会去连官方地址然后失败。
        if name == "openai_compat" and not base_url:
            return "rule_based", (
                "provider='openai_compat' 必须同时给出 model.base_url，"
                "否则无法确定要连哪个端点；已降级到规则后端"
            )
        if env and not os.environ.get(env) and not api_key and not local and not model.base_url:
            return "rule_based", (
                f"provider='{name}' 需要密钥，但环境变量 {env} 为空，"
                f"也没有在命令行/界面上直接给密钥，"
                f"已降级到规则后端（要启用请导出 {env}，或用 --api-key 传入）"
            )
    return name, None


def build(
    model: ModelConfig,
    *,
    override: Optional[LlmOverride] = None,
    role_id: str,
    sections: List[str],
    category: str = "",
    tool_order: List[str],
    tool_defaults: Dict[str, Dict[str, Any]],
    peers: Optional[List[Dict[str, Any]]] = None,
) -> tuple:
    """构造后端实例。

    返回 ``(provider, used_name, fallback_reason)``。降级信息原样透出，
    由 :mod:`finpulse_engine.agent.roles` 记录到执行轨迹里。

    ``override`` 是**本次运行**的显式覆盖（命令行 ``--provider/--api-key``、
    或界面上的输入框）。它是"显式接入大模型"这件事的落点：不用改 JSON、
    也不用事先导出环境变量，当场就能把这场研判接到真实模型上。
    """
    override = override or LlmOverride()

    if override.base_url or override.model_id:
        # 换端点与换模型名是同一件事的两面（例如把 ollama 指向另一台机器），
        # 没理由为此逼用户去改配置文件。
        model = replace(
            model,
            base_url=override.base_url or model.base_url,
            model_id=override.model_id or model.model_id,
        )

    used, reason = resolve(override.provider or model.provider, model,
                           api_key=override.api_key)

    if used == "rule_based":
        return (
            RuleBasedProvider(
                role_id=role_id,
                sections=sections,
                category=category,
                tool_order=tool_order,
                tool_defaults=tool_defaults,
                peers=peers,
            ),
            used,
            reason,
        )

    if used not in _HTTP_PROVIDERS:
        raise LlmError(f"provider='{used}' 没有对应的构造实现")

    env = API_KEY_ENV.get(used, "")
    provider = OpenAiCompatProvider(
        model_id=model.model_id,
        provider=used,
        base_url=model.base_url,
        # 运行时给的密钥优先于环境变量（构造函数内部就是这个优先级）。
        api_key=override.api_key,
        api_key_env=env,
    )
    if not provider.available():
        # resolve() 已经检查过一遍，这里是第二道闸：配置里显式给了 base_url
        # 但指向的不是本机、又没有密钥，就会走到这。
        return (
            RuleBasedProvider(
                role_id=role_id,
                sections=sections,
                category=category,
                tool_order=tool_order,
                tool_defaults=tool_defaults,
                peers=peers,
            ),
            "rule_based",
            f"provider='{used}' 实例化后仍不可用（缺密钥），已降级到规则后端",
        )
    return provider, used, None


def build_chat(
    model: ModelConfig,
    *,
    override: Optional[LlmOverride] = None,
) -> tuple:
    """为**自由问答**构造后端。返回 ``(provider|None, used_name, fallback_reason)``。

    与 :func:`build` 的区别在于任务形态：那里是"按契约出报告"
    （角色 + 段落契约 + 工具循环），这里是"按提问回答"。所以不需要
    sections / tools / peers 那一套。

    关键差异：**rule_based 不参与对话**。它靠真实数值把固定段落填成
    合乎格式的文本，做不了自由问答 —— 硬凑出来的"回答"会是一段看起来
    像话、其实和问题无关的模板，比明说"没有连接大模型"糟糕得多。
    因此这种情况返回 ``provider=None``，由上层把话说清楚。
    """
    override = override or LlmOverride()

    if override.base_url or override.model_id:
        model = replace(
            model,
            base_url=override.base_url or model.base_url,
            model_id=override.model_id or model.model_id,
        )

    used, reason = resolve(override.provider or model.provider, model,
                           api_key=override.api_key)

    if used == "rule_based":
        if not reason:
            # 配置里**主动**选了 rule_based（不是降级）：这不是错误，
            # 但对话这件事它确实做不了，得给一句能读懂的话。
            reason = ("当前后端是内置规则后端（rule_based），它按模板填充段落，"
                      "不支持自由对话；要对话请在设置里选一个真实模型后端")
        return None, used, reason

    if used not in _HTTP_PROVIDERS:
        raise LlmError(f"provider='{used}' 没有对应的构造实现")

    env = API_KEY_ENV.get(used, "")
    provider = OpenAiCompatProvider(
        model_id=model.model_id,
        provider=used,
        base_url=model.base_url,
        api_key=override.api_key,
        api_key_env=env,
    )
    if not provider.available():
        return (None, "rule_based",
                f"provider='{used}' 实例化后仍不可用（缺密钥）：请在设置里填密钥，"
                f"或导出环境变量 {env}")
    return provider, used, None


def list_providers() -> List[Dict[str, Any]]:
    """给 ``agent.roles`` / ``engine.info`` 用的自述列表。绝不回传密钥。"""
    out: List[Dict[str, Any]] = []
    for name, spec in sorted(SPECS.items()):
        available = True
        detail = ""
        if spec.needs_network and spec.default_key_env:
            available = bool(os.environ.get(spec.default_key_env))
            detail = f"需要环境变量 {spec.default_key_env}"
        elif name == "ollama":
            detail = "默认连 127.0.0.1:11434，可被 model.base_url 覆盖"
        out.append({
            "name": name,
            "description": spec.description,
            "needs_network": spec.needs_network,
            "available": available,
            "detail": detail,
        })
    return out
