// FinPulse Terminal — 帧编解码
//
// 线格式： [4 字节大端长度 N][N 字节 UTF-8 JSON]
//
// 为什么用长度前缀而不是"一行一条 JSON"：
//   1. JSON 体里可以合法地出现换行（缩进格式），按 \n 切会切错；
//   2. 引擎一旦有任何意外输出（第三方库 debug print、warning），
//      按行切会直接把协议流污染成垃圾，且极难定位；
//   3. 长度前缀可以被增量解码器 O(1) 地判断"当前是否拿到了完整帧"。
//
// 为什么不用 JSON-RPC over HTTP/WebSocket：
//   子进程和父进程之间已经有管道这条更短的路，套一层 HTTP 只会
//   多出握手、分块编码、额外的失败模式，还得再引一个网络栈依赖。
//
// 这个类只负责字节 ↔ 帧，不认识 JSON 内容，也不做任何 IO。
#pragma once

#include <cstddef>
#include <cstdint>
#include <stdexcept>
#include <string>
#include <string_view>
#include <vector>

namespace fp {

/// 流损坏（零长度帧、超长帧、非法长度）时抛出。
/// 语义上表示"这条管道已经不可信，上层应当丢掉连接重建"。
class FrameError : public std::runtime_error {
public:
    explicit FrameError(std::string msg) : std::runtime_error(std::move(msg)) {}
};

class FrameCodec {
public:
    static constexpr std::size_t   kHeaderSize     = 4;
    /// 16 MiB。2000 根 K 线约 200 KB，1 万根约 1 MB，16 MiB 有三个数量级余量；
    /// 真收到比这还长的"帧"，几乎一定是流错位而不是合法大数据。
    static constexpr std::uint32_t kDefaultMaxFrame = 16u * 1024u * 1024u;

    explicit FrameCodec(std::uint32_t max_frame = kDefaultMaxFrame)
        : max_frame_(max_frame) {}

    /// 喂入原始字节。可以喂任意长的任意切片，内部会自行处理半包/粘包。
    void append(const char* data, std::size_t n);
    void append(std::string_view s) { append(s.data(), s.size()); }

    /// 取出一个完整帧。缓冲区里没有完整帧时返回 false（不阻塞、不等待）。
    /// 流损坏时抛 FrameError 并清空缓冲。
    bool next(std::string& body);

    /// 组帧。encode 每次新建字符串；高频路径用 encode_into 复用缓冲。
    static std::string encode(std::string_view body);
    static void        encode_into(std::string& out, std::string_view body);

    std::size_t buffered() const noexcept { return buf_.size() - rpos_; }
    std::size_t frames_decoded() const noexcept { return frames_; }
    std::size_t bytes_ingested() const noexcept { return bytes_in_; }

    /// 主动丢弃残留字节（重连、流重建时用）。
    void clear();

private:
    /// 回收已消费的前缀空间。高频小帧场景下不每次都搬内存。
    void compact();

    std::vector<char> buf_;
    std::size_t       rpos_{0};
    std::uint32_t     max_frame_;
    std::size_t       frames_{0};
    std::size_t       bytes_in_{0};
};

}  // namespace fp
