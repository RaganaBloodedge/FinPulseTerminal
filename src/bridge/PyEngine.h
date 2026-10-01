// FinPulse Terminal — Python 分析引擎的生命周期管理
//
// 职责边界：
//   RpcClient  只负责"跟一个已经跑起来的子进程说话"；
//   PyEngine   负责"怎么把那个子进程弄起来、怎么判断它是不是健康、
//               坏了以后要不要并且能不能重来"。
//
// 关于 -u 参数：Python 的 stdout 连管道时默认是块缓冲（约 8KB）。
// 不加 -u 的话，引擎算完一个请求把 JSON 写进 stdout，字节却卡在缓冲区里，
// 直到攒满 8KB 才真正发出来 —— 表现为"每个请求都要等好几秒才回"。
// 这是子进程桥接最经典的一个坑，所以 -u 和 PYTHONUNBUFFERED=1 双重保险。
#pragma once

#include <atomic>
#include <cstdint>
#include <stdexcept>
#include <string>
#include <vector>

#include "bridge/RpcClient.h"
#include "model/Types.h"

namespace fp {

/// 引擎无法启动 / 无法恢复时抛出。
class EngineError : public std::runtime_error {
public:
    explicit EngineError(std::string msg) : std::runtime_error(std::move(msg)) {}
};

class PyEngine {
public:
    struct Config {
        std::string python;                      ///< 空 = 自动探测
        std::string module{"finpulse_engine"};
        /// 包含 finpulse_engine 包的目录，会作为 PYTHONPATH 传给子进程。
        std::string python_root;
        int  handshake_timeout_ms{12000};
        int  call_timeout_ms{20000};
        int  max_restarts{3};
        bool verbose{false};                     ///< true 时把引擎 stderr 也转发到我们的日志
    };

    /// 握手时引擎上报的能力清单，用于在 UI 上列出可选数据源 / 预测方法。
    struct EngineInfo {
        std::string              name;
        std::string              version;
        std::string              python_version;
        std::vector<std::string> sources;
        std::vector<std::string> forecasters;
        int                      protocol{1};
    };

    PyEngine() = default;
    explicit PyEngine(Config cfg);
    ~PyEngine();

    PyEngine(const PyEngine&)            = delete;
    PyEngine& operator=(const PyEngine&) = delete;

    /// 启动并握手。失败抛 EngineError。
    void start();
    /// 主动停机（不触发重启）。
    void stop();

    bool              alive() const { return rpc_.alive(); }
    const EngineInfo& info() const noexcept { return info_; }
    const std::string& interpreter() const noexcept { return interpreter_; }
    std::uint64_t     restart_count() const noexcept { return restarts_; }
    bool              started() const noexcept { return started_; }

    RpcClient&       rpc() noexcept { return rpc_; }
    const RpcClient& rpc() const noexcept { return rpc_; }

    /// 检查引擎健康；不健康则按指数退避重启。仍失败则抛 EngineError。
    /// 所有语义化调用都会先走这一步，因此上层不必自己关心重启。
    void ensure_alive();

    // ── 语义化 API（内部就是 rpc().call，只是把参数/返回都强类型化）──

    /// 从指定数据源取一段日线。seed >= 0 时合成源可复现。
    /// 装载一段行情。
    ///
    /// ``extra`` 是**透传给数据源**的额外参数：数据源是可插拔的，接口上
    /// 不该硬编码某个数据源的参数名（tushare 的 token 就走这里）。
    /// ``raw_out`` 非空时把引擎的原始响应整份拷出来 —— 数据出处
    /// （实时接口 / 本地缓存 / 合成演示）只在响应里，序列本身带不了它。
    CandleSeries load_series(const std::string& source,
                             const std::string& symbol,
                             std::size_t        bars,
                             long long          seed = -1,
                             const Json&        extra = Json::object(),
                             Json*              raw_out = nullptr);

    /// 计算指标。specs 形如 {"ma:5,20", "rsi:14", "macd:12,26,9", "boll:20,2"}。
    Json compute_indicators(const std::vector<Candle>& bars,
                            const std::vector<std::string>& specs);

    Json compute_stats(const std::vector<Candle>& bars);

    ForecastResult forecast(const std::vector<Candle>& bars,
                            const std::string&  method,
                            std::size_t         horizon,
                            const Json&         options = Json::object());

    BacktestMetrics backtest(const std::vector<Candle>& bars,
                             const std::string&  method,
                             std::size_t         horizon,
                             std::size_t         folds);

    void set_event_handler(RpcClient::EventHandler h) { rpc_.set_event_handler(std::move(h)); }

private:
    void        handshake();
    void        do_start(const std::string& interpreter);
    void        restart_with_backoff(const std::string& why);
    static std::string detect_interpreter(const Config& cfg);

    Config        cfg_;
    RpcClient     rpc_;
    EngineInfo    info_;
    std::string   interpreter_;
    std::uint64_t restarts_{0};
    bool          started_{false};
    std::atomic<bool> restarting_{false};
};

}  // namespace fp
