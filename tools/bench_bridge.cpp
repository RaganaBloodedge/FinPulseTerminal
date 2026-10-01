// FinPulse Terminal — 桥接层性能基准
//
// 三个指标，各自回答一个不同的问题：
//
//   1. 帧编解码吞吐     —— 纯 CPU 开销。回答"这条协议本身贵不贵"。
//   2. DataHub 发布吞吐 —— 锁竞争与派发开销。回答"总线会不会成为瓶颈"。
//   3. RPC 端到端往返   —— 真的拉起 Python 引擎，测一轮完整往返。
//                          回答"跨语言 + 跨进程这件事到底有多慢"。
//
// 第 3 项是这个项目里唯一同时跨越了进程边界和语言边界的数字，
// 也是判断桥接设计是否合格的关键依据：如果一轮往返要几十毫秒，
// 那么 UI 上"每改一次参数就重算指标"就是不可行的，必须改成批量请求。
//
// 用法::
//     ./finpulse-bench            # 全跑
//     ./finpulse-bench codec      # 只跑某一项
//     ./finpulse-bench rpc 500    # 指定第二项的次数

#include "bridge/FrameCodec.h"
#include "bridge/PyEngine.h"
#include "core/DataHub.h"
#include "core/Json.h"
#include "core/Log.h"
#include "core/Version.h"

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <string>
#include <vector>

// CMake 会把这个定义注进来（finpulse_core 的 PUBLIC 定义）。
// 单独编译时兜底成空串，运行时再靠环境变量找引擎。
#ifndef FINPULSE_PYTHON_ROOT
#  define FINPULSE_PYTHON_ROOT ""
#endif

namespace {

using Clock = std::chrono::steady_clock;

double ms_since(Clock::time_point t0) {
    return std::chrono::duration<double, std::milli>(Clock::now() - t0).count();
}

void headline(const char* text) {
    std::printf("\n%s\n", text);
    std::printf("%s\n", std::string(std::strlen(text) + 4, '-').c_str());
}

void row(const char* label, double value, const char* unit) {
    std::printf("  %-34s %14.3f %s\n", label, value, unit);
}

std::string human_bytes(double b) {
    const char* units[] = {"B", "KiB", "MiB", "GiB"};
    int i = 0;
    while (b >= 1024.0 && i < 3) {
        b /= 1024.0;
        ++i;
    }
    char buf[64];
    std::snprintf(buf, sizeof(buf), "%.2f %s", b, units[i]);
    return buf;
}

// ── 1. 帧编解码 ───────────────────────────────────────────

void bench_codec(int iterations) {
    headline("1. 帧编解码（纯 CPU）");

    fp::Json payload = fp::Json::object();
    payload.set("symbol", "AAPL");
    payload.set("last", 187.42);
    payload.set("volume", 2310000LL);
    fp::Json bars = fp::Json::array();
    for (int i = 0; i < 8; ++i) {
        bars.push(static_cast<double>(100 + i) + 0.25);
    }
    payload.set("bars", bars);

    const std::string body = payload.dump();
    std::printf("  单帧正文 %zu 字节\n\n", body.size());

    // 编码
    auto t0 = Clock::now();
    std::size_t sink = 0;
    std::string wire;
    for (int i = 0; i < iterations; ++i) {
        wire = fp::FrameCodec::encode(body);
        sink += wire.size();
    }
    const double enc_ms = ms_since(t0);

    // 解码（每次都新建 codec，把构造开销也算进去，更贴近真实用法）
    t0 = Clock::now();
    std::size_t frames = 0;
    for (int i = 0; i < iterations; ++i) {
        fp::FrameCodec codec;
        codec.append(wire);
        std::string out;
        if (codec.next(out)) ++frames;
    }
    const double dec_ms = ms_since(t0);

    const double bytes = static_cast<double>(wire.size()) * iterations;
    row("编码吞吐", iterations / (enc_ms / 1000.0) / 1e6, "M 帧/秒");
    row("解码吞吐", frames / (dec_ms / 1000.0) / 1e6, "M 帧/秒");
    row("编码带宽", bytes / (enc_ms / 1000.0) / 1024.0 / 1024.0, "MiB/秒");
    row("编码单帧耗时", enc_ms / iterations * 1000.0, "µs");
    row("解码单帧耗时", dec_ms / iterations * 1000.0, "µs");
    std::printf("  （累计处理 %s）\n", human_bytes(bytes).c_str());
    (void)sink;
}

// ── 2. DataHub ────────────────────────────────────────────

void bench_datashub(int iterations) {
    headline("2. DataHub 发布/派发");

    fp::Json payload = fp::Json::object();
    payload.set("symbol", "AAPL");
    payload.set("last", 187.42);

    // 场景 A：无订阅者（纯匹配开销 + 统计更新）
    {
        fp::DataHub hub;
        auto t0 = Clock::now();
        for (int i = 0; i < iterations; ++i) {
            hub.publish("market.quote.AAPL", payload);
        }
        const double ms = ms_since(t0);
        row("无订阅者 · 吞吐", iterations / (ms / 1000.0) / 1e6, "M 条/秒");
        row("无订阅者 · 单条", ms / iterations * 1000.0, "µs");
    }

    // 场景 B：1 个订阅者命中
    {
        fp::DataHub hub;
        std::size_t  delivered = 0;
        hub.subscribe("market.quote.*", [&](const fp::Topic&, const fp::Json&) { ++delivered; });

        auto t0 = Clock::now();
        for (int i = 0; i < iterations; ++i) {
            hub.publish("market.quote.AAPL", payload);
        }
        const double ms = ms_since(t0);
        row("1 订阅者 · 吞吐", iterations / (ms / 1000.0) / 1e6, "M 条/秒");
        row("1 订阅者 · 单条", ms / iterations * 1000.0, "µs");
        std::printf("  （投递 %zu 次，校验无丢失）\n", delivered);
    }

    // 场景 C：8 个订阅者，其中 2 个用 ** 通配
    {
        fp::DataHub hub;
        std::size_t  hits = 0;
        for (int s = 0; s < 6; ++s) {
            hub.subscribe("market.quote.*", [&](const fp::Topic&, const fp::Json&) { ++hits; });
        }
        for (int s = 0; s < 2; ++s) {
            hub.subscribe("market.**", [&](const fp::Topic&, const fp::Json&) { ++hits; });
        }

        auto t0 = Clock::now();
        for (int i = 0; i < iterations; ++i) {
            hub.publish("market.quote.AAPL", payload);
        }
        const double ms = ms_since(t0);
        row("8 订阅者 · 吞吐", iterations / (ms / 1000.0) / 1e6, "M 条/秒");
        row("8 订阅者 · 单条", ms / iterations * 1000.0, "µs");
        std::printf("  （投递 %zu 次，期望 %d）\n", hits, iterations * 8);
    }
}

// ── 3. RPC 端到端 ─────────────────────────────────────────

void bench_rpc(int iterations, const std::string& python_root) {
    headline("3. RPC 端到端往返（跨进程 + 跨语言）");

    fp::PyEngine::Config cfg;
    cfg.python_root = python_root;
    fp::PyEngine engine(cfg);

    auto t0 = Clock::now();
    try {
        engine.start();
    } catch (const std::exception& e) {
        std::printf("  跳过：无法启动引擎（%s）\n", e.what());
        std::printf("  提示：用 --python-root 指定 python/ 目录，或设置 FINPULSE_PYTHON_ROOT\n");
        return;
    }
    const double boot_ms = ms_since(t0);

    std::printf("  引擎 %s v%s  (Python %s)\n",
                engine.info().name.c_str(), engine.info().version.c_str(),
                engine.info().python_version.c_str());
    std::printf("  冷启动（含握手）: %.1f ms\n\n", boot_ms);

    // 用 ping 测纯往返开销：不发数据、不算东西，只有管道 + 分帧 + JSON
    std::vector<double> samples;
    samples.reserve(static_cast<std::size_t>(iterations));

    for (int i = 0; i < iterations; ++i) {
        fp::Json params = fp::Json::object();
        params.set("nonce", static_cast<long long>(i));

        const auto t = Clock::now();
        engine.rpc().call("ping", std::move(params), 10000);
        samples.push_back(ms_since(t));
    }

    std::sort(samples.begin(), samples.end());
    const auto at = [&samples](double q) {
        const std::size_t idx = static_cast<std::size_t>(q * (samples.size() - 1));
        return samples[idx];
    };

    double sum = 0.0;
    for (double v : samples) sum += v;

    row("ping 往返 · 最小", samples.front(), "ms");
    row("ping 往返 · p50", at(0.50), "ms");
    row("ping 往返 · p95", at(0.95), "ms");
    row("ping 往返 · p99", at(0.99), "ms");
    row("ping 往返 · 最大", samples.back(), "ms");
    row("ping 往返 · 平均", sum / samples.size(), "ms");
    row("折算吞吐", 1000.0 / (sum / samples.size()), "次/秒");

    // 顺带测一次"真实工作负载"的往返，这才是 UI 上真正会感受到的延迟
    {
        fp::CandleSeries cs = engine.load_series("synthetic", "SYNTH", 250, 42);
        std::printf("\n  数据装载 250 根: ");
        auto t = Clock::now();
        engine.compute_indicators(cs.bars(), {"ma:5,20", "rsi:14", "macd", "boll"});
        std::printf("%.1f ms（4 条指标）\n", ms_since(t));

        t = Clock::now();
        engine.compute_stats(cs.bars());
        std::printf("  风险指标计算:    %.1f ms\n", ms_since(t));

        t = Clock::now();
        engine.forecast(cs.bars(), "ar", 5);
        std::printf("  AR 预测(h=5):    %.1f ms\n", ms_since(t));

        t = Clock::now();
        engine.backtest(cs.bars(), "ar", 5, 5);
        std::printf("  回测(5折×5步):   %.1f ms\n", ms_since(t));
    }

    const auto st = engine.rpc().stats();
    std::printf("\n  累计 RPC: %llu 次（成功 %llu / 失败 %llu / 超时 %llu）\n",
                static_cast<unsigned long long>(st.requests_sent),
                static_cast<unsigned long long>(st.responses_ok),
                static_cast<unsigned long long>(st.responses_err),
                static_cast<unsigned long long>(st.timeouts));

    engine.stop();
}

int parse_int(const char* s, int fallback) {
    try {
        return std::stoi(s);
    } catch (...) {
        return fallback;
    }
}

}  // namespace

int main(int argc, char** argv) {
    std::string which = argc > 1 ? argv[1] : "all";
    const int   n     = argc > 2 ? parse_int(argv[2], 0) : 0;

    std::string python_root;
    for (int i = 1; i < argc - 1; ++i) {
        if (std::strcmp(argv[i], "--python-root") == 0) python_root = argv[i + 1];
    }
    if (python_root.empty()) {
        if (const char* env = std::getenv("FINPULSE_PYTHON_ROOT")) python_root = env;
    }
    if (python_root.empty()) python_root = FINPULSE_PYTHON_ROOT;

    fp::log_set_level(fp::LogLevel::Warn);

    std::printf("FinPulse Terminal %s — 桥接层基准\n", fp::kVersion);
    std::printf("编译器 %s\n", __VERSION__);
    std::printf("构建类型 %s\n",
#ifdef NDEBUG
                "Release"
#else
                "Debug（数字会明显偏慢，用 Release 才有参考价值）"
#endif
    );

    if (which == "all" || which == "codec") bench_codec(n > 0 ? n : 200000);
    if (which == "all" || which == "datashub") bench_datashub(n > 0 ? n : 200000);
    if (which == "all" || which == "rpc") bench_rpc(n > 0 ? n : 300, python_root);

    std::printf("\n");
    return 0;
}
