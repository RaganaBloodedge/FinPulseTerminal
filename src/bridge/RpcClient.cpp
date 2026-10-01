#include "bridge/RpcClient.h"

#include "core/Log.h"

#include <vector>

namespace fp {

namespace {
constexpr int kPollSliceMs = 50;   // 读线程轮询粒度，也是超时检查的精度上限
}  // namespace

RpcClient::~RpcClient() {
    // 析构里不能抛，stop() 本身已经吞掉了所有异常
    stop();
}

void RpcClient::start(const Options& opt) {
    if (reader_.joinable()) stop();

    opt_ = opt;
    tag_ = opt.tag.empty() ? "rpc" : opt.tag;

    {
        std::lock_guard<std::mutex> lk(mu_);
        pending_.clear();
        next_id_ = 1;
        closed_reason_.clear();
    }
    codec_.clear();

    proc_.start(opt_.proc);           // 失败会抛，此时还没有线程需要清理
    alive_.store(true, std::memory_order_release);
    stop_.store(false, std::memory_order_release);
    reader_ = std::thread([this] { reader_loop(); });

    FP_DEBUG(tag_, "已启动，子进程 " << proc_.describe());
}

void RpcClient::stop() {
    const bool was_running = reader_.joinable();
    stop_.store(true, std::memory_order_release);

    if (was_running) {
        // 先关 stdin：正常实现的引擎读到 EOF 会自己收尾退出。
        try {
            proc_.close_stdin();
        } catch (...) {
            // 管道可能已经没了，无所谓
        }
        if (proc_.wait(1500) < 0) {
            FP_WARN(tag_, "引擎未在 1.5s 内退出，强制终止");
            proc_.kill();
        }
    } else {
        proc_.kill();
    }

    // 读线程最多在 kPollSliceMs 后醒来看见 stop_ 而退出
    if (reader_.joinable() && std::this_thread::get_id() != reader_.get_id()) {
        reader_.join();
    } else if (reader_.joinable()) {
        reader_.detach();  // 从读线程回调里调 stop() 的极端情况，不能自 join
    }

    alive_.store(false, std::memory_order_release);
    // 兜底：读线程若已退出，pending 里可能还有没回调的
    if (!was_running) fail_all_pending("连接已停止", RpcError::kTransport);
}

Json RpcClient::call(const std::string& method, Json params, int timeout_ms) {
    std::mutex              m;
    std::condition_variable cv;
    bool                    done = false;
    Json                    result;
    std::unique_ptr<RpcError> err;

    call_async(method, std::move(params),
               [&](const Json& r, const RpcError* e) {
                   std::lock_guard<std::mutex> lk(m);
                   if (e) err = std::make_unique<RpcError>(*e);
                   else   result = r;
                   done = true;
                   cv.notify_one();
               },
               timeout_ms);

    std::unique_lock<std::mutex> lk(m);
    // 这里不做超时等待：超时判定只应该有一个权威来源（读线程的 deadline），
    // 两处各自计时只会引入"到底谁先谁后"的不确定性。
    cv.wait(lk, [&] { return done; });

    if (err) throw *err;
    return result;
}

std::uint64_t RpcClient::call_async(std::string method, Json params, ResultCallback cb, int timeout_ms) {
    const int tmo = timeout_ms > 0 ? timeout_ms : opt_.default_timeout_ms;

    Json          req = Json::object();
    std::uint64_t id  = 0;

    {
        std::lock_guard<std::mutex> lk(mu_);
        if (!alive_.load(std::memory_order_acquire)) {
            // 走下面的"未运行"分支，注意此时 cb 还没有被 move 走
        } else {
            id = next_id_++;
            req.set("id", static_cast<long long>(id));
            req.set("method", method);
            req.set("params", std::move(params));

            Pending p;
            p.method   = std::move(method);
            p.cb       = cb;   // 拷贝而非 move —— 发送失败时还需要原始 cb 兜底
            p.deadline = std::chrono::steady_clock::now() + std::chrono::milliseconds(tmo);
            pending_.emplace(id, std::move(p));
        }
    }

    if (id == 0) {
        if (cb) {
            const RpcError err("引擎未运行，请求被拒绝", RpcError::kTransport);
            cb(Json{}, &err);
        }
        return 0;
    }

    // ── 锁外发送 ──
    std::string send_error;
    try {
        send_frame(req);
    } catch (const std::exception& e) {
        send_error = e.what();
    }

    if (!send_error.empty()) {
        Pending p;
        {
            std::lock_guard<std::mutex> lk(mu_);
            auto it = pending_.find(id);
            if (it != pending_.end()) {
                p = std::move(it->second);
                pending_.erase(it);
            }
        }
        if (p.cb) {
            const RpcError err(send_error, RpcError::kTransport);
            p.cb(Json{}, &err);
        }
        alive_.store(false, std::memory_order_release);
        return id;
    }

    {
        std::lock_guard<std::mutex> lk(mu_);
        ++stats_.requests_sent;
    }
    return id;
}

bool RpcClient::cancel(std::uint64_t id, const std::string& reason) {
    Pending p;
    {
        std::lock_guard<std::mutex> lk(mu_);
        auto it = pending_.find(id);
        if (it == pending_.end()) return false;
        p = std::move(it->second);
        pending_.erase(it);
    }
    if (p.cb) {
        const RpcError err(reason, RpcError::kTimeout);
        p.cb(Json{}, &err);
    }
    return true;
}

void RpcClient::send_frame(const Json& msg) {
    const std::string wire = FrameCodec::encode(msg.dump());
    // 写锁只圈住这一次 write：多线程同时 call 时，各帧的字节不能交错。
    // 用独立于 mu_ 的锁，是因为发送可能阻塞在管道上，不该拖住 pending 表的访问。
    std::lock_guard<std::mutex> lk(write_mu_);
    proc_.write(wire);
}

void RpcClient::reader_loop() {
    std::vector<char> chunk(64 * 1024);
    std::string       reason;

    while (!stop_.load(std::memory_order_acquire)) {
        try {
            if (!proc_.wait_readable(kPollSliceMs)) {
                check_timeouts();   // 没有数据可读的间隙顺便扫一遍超时
                continue;
            }

            const std::size_t n = proc_.read(chunk.data(), chunk.size());
            if (n == 0) {
                // EOF 就是 EOF，这里只报告事实。
                // 收尸（waitpid）是 stop() 的职责 —— 两个线程抢着回收同一个子进程时，
                // 慢的那一方会拿到 ECHILD，被误读成"进程还在跑"，
                // 进而触发一次毫无必要的强杀和一条误导性的警告。
                reason = "引擎关闭了输出流（stdout EOF）";
                break;
            }

            codec_.append(chunk.data(), n);
            std::string body;
            while (codec_.next(body)) {
                handle_body(body);
            }
        } catch (const FrameError& e) {
            {
                std::lock_guard<std::mutex> lk(mu_);
                ++stats_.protocol_errors;
            }
            // 坏帧意味着帧边界已经不可信，继续解只会产生一串垃圾帧。
            // 唯一安全的动作是丢掉整条流，让上层重建引擎。
            reason = e.what();
            FP_ERROR(tag_, "帧同步丢失: " << e.what());
            break;
        } catch (const std::exception& e) {
            reason = std::string("读取失败: ") + e.what();
            break;
        }
    }

    const bool unexpected = !stop_.load(std::memory_order_acquire);
    alive_.store(false, std::memory_order_release);
    {
        std::lock_guard<std::mutex> lk(mu_);
        if (!reason.empty()) closed_reason_ = reason;
    }
    fail_all_pending(reason.empty() ? "连接已关闭" : reason, RpcError::kTransport);

    if (unexpected) {
        FP_WARN(tag_, "管道意外断开: " << (reason.empty() ? "未知原因" : reason));
        if (closed_handler_) {
            try {
                closed_handler_(reason);
            } catch (const std::exception& e) {
                FP_ERROR(tag_, "断开回调抛异常: " << e.what());
            }
        }
    }
}

void RpcClient::handle_body(const std::string& body) {
    Json msg;
    try {
        msg = Json::parse(body);
    } catch (const JsonError& e) {
        // 单帧内容坏了不代表流坏了（长度头是对的），记一笔继续
        std::lock_guard<std::mutex> lk(mu_);
        ++stats_.protocol_errors;
        FP_ERROR(tag_, "收到非 JSON 帧: " << e.what() << " | 前 120 字节: " << body.substr(0, 120));
        return;
    }

    if (!msg.is_object()) {
        std::lock_guard<std::mutex> lk(mu_);
        ++stats_.protocol_errors;
        FP_WARN(tag_, "帧顶层不是对象，已丢弃");
        return;
    }

    // ── 单向事件 ──
    if (msg.has("event")) {
        const std::string ev = msg["event"].as_string_or("");
        {
            std::lock_guard<std::mutex> lk(mu_);
            ++stats_.events;
        }
        if (event_handler_) {
            try {
                event_handler_(ev, msg["data"]);
            } catch (const std::exception& e) {
                FP_ERROR(tag_, "事件处理器抛异常 (" << ev << "): " << e.what());
            }
        }
        return;
    }

    // ── 响应 ──
    if (!msg.has("id")) {
        std::lock_guard<std::mutex> lk(mu_);
        ++stats_.protocol_errors;
        FP_WARN(tag_, "收到既无 id 也无 event 的消息，已丢弃");
        return;
    }

    const auto id = static_cast<std::uint64_t>(msg["id"].as_int_or(-1));

    Pending p;
    {
        std::lock_guard<std::mutex> lk(mu_);
        auto it = pending_.find(id);
        if (it == pending_.end()) {
            // 超时后响应才姗姗来迟是常态，不能当成错误
            FP_DEBUG(tag_, "收到未知 id=" << id << " 的响应（多半已超时清理），丢弃");
            return;
        }
        p = std::move(it->second);
        pending_.erase(it);
    }

    const bool ok = msg["ok"].as_bool_or(false);
    if (ok) {
        {
            std::lock_guard<std::mutex> lk(mu_);
            ++stats_.responses_ok;
        }
        if (p.cb) p.cb(msg["result"], nullptr);
        return;
    }

    const Json& e = msg["error"];
    const std::string code_name = e["code"].as_string_or("EngineError");
    const std::string message   = e["message"].as_string_or("引擎返回了未说明的错误");
    const std::string detail    = e["detail"].as_string_or("");
    {
        std::lock_guard<std::mutex> lk(mu_);
        ++stats_.responses_err;
    }
    FP_WARN(tag_, "[" << p.method << "] 引擎报错 " << code_name << ": " << message);

    if (p.cb) {
        const RpcError err(message, RpcError::kRemote, code_name, detail);
        p.cb(Json{}, &err);
    }
}

void RpcClient::check_timeouts() {
    std::vector<std::pair<std::uint64_t, Pending>> expired;
    const auto now = std::chrono::steady_clock::now();
    {
        std::lock_guard<std::mutex> lk(mu_);
        for (auto it = pending_.begin(); it != pending_.end();) {
            if (it->second.deadline <= now) {
                expired.emplace_back(it->first, std::move(it->second));
                it = pending_.erase(it);
                ++stats_.timeouts;
            } else {
                ++it;
            }
        }
    }
    for (auto& [id, p] : expired) {
        FP_WARN(tag_, "调用 " << p.method << " 超时 (id=" << id << ")");
        if (p.cb) {
            const RpcError err("调用 " + p.method + " 超时", RpcError::kTimeout);
            p.cb(Json{}, &err);
        }
    }
}

void RpcClient::fail_all_pending(const std::string& reason, int code) {
    std::vector<Pending> victims;
    {
        std::lock_guard<std::mutex> lk(mu_);
        victims.reserve(pending_.size());
        for (auto& [id, p] : pending_) victims.push_back(std::move(p));
        pending_.clear();
    }
    if (victims.empty()) return;

    FP_WARN(tag_, "连接中断，作废 " << victims.size() << " 个在途调用: " << reason);
    for (auto& p : victims) {
        if (p.cb) {
            const RpcError err(reason, code);
            p.cb(Json{}, &err);
        }
    }
}

void RpcClient::set_event_handler(EventHandler h) {
    std::lock_guard<std::mutex> lk(mu_);
    event_handler_ = std::move(h);
}

void RpcClient::set_closed_handler(ClosedHandler h) {
    std::lock_guard<std::mutex> lk(mu_);
    closed_handler_ = std::move(h);
}

RpcClient::Stats RpcClient::stats() const {
    std::lock_guard<std::mutex> lk(mu_);
    Stats s = stats_;
    s.pending = pending_.size();
    return s;
}

std::string RpcClient::closed_reason() const {
    std::lock_guard<std::mutex> lk(mu_);
    return closed_reason_;
}

}  // namespace fp
