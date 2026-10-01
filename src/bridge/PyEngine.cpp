#include "bridge/PyEngine.h"

#include "core/Log.h"

#include <algorithm>
#include <chrono>
#include <fstream>
#include <sstream>
#include <thread>

namespace fp {

namespace {

constexpr const char* kTag = "engine";

bool file_exists(const std::string& path) {
    if (path.empty()) return false;
    std::ifstream f(path, std::ios::binary);
    return f.good();
}

std::string join(const std::vector<std::string>& v, const char* sep = ", ") {
    std::string out;
    for (std::size_t i = 0; i < v.size(); ++i) {
        if (i) out += sep;
        out += v[i];
    }
    return out;
}

Json bars_to_json(const std::vector<Candle>& bars) {
    Json arr = Json::array();
    for (const auto& b : bars) arr.push(b.to_json());
    return arr;
}

}  // namespace

PyEngine::PyEngine(Config cfg) : cfg_(std::move(cfg)) {}

PyEngine::~PyEngine() {
    stop();
}

std::string PyEngine::detect_interpreter(const Config& cfg) {
    if (!cfg.python.empty()) return cfg.python;

    // 打包发行时项目会自带一个 .venv，优先用它 —— 用户机器上装没装 Python 都能跑
    if (!cfg.python_root.empty()) {
#if defined(_WIN32)
        const std::string venv = cfg.python_root + "/.venv/Scripts/python.exe";
#else
        const std::string venv = cfg.python_root + "/.venv/bin/python3";
#endif
        if (file_exists(venv)) return venv;
    }

#if defined(_WIN32)
    return "python";
#else
    return "python3";
#endif
}

void PyEngine::start() {
    if (started_ && rpc_.alive()) return;

    std::vector<std::string> candidates;
    const std::string primary = detect_interpreter(cfg_);
    candidates.push_back(primary);
    // 探测是"猜"，猜错了要有回退。列表不长，逐个试的成本远低于让用户改配置。
#if defined(_WIN32)
    for (const char* alt : {"python", "py"})
        if (primary != alt) candidates.push_back(alt);
#else
    for (const char* alt : {"python3", "python"})
        if (primary != alt) candidates.push_back(alt);
#endif

    std::string last_error;
    for (const auto& cand : candidates) {
        try {
            do_start(cand);
            interpreter_ = cand;
            return;
        } catch (const std::exception& e) {
            last_error = e.what();
            FP_WARN(kTag, "以 '" << cand << "' 启动失败: " << e.what());
        }
    }
    throw EngineError("无法启动 Python 引擎，尝试过的解释器: " + join(candidates) +
                      "；最后一次错误: " + last_error);
}

void PyEngine::do_start(const std::string& interpreter) {
    Subprocess::Options proc;
    proc.program = interpreter;
    proc.args    = {"-u", "-m", cfg_.module};   // -u：见头文件里关于块缓冲的说明
    if (!cfg_.python_root.empty()) {
        proc.working_dir = cfg_.python_root;
        proc.env.emplace_back("PYTHONPATH", cfg_.python_root);
    }
    proc.env.emplace_back("PYTHONUNBUFFERED", "1");
    proc.env.emplace_back("PYTHONIOENCODING", "utf-8");
    proc.env.emplace_back("FINPULSE_LOG", cfg_.verbose ? "debug" : "warn");
#if !defined(_WIN32)
    proc.env.emplace_back("PYTHONDONTWRITEBYTECODE", "1");
#endif

    RpcClient::Options ropt;
    ropt.proc               = std::move(proc);
    ropt.default_timeout_ms = cfg_.call_timeout_ms;
    ropt.tag                = kTag;

    rpc_.start(ropt);
    rpc_.set_closed_handler([this](const std::string& reason) {
        // 这个回调跑在读线程里。只记日志，绝不做重活：
        // 在这里调 start() 会把读线程自己卡住，而超时检查也搭在那条线程上。
        // 真正的恢复动作交给下一次 ensure_alive()。
        FP_WARN(kTag, "引擎连接断开: " << reason);
    });

    try {
        handshake();
    } catch (...) {
        rpc_.stop();
        throw;
    }
    started_ = true;
}

void PyEngine::handshake() {
    Json params = Json::object();
    params.set("client", "finpulse-cpp");
    params.set("protocol", 1);

    const Json res = rpc_.call("handshake", std::move(params), cfg_.handshake_timeout_ms);
    if (!res.is_object()) throw EngineError("握手响应不是对象，引擎实现有问题");

    info_ = EngineInfo{};
    info_.name           = res["name"].as_string_or("finpulse-engine");
    info_.version        = res["version"].as_string_or("0.0.0");
    info_.python_version = res["python_version"].as_string_or("");
    info_.protocol       = static_cast<int>(res["protocol"].as_int_or(1));

    if (res["sources"].is_array()) {
        for (const auto& s : res["sources"].items()) info_.sources.push_back(s.as_string_or(""));
    }
    if (res["forecasters"].is_array()) {
        for (const auto& s : res["forecasters"].items()) info_.forecasters.push_back(s.as_string_or(""));
    }

    if (info_.protocol != 1) {
        throw EngineError("协议版本不匹配：壳=1 引擎=" + std::to_string(info_.protocol));
    }

    FP_INFO(kTag, "已连接 " << info_.name << " v" << info_.version
                            << " (python " << info_.python_version << ")");
    FP_DEBUG(kTag, "sources=[" << join(info_.sources) << "]");
    FP_DEBUG(kTag, "forecasters=[" << join(info_.forecasters) << "]");
}

void PyEngine::stop() {
    if (!started_ && !rpc_.alive()) return;
    restarting_.store(false);
    rpc_.stop();
    started_ = false;
    FP_DEBUG(kTag, "引擎已停止");
}

void PyEngine::ensure_alive() {
    if (rpc_.alive()) return;
    restart_with_backoff(rpc_.closed_reason().empty() ? "连接不可用" : rpc_.closed_reason());
}

void PyEngine::restart_with_backoff(const std::string& why) {
    if (restarts_ >= static_cast<std::uint64_t>(cfg_.max_restarts)) {
        throw EngineError("引擎已连续失败 " + std::to_string(restarts_) +
                          " 次，放弃自动恢复。最后原因: " + why);
    }

    // 两个线程同时发现引擎挂了是很常见的（比如并发发了两个请求）。
    // 只让第一个进来做重启，其余的立刻报错回去，由调用方决定要不要重试。
    bool expected = false;
    if (!restarting_.compare_exchange_strong(expected, true)) {
        throw EngineError("引擎正在重启中（" + why + "），请稍后重试");
    }
    struct FlagGuard {
        std::atomic<bool>* f;
        ~FlagGuard() { f->store(false); }
    } guard{&restarting_};

    const auto exp = std::min<std::uint64_t>(restarts_, 5);
    const auto delay = std::chrono::milliseconds(200 * (1u << exp));  // 200/400/800/1600ms
    FP_WARN(kTag, "引擎不可用（" << why << "），" << delay.count() << "ms 后执行第 "
                                 << (restarts_ + 1) << " 次重启");
    std::this_thread::sleep_for(delay);

    ++restarts_;
    start();   // 失败会抛 EngineError
    FP_INFO(kTag, "引擎已恢复（累计重启 " << restarts_ << " 次）");
}

// ── 语义化 API ────────────────────────────────────────────

CandleSeries PyEngine::load_series(const std::string& source,
                                   const std::string& symbol,
                                   std::size_t        bars,
                                   long long          seed,
                                   const Json&        extra,
                                   Json*              raw_out) {
    ensure_alive();

    Json p = Json::object();
    p.set("source", source);
    p.set("symbol", symbol);
    p.set("bars", static_cast<long long>(bars));
    if (seed >= 0) p.set("seed", seed);
    // 额外参数在**后面**设置：同名字段以调用方显式给出的为准。
    for (const auto& kv : extra.members()) p.set(kv.first, kv.second);

    const Json res = rpc_.call("source.load", std::move(p), cfg_.call_timeout_ms);
    if (raw_out) *raw_out = res;

    CandleSeries cs = CandleSeries::from_json(res);
    cs.set_symbol(symbol);
    cs.normalize();

    const auto issues = cs.validate();
    if (!issues.empty()) {
        FP_WARN(kTag, "数据质量检查发现 " << issues.size() << " 个问题，首个: " << issues.front());
    }
    return cs;
}

Json PyEngine::compute_indicators(const std::vector<Candle>& bars,
                                  const std::vector<std::string>& specs) {
    ensure_alive();

    Json p = Json::object();
    p.set("bars", bars_to_json(bars));
    Json sp = Json::array();
    for (const auto& s : specs) sp.push(Json(s));
    p.set("specs", std::move(sp));

    return rpc_.call("analysis.indicators", std::move(p), cfg_.call_timeout_ms);
}

Json PyEngine::compute_stats(const std::vector<Candle>& bars) {
    ensure_alive();

    Json p = Json::object();
    p.set("bars", bars_to_json(bars));
    return rpc_.call("analysis.stats", std::move(p), cfg_.call_timeout_ms);
}

ForecastResult PyEngine::forecast(const std::vector<Candle>& bars,
                                  const std::string&  method,
                                  std::size_t         horizon,
                                  const Json&         options) {
    ensure_alive();

    Json p = Json::object();
    p.set("bars", bars_to_json(bars));
    p.set("method", method);
    p.set("horizon", static_cast<long long>(horizon));
    p.set("options", options);

    const Json res = rpc_.call("forecast.run", std::move(p), cfg_.call_timeout_ms);
    return ForecastResult::from_json(res);
}

BacktestMetrics PyEngine::backtest(const std::vector<Candle>& bars,
                                   const std::string&  method,
                                   std::size_t         horizon,
                                   std::size_t         folds) {
    ensure_alive();

    Json p = Json::object();
    p.set("bars", bars_to_json(bars));
    p.set("method", method);
    p.set("horizon", static_cast<long long>(horizon));
    p.set("folds", static_cast<long long>(folds));

    const Json res = rpc_.call("forecast.backtest", std::move(p), cfg_.call_timeout_ms);
    return BacktestMetrics::from_json(res);
}

}  // namespace fp
