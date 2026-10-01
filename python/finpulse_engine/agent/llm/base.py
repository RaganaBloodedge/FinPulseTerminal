"""分析后端的统一接口。

FinPulse 不依赖 Agno 之类的 agent 框架（见 docs/design-decisions.md），
所以 **tool-calling 循环是自己实现的**。这个模块定义循环所依赖的两个原语：

* :class:`Message` / :class:`ToolCall` / :class:`LlmResponse` —— 后端与循环之间的
  数据契约。刻意对齐 OpenAI 的消息协议（``role`` 取值 ``system`` / ``user`` /
  ``assistant`` / ``tool``），这样接真实 API 时不需要额外翻译层。
* :class:`LlmProvider` —— 后端必须实现的唯一方法 ``complete()``。

**为什么后端要分多种实现**：真实 LLM 需要 API key 和外网。一个只能在配好 key
的机器上跑的系统，等于没有 CI、也没法在无网环境当场演示。所以这里允许
注册"不需要网络的确定性后端"（:mod:`finpulse_engine.agent.llm.rule_based`），
它读同样的工具输出、产出同样结构的研判，只是不做自然语言推理。

这不是"假装有 AI"——它是**同一套接口的两种实现**，切换靠配置里的
``model.provider``，上层（角色运行时、编排、护栏）完全不知道区别。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence


class LlmError(RuntimeError):
    """后端调用失败。

    ``retryable`` 用来区分"再试一次可能就好"（限流、超时、5xx）与
    "重试没有意义"（认证失败、参数不合法）。编排层据此决定是否退避重试。
    """

    def __init__(self, message: str, *, retryable: bool = False, status: int = 0) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status = status


@dataclass
class Message:
    """一条对话消息。``role`` 只用 OpenAI 的四个取值。"""

    role: str
    content: str = ""
    #: 仅 role == "assistant" 且模型要求调用工具时使用。
    tool_calls: List["ToolCall"] = field(default_factory=list)
    #: 仅 role == "tool" 时使用，指向被回应的那次调用。
    tool_call_id: str = ""
    name: str = ""

    def to_json(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"role": self.role, "content": self.content}
        if self.name:
            d["name"] = self.name
        if self.tool_calls:
            d["tool_calls"] = [tc.to_json() for tc in self.tool_calls]
        if self.tool_call_id:
            d["tool_call_id"] = self.tool_call_id
        return d

    @staticmethod
    def system(text: str) -> "Message":
        return Message(role="system", content=text)

    @staticmethod
    def user(text: str) -> "Message":
        return Message(role="user", content=text)

    @staticmethod
    def assistant(text: str, *, tool_calls: Optional[List["ToolCall"]] = None) -> "Message":
        """模型（或回放的模型历史）说的一句话。

        ``system`` / ``user`` 都有对应的静态构造器，这里补齐第三个：
        **多轮对话**回放历史时必须能造出 assistant 消息，而
        ``Message(role="assistant", content=...)`` 这种裸构造在调用点
        很容易把 role 拼错成别的东西，而拼错的表现是"模型看不见自己
        说过什么"，不报错。
        """
        return Message(role="assistant", content=text, tool_calls=list(tool_calls or []))

    @staticmethod
    def tool_result(call_id: str, name: str, payload: str) -> "Message":
        return Message(role="tool", content=payload, tool_call_id=call_id, name=name)


@dataclass
class ToolCall:
    """模型要求调用某个工具。``arguments`` 已经是解析好的对象。"""

    id: str
    name: str
    arguments: Dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> Dict[str, Any]:
        import json as _json
        return {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.name,
                # OpenAI 协议里 arguments 是**字符串**，不是对象。这里做转换，
                # 免得每个后端各自记得这件事。
                "arguments": _json.dumps(self.arguments, ensure_ascii=False),
            },
        }


@dataclass
class Usage:
    """token 用量。确定性后端一律返回 0 —— 它不消耗 token，如实报 0。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def to_json(self) -> Dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass
class LlmResponse:
    """一次后端调用的结果。"""

    text: str = ""
    tool_calls: List[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    #: "stop" 表示正常结束；"tool_calls" 表示需要执行工具后再问一轮；
    #: "length" 表示被 max_tokens 截断（上层应当提示而不是当成完整答案）。
    finish_reason: str = "stop"
    model: str = ""

    @property
    def wants_tools(self) -> bool:
        return bool(self.tool_calls)


class LlmProvider(ABC):
    """分析后端的接口。实现这一个方法就能接入。"""

    #: 配置里 ``model.provider`` 用这个名字来选中本实现。
    name: str = "abstract"

    @abstractmethod
    def complete(
        self,
        messages: Sequence[Message],
        *,
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: float = 0.3,
        max_tokens: int = 2048,
    ) -> LlmResponse:
        """跑一轮。

        ``tools`` 是 OpenAI function-calling 格式的工具目录；为 ``None``
        或空列表表示本轮不允许调用工具。实现**不应**自己执行工具——执行与
        循环由 :mod:`finpulse_engine.agent.roles` 负责。
        """

    def available(self) -> bool:
        """当前环境能不能用。例如缺少 API key 时应返回 False。

        默认 True；需要凭据的实现要覆盖它，这样角色运行时会自动降级到
        可用的后端，而不是在发请求时才失败。
        """
        return True

    def describe(self) -> Dict[str, Any]:
        """给 ``agent.roles`` 接口回传的自述信息。不要包含密钥。"""
        return {"provider": self.name, "available": self.available()}
