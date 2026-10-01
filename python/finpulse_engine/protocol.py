"""帧编解码 —— 与 ``src/bridge/FrameCodec.cpp`` 严格对偶。

线格式::

    [ 4 字节大端长度 N ][ N 字节 UTF-8 JSON ]

两边任何一侧改了格式，另一侧必须同步改，并且 ``docs/bridge-protocol.md``
里的版本号要跟着走。这是整套桥接里唯一一处"跨越语言边界的硬约定"。
"""

from __future__ import annotations

import json
import struct
from typing import Any, Dict, Iterator

HEADER = struct.Struct(">I")
HEADER_SIZE = HEADER.size

#: 与 FrameCodec::kDefaultMaxFrame 保持一致
DEFAULT_MAX_FRAME = 16 * 1024 * 1024


class FrameError(Exception):
    """流已损坏，无法继续解码。语义上等同于"这条管道不可信了"。"""


def encode_frame(obj: Any) -> bytes:
    """把一个对象序列化成一帧。

    ``allow_nan=False`` 是刻意的：NaN/Infinity 不是合法 JSON，
    C++ 侧的解析器会直接拒绝。宁可在发送端当场炸掉，也不要让它变成一个
    "对面看不懂但又不报错"的响应。指标计算出 NaN 属于 bug，应该在源头修掉。
    """
    try:
        body = json.dumps(
            obj,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except ValueError as exc:
        raise FrameError(f"响应无法序列化为合法 JSON（可能含 NaN/Inf）: {exc}") from exc

    if len(body) > DEFAULT_MAX_FRAME:
        raise FrameError(f"待发送的帧过大: {len(body)} 字节 > {DEFAULT_MAX_FRAME}")
    return HEADER.pack(len(body)) + body


class FrameReader:
    """增量解帧器。

    喂多少字节都行 —— 半包会攒着，粘包会一次吐多帧。
    C++ 侧对应的是 ``FrameCodec::append`` + ``next`` 的循环。
    """

    def __init__(self, max_frame: int = DEFAULT_MAX_FRAME) -> None:
        self._buf = bytearray()
        self._max_frame = max_frame
        #: 统计量，出问题时用得上
        self.frames_decoded = 0
        self.bytes_ingested = 0

    def feed(self, chunk: bytes) -> Iterator[Dict[str, Any]]:
        if chunk:
            self._buf.extend(chunk)
            self.bytes_ingested += len(chunk)

        while True:
            if len(self._buf) < HEADER_SIZE:
                return

            (length,) = HEADER.unpack_from(self._buf, 0)

            # 零长度帧在协议里没有合法用途。看到它就说明长度头本身已经错了，
            # 继续解只会产出一串垃圾 —— 与 C++ 侧同样选择立刻判定流损坏。
            if length == 0:
                self._buf.clear()
                raise FrameError("收到零长度帧，判定管道流已损坏")

            if length > self._max_frame:
                self._buf.clear()
                raise FrameError(
                    f"声明长度 {length} 超过上限 {self._max_frame}，判定管道流已损坏"
                )

            if len(self._buf) < HEADER_SIZE + length:
                return  # 半包，等更多字节

            body = bytes(self._buf[HEADER_SIZE:HEADER_SIZE + length])
            del self._buf[:HEADER_SIZE + length]
            self.frames_decoded += 1
            yield json.loads(body.decode("utf-8"))

    def reset(self) -> None:
        self._buf.clear()
