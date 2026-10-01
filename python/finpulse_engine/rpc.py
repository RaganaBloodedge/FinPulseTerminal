"""RPC 方法注册与调度。

对应 C++ 侧的 ``RpcClient``。这一层只做三件事：

1. 用装饰器把函数登记成 RPC 方法（免去维护一张巨大的 if/elif 分派表）；
2. 参数名不匹配时给出明确的 ``BadParams``，而不是让它变成函数内部的 TypeError；
3. **任何异常都不能让引擎进程死掉** —— 全部转成 error 响应送回壳里。
   一个分析请求把整条终端带崩是不可接受的，这条是硬性要求。
"""

from __future__ import annotations

import inspect
import logging
import traceback
from typing import Any, Callable, Dict, Optional

log = logging.getLogger("finpulse.rpc")


class RpcError(Exception):
    """业务错误。

    ``code`` 是给 C++ 侧看的稳定标识（不要往里塞人类可读的细节，
    那些放 message / detail）。前端可以据此决定怎么提示。
    """

    def __init__(self, message: str, code: str = "EngineError", detail: str = "") -> None:
        super().__init__(message)
        self.code = code
        self.detail = detail


class BadParams(RpcError):
    """调用参数不合法。这类错误基本一定是壳里的 bug，值得单独一个 code。"""

    def __init__(self, message: str, detail: str = "") -> None:
        super().__init__(message, code="BadParams", detail=detail)


class NotFound(RpcError):
    """请求的东西不存在（未知符号、未知数据源等）。"""

    def __init__(self, message: str, detail: str = "") -> None:
        super().__init__(message, code="NotFound", detail=detail)


class BadData(RpcError):
    """数据本身有问题（CSV 缺列、价格非正、序列过短等）。"""

    def __init__(self, message: str, detail: str = "") -> None:
        super().__init__(message, code="BadData", detail=detail)


class Dispatcher:
    """方法表 + 调度。"""

    def __init__(self) -> None:
        self._methods: Dict[str, Callable[..., Any]] = {}
        self._summaries: Dict[str, str] = {}

    # ── 注册 ──────────────────────────────────────────────

    def method(self, name: Optional[str] = None) -> Callable[[Callable], Callable]:
        """装饰器：把一个函数暴露成 RPC 方法。

        >>> @dispatcher.method("source.load")
        ... def load(source: str, symbol: str, bars: int = 250):
        ...     ...
        """

        def deco(fn: Callable[..., Any]) -> Callable[..., Any]:
            key = name or fn.__name__
            if key in self._methods:
                raise RuntimeError(f"RPC 方法重复注册: {key}")
            self._methods[key] = fn
            doc = (fn.__doc__ or "").strip()
            self._summaries[key] = doc.splitlines()[0] if doc else ""
            return fn

        return deco

    @property
    def methods(self) -> Dict[str, str]:
        """方法名 → 一行说明。用于自省和文档生成。"""
        return dict(self._summaries)

    # ── 调度 ──────────────────────────────────────────────

    def handle(self, msg: Any) -> Optional[Dict[str, Any]]:
        """处理一条已解码的消息，返回要发回去的响应（None 表示无需响应）。"""
        if not isinstance(msg, dict):
            return self._error(None, RpcError("请求不是 JSON 对象", "Protocol"))

        rid = msg.get("id")
        if rid is None:
            # 没有 id 的消息我们无法关联回去。C++ 侧把"无 id"当事件，
            # 所以这里也不该出现无 id 的请求，记一笔丢弃。
            log.warning("收到缺少 id 的请求，已丢弃: %s", str(msg)[:120])
            return None

        method = msg.get("method")
        if not isinstance(method, str):
            return self._error(rid, RpcError("请求缺少 method 字段", "Protocol"))

        params = msg.get("params")
        if params is None:
            params = {}
        if not isinstance(params, dict):
            return self._error(rid, BadParams(f"{method} 的 params 必须是对象"))

        fn = self._methods.get(method)
        if fn is None:
            return self._error(rid, RpcError(f"未知方法: {method}", "UnknownMethod"))

        # 先把"参数名对不上"和"函数内部报错"区分开。
        # 少了这一步，一个拼错的参数名会被吞成 TypeError，排查起来很痛苦。
        try:
            inspect.signature(fn).bind(**params)
        except TypeError as exc:
            return self._error(rid, BadParams(f"{method} 参数不匹配: {exc}"))

        try:
            result = fn(**params)
        except RpcError as exc:
            return self._error(rid, exc)
        except ZeroDivisionError as exc:
            return self._error(rid, RpcError(f"{method} 计算中出现除零: {exc}", "MathError"))
        except (ValueError, KeyError, IndexError) as exc:
            return self._error(rid, RpcError(f"{method} 输入不合法: {exc}", "BadData"))
        except Exception as exc:  # noqa: BLE001 — 兜底是这里的职责
            tb = traceback.format_exc(limit=8)
            log.error("方法 %s 抛出未预期异常:\n%s", method, tb)
            return self._error(rid, RpcError(str(exc), type(exc).__name__, tb))

        return {"id": rid, "ok": True, "result": result}

    @staticmethod
    def _error(rid: Any, exc: RpcError) -> Dict[str, Any]:
        log.info("请求 %s 返回错误 [%s]: %s", rid, exc.code, exc)
        return {
            "id": rid,
            "ok": False,
            "error": {
                "code": exc.code,
                "message": str(exc),
                "detail": exc.detail,
            },
        }
