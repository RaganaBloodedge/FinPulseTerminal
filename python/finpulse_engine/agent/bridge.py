"""反向工具通道 —— 让 C++ 终端的能力成为智能体的工具箱。

这是本项目"联合能力"的核心，也是 Fincept 那套架构里最值得抄的一点：

    通常的 C++/Python 分工是单向的：C++ 发请求，Python 算完返回。
    但真正的终端里有大量状态只存在于 C++ 侧——正在接收的实时行情、
    总线投递统计、连接状态。Python 分析进程**看不见**这些东西。

    Fincept 的解法是 ``TerminalMcpBridge``：C++ 起一个 localhost HTTP 服务端，
    把自己注册的工具目录暴露出去，Python 侧（``TerminalToolkit``）用
    ``urllib`` 回调 ``POST /tool``。于是工具调用方向反过来了 ——
    智能体在推理过程中可以随时回头问终端"现在盘口什么样"。

这里复刻同样的机制，但把工具目录压缩到 FinPulse 真正拥有的东西上
（行情快照、总线统计、数据质量），不做无意义的规模模仿。

**降级是明确可见的**：CLI 里单独跑分析时没有终端在监听，此时所有远程工具
返回 ``{"available": false, "reason": ...}``，而不是抛异常或返回空字典。
报告里会如实写出"该工具当前不可用"，而不是假装查过了。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional

#: 单次工具调用的超时。工具调用是推理链路里的一步，不能卡太久。
DEFAULT_TIMEOUT = 10.0


class RemoteToolClient:
    """远程工具客户端接口。"""

    def available(self) -> bool:
        return False

    def describe(self) -> Dict[str, Any]:
        return {"available": False, "endpoint": "", "tool_count": 0}

    def call(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        raise NotImplementedError

    def list_tools(self) -> List[Dict[str, Any]]:
        return []


class NullToolClient(RemoteToolClient):
    """没有终端连接时的占位实现。

    存在的意义是**让"没有终端"这件事变成一个正常的、可观察的状态**，
    而不是一个需要到处 try/except 的异常。每个调用都返回一条可读的原因，
    模型看到之后会如实写进报告（提示词里明确要求"工具没给数据就写数据未提供"）。
    """

    def __init__(self, reason: str = "未连接终端工具桥") -> None:
        self._reason = reason

    def describe(self) -> Dict[str, Any]:
        return {"available": False, "endpoint": "", "tool_count": 0,
                "reason": self._reason}

    def call(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "available": False,
            "tool": name,
            "reason": self._reason,
            "hint": "该工具需要 C++ 终端进程在监听。CLI 单独调用分析引擎时属正常现象。",
        }


class HttpToolClient(RemoteToolClient):
    """通过 HTTP 回调 C++ 侧的 :cpp:class:`fp::ToolBridge`。

    协议刻意保持最小：

    * ``GET  {endpoint}/tools`` → ``{"tools": [...], "count": n}``
    * ``POST {endpoint}/tool``  body ``{"name": ..., "arguments": {...}}``
      → ``{"ok": true, "result": {...}}`` 或 ``{"ok": false, "error": "..."}``

    令牌放在 ``X-FinPulse-Token`` 头里。服务端只监听 127.0.0.1，
    令牌是防同机其它进程顺手调用的第二道闸，不是网络防护。
    """

    def __init__(
        self,
        endpoint: str,
        token: str = "",
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self.endpoint = endpoint.rstrip("/")
        self.token = token
        self.timeout = timeout
        self._tools: Optional[List[Dict[str, Any]]] = None
        self._last_error: str = ""

    # ── 可用性 ────────────────────────────────────────────────
    def available(self) -> bool:
        if not self.endpoint:
            return False
        # 主动探一次目录：连不上就说明桥没起来，角色运行时会据此换用 NullToolClient。
        return self.list_tools() is not None and self._last_error == ""

    def describe(self) -> Dict[str, Any]:
        return {
            "available": self._last_error == "",
            "endpoint": self.endpoint,
            "tool_count": len(self._tools or []),
            "last_error": self._last_error or None,
        }

    # ── 目录 ─────────────────────────────────────────────────
    def list_tools(self) -> List[Dict[str, Any]]:
        if self._tools is not None:
            return self._tools
        data = self._request("GET", "/tools", None)
        if data is None:
            return []
        tools = data.get("tools")
        self._tools = tools if isinstance(tools, list) else []
        return self._tools

    # ── 调用 ─────────────────────────────────────────────────
    def call(self, name: str, arguments: Dict[str, Any]) -> Dict[str, Any]:
        data = self._request("POST", "/tool", {"name": name, "arguments": arguments or {}})
        if data is None:
            return {
                "available": False,
                "tool": name,
                "reason": self._last_error or "工具桥调用失败",
            }
        if not data.get("ok", False):
            # 工具在 C++ 侧执行失败：这是**工具失败**而不是**桥不可用**，
            # 两者要分开报——前者说明参数有问题，后者说明环境有问题。
            return {
                "available": True,
                "tool": name,
                "error": str(data.get("error") or "终端未说明失败原因"),
            }
        result = data.get("result")
        if not isinstance(result, dict):
            result = {"value": result}
        result.setdefault("available", True)
        result.setdefault("tool", name)
        return result

    # ── 内部 ─────────────────────────────────────────────────
    def _request(self, method: str, path: str, body: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        url = self.endpoint + path
        data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        headers = {"Content-Type": "application/json",
                   "User-Agent": "FinPulseEngine/0.5 (+tool-bridge)"}
        if self.token:
            headers["X-FinPulse-Token"] = self.token

        req = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = resp.read()
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", "replace")[:200]
            except Exception:
                pass
            self._last_error = f"HTTP {exc.code}: {detail or exc.reason}"
            return None
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            # 连不上是最常见的失败：桥没启动、端口被回收、终端已退出。
            self._last_error = f"无法连接工具桥 {self.endpoint}: {exc}"
            return None

        try:
            parsed = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            self._last_error = f"工具桥返回的不是合法 JSON: {exc}"
            return None
        if not isinstance(parsed, dict):
            self._last_error = "工具桥返回的 JSON 顶层不是对象"
            return None
        self._last_error = ""
        return parsed


def connect(endpoint: str, token: str = "") -> RemoteToolClient:
    """按端点建客户端；端点为空或连不上就返回 Null 实现。

    刻意不抛异常：**"没有终端"是一种正常状态**（CLI 单独跑分析时就是），
    不应该让调用方在每一处都写 try/except。
    """
    if not endpoint:
        return NullToolClient("未提供工具桥端点（--tool-bridge 未设置）")
    client = HttpToolClient(endpoint, token)
    if not client.list_tools():
        reason = client.describe().get("last_error") or "工具桥无响应"
        client._last_error = str(reason)
        return client  # 保留实例：后续每次调用都会带上具体的失败原因
    return client
