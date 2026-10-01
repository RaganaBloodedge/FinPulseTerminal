// FinPulse Terminal — RPC 客户端（请求关联 + 超时 + 事件分发）
//
// 线协议（与 python/finpulse_engine/protocol.py 严格对偶）：
//
//   请求            {"id": 7, "method": "analysis.indicators", "params": {...}}
//   成功响应        {"id": 7, "ok": true,  "result": {...}}
//   失败响应        {"id": 7, "ok": false, "error": {"code": "...", "message": "...", "detail": "..."}}
//   单向事件        {"event": "engine.progress", "data": {...}}
//
// 用 "有没有 id" 区分响应和事件，而不是加一个 "type" 字段：
// 少一个字段就少一处两边不一致的机会，而且事件天然不需要被关联。
//
// 线程模型：单读线程 + 任意多写线程。
//   读线程负责：取帧 → 解 JSON → 按 id 找 pending → 回调 / 或当事件分发。
//   写线程：call()/call_async() 可以在任意线程调，写入用独立的写锁串行化，
//           保证"一次 call 的帧字节连续落进管道"，不会和其它线程的帧交错。
//   超时检查搭在读线程的轮询里（wait_readable(50ms)），因此不需要额外的看门狗线程。
#pragma once

#include <atomic>
#include <chrono>
#include <condition_variable>
#include <cstdint>
#include <functional>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>
#include <thread>
#include <unordered_map>

#include "bridge/FrameCodec.h"
#include "bridge/Subprocess.h"
#include "core/Json.h"

namespace fp {

/// RPC 层错误。code 为负数是本地错误，正数是引擎返回的业务错误。
class RpcError : public std::runtime_error {
public:
    static constexpr int kTimeout   = -1;   ///< 超时（引擎没在期限内回）
    static constexpr int kTransport = -2;   ///< 引擎已退出 / 管道断开
    static constexpr int kProtocol  = -3;   ///< 坏帧、非法 JSON、缺字段
    static constexpr int kRemote    = 1;    ///< 引擎显式报错

    RpcError(std::string msg, int code, std::string code_name = {}, std::string detail = {})
        : std::runtime_error(std::move(msg)),
          code_(code),
          code_name_(std::move(code_name)),
          detail_(std::move(detail)) {}

    int                code() const noexcept { return code_; }
    const std::string& code_name() const noexcept { return code_name_; }  ///< 引擎侧的错误类名
    const std::string& detail() const noexcept { return detail_; }        ///< 引擎侧的 traceback 摘要

private:
    int         code_;
    std::string code_name_;
    std::string detail_;
};

class RpcClient {
public:
    /// 调用完成时回调。error 为 nullptr 表示成功。
    using ResultCallback = std::function<void(const Json& result, const RpcError* error)>;
    /// 引擎主动推来的单向事件。
    using EventHandler  = std::function<void(const std::string& event, const Json& data)>;
    /// 管道意外断开（非主动 stop）时回调，上层据此触发重启。
    using ClosedHandler = std::function<void(const std::string& reason)>;

    struct Options {
        Subprocess::Options proc;
        int                 default_timeout_ms{15000};
        std::string         tag{"rpc"};
    };

    struct Stats {
        std::uint64_t requests_sent{0};
        std::uint64_t responses_ok{0};
        std::uint64_t responses_err{0};
        std::uint64_t timeouts{0};
        std::uint64_t events{0};
        std::uint64_t protocol_errors{0};
        std::size_t   pending{0};
    };

    RpcClient() = default;
    ~RpcClient();

    RpcClient(const RpcClient&)            = delete;
    RpcClient& operator=(const RpcClient&) = delete;

    /// 启动子进程与读线程。已在运行时先 stop()。
    void start(const Options& opt);
    /// 优雅停止：关 stdin → 等 1.5s → 杀。可在任意状态重复调用。
    void stop();

    bool alive() const noexcept { return alive_.load(std::memory_order_acquire); }

    /// 同步调用。超时或错误时抛 RpcError。
    /// timeout_ms <= 0 表示用 Options::default_timeout_ms。
    Json call(const std::string& method, Json params = Json::object(), int timeout_ms = -1);

    /// 异步调用。返回请求 id（0 表示引擎未运行，回调已被就地以错误调用过）。
    std::uint64_t call_async(std::string method, Json params, ResultCallback cb, int timeout_ms = -1);

    /// 主动放弃一个尚未返回的调用。用于"用户切走了页面"这类场景，
    /// 避免迟到的响应把已经无意义的回调再跑一遍。
    bool cancel(std::uint64_t id, const std::string& reason = "调用已取消");

    void set_event_handler(EventHandler h);
    void set_closed_handler(ClosedHandler h);

    Stats stats() const;
    const Subprocess& process() const noexcept { return proc_; }

    /// 最近一次断开原因（空表示没断过）。
    std::string closed_reason() const;

private:
    struct Pending {
        std::string                            method;
        ResultCallback                         cb;
        std::chrono::steady_clock::time_point  deadline;
    };

    void reader_loop();
    void handle_body(const std::string& body);
    void check_timeouts();
    void fail_all_pending(const std::string& reason, int code);
    void send_frame(const Json& msg);

    Options       opt_;
    std::string   tag_{"rpc"};
    Subprocess    proc_;
    FrameCodec    codec_{};

    mutable std::mutex        mu_;          ///< 保护 pending_ / next_id_ / stats_ / alive_
    std::unordered_map<std::uint64_t, Pending> pending_;
    std::uint64_t             next_id_{1};
    Stats                     stats_{};
    std::atomic<bool>         alive_{false};
    std::atomic<bool>         stop_{false};
    std::string               closed_reason_;

    std::mutex  write_mu_;                  ///< 保证单帧字节连续写入
    std::thread reader_;
    EventHandler  event_handler_;
    ClosedHandler closed_handler_;
};

}  // namespace fp
