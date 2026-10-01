"""协议层测试：帧编解码与 C++ 侧 FrameCodec 的对偶性。

这层是整套桥接里唯一"跨语言硬约定"的落点，所以测试重点是
**线格式不变量**，而不是 JSON 内容本身。
"""

from __future__ import annotations

import math
import struct
import unittest

from finpulse_engine.protocol import DEFAULT_MAX_FRAME, FrameError, FrameReader, encode_frame


def _frames(*objs) -> bytes:
    return b"".join(encode_frame(o) for o in objs)


class EncodeFrameTests(unittest.TestCase):
    def test_线格式是_4字节大端长度加_JSON_体(self):
        raw = encode_frame({"a": 1})
        # separators=(",",":") → 无空格紧凑 JSON
        self.assertEqual(raw[4:], b'{"a":1}')
        (n,) = struct.unpack(">I", raw[:4])
        self.assertEqual(n, 7)
        self.assertEqual(len(raw), 4 + 7)

    def test_头部长度字段与实际字节数一致(self):
        for obj in ({"x": [1, 2, 3]}, "中文", {"nested": {"deep": None}}):
            raw = encode_frame(obj)
            (n,) = struct.unpack(">I", raw[:4])
            self.assertEqual(n, len(raw) - 4)

    def test_非_ASCII_按_UTF_8_透传而不转义(self):
        raw = encode_frame({"k": "中"})
        self.assertIn("中".encode("utf-8"), raw)  # ensure_ascii=False
        self.assertNotIn(b"\\u", raw)

    def test_NaN_会被拒绝而不是悄悄发出去(self):
        # C++ 侧解析器不认 NaN/Infinity；在发送端炸掉才是正确行为
        with self.assertRaises(FrameError):
            encode_frame({"v": float("nan")})
        with self.assertRaises(FrameError):
            encode_frame({"v": float("inf")})

    def test_空对象与_null_都是合法帧(self):
        self.assertEqual(encode_frame({})[4:], b"{}")
        self.assertEqual(encode_frame(None)[4:], b"null")


class FrameReaderTests(unittest.TestCase):
    def setUp(self):
        self.reader = FrameReader()

    def test_完整帧一次吐出(self):
        out = list(self.reader.feed(_frames({"a": 1}, {"b": 2})))
        self.assertEqual(out, [{"a": 1}, {"b": 2}])
        self.assertEqual(self.reader.frames_decoded, 2)

    def test_半包会攒着直到凑齐(self):
        raw = _frames({"id": 7})
        self.assertEqual(list(self.reader.feed(raw[:3])), [])          # 只有头的一部分
        self.assertEqual(list(self.reader.feed(raw[3:8])), [])         # 头齐了但体不够
        self.assertEqual(list(self.reader.feed(raw[8:])), [{"id": 7}])

    def test_逐字节喂入与一次喂入结果一致(self):
        raw = _frames({"n": 1}, {"n": 2}, {"n": 3})
        r = FrameReader()
        out = []
        for byte in raw:                      # 最恶劣的分片方式
            out.extend(r.feed(bytes([byte])))
        self.assertEqual(out, [{"n": 1}, {"n": 2}, {"n": 3}])

    def test_零长度帧判定流损坏(self):
        with self.assertRaises(FrameError):
            list(self.reader.feed(b"\x00\x00\x00\x00"))

    def test_超长帧判定流损坏(self):
        r = FrameReader(max_frame=16)
        with self.assertRaises(FrameError):
            list(r.feed(struct.pack(">I", 17) + b"x" * 17))

    def test_损坏后缓冲区被清空_后续帧仍可解码(self):
        # bytes_ingested 是累计统计量，不会因错误清零；
        # "流损坏后状态干净"的正确判据是：之后喂合法帧还能正常解出来
        with self.assertRaises(FrameError):
            list(self.reader.feed(b"\x00\x00\x00\x00"))
        self.assertEqual(list(self.reader.feed(_frames({"ok": 1}))), [{"ok": 1}])

    def test_reset_清空缓冲但保留统计(self):
        list(self.reader.feed(_frames({"a": 1})))
        self.reader.reset()
        self.assertEqual(list(self.reader.feed(_frames({"b": 2}))), [{"b": 2}])
        self.assertEqual(self.reader.frames_decoded, 2)

    def test_默认上限与_Cpp_侧一致(self):
        # 两边任何一侧改了这个数，另一侧必须同步 —— 测试在这里兜底
        self.assertEqual(DEFAULT_MAX_FRAME, 16 * 1024 * 1024)

    def test_长度字段理论边界_与默认上限相等时仍合法(self):
        # 恰好等于上限不报错（> 上限才报错），这里只验证不抛 FrameError 的路径
        r = FrameReader(max_frame=4)
        self.assertEqual(list(r.feed(struct.pack(">I", 4) + b"null")), [None])


if __name__ == "__main__":
    unittest.main()
