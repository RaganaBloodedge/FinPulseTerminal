"""事件推送通道 —— 让一次长任务能在执行过程中把中间状态推给壳。

线格式沿用 ``RpcClient`` 已有的约定（见 ``src/bridge/RpcClient.h``）::

    单向事件    {"event": "agent.stream", "data": {...}}

用"没有 id"来区分事件与响应，而不是加一个 ``type`` 字段。理由和 C++ 侧
注释里写的一样：少一个字段就少一处两边不一致的机会，而且事件天生不需要
被关联回某个请求。

**为什么需要这个**：智能体研判是秒级的任务，而且过程比结果更有信息量
——第一轮各委员给了什么方向、第二轮谁改了口、主席为什么下调置信度。
如果只返回一个最终 JSON，界面上就只能转圈然后啪地出现一大段文字，
用户看不到"结论是怎么被评出来的"。Fincept 的做法是在 stdout 上打
``THINKING: / TOKEN: / TOOL: / DONE:`` 前缀行；那套东西的问题是它把
**协议和日志混在同一条流上**，解析靠前缀字符串，一个含冒号的 token
就能把解析带偏。

这里改成把事件塞进已有的帧协议里：结构化的、和响应走同一条管道、
不需要任何前缀约定。

**并发**：智能体内部用线程池并行跑委员，所以 ``emit`` 会被多个线程调用。
stdout 的写入必须整帧连续，否则两个线程的字节交错就是一个坏帧、
C++ 侧会判定流损坏然后杀掉引擎。所以这里用一把锁把"编码 + 写入 + 刷"
整体串起来 —— 不是保护共享数据，是保护**帧边界**。
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Dict, Optional, Protocol

from .protocol import FrameError, encode_frame

log = logging.getLogger("finpulse.stream")


class EventSink(Protocol):
    """事件的落点。存在的意义是让上层可以注入一个假实现来做测试。"""

    def emit(self, event: str, data: Dict[str, Any]) -> None: ...


class NullSink:
    """不推任何东西。单测和离线调用用这个。"""

    enabled = False

    def emit(self, event: str, data: Dict[str, Any]) -> None:  # noqa: D102
        return None


class FrameSink:
    """把事件编码成帧写进一条二进制流（实际上就是引擎的 stdout）。

    ``enabled=False`` 时 ``emit`` 变成空操作。这一点很重要：不是所有调用方
    都在等事件（比如单测直接调 RPC 函数），而"调用方没在听"不该让调用
    逻辑分叉成两套代码。检查放在最内层，业务代码只管 emit。
    """

    def __init__(self, out: Any, enabled: bool = True,
                 lock: Optional[threading.Lock] = None) -> None:
        self._out = out
        # 允许外部传进来同一把锁：引擎主循环写"响应帧"、这里写"事件帧"，
        # 用的是同一条 stdout。两处各自持一把锁并不能阻止交错 ——
        # 必须是**同一把**才能把帧边界守住。
        self._lock = lock if lock is not None else threading.Lock()
        self.enabled = enabled
        self.emitted = 0
        self.dropped = 0

    def emit(self, event: str, data: Dict[str, Any]) -> None:
        if not self.enabled:
            return
        try:
            frame = encode_frame({"event": event, "data": data})
        except FrameError as exc:
            # 事件里混进了 NaN 之类不可序列化的东西。**绝不能因此中断研判**
            # —— 事件是锦上添花，结果才是正事。记一笔，丢掉这一条。
            self.dropped += 1
            log.warning("事件 %s 无法编码，已丢弃: %s", event, exc)
            return

        try:
            with self._lock:
                self._out.write(frame)
                self._out.flush()
        except (BrokenPipeError, ValueError, OSError) as exc:
            # 壳已经不在了。停止推送，但让研判继续跑完 —— 结果写不出去是
            # 壳的问题，不是引擎该崩的理由。
            self.enabled = False
            log.warning("事件通道已断开，后续事件不再推送: %s", exc)
            return
        self.emitted += 1

    def stats(self) -> Dict[str, Any]:
        return {"enabled": self.enabled, "emitted": self.emitted,
                "dropped": self.dropped}


class MultiplexSink:
    """把事件同时发给多个落点。用于"既要推给壳、又要记进 trace"。"""

    def __init__(self, *sinks: EventSink) -> None:
        self._sinks = [s for s in sinks if s is not None]
        self.enabled = any(getattr(s, "enabled", True) for s in self._sinks)

    def emit(self, event: str, data: Dict[str, Any]) -> None:
        for sink in self._sinks:
            try:
                sink.emit(event, data)
            except Exception as exc:  # noqa: BLE001 — 一个落点坏了不该影响其它
                log.warning("事件落点 %s 失败: %s", type(sink).__name__, exc)


def null_sink() -> EventSink:
    return NullSink()


def make_sink(out: Optional[Any], enabled: bool = True,
              lock: Optional[threading.Lock] = None) -> EventSink:
    """没给流就返回 Null 实现，避免调用方到处判空。"""
    if out is None:
        return NullSink()
    return FrameSink(out, enabled=enabled, lock=lock)
