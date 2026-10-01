"""智能体分析层。

复刻 Fincept Terminal ``scripts/agents/finagent_core`` 的核心设计，
但**不依赖 Agno 之类的 agent 框架**，也**不做模型训练**：

* 角色用 JSON **声明式**定义（:mod:`config`），加角色不改代码；
* 分析与模型交互通过统一后端接口（:mod:`llm`），可在真实 LLM 与
  确定性规则后端之间切换；
* 工具分本地与远程两类（:mod:`tools`），远程工具**反向回传给 C++ 终端执行**
  ——这是本项目"联合能力"的关键：终端自身的能力成为智能体的工具箱；
* 输出受结构化契约与护栏约束（:mod:`schemas` / :mod:`guardrails`）；
* 多角色通过 :mod:`orchestrator` 编排，支持独立研判与投委会辩论。
"""

from finpulse_engine.agent.config import (
    ConfigError,
    KNOWN_CATEGORIES,
    KNOWN_SCHEMAS,
    ModelConfig,
    RoleConfig,
    load_all,
    load_config,
    parse_config,
    select_roles,
)

__all__ = [
    "ConfigError",
    "RoleConfig",
    "ModelConfig",
    "KNOWN_CATEGORIES",
    "KNOWN_SCHEMAS",
    "load_config",
    "load_all",
    "parse_config",
    "select_roles",
]
