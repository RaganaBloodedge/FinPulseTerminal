#include "TestFramework.h"

#include "bridge/FrameCodec.h"

#include <algorithm>
#include <cstring>
#include <string>

using namespace fp;

namespace {

std::string header_for(std::uint32_t len) {
    std::string h(4, '\0');
    h[0] = static_cast<char>((len >> 24) & 0xFF);
    h[1] = static_cast<char>((len >> 16) & 0xFF);
    h[2] = static_cast<char>((len >> 8) & 0xFF);
    h[3] = static_cast<char>(len & 0xFF);
    return h;
}

}  // namespace

FP_TEST(codec, "单帧往返") {
    const std::string body = R"({"id":1,"method":"ping"})";
    const std::string wire = FrameCodec::encode(body);
    FP_CHECK_EQ(wire.size(), body.size() + 4);

    FrameCodec  c;
    std::string out;
    c.append(wire);
    FP_CHECK(c.next(out));
    FP_CHECK_EQ(out, body);
    FP_CHECK(!c.next(out));  // 没有第二帧
}

FP_TEST(codec, "长度头是大端") {
    FrameCodec c;
    c.append(FrameCodec::encode("abc"));
    // 编码结果应当以 0,0,0,3 开头
    const std::string wire = FrameCodec::encode("abc");
    FP_CHECK_EQ(static_cast<unsigned char>(wire[0]), 0u);
    FP_CHECK_EQ(static_cast<unsigned char>(wire[1]), 0u);
    FP_CHECK_EQ(static_cast<unsigned char>(wire[2]), 0u);
    FP_CHECK_EQ(static_cast<unsigned char>(wire[3]), 3u);
}

FP_TEST(codec, "粘包一次解出多帧") {
    FrameCodec c;
    c.append(FrameCodec::encode("first"));
    c.append(FrameCodec::encode("second"));
    c.append(FrameCodec::encode("third"));

    std::string out;
    FP_CHECK(c.next(out)); FP_CHECK_EQ(out, std::string("first"));
    FP_CHECK(c.next(out)); FP_CHECK_EQ(out, std::string("second"));
    FP_CHECK(c.next(out)); FP_CHECK_EQ(out, std::string("third"));
    FP_CHECK(!c.next(out));
    FP_CHECK_EQ(c.frames_decoded(), 3u);
}

FP_TEST(codec, "半包要等字节到齐") {
    FrameCodec c;
    const std::string wire = FrameCodec::encode("hello framing");
    std::string       out;

    // 只喂长度头，不该出帧
    c.append(wire.data(), 4);
    FP_CHECK(!c.next(out));
    FP_CHECK_EQ(c.buffered(), 4u);

    // 喂一半正文，还是不够
    c.append(wire.data() + 4, 5);
    FP_CHECK(!c.next(out));

    // 补齐剩余
    c.append(wire.data() + 9, wire.size() - 9);
    FP_CHECK(c.next(out));
    FP_CHECK_EQ(out, std::string("hello framing"));
    FP_CHECK_EQ(c.buffered(), 0u);
}

FP_TEST(codec, "逐字节喂入也能解出") {
    const std::string wire = FrameCodec::encode("byte-by-byte");
    FrameCodec        c;
    std::string       out;

    for (std::size_t i = 0; i < wire.size(); ++i) {
        c.append(wire.data() + i, 1);
        if (i + 1 < wire.size()) {
            FP_CHECK(!c.next(out));  // 尚未完整
        }
    }
    FP_CHECK(c.next(out));
    FP_CHECK_EQ(out, std::string("byte-by-byte"));
}

FP_TEST(codec, "跨帧边界的切片") {
    // 一次 append 里包含 1.5 个帧，再补上剩下的
    FrameCodec c;
    const std::string a = FrameCodec::encode("AAAAAAAA");
    const std::string b = FrameCodec::encode("BBBBBBBB");
    const std::string both = a + b;

    c.append(both.data(), both.size() - 3);  // 第二帧缺 3 字节
    std::string out;
    FP_CHECK(c.next(out)); FP_CHECK_EQ(out, std::string("AAAAAAAA"));
    FP_CHECK(!c.next(out));

    c.append(both.data() + both.size() - 3, 3);
    FP_CHECK(c.next(out)); FP_CHECK_EQ(out, std::string("BBBBBBBB"));
}

FP_TEST(codec, "零长度帧判定流损坏") {
    FrameCodec c;
    c.append(header_for(0));
    std::string out;
    FP_CHECK_THROWS(c.next(out), FrameError);
    FP_CHECK_EQ(c.buffered(), 0u);  // 缓冲被清空，避免持续报错
}

FP_TEST(codec, "超长帧判定流损坏") {
    FrameCodec c(1024);  // 上限设为 1 KiB
    c.append(header_for(1024 * 1024));
    std::string out;
    FP_CHECK_THROWS(c.next(out), FrameError);
    FP_CHECK_EQ(c.buffered(), 0u);
}

FP_TEST(codec, "恰好等于上限可以过") {
    FrameCodec c(16);
    const std::string body(16, 'x');
    c.append(FrameCodec::encode(body));
    std::string out;
    FP_CHECK(c.next(out));
    FP_CHECK_EQ(out.size(), 16u);
}

FP_TEST(codec, "多字节 UTF-8 不被截断") {
    const std::string body = R"({"msg":"涨跌幅 — 📈 测试","ok":true})";
    FrameCodec c;
    c.append(FrameCodec::encode(body));
    std::string out;
    FP_CHECK(c.next(out));
    FP_CHECK_EQ(out, body);
}

FP_TEST(codec, "大帧处理") {
    // 约 1 MiB 的正文，模拟一次批量拉取很多 K 线
    std::string body = "{\"bars\":[";
    while (body.size() < 1024 * 1024) body += "1,";
    body += "0]}";

    FrameCodec c;
    const std::string wire = FrameCodec::encode(body);
    // 拆成不规则的块喂进去
    std::size_t pos = 0;
    const std::size_t steps[] = {1, 7, 4096, 65536, 131072};
    std::size_t si = 0;
    std::string out;
    while (pos < wire.size()) {
        const std::size_t n = std::min(steps[si % 5], wire.size() - pos);
        c.append(wire.data() + pos, n);
        pos += n;
        ++si;
        if (pos >= wire.size()) break;
        c.next(out);  // 中间可能拿到也可能拿不到，都正常
    }
    FP_CHECK(c.next(out));
    FP_CHECK_EQ(out.size(), body.size());
    FP_CHECK_EQ(out, body);
}

FP_TEST(codec, "计数与统计") {
    FrameCodec c;
    FP_CHECK_EQ(c.frames_decoded(), 0u);
    FP_CHECK_EQ(c.bytes_ingested(), 0u);

    const std::string wire = FrameCodec::encode("x") + FrameCodec::encode("yy");
    c.append(wire);

    std::string out;
    c.next(out);
    c.next(out);

    FP_CHECK_EQ(c.frames_decoded(), 2u);
    FP_CHECK_EQ(c.bytes_ingested(), wire.size());
    FP_CHECK_EQ(c.buffered(), 0u);
}

FP_TEST(codec, "clear 丢弃残留") {
    FrameCodec c;
    c.append(header_for(100));
    c.append("partial");
    FP_CHECK(c.buffered() > 0);
    c.clear();
    FP_CHECK_EQ(c.buffered(), 0u);

    // 清空后重新喂完整帧仍能正常工作
    c.append(FrameCodec::encode("after-clear"));
    std::string out;
    FP_CHECK(c.next(out));
    FP_CHECK_EQ(out, std::string("after-clear"));
}
