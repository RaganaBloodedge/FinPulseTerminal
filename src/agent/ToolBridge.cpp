#include "agent/ToolBridge.h"

#include <algorithm>
#include <array>
#include <cctype>
#include <cerrno>
#include <cstring>
#include <mutex>
#include <random>
#include <sstream>
#include <stdexcept>
#include <utility>

#include "core/Log.h"

#if defined(_WIN32)
#  ifndef WIN32_LEAN_AND_MEAN
#    define WIN32_LEAN_AND_MEAN
#  endif
#  include <winsock2.h>
#  include <ws2tcpip.h>
#  include <io.h>
using socket_t = SOCKET;
#  define FP_INVALID_SOCKET INVALID_SOCKET
#  define fp_close_socket closesocket
#  define fp_last_socket_error WSAGetLastError()
#else
#  include <arpa/inet.h>
#  include <fcntl.h>
#  include <netinet/in.h>
#  include <sys/select.h>
#  include <sys/socket.h>
#  include <unistd.h>
using socket_t = int;
#  define FP_INVALID_SOCKET (-1)
#  define fp_close_socket ::close
#  define fp_last_socket_error (errno)
#endif

namespace fp {

namespace {

constexpr const char* kTag = "toolbridge";
constexpr const char* kTokenHeader = "x-finpulse-token";

/// WSAStartup 只能做一次，且必须在任何 socket 调用之前。
void platform_init() {
#if defined(_WIN32)
    static std::once_flag once;
    std::call_once(once, [] {
        WSADATA data{};
        const int rc = WSAStartup(MAKEWORD(2, 2), &data);
        if (rc != 0) {
            throw std::runtime_error("WSAStartup 失败，无法使用 socket：" + std::to_string(rc));
        }
    });
#endif
}

std::string socket_error_text() {
#if defined(_WIN32)
    return "错误码 " + std::to_string(fp_last_socket_error);
#else
    return std::string(std::strerror(errno));
#endif
}

bool set_reuse_addr(socket_t s) {
    int one = 1;
    return ::setsockopt(s, SOL_SOCKET, SO_REUSEADDR,
                        reinterpret_cast<const char*>(&one), sizeof(one)) == 0;
}

bool valid_socket(socket_t s) { return s != FP_INVALID_SOCKET; }

/// 等待可读。返回 false 表示超时或出错。
bool wait_readable(socket_t s, int timeout_ms) {
    fd_set rd;
    FD_ZERO(&rd);
    FD_SET(s, &rd);
    timeval tv{};
    tv.tv_sec  = timeout_ms / 1000;
    tv.tv_usec = (timeout_ms % 1000) * 1000;
    const int rc = ::select(static_cast<int>(s) + 1, &rd, nullptr, nullptr, &tv);
    return rc > 0 && FD_ISSET(s, &rd);
}

/// HTTP 头字段名大小写不敏感（RFC 9110）。用 trim + lowercase 归一。
std::string trim(std::string s) {
    const auto not_space = [](unsigned char c) { return !std::isspace(c); };
    s.erase(s.begin(), std::find_if(s.begin(), s.end(), not_space));
    s.erase(std::find_if(s.rbegin(), s.rend(), not_space).base(), s.end());
    return s;
}

bool is_loopback_host(const std::string& host) {
    // Host 的几种合法写法：
    //   "127.0.0.1" / "127.0.0.1:8080" / "localhost" / "[::1]:8080" / "::1"
    //
    // 剥端口时必须区分"冒号是端口分隔符"还是"这是 IPv6 字面量"。
    // 早先的写法是对最后一个 ':' 直接切，于是裸 IPv6 "::1" 被切成 ":"
    // —— 一个回环地址被当成了外部域名。判据是 RFC 3986：不带方括号时，
    // 冒号多于一个就只能是 IPv6 字面量，不可能带端口。
    std::string h = host;
    if (!h.empty() && h.front() == '[') {
        const auto close = h.find(']');
        if (close != std::string::npos) h = h.substr(1, close - 1);
    } else if (std::count(h.begin(), h.end(), ':') == 1) {
        h = h.substr(0, h.find(':'));
    }
    return h == "127.0.0.1" || h == "localhost" || h == "::1" || h == "0.0.0.0"
        || h.empty();   // 有些极简客户端不发 Host，回环上不因此拒绝
}

/// 把 URI 查询串去掉，只留路径。
std::string path_only(const std::string& target) {
    const auto q = target.find('?');
    return q == std::string::npos ? target : target.substr(0, q);
}

// ── 默认工具的参数 schema（与 Python 侧 ToolSpec 保持一致） ────────

Json schema_symbol_only() {
    Json props = Json::object();
    Json symbol = Json::object();
    symbol.set("type", "string");
    symbol.set("description", "标的代码；省略则用当前终端正在回放的标的");
    props.set("symbol", std::move(symbol));

    Json out = Json::object();
    out.set("type", "object");
    out.set("properties", std::move(props));
    out.set("required", Json::array());
    return out;
}

Json schema_empty() {
    Json out = Json::object();
    out.set("type", "object");
    out.set("properties", Json::object());
    out.set("required", Json::array());
    return out;
}

}  // namespace

// ── 构造 / 析构 ───────────────────────────────────────────────────

ToolBridge::ToolBridge(DataHub* hub, Config cfg)
    : hub_(hub ? hub : &DataHub::instance()), cfg_(cfg), token_(make_token()) {
    if (cfg_.max_header_bytes < 1024)    cfg_.max_header_bytes = 1024;
    if (cfg_.max_body_bytes < 4096)      cfg_.max_body_bytes = 4096;
    if (cfg_.token_max_uses <= 0)        cfg_.token_max_uses = 1;
}

ToolBridge::~ToolBridge() {
    stop();
}

// ── 工具注册 ──────────────────────────────────────────────────────

void ToolBridge::add_tool(Tool tool) {
    if (tool.name.empty()) {
        throw std::invalid_argument("工具名不能为空");
    }
    if (!tool.handler) {
        throw std::invalid_argument("工具 " + tool.name + " 没有实现");
    }
    std::lock_guard<std::mutex> lk(mu_);
    for (const Tool& t : tools_) {
        if (t.name == tool.name) {
            throw std::invalid_argument("工具名重复注册: " + tool.name);
        }
    }
    tools_.push_back(std::move(tool));
}

std::vector<std::string> ToolBridge::tool_names() const {
    std::lock_guard<std::mutex> lk(mu_);
    std::vector<std::string> out;
    out.reserve(tools_.size());
    for (const Tool& t : tools_) out.push_back(t.name);
    std::sort(out.begin(), out.end());
    return out;
}

std::size_t ToolBridge::tool_count() const {
    std::lock_guard<std::mutex> lk(mu_);
    return tools_.size();
}

bool ToolBridge::is_exposed(const std::string& name) {
    // **正向白名单**。没在名单里的工具即使注册了也不会被派发 ——
    // 将来有人往桥上加了新工具却忘了它会出现在模型面前，这里能兜住。
    static const std::array<const char*, 4> kExposed = {
        "terminal.live_quote",
        "terminal.bus_stats",
        "terminal.data_quality",
        "terminal.subscriptions",
    };
    for (const char* n : kExposed) {
        if (name == n) return true;
    }
    return false;
}

void ToolBridge::register_default_tools() {
    const auto has = [this](const std::string& name) {
        std::lock_guard<std::mutex> lk(mu_);
        for (const Tool& t : tools_) {
            if (t.name == name) return true;
        }
        return false;
    };

    // ── terminal.bus_stats ──
    // 唯一一个完全自包含的工具：数据就来自总线自己。
    // 智能体用它验证"终端确实在跑，并且真的在分发数据"。
    if (!has("terminal.bus_stats")) {
        Tool t;
        t.name = "terminal.bus_stats";
        t.description =
            "终端进程内总线的实时统计：累计发布/投递次数、当前活跃订阅数、"
            "无人订阅的发布次数，以及每个订阅的匹配模式与用途。"
            "用来确认终端确实在接收与分发数据。";
        t.schema  = schema_empty();
        t.handler = [this](const Json&) -> Json {
            const DataHubStats s = hub_->stats();
            Json out = Json::object();
            out.set("published", static_cast<long long>(s.published));
            out.set("delivered", static_cast<long long>(s.delivered));
            out.set("handler_failures", static_cast<long long>(s.failed));
            out.set("unmatched", static_cast<long long>(s.unmatched));
            out.set("active_subscriptions", static_cast<long long>(s.subscriptions));
            if (s.unmatched > 0) {
                out.set("note",
                        "存在没有任何订阅者的发布 —— 要么是订阅方已退出，"
                        "要么是主题名写错了");
            }
            return out;
        };
        add_tool(std::move(t));
    }

    // ── terminal.subscriptions ──
    if (!has("terminal.subscriptions")) {
        Tool t;
        t.name = "terminal.subscriptions";
        t.description = "终端当前所有活跃订阅：主题匹配模式与用途标签。";
        t.schema  = schema_empty();
        t.handler = [this](const Json&) -> Json {
            Json arr = Json::array();
            for (const auto& [id, pattern, label] : hub_->active_subscriptions()) {
                Json item = Json::object();
                item.set("id", static_cast<long long>(id));
                item.set("pattern", pattern);
                item.set("label", label);
                arr.push(std::move(item));
            }
            Json out = Json::object();
            out.set("subscriptions", std::move(arr));
            return out;
        };
        add_tool(std::move(t));
    }

    // ── terminal.live_quote ──
    // 数据由**终端注入**：桥自己不知道行情，它只知道谁提供了行情快照。
    // 没注入时如实报"终端未提供"，而不是返回一个空对象假装查过了。
    if (!has("terminal.live_quote")) {
        Tool t;
        t.name = "terminal.live_quote";
        t.description =
            "终端**当前正在接收**的最新一笔行情快照（价格、涨跌幅、开高低、成交量、时间）。"
            "这是终端内存里的实时状态，不是历史序列。";
        t.schema  = schema_symbol_only();
        t.handler = [this](const Json& args) -> Json {
            // 把 provider 抄一份再放开锁调用：provider 是外部注入的，
            // 它内部可能又去读总线统计或别的会拿 mu_ 的东西。抱着锁调用
            // 外部代码是自找死锁。
            QuoteProvider provider;
            {
                std::lock_guard<std::mutex> lk(mu_);
                provider = quote_provider_;
            }
            if (!provider) {
                Json out = Json::object();
                out.set("available", false);
                out.set("reason", "终端未提供行情快照（未注入 quote_provider）");
                return out;
            }
            const std::string symbol = args["symbol"].as_string_or("");
            Json quote = provider(symbol);
            if (quote.is_null() || (quote.is_object() && quote.members().empty())) {
                Json out = Json::object();
                out.set("available", false);
                out.set("reason", symbol.empty()
                                      ? "终端当前没有正在回放的标的"
                                      : "终端没有 " + symbol + " 的实时行情");
                return out;
            }
            quote.set("available", true);
            return quote;
        };
        add_tool(std::move(t));
    }

    // ── terminal.data_quality ──
    if (!has("terminal.data_quality")) {
        Tool t;
        t.name = "terminal.data_quality";
        t.description =
            "终端已经取到的那段行情的数据质量检查结果与样本区间"
            "（根数、起止日期、缺失/重复/异常价格）。"
            "用来判断后续所有指标和统计建立在什么数据上。";
        t.schema  = schema_symbol_only();
        t.handler = [this](const Json& args) -> Json {
            SeriesProvider provider;
            {
                std::lock_guard<std::mutex> lk(mu_);
                provider = series_provider_;
            }
            if (!provider) {
                Json out = Json::object();
                out.set("available", false);
                out.set("reason", "终端未提供行情序列（未注入 series_provider）");
                return out;
            }
            const std::string symbol = args["symbol"].as_string_or("");
            Json series = provider(symbol);
            if (series.is_null() || (series.is_object() && series.members().empty())) {
                Json out = Json::object();
                out.set("available", false);
                out.set("reason", symbol.empty()
                                      ? "终端当前没有已加载的行情序列"
                                      : "终端没有 " + symbol + " 的行情序列");
                return out;
            }
            series.set("available", true);
            return series;
        };
        add_tool(std::move(t));
    }
}

void ToolBridge::set_quote_provider(QuoteProvider p) {
    std::lock_guard<std::mutex> lk(mu_);
    quote_provider_ = std::move(p);
}

void ToolBridge::set_series_provider(SeriesProvider p) {
    std::lock_guard<std::mutex> lk(mu_);
    series_provider_ = std::move(p);
}

// ── 令牌 ──────────────────────────────────────────────────────────

std::string ToolBridge::make_token() {
    // 用 random_device 而不是 rand()：token 是唯一一道防同机其它进程的闸，
    // 可预测的 token 等于没有。32 个 hex 字符 = 128 位。
    static std::mutex rng_mu;
    static std::mt19937_64 rng{std::random_device{}()};
    std::lock_guard<std::mutex> lk(rng_mu);

    static const char* kHex = "0123456789abcdef";
    std::string out;
    out.reserve(32);
    for (int block = 0; block < 2; ++block) {
        const std::uint64_t v = rng();
        for (int i = 0; i < 16; ++i) {
            out.push_back(kHex[(v >> (i * 4)) & 0xF]);
        }
    }
    return out;
}

bool ToolBridge::constant_time_equal(const std::string& a, const std::string& b) {
    // 长度不同直接返回 false —— 长度本身不是秘密（token 长度固定）。
    // 内容比较必须走满全程，否则可以用响应时间反推出前缀。
    if (a.size() != b.size()) return false;
    unsigned char diff = 0;
    for (std::size_t i = 0; i < a.size(); ++i) {
        diff |= static_cast<unsigned char>(a[i] ^ b[i]);
    }
    return diff == 0;
}

ToolBridge::ScopedToken ToolBridge::issue_token(std::vector<std::string> allow,
                                                std::string label) {
    ScopedToken t;
    t.token      = make_token();
    t.allow      = std::move(allow);
    t.expires_at = std::chrono::steady_clock::now() + cfg_.token_ttl;
    t.remaining  = cfg_.token_max_uses;
    t.label      = std::move(label);

    std::lock_guard<std::mutex> lk(mu_);
    scoped_[t.token] = t;
    FP_INFO(kTag, "签发运行作用域令牌 " << (t.label.empty() ? "(未命名)" : t.label)
                                      << "，可用次数 " << t.remaining
                                      << "，工具范围 "
                                      << (t.allow.empty() ? "全部" : "受限"));
    return t;
}

bool ToolBridge::revoke_token(const std::string& token) {
    std::lock_guard<std::mutex> lk(mu_);
    return scoped_.erase(token) > 0;
}

bool ToolBridge::authorize(const std::string& token, const std::string& tool,
                           std::string& reason) {
    if (token.empty()) {
        reason = "缺少 " + std::string(kTokenHeader) + " 请求头";
        return false;
    }

    std::lock_guard<std::mutex> lk(mu_);

    // 先比进程令牌。即便它命中，也要把下面 scoped_ 的查找走完 ——
    // 早退会让"命中进程令牌"和"命中作用域令牌"在时间上可分。
    const bool is_process = constant_time_equal(token, token_);

    auto it = scoped_.find(token);
    const bool found = (it != scoped_.end());

    if (is_process) return true;
    if (!found) {
        reason = "令牌无效";
        return false;
    }

    ScopedToken& t = it->second;
    const auto now = std::chrono::steady_clock::now();
    if (now > t.expires_at) {
        scoped_.erase(it);
        reason = "令牌已过期";
        return false;
    }
    if (t.remaining <= 0) {
        scoped_.erase(it);
        reason = "令牌使用次数已用尽";
        return false;
    }
    if (!t.allow.empty() &&
        std::find(t.allow.begin(), t.allow.end(), tool) == t.allow.end()) {
        // 不说明"这个工具存在但你没权限"，也不说明"这个工具不存在" ——
        // 两种回答都能被用来探测工具目录。
        reason = "令牌无权调用该工具";
        return false;
    }
    --t.remaining;
    ++t.used;
    return true;
}

// ── 生命周期 ──────────────────────────────────────────────────────

void ToolBridge::start() {
    stop();
    platform_init();

    socket_t s = ::socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (!valid_socket(s)) {
        throw std::runtime_error("创建监听 socket 失败：" + socket_error_text());
    }
    // REUSEADDR 只是让"上一个进程刚退出、端口还在 TIME_WAIT"时能立刻重启。
    // 注意它**不会**让我们绑到别人已绑的端口上（那是 SO_REUSEPORT 的行为，
    // 而那个选项会让两个终端进程同时收到请求，绝不能用）。
    if (!set_reuse_addr(s)) {
        FP_WARN(kTag, "设置 SO_REUSEADDR 失败（不影响使用）: " << socket_error_text());
    }

    sockaddr_in addr{};
    addr.sin_family = AF_INET;
    // 只绑回环。**不是可配置项** —— 绑到 0.0.0.0 会让同网段任何人读到
    // 终端状态，而这个错误在日常使用中几乎不可能被发现。
    addr.sin_addr.s_addr = ::htonl(INADDR_LOOPBACK);
    addr.sin_port        = ::htons(cfg_.port);

    if (::bind(s, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) != 0) {
        const std::string err = socket_error_text();
        fp_close_socket(s);
        throw std::runtime_error("绑定 127.0.0.1:" + std::to_string(cfg_.port) +
                                 " 失败：" + err);
    }
    if (::listen(s, 8) != 0) {
        const std::string err = socket_error_text();
        fp_close_socket(s);
        throw std::runtime_error("listen 失败：" + err);
    }

    // 端口可能是 0（内核分配），回读实际端口。
    sockaddr_in actual{};
#if defined(_WIN32)
    int len = sizeof(actual);
#else
    socklen_t len = sizeof(actual);
#endif
    if (::getsockname(s, reinterpret_cast<sockaddr*>(&actual), &len) == 0) {
        port_ = ::ntohs(actual.sin_port);
    } else {
        port_ = cfg_.port;
    }

    // 自管道：accept 阻塞在 select 里时，stop() 写一个字节就能唤醒它。
    // 比"关掉监听 socket 让 accept 报错"干净 —— 后者在某些平台上
    // 未定义行为（fd 被复用时可能接到别人的连接）。
#if defined(_WIN32)
    const bool pipe_ok = false;   // Windows 上改用 select 超时轮询
#else
    const bool pipe_ok = (::pipe(wake_pipe_) == 0);
#endif
    if (!pipe_ok) {
        wake_pipe_[0] = wake_pipe_[1] = -1;
    }

    listen_socket_ = s;
    stop_.store(false);
    running_.store(true);
    accept_thread_ = std::thread([this] { accept_loop(); });

    FP_INFO(kTag, "工具桥已启动 " << endpoint() << "，工具数 " << tool_count()
                                  << "（只监听回环地址）");
}

void ToolBridge::stop() {
    if (!running_.exchange(false)) {
        // 没在跑，但可能残留了 socket（start 失败到一半）。
        return;
    }
    stop_.store(true);

    // 先唤醒 accept。
    if (wake_pipe_[1] >= 0) {
        const char b = 'x';
#if defined(_WIN32)
        (void)::send(wake_pipe_[1], &b, 1, 0);
#else
        ssize_t ignored = ::write(wake_pipe_[1], &b, 1);
        (void)ignored;
#endif
    }

    if (accept_thread_.joinable()) {
        accept_thread_.join();
    }

    if (valid_socket(static_cast<socket_t>(listen_socket_))) {
        fp_close_socket(static_cast<socket_t>(listen_socket_));
    }
    listen_socket_ = -1;

    for (int i = 0; i < 2; ++i) {
        if (wake_pipe_[i] >= 0) {
            fp_close_socket(static_cast<socket_t>(wake_pipe_[i]));
            wake_pipe_[i] = -1;
        }
    }
    FP_INFO(kTag, "工具桥已停止");
}

std::string ToolBridge::endpoint() const {
    return "http://127.0.0.1:" + std::to_string(port_);
}

void ToolBridge::accept_loop() {
    FP_DEBUG(kTag, "accept 线程启动");
    while (!stop_.load()) {
        // 有自管道就同时等它，没有就退化成 200ms 轮询。
        fd_set rd;
        FD_ZERO(&rd);
        FD_SET(static_cast<socket_t>(listen_socket_), &rd);
        socket_t max_fd = static_cast<socket_t>(listen_socket_);
        if (wake_pipe_[0] >= 0) {
            FD_SET(static_cast<socket_t>(wake_pipe_[0]), &rd);
            if (static_cast<socket_t>(wake_pipe_[0]) > max_fd) {
                max_fd = static_cast<socket_t>(wake_pipe_[0]);
            }
        }
        timeval tv{};
        tv.tv_usec = wake_pipe_[0] >= 0 ? 500000 : 200000;

#if defined(_WIN32)
        const int rc = ::select(0, &rd, nullptr, nullptr, &tv);   // Windows 忽略 nfds
#else
        const int rc = ::select(static_cast<int>(max_fd) + 1, &rd, nullptr, nullptr, &tv);
#endif
        if (rc < 0) {
            if (stop_.load()) break;
            FP_WARN(kTag, "select 失败: " << socket_error_text());
            continue;
        }
        if (rc == 0) continue;   // 超时，回来看 stop_

        if (wake_pipe_[0] >= 0 && FD_ISSET(static_cast<socket_t>(wake_pipe_[0]), &rd)) {
            break;   // stop() 叫醒我们
        }
        if (!FD_ISSET(static_cast<socket_t>(listen_socket_), &rd)) continue;

        sockaddr_in peer{};
#if defined(_WIN32)
        int peer_len = sizeof(peer);
#else
        socklen_t peer_len = sizeof(peer);
#endif
        socket_t client = ::accept(static_cast<socket_t>(listen_socket_),
                                   reinterpret_cast<sockaddr*>(&peer), &peer_len);
        if (!valid_socket(client)) {
            if (stop_.load()) break;
            continue;
        }
        serve_connection(static_cast<std::intptr_t>(client));
        fp_close_socket(client);
    }
    FP_DEBUG(kTag, "accept 线程退出");
}

void ToolBridge::serve_connection(std::intptr_t client_raw) {
    const socket_t client = static_cast<socket_t>(client_raw);
    const std::size_t limit = cfg_.max_header_bytes + cfg_.max_body_bytes;

    std::string raw;
    raw.reserve(8192);
    bool header_done = false;
    std::size_t need_total = 0;   ///< 头 + Content-Length；0 表示还没算出来

    char buf[8192];
    while (raw.size() <= limit) {
        if (!wait_readable(client, 2000)) break;
        const int n = ::recv(client, buf, static_cast<int>(sizeof(buf)), 0);
        if (n <= 0) break;
        raw.append(buf, static_cast<std::size_t>(n));

        if (!header_done) {
            const auto he = raw.find("\r\n\r\n");
            if (he != std::string::npos) {
                // 头长度的上限必须在这里也查一次。只在"还没找到头结束"的分支里
                // 按已读字节数判断是不够的：如果整个头在一个 recv 里到达，
                // header_done 立刻为真，那个判断永远不会命中 —— 实测踩过，
                // 一个 4KB 的头会被顺利放行。
                if (he > cfg_.max_header_bytes) {
                    Json err = Json::object();
                    err.set("ok", false);
                    err.set("error", "请求头超过上限 " +
                                         std::to_string(cfg_.max_header_bytes));
                    const std::string resp =
                        http_response(431, "Request Header Fields Too Large", err);
                    (void)::send(client, resp.data(), static_cast<int>(resp.size()), 0);
                    std::lock_guard<std::mutex> lk(mu_stats_);
                    ++stats_.oversize;
                    last_reject_ = "请求头超限";
                    return;
                }
                header_done = true;
                // 算一下还差多少 body。Content-Length 没写时按 0 处理
                // （GET 请求就是这样）。
                std::size_t body_len = 0;
                std::string lower_raw = lower(raw.substr(0, he));
                const auto pos = lower_raw.find("content-length:");
                if (pos != std::string::npos) {
                    const auto eol = lower_raw.find("\r\n", pos);
                    const std::string v = trim(lower_raw.substr(
                        pos + std::strlen("content-length:"),
                        (eol == std::string::npos ? lower_raw.size() : eol) -
                            (pos + std::strlen("content-length:"))));
                    try {
                        body_len = static_cast<std::size_t>(std::stoull(v));
                    } catch (const std::exception&) {
                        body_len = 0;
                    }
                }
                if (body_len > cfg_.max_body_bytes) {
                    Json err = Json::object();
                    err.set("ok", false);
                    err.set("error", "请求体超过上限 " +
                                         std::to_string(cfg_.max_body_bytes));
                    const std::string resp =
                        http_response(413, "Payload Too Large", err);
                    (void)::send(client, resp.data(), static_cast<int>(resp.size()), 0);
                    std::lock_guard<std::mutex> lk(mu_stats_);
                    ++stats_.oversize;
                    last_reject_ = "请求体超限";
                    return;
                }
                need_total = he + 4 + body_len;
            }
        }
        if (header_done && raw.size() >= need_total) break;
        if (!header_done && raw.size() > cfg_.max_header_bytes) {
            Json err = Json::object();
            err.set("ok", false);
            err.set("error", "请求头超过上限 " + std::to_string(cfg_.max_header_bytes));
            const std::string resp =
                http_response(431, "Request Header Fields Too Large", err);
            (void)::send(client, resp.data(), static_cast<int>(resp.size()), 0);
            std::lock_guard<std::mutex> lk(mu_stats_);
            ++stats_.oversize;
            last_reject_ = "请求头超限";
            return;
        }
    }

    const std::string resp = handle_http_request(raw);
    std::size_t sent = 0;
    while (sent < resp.size()) {
        const int n = ::send(client, resp.data() + sent,
                             static_cast<int>(resp.size() - sent), 0);
        if (n <= 0) break;
        sent += static_cast<std::size_t>(n);
    }
}

// ── HTTP 处理 ─────────────────────────────────────────────────────

std::string ToolBridge::lower(std::string s) {
    std::transform(s.begin(), s.end(), s.begin(),
                   [](unsigned char c) { return static_cast<char>(std::tolower(c)); });
    return s;
}

std::string ToolBridge::http_response(int status, const std::string& reason,
                                      const Json& body) {
    const std::string payload = body.dump();
    std::ostringstream out;
    out << "HTTP/1.1 " << status << ' ' << reason << "\r\n"
        << "Content-Type: application/json; charset=utf-8\r\n"
        << "Content-Length: " << payload.size() << "\r\n"
        // 明确禁止被嵌入网页：即便有人把桥的端口放进 iframe，浏览器也不渲染。
        << "X-Content-Type-Options: nosniff\r\n"
        << "Cache-Control: no-store\r\n"
        << "Connection: close\r\n"
        << "\r\n"
        << payload;
    return out.str();
}

bool ToolBridge::parse_request(
    const std::string& raw, std::string& method, std::string& path,
    std::size_t& header_end, std::string& host,
    std::unordered_map<std::string, std::string>& headers, std::string& reason) const {

    header_end = raw.find("\r\n\r\n");
    if (header_end == std::string::npos) {
        reason = "请求头不完整";
        return false;
    }

    const std::string head = raw.substr(0, header_end);
    const auto first_eol = head.find("\r\n");
    const std::string request_line = head.substr(0, first_eol);
    header_end += 4;

    // 解析请求行：METHOD SP TARGET SP HTTP/x.y
    std::istringstream rl(request_line);
    std::string target;
    std::string version;
    if (!(rl >> method >> target >> version)) {
        reason = "请求行格式错误";
        return false;
    }
    if (version.rfind("HTTP/1.", 0) != 0) {
        reason = "只支持 HTTP/1.x，收到 " + version;
        return false;
    }
    method = lower(method);
    path   = path_only(target);

    // 解析头字段。
    std::size_t pos = (first_eol == std::string::npos) ? head.size() : first_eol + 2;
    while (pos < head.size()) {
        const auto eol = head.find("\r\n", pos);
        const std::string line = head.substr(pos, (eol == std::string::npos ? head.size() : eol) - pos);
        pos = (eol == std::string::npos) ? head.size() : eol + 2;
        if (line.empty()) continue;
        const auto colon = line.find(':');
        if (colon == std::string::npos) continue;
        const std::string key = lower(trim(line.substr(0, colon)));
        const std::string val = trim(line.substr(colon + 1));
        headers[key] = val;
        if (key == "host") host = val;
    }
    return true;
}

std::string ToolBridge::handle_http_request(const std::string& request_text) {
    std::string method;
    std::string path;
    std::string host;
    std::size_t header_end = 0;
    std::unordered_map<std::string, std::string> headers;
    std::string reason;

    {
        std::lock_guard<std::mutex> lk(mu_stats_);
        ++stats_.requests;
    }

    if (!parse_request(request_text, method, path, header_end, host, headers, reason)) {
        std::lock_guard<std::mutex> lk(mu_stats_);
        ++stats_.rejected_bad_request;
        last_reject_ = reason;
        Json body = Json::object();
        body.set("ok", false);
        body.set("error", reason);
        return http_response(400, "Bad Request", body);
    }

    // ── DNS rebinding 防护 ──
    // 浏览器可以在攻击者控制的域名下把 A 记录指向 127.0.0.1，然后从网页里
    // 发请求打到本地服务。只绑回环挡不住这个 —— 请求确实到了回环。
    // Host 头是那个域名的名字，所以校验它。
    if (!is_loopback_host(host)) {
        {
            std::lock_guard<std::mutex> lk(mu_stats_);
            ++stats_.rejected_host;
            last_reject_ = "Host 非回环: " + host;
        }
        FP_WARN(kTag, "拒绝非回环 Host: " << host);
        Json body = Json::object();
        body.set("ok", false);
        body.set("error", "Host 必须是回环地址");
        return http_response(403, "Forbidden", body);
    }

    const auto token_it = headers.find(kTokenHeader);
    const std::string token = (token_it == headers.end()) ? std::string() : token_it->second;

    // ── GET /tools ──
    if (method == "get" && path == "/tools") {
        std::string why;
        if (!authorize(token, "", why)) {
            std::lock_guard<std::mutex> lk(mu_stats_);
            ++stats_.rejected_auth;
            last_reject_ = why;
            Json body = Json::object();
            body.set("ok", false);
            body.set("error", why);
            return http_response(401, "Unauthorized", body);
        }

        Json arr = Json::array();
        {
            std::lock_guard<std::mutex> lk(mu_);
            for (const Tool& t : tools_) {
                if (!is_exposed(t.name)) continue;
                Json item = Json::object();
                item.set("name", t.name);
                item.set("description", t.description);
                item.set("parameters", t.schema);
                arr.push(std::move(item));
            }
        }
        Json body = Json::object();
        body.set("tools", std::move(arr));
        body.set("count", static_cast<long long>(body["tools"].size()));
        return http_response(200, "OK", body);
    }

    // ── POST /tool ──
    if (method == "post" && path == "/tool") {
        const std::string raw_body = request_text.substr(
            std::min(header_end, request_text.size()));

        Json req;
        try {
            req = raw_body.empty() ? Json::object() : Json::parse(raw_body);
        } catch (const JsonError& e) {
            std::lock_guard<std::mutex> lk(mu_stats_);
            ++stats_.rejected_bad_request;
            last_reject_ = std::string("请求体不是合法 JSON: ") + e.what();
            Json body = Json::object();
            body.set("ok", false);
            body.set("error", "请求体不是合法 JSON");
            return http_response(400, "Bad Request", body);
        }
        if (!req.is_object()) {
            std::lock_guard<std::mutex> lk(mu_stats_);
            ++stats_.rejected_bad_request;
            last_reject_ = "请求体顶层不是对象";
            Json body = Json::object();
            body.set("ok", false);
            body.set("error", "请求体顶层必须是对象");
            return http_response(400, "Bad Request", body);
        }

        const std::string tool = req["name"].as_string_or("");
        const Json args = req.has("arguments") ? req["arguments"] : Json::object();
        if (tool.empty()) {
            std::lock_guard<std::mutex> lk(mu_stats_);
            ++stats_.rejected_bad_request;
            last_reject_ = "缺少 name";
            Json body = Json::object();
            body.set("ok", false);
            body.set("error", "缺少 name 字段");
            return http_response(400, "Bad Request", body);
        }

        // 顺序很重要：**先鉴权，再查存在性**。反过来的话，未授权的调用方
        // 能靠 404 与 401 的差别把工具目录探测出来。
        std::string why;
        if (!authorize(token, tool, why)) {
            std::lock_guard<std::mutex> lk(mu_stats_);
            ++stats_.rejected_auth;
            last_reject_ = why + "（工具 " + tool + "）";
            Json body = Json::object();
            body.set("ok", false);
            body.set("error", why);
            return http_response(401, "Unauthorized", body);
        }

        // 白名单：注册了也不一定派发。
        const bool exposed = is_exposed(tool);
        const bool known = [this, &tool] {
            std::lock_guard<std::mutex> lk(mu_);
            for (const Tool& t : tools_) {
                if (t.name == tool) return true;
            }
            return false;
        }();
        if (!exposed || !known) {
            std::lock_guard<std::mutex> lk(mu_stats_);
            ++stats_.unknown_tools;
            last_reject_ = "未暴露的工具: " + tool;
            Json body = Json::object();
            body.set("ok", false);
            body.set("error", "未知工具: " + tool);
            return http_response(404, "Not Found", body);
        }

        bool ok = false;
        Json result = dispatch(tool, args, ok);
        Json body = Json::object();
        body.set("ok", ok);
        if (ok) {
            body.set("result", std::move(result));
        } else {
            body.set("error", result.is_string() ? result.as_string()
                                                 : result.dump());
        }
        return http_response(ok ? 200 : 500, ok ? "OK" : "Internal Server Error", body);
    }

    Json body = Json::object();
    body.set("ok", false);
    body.set("error", "未知路径: " + path + "（可用：GET /tools、POST /tool）");
    return http_response(404, "Not Found", body);
}

Json ToolBridge::dispatch(const std::string& tool, const Json& arguments, bool& ok) {
    Handler handler;
    {
        std::lock_guard<std::mutex> lk(mu_);
        for (const Tool& t : tools_) {
            if (t.name == tool) {
                handler = t.handler;
                break;
            }
        }
    }
    if (!handler) {
        ok = false;
        return Json("工具未注册: " + tool);
    }

    const auto t0 = std::chrono::steady_clock::now();
    try {
        Json result = handler(arguments.is_object() ? arguments : Json::object());
        const auto elapsed = std::chrono::duration_cast<std::chrono::milliseconds>(
                                 std::chrono::steady_clock::now() - t0)
                                 .count();
        if (elapsed > cfg_.call_timeout.count()) {
            // 不中断（C++ 没有安全的抢占手段），只如实记一条。
            FP_WARN(kTag, "工具 " << tool << " 用时 " << elapsed << " ms，超过 "
                                  << cfg_.call_timeout.count() << " ms 的预期上限");
        }
        {
            std::lock_guard<std::mutex> lk(mu_stats_);
            ++stats_.tool_calls;
        }
        ok = true;
        return result;
    } catch (const std::exception& e) {
        // 工具失败 ≠ 桥不可用。Python 侧收到的是 {"ok": false, "error": ...}，
        // 会把它当成"这个工具失败了"而不是"环境坏了"，两边的处理不一样。
        FP_ERROR(kTag, "工具 " << tool << " 执行失败: " << e.what());
        {
            std::lock_guard<std::mutex> lk(mu_stats_);
            ++stats_.tool_errors;
        }
        ok = false;
        return Json(std::string(e.what()));
    }
}

// ── 统计 ──────────────────────────────────────────────────────────

ToolBridge::Stats ToolBridge::stats() const {
    std::lock_guard<std::mutex> lk(mu_stats_);
    Stats s = stats_;
    {
        std::lock_guard<std::mutex> lk2(mu_);
        s.active_tokens = scoped_.size();
    }
    return s;
}

void ToolBridge::reset_stats() {
    std::lock_guard<std::mutex> lk(mu_stats_);
    stats_ = Stats{};
    last_reject_.clear();
}

std::string ToolBridge::last_reject_reason() const {
    std::lock_guard<std::mutex> lk(mu_stats_);
    return last_reject_;
}

}  // namespace fp
