// FinPulse Terminal — 终端工具桥（反向工具通道）
//
// ── 这个文件解决什么问题
//
// 通常的 C++/Python 分工是单向的：C++ 发请求，Python 算完返回。但终端里有
// 大量状态**只存在于 C++ 侧** —— 正在回放的行情、总线投递统计、数据质量。
// Python 分析进程看不见这些。于是 agent 在推理时只能凭它自己从 bars 算出来
// 的东西说话，问不了"终端现在正在收哪只票的行情"。
//
// Fincept 的解法是 TerminalMcpBridge：C++ 起一个 localhost HTTP 服务端，
// 把自己注册的工具目录暴露出去，Python 侧用 urllib 回调 POST /tool。
// 工具调用的方向就此反转。本项目复刻同一机制，但只暴露**我们真正拥有的**
// 那几样东西（行情快照 / 总线统计 / 数据质量），不做无意义的规模模仿。
//
// ── 安全边界（照抄 Fincept 的三条，并说明哪一条不适用）
//
//   1. **只监听回环地址**。不是可配置项 —— 一旦绑到 0.0.0.0，同网段任何
//      人都能读到终端状态，而且这个错误很难在后期的使用中被发现。
//      另外校验 Host 头：DNS rebinding 能让浏览器把一个外部域名解析到
//      127.0.0.1，出站请求就变成了对本地服务的请求。Host 不是回环就拒绝。
//   2. **令牌**。进程令牌 + 运行作用域令牌两种，都是常量时间比较。
//      Fincept 还有第三种"破坏性操作令牌"，本项目**没有**破坏性工具，
//      所以不实现 —— 加一个永远用不上的机制只会增加理解成本。
//   3. **帧大小上限**。请求头 16 KiB、请求体 4 MiB。没有上限的话，
//      一个坏掉的（或被劫持的）客户端能让终端把内存吃光。
//
// 不做的：TLS（回环上没有中间人）、认证协商（令牌就是全部）、
// 多连接并发（单线程串行 accept，工具调用本身是毫秒级）。
//
// ── 线程模型
//
// 一个 accept 线程 + 每连接就地串行处理。工具处理器会读总线统计等共享状态，
// 那些状态自己保证线程安全；这里不额外加锁，避免"两层锁互相等"。
// stop() 通过 shutdown() 唤醒阻塞中的 accept，然后 join。
#pragma once

#include <atomic>
#include <chrono>
#include <cstdint>
#include <functional>
#include <mutex>
#include <string>
#include <thread>
#include <unordered_map>
#include <vector>

#include "core/DataHub.h"
#include "core/Json.h"

namespace fp {

/// 工具桥的运行参数。
///
/// **为什么在命名空间作用域而不嵌套在类里**：GCC 不允许把
/// ``= Config{}`` 这类默认实参指向"当前正在定义的类"的嵌套类型，因为
/// 该类型的默认成员初始化器在类的右大括号之前还不算完整。
struct ToolBridgeConfig {
    /// 监听端口。0 = 让内核挑一个空闲端口（单测与"不想占用固定端口"用）。
    std::uint16_t port{0};
    /// 运行作用域令牌的有效期。Fincept 取 6 小时。
    std::chrono::seconds token_ttl{std::chrono::hours(6)};
    /// 运行作用域令牌的最大使用次数。防的是"令牌泄了之后被无限用"。
    int token_max_uses{256};
    /// 请求头 / 请求体的字节上限。没有上限的话，一个坏掉的（或被劫持的）
    /// 客户端能让终端把内存吃光。
    std::size_t max_header_bytes{16 * 1024};
    std::size_t max_body_bytes{4 * 1024 * 1024};
    /// 单次工具调用的执行时间上限。超时**不中断**处理（C++ 没有安全的
    /// 抢占手段），只记一条日志 —— 假装能中断比不中断更糟。
    std::chrono::milliseconds call_timeout{std::chrono::seconds(10)};
};

class ToolBridge {
public:
    /// 一个工具的实现：入参（JSON 对象）→ 结果（JSON 对象）。
    /// 抛异常会被翻译成 ``{"ok": false, "error": "..."}`` 返回给调用方。
    using Handler = std::function<Json(const Json& arguments)>;

    struct Tool {
        std::string name;
        std::string description;
        /// 参数的 JSON Schema 片段（properties / required），直接透传给模型。
        Json        schema{Json::object()};
        Handler     handler;
    };

    /// 运行作用域令牌：绑一组工具名的过滤器，有有效期与使用次数上限。
    struct ScopedToken {
        std::string                            token;
        std::vector<std::string>               allow;      ///< 空 = 全部
        std::chrono::steady_clock::time_point  expires_at;
        int                                    remaining{0};
        int                                    used{0};
        std::string                            label;
    };

    /// 类内别名，让调用点仍然可以写成 ``ToolBridge::Config``。
    using Config = ToolBridgeConfig;

    explicit ToolBridge(DataHub* hub = nullptr, Config cfg = ToolBridgeConfig{});
    ~ToolBridge();

    ToolBridge(const ToolBridge&)            = delete;
    ToolBridge& operator=(const ToolBridge&) = delete;

    // ── 工具注册 ────────────────────────────────────────────

    /// 注册一个工具。同名重复注册抛 std::invalid_argument —— 静默覆盖会让
    /// "我改了实现但行为没变"变成一个需要查半天的问题。
    void add_tool(Tool tool);

    /// 注册本项目真正拥有的那几样工具。幂等。
    void register_default_tools();

    /// 行情快照的提供者。入参是标的代码（可能为空 = 用终端当前回放的标的），
    /// 返回 ``Quote::to_json()`` 那样的对象；没有数据时返回空对象。
    ///
    /// **为什么不在桥里直接读行情**：终端里"当前行情"存在哪儿是实现细节
    /// （CliApp 一份、MainWindow 一份，将来还可能有第三个）。桥只声明它需要
    /// 什么，由持有状态的那一方注入 —— 否则桥就得反过来认识所有前端。
    using QuoteProvider = std::function<Json(const std::string& symbol)>;
    void set_quote_provider(QuoteProvider p);

    /// 已加载行情序列 + 数据质量的提供者。契约同上。
    using SeriesProvider = std::function<Json(const std::string& symbol)>;
    void set_series_provider(SeriesProvider p);

    std::vector<std::string> tool_names() const;
    std::size_t              tool_count() const;

    /// 只暴露给**智能体**的工具名白名单。不在名单里的工具永远不会被派发。
    ///
    /// Fincept 那边的对应物是"永不派发 navigation/system/settings/ai-chat/meta"。
    /// 本项目目前没有那些工具，所以这里是一份**正向**白名单：将来有人往
    /// 桥上加了一个工具，但忘了它会出现在模型面前，这个白名单能兜住。
    static bool is_exposed(const std::string& name);

    // ── 生命周期 ────────────────────────────────────────────

    /// 启动监听。失败抛 std::runtime_error（含原因）。
    /// 已在运行时先 stop()。
    void start();
    /// 停止并 join。可重复调用。
    void stop();

    bool          running() const noexcept { return running_.load(); }
    std::uint16_t port() const noexcept { return port_; }
    /// 形如 "http://127.0.0.1:34567"，直接交给 Python 侧的 tool_bridge.endpoint。
    std::string   endpoint() const;

    /// 进程令牌。**不要写进日志**。进程启动时随机生成。
    const std::string& token() const noexcept { return token_; }

    /// 签发一个运行作用域令牌（可绑定工具子集）。
    ScopedToken issue_token(std::vector<std::string> allow = {},
                            std::string label = {});
    /// 主动吊销。
    bool revoke_token(const std::string& token);

    // ── 统计 ────────────────────────────────────────────────

    struct Stats {
        std::uint64_t requests{0};
        std::uint64_t rejected_auth{0};
        std::uint64_t rejected_host{0};
        std::uint64_t rejected_bad_request{0};
        std::uint64_t tool_calls{0};
        std::uint64_t tool_errors{0};
        std::uint64_t unknown_tools{0};
        std::uint64_t oversize{0};
        std::uint64_t active_tokens{0};
    };
    Stats stats() const;
    void  reset_stats();

    /// 最近一次拒绝的原因。界面/CLI 用它解释"为什么桥连不上"。
    std::string last_reject_reason() const;

    /// 供测试直接喂一条 HTTP 请求（不开真实连接）。
    /// 返回完整的 HTTP 响应文本。生产路径也会走到这里，所以测的就是真代码。
    std::string handle_http_request(const std::string& request_text);

private:
    static std::string        make_token();
    static bool               constant_time_equal(const std::string& a, const std::string& b);
    static std::string        http_response(int status, const std::string& reason,
                                            const Json& body);
    static std::string        lower(std::string s);

    /// 解析请求行 + 头。失败返回 false 并填 reason。
    bool parse_request(const std::string& raw, std::string& method, std::string& path,
                       std::size_t& header_end, std::string& host,
                       std::unordered_map<std::string, std::string>& headers,
                       std::string& reason) const;

    void accept_loop();
    void serve_connection(std::intptr_t client_socket);

    bool authorize(const std::string& token, const std::string& tool,
                   std::string& reason);
    Json dispatch(const std::string& tool, const Json& arguments, bool& ok);

    DataHub*  hub_;
    Config    cfg_;

    mutable std::mutex  mu_;
    std::vector<Tool>   tools_;
    std::unordered_map<std::string, ScopedToken> scoped_;
    QuoteProvider       quote_provider_;
    SeriesProvider      series_provider_;

    std::string   token_;              ///< 进程令牌；仅在 mu_ 下读
    std::uint16_t port_{0};
    std::atomic<bool> running_{false};
    std::atomic<bool> stop_{false};
    std::thread       accept_thread_;

    std::intptr_t listen_socket_{-1};
    int wake_pipe_[2]{-1, -1};   ///< 自管道：用 select 同时等 accept 和退出信号

    mutable std::mutex mu_stats_;
    Stats              stats_{};
    std::string        last_reject_;
};

}  // namespace fp
