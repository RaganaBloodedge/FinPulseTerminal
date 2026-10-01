"""分析后端层。

对外只暴露三样东西：接口（:mod:`base`）、注册表（:mod:`registry`）、
以及两个具体实现（:mod:`openai_compat` 与 :mod:`rule_based`）。
"""

from finpulse_engine.agent.llm.base import (
    LlmError,
    LlmProvider,
    LlmResponse,
    Message,
    ToolCall,
    Usage,
)
from finpulse_engine.agent.llm.registry import (
    API_KEY_ENV,
    SPECS,
    ProviderSpec,
    build,
    list_providers,
    resolve,
)
from finpulse_engine.agent.llm.rule_based import RuleBasedProvider

__all__ = [
    "LlmError",
    "LlmProvider",
    "LlmResponse",
    "Message",
    "ToolCall",
    "Usage",
    "RuleBasedProvider",
    "ProviderSpec",
    "SPECS",
    "API_KEY_ENV",
    "build",
    "list_providers",
    "resolve",
]
