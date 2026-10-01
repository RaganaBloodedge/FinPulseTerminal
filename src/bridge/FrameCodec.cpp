#include "bridge/FrameCodec.h"

#include <cstring>

namespace fp {

namespace {
constexpr std::size_t kCompactThreshold = 64u * 1024u;
}  // namespace

void FrameCodec::compact() {
    if (rpos_ == 0) return;
    if (rpos_ == buf_.size()) {
        buf_.clear();
        rpos_ = 0;
        return;
    }
    // 已消费的头部还小，就留着不搬 —— 否则"喂 8 字节 / 取一帧"的循环
    // 每轮都要 memmove 一次整个缓冲区。
    if (rpos_ < kCompactThreshold && rpos_ * 2 < buf_.size()) return;

    buf_.erase(buf_.begin(), buf_.begin() + static_cast<std::ptrdiff_t>(rpos_));
    rpos_ = 0;
}

void FrameCodec::append(const char* data, std::size_t n) {
    if (n == 0) return;
    compact();
    buf_.insert(buf_.end(), data, data + n);
    bytes_in_ += n;
}

bool FrameCodec::next(std::string& body) {
    const std::size_t avail = buffered();
    if (avail < kHeaderSize) return false;  // 连长度头都没收全

    const auto* h = reinterpret_cast<const unsigned char*>(buf_.data() + rpos_);
    const std::uint32_t len = (static_cast<std::uint32_t>(h[0]) << 24) |
                              (static_cast<std::uint32_t>(h[1]) << 16) |
                              (static_cast<std::uint32_t>(h[2]) << 8) |
                              (static_cast<std::uint32_t>(h[3]));

    // 零长度帧在协议里没有合法用途。看到它，说明长度头本身已经错了
    // （最常见的是流错位），继续解下去只会产生一堆垃圾帧。
    if (len == 0) {
        clear();
        throw FrameError("[frame] 收到零长度帧，判定管道流已损坏");
    }
    if (len > max_frame_) {
        clear();
        throw FrameError("[frame] 声明长度 " + std::to_string(len) +
                         " 超过上限 " + std::to_string(max_frame_) + "，判定管道流已损坏");
    }

    if (avail < kHeaderSize + len) return false;  // 半包：头到了，体还没到

    body.assign(buf_.data() + rpos_ + kHeaderSize, len);
    rpos_ += kHeaderSize + len;
    ++frames_;
    return true;
}

void FrameCodec::encode_into(std::string& out, std::string_view body) {
    const auto len = static_cast<std::uint32_t>(body.size());
    out.push_back(static_cast<char>((len >> 24) & 0xFF));
    out.push_back(static_cast<char>((len >> 16) & 0xFF));
    out.push_back(static_cast<char>((len >> 8) & 0xFF));
    out.push_back(static_cast<char>(len & 0xFF));
    out.append(body.data(), body.size());
}

std::string FrameCodec::encode(std::string_view body) {
    std::string out;
    out.reserve(body.size() + kHeaderSize);
    encode_into(out, body);
    return out;
}

void FrameCodec::clear() {
    buf_.clear();
    rpos_ = 0;
}

}  // namespace fp
