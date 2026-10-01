"""OpenAI 兼容后端。

一个实现覆盖所有说 ``/chat/completions`` 协议的服务：OpenAI 官方、
DeepSeek、Moonshot、通义、OpenRouter、本地 vLLM / LM Studio / Ollama
（``/v1`` 端点），以及任何自建的兼容网关。**要接入一家新服务，通常只需要
改配置里的 ``base_url`` 和 ``model_id``，不用写新代码。**

用 ``urllib`` 而不是 ``requests``：这里只有一个 POST，多一个第三方依赖
换不来什么。SSE 流式同理，逐行读就够了。

## 本模块负责把工具名翻译成"线上名"

``function.name`` 只允许 ``[A-Za-z0-9_-]``，而我们的远程工具叫
``terminal.live_quote`` —— 点号。实测 DeepSeek 会因此直接 400：

    Invalid 'tools[2].function.name': string does not match pattern
    '^[a-zA-Z0-9_-]+$'

它拒的是**整次请求**，所以三个委员一起"未产出结论"，一场投委会废掉。
**翻译放在这里，不放在注册表也不放在角色运行时**：点号只对"要 POST 出去"
这件事有问题，引擎内部、规则后端、轨迹、界面都按原名工作最省心。
翻译规则与那两个自检（撞名 / 超长）在 :mod:`finpulse_engine.agent.tools`
的"线上名"一节。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Sequence

from finpulse_engine.agent.llm.base import (
    LlmError,
    LlmProvider,
    LlmResponse,
    Message,
    ToolCall,
    Usage,
)
from finpulse_engine.agent.tools import WireCatalog, to_wire_catalog, to_wire_name

#: 单个请求的超时。分析类请求动辄生成上千 token，给宽一点。
DEFAULT_TIMEOUT = 120.0

#: 这些 HTTP 状态码值得重试；其余（401/403/400/404）重试没有意义。
_RETRYABLE_STATUS = frozenset({408, 409, 425, 429, 500, 502, 503, 504})


#: 各 provider 的官方 ``/v1`` 端点。模块级常量而不是函数里的一张字面量表 ——
#: 因为「取模型列表」也要用同一张表。两处各写一份的下场是：加了一家服务商，
#: 会话能通、模型列表拉不到，排查时才会发现是漏改了第二张表。
DEFAULT_BASE_URLS: Dict[str, str] = {
    "openai": "https://api.openai.com/v1",
    "deepseek": "https://api.deepseek.com/v1",
    "moonshot": "https://api.moonshot.cn/v1",
    "dashscope": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    "openrouter": "https://openrouter.ai/api/v1",
    "ollama": "http://127.0.0.1:11434/v1",
    "groq": "https://api.groq.com/openai/v1",
}


def base_api_url(base_url: str, provider: str) -> str:
    """把用户填的地址规整成"到 ``/v1`` 为止"的端点根。

    配置里允许写成四种形式，都接受（**末尾带什么路径都能吃**）：
      * ``https://api.deepseek.com``                      （补 /v1）
      * ``https://api.deepseek.com/v1``                   （原样）
      * ``https://api.deepseek.com/v1/``                  （原样）
      * ``https://api.deepseek.com/v1/chat/completions``  （砍掉尾部路径）

    把"砍路径"这件事收在这里，是为了让 :func:`models_url` 和
    :func:`_normalize_base_url` 共用同一套规整规则。否则用户从别处抄来一个
    带 ``/chat/completions`` 的完整地址，会话能通而模型列表 404 —— 这种
    "一半能用"的故障最难解释。
    """
    url = (base_url or "").strip().rstrip("/")
    if not url:
        url = DEFAULT_BASE_URLS.get(provider, "")
        if not url:
            raise LlmError(
                f"provider='{provider}' 没有内置端点，必须显式给出 base_url"
            )
    for suffix in ("/chat/completions", "/completions", "/models"):
        if url.endswith(suffix):
            url = url[: -len(suffix)].rstrip("/")
            break
    if not url.endswith("/v1"):
        url += "/v1"
    return url


def models_url(base_url: str, provider: str) -> str:
    """``GET`` 这个地址能问出"这个端点有哪些模型"。

    这是 OpenAI 兼容协议里唯一被普遍实现的发现机制 —— Cherry Studio、
    Open WebUI、Cline 都靠它填模型下拉。有了它，用户就不必去服务商文档里
    抄模型名。
    """
    return base_api_url(base_url, provider) + "/models"


def _normalize_base_url(base_url: str, provider: str) -> str:
    """把用户填的 base_url 规整成"到 /chat/completions 为止"的完整地址。"""
    return base_api_url(base_url, provider) + "/chat/completions"


class OpenAiCompatProvider(LlmProvider):
    """通过 HTTP 调用 OpenAI 兼容的 /chat/completions。"""

    name = "openai_compat"

    def __init__(
        self,
        *,
        model_id: str,
        provider: str = "openai",
        base_url: str = "",
        api_key: str = "",
        api_key_env: str = "",
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.model_id = model_id
        self.provider = provider
        self.endpoint = _normalize_base_url(base_url, provider)
        # 密钥优先级：配置里直接给的 > 环境变量。配置里给明文密钥不推荐，
        # 所以 C++ 侧注入的是环境变量；但本地调试时两种都得能用。
        self._api_key = api_key or (os.environ.get(api_key_env, "") if api_key_env else "")
        self._api_key_env = api_key_env
        self.timeout = timeout

    # ── 可用性 ────────────────────────────────────────────────
    def available(self) -> bool:
        # 本地端点（ollama / vLLM / 自建网关）常常不校验密钥，没有 key 也能用，
        # 所以 base_url 指向本机时不算缺失凭据。
        if "127.0.0.1" in self.endpoint or "localhost" in self.endpoint:
            return True
        return bool(self._api_key)

    def describe(self) -> Dict[str, Any]:
        d = super().describe()
        d["endpoint"] = self.endpoint
        d["model_id"] = self.model_id
        # 只回传"有没有"，绝不回传密钥本身或它的片段。
        d["has_api_key"] = bool(self._api_key)
        d["api_key_env"] = self._api_key_env
        return d

    # ── 调用 ─────────────────────────────────────────────────
    def complete(
        self,
        messages: Sequence[Message],
        *,
        tools: Optional[List[Dict[str, Any]]] = None,
        temperature: float = 0.3,
        max_tokens: int = 2048,
    ) -> LlmResponse:
        """跑一轮。``tools`` 里的名字是**注册表原名**，本方法负责把它与
        消息里携带的工具名一起换成线上名（见模块文档）。
        """
        if not self.available():
            raise LlmError(
                f"provider='{self.provider}' 缺少 API 密钥"
                + (f"（环境变量 {self._api_key_env} 为空）" if self._api_key_env else ""),
                retryable=False,
            )

        wire = to_wire_catalog(tools or [])
        body: Dict[str, Any] = {
            "model": self.model_id,
            "messages": [self._wire_message(m) for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if wire.schemas:
            body["tools"] = wire.schemas
            body["tool_choice"] = "auto"

        raw = self._post(body)
        return self._parse(raw, wire)

    # ── 线上名 ───────────────────────────────────────────────
    @staticmethod
    def _wire_message(message: Message) -> Dict[str, Any]:
        """把一条消息转成能发出去的形态。两件事：

        * **工具名换成线上名**。只改工具目录是不够的：``assistant`` 的历史
          ``tool_calls`` 与 ``role='tool'`` 消息都带着工具名，它们是同一个
          字段在另外两个方向上的复现。第二轮就会把第一轮的名字回填给服务端，
          照样 400 —— 而且报错位置从 ``tools[2]`` 变成 ``messages[3]``，
          看起来像另一个问题。
        * **``role='tool'`` 只带协议定义过的三个字段**。``Message.name`` 是
          我们自己为了可追溯加的；OpenAI 的 tool 消息类型里没有它，DeepSeek
          的官方示例也不带它。多送一个未定义的字段就是在赌服务端的宽容度，
          而 ``tool_call_id`` 已经足够把结果和调用对上。
        """
        if message.role == "tool":
            return {"role": "tool", "content": message.content,
                    "tool_call_id": message.tool_call_id}
        d = message.to_json()
        for call in d.get("tool_calls") or []:
            fn = call.get("function") or {}
            if fn.get("name"):
                fn["name"] = to_wire_name(fn["name"])
        return d

    # ── 内部 ─────────────────────────────────────────────────
    def _post(self, body: Dict[str, Any]) -> Dict[str, Any]:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            # 有些网关按 UA 做限流区分，给一个明确标识比留着默认的
            # Python-urllib 更礼貌，也便于对方排查。
            "User-Agent": "FinPulseTerminal/0.5 (+agent)",
        }
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"

        req = urllib.request.Request(self.endpoint, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = resp.read()
        except urllib.error.HTTPError as exc:
            # 错误响应体里通常有服务端的说明（配额、模型名写错等），
            # 直接丢掉会让排查变得很痛苦。截断保留前 400 字节。
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:400]
            except Exception:
                pass
            raise LlmError(
                f"HTTP {exc.code} 调用 {self.endpoint} 失败: {detail or exc.reason}",
                retryable=exc.code in _RETRYABLE_STATUS,
                status=exc.code,
            ) from exc
        except urllib.error.URLError as exc:
            raise LlmError(
                f"无法连接 {self.endpoint}: {exc.reason}", retryable=True
            ) from exc
        except TimeoutError as exc:
            raise LlmError(f"调用 {self.endpoint} 超时", retryable=True) from exc

        try:
            parsed = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LlmError(f"服务端返回的不是合法 JSON: {exc}", retryable=True) from exc
        if not isinstance(parsed, dict):
            raise LlmError("服务端返回的 JSON 顶层不是对象", retryable=True)
        return parsed

    @staticmethod
    def _parse(raw: Dict[str, Any], wire: WireCatalog) -> LlmResponse:
        # 有些网关把错误塞在 200 响应体里。
        if "error" in raw and not raw.get("choices"):
            err = raw["error"]
            msg = err.get("message") if isinstance(err, dict) else str(err)
            raise LlmError(f"服务端报错: {msg}")

        choices = raw.get("choices") or []
        if not choices:
            raise LlmError("服务端未返回任何 choice", retryable=True)

        choice = choices[0]
        msg = choice.get("message") or {}

        text = msg.get("content") or ""
        if isinstance(text, list):
            # 少数网关会返回 content 分块数组，拼起来。
            text = "".join(
                part.get("text", "") for part in text if isinstance(part, dict)
            )

        calls: List[ToolCall] = []
        for i, item in enumerate(msg.get("tool_calls") or []):
            fn = item.get("function") or {}
            name = fn.get("name") or ""
            if not name:
                continue
            args_raw = fn.get("arguments")
            args: Dict[str, Any] = {}
            if isinstance(args_raw, str) and args_raw.strip():
                try:
                    decoded = json.loads(args_raw)
                    # 模型偶尔会把参数包成数组，这里统一收敛成对象。
                    args = decoded if isinstance(decoded, dict) else {"value": decoded}
                except json.JSONDecodeError:
                    # 参数不是合法 JSON 时不静默丢弃：把原文塞进 _raw，
                    # 让工具执行阶段能给出"模型参数格式错误"的明确信息。
                    args = {"_raw": args_raw}
            elif isinstance(args_raw, dict):
                args = args_raw
            # 名字翻回注册表原名。认不出来的（模型编的）原样带回去，
            # 由工具层给出"未知工具"的结构化结果。
            calls.append(ToolCall(id=item.get("id") or f"call_{i}",
                                  name=wire.resolve(name), arguments=args))

        usage_raw = raw.get("usage") or {}
        usage = Usage(
            prompt_tokens=int(usage_raw.get("prompt_tokens") or 0),
            completion_tokens=int(usage_raw.get("completion_tokens") or 0),
        )

        return LlmResponse(
            text=text,
            tool_calls=calls,
            usage=usage,
            finish_reason=choice.get("finish_reason") or "stop",
            model=raw.get("model") or "",
        )
