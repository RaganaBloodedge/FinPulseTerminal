// FinPulse Terminal — 命令行前端
//
// 存在的意义有两个：
//
//   1. **可验证**。GUI 的截图谁都能 PS，但一段"输入什么命令、输出什么数字"
//      的终端记录是假的不了的。这个 CLI 跑出来的每个数字都来自真实的
//      Python 引擎计算。
//
//   2. **可回归**。图形界面的自动化测试成本很高，而核心链路（桥接、指标、
//      预测、回测）全都不依赖图形。把这条链路做成 CLI，就能用脚本一键跑通，
//      回归时不需要人拉窗口截图对比。
//
// 输出刻意做成固定的分节格式：人看是报告，脚本看是可 grep 的结构。
#pragma once

#include <cstddef>
#include <string>
#include <vector>

#include "bridge/PyEngine.h"
#include "model/Types.h"

namespace fp {

class CliApp {
public:
    struct Options {
        std::string python;          ///< 指定解释器，空 = 自动探测
        std::string python_root;     ///< 引擎包所在目录（默认自动推导）
        std::string source{"synthetic"};
        std::string symbol{"SYNTH"};
        std::string csv_path;        ///< source=csv 时的文件路径
        std::size_t bars{250};
        long long   seed{42};
        /// Tushare 密钥。留空时数据源会依次去环境变量 TUSHARE_TOKEN、
        /// 启动配置档里找 —— 所以这里通常不需要填。
        std::string tushare_token;

        // ── 启动配置档 ────────────────────────────────────────
        bool save_profile{false};   ///< 把当前设置写成 `~/.finpulse/profile.json`
        bool use_profile{true};     ///< 关闭后完全忽略配置档（CI / 复现用）

        // ── 批量拉取 ──────────────────────────────────────────
        bool                     pull{false};       ///< 只做批量拉取，然后退出
        std::vector<std::string> pull_symbols;      ///< 留空 = 用配置档/内置清单

        std::string forecast_method{"ar"};
        std::size_t horizon{5};
        std::size_t folds{5};
        std::size_t min_train{60};

        std::vector<std::string> indicators{"ma:5,20", "rsi:14", "macd:12,26,9", "boll:20,2"};

        bool   bus_demo{false};      ///< 回放行情并展示 DataHub 的订阅/投递统计
        double replay_speed{0.0};    ///< 回放倍速，0 = 全速

        // ── 智能体研判 ────────────────────────────────────────
        //
        // 这一节能跑起来，靠的是"同一把 RPC 通道同时装着分析方法和 agent
        // 方法"：引擎是同一个，智能体不是第二个进程。所以研判用的行情就是
        // 上面 [2/6] 刚装载的那 250 根，不需要重新喂一遍。
        bool        agent{false};            ///< 跑一场投委会研判
        std::string agent_role;              ///< 非空 → 只跑这一个角色（不组会）
        std::string agent_panel;             ///< 指定投委会 id，空 = 默认
        int         agent_rounds{0};         ///< 0 = 用投委会配置里的轮数
        std::string agent_provider;          ///< 空 = 各角色配置里自己的 provider
        /// 推理后端接入参数。有它们就能**当场**接上大模型：
        ///   --provider deepseek --api-key <密钥> --model deepseek-chat
        /// 不必改 JSON，也不必事先导出环境变量。
        std::string agent_model;             ///< 空 = 用角色配置里的 model_id
        std::string agent_base_url;          ///< 空 = 用该 provider 的官方端点
        std::string agent_api_key;           ///< 空 = 回落到对应环境变量
        bool        agent_no_bridge{false};  ///< 不起工具桥，演示"终端工具不可用"的降级路径
        bool        agent_report{true};      ///< 打印报告正文（--brief 关掉）
        bool        agent_list{false};       ///< 只列出角色与投委会然后退出

        bool   verbose{false};
        bool   json_out{false};      ///< 只输出一行汇总 JSON，便于脚本消费

        /// 由 main.cpp 填：本次有哪些设置来自启动配置档。
        /// 打在报告开头 —— "它为什么自己去拉了茅台"必须有个能读到的答案。
        std::string profile_note;
    };

    /// 一场研判的要点。**由 run_agent 返回而不是塞进成员变量**：
    /// 收尾汇总里要用到，但隐式状态会让"run_agent 被调了几次"变成
    /// 一个要靠读代码才能回答的问题。
    struct AgentSummary {
        bool        valid{false};
        std::string run_id;
        std::string direction;      ///< 空 = 有效决议但未给方向，或根本没成会
        std::string confidence;
        std::size_t tool_calls{0};  ///< Python 反向回调 C++ 终端的次数
    };

    explicit CliApp(Options opt);

    /// 返回进程退出码：0 成功，非 0 失败。
    int run();

    static void print_usage(const char* argv0);

private:
    // 输出辅助
    void section(const std::string& title) const;
    void kv(const std::string& key, const std::string& value) const;
    /// kv 的续行：缩进对齐到 value 列。用 kv("", ...) 当续行会打出一个
    /// 孤零零的冒号，读起来像键名丢了。
    void note(const std::string& text) const;
    /// 打印行情摘要。第二个参数是引擎的原始响应 —— 数据出处（实时接口
    /// 还是本地缓存）只在响应里，序列本身带不了这个信息。
    void print_series(const CandleSeries& cs, const Json& raw) const;
    void print_indicators(const Json& res) const;
    void print_stats(const Json& s) const;
    void print_forecast(const ForecastResult& r) const;
    void print_backtest(const Json& raw, const BacktestMetrics& m) const;
    /// 按值接收：回放会把序列的所有权交出去，调用方之后还要用原序列。
    void run_bus_demo(CandleSeries cs);

    /// 智能体研判：起工具桥 → 跑投委会（或单角色）→ 打印结论与事件流。
    /// 同样按值接收，理由同 run_bus_demo。
    AgentSummary run_agent(CandleSeries cs);

    /// 批量拉取：把清单里的标的逐个取数并落成 CSV 缓存。
    /// 只做这一件事，做完就退出 —— 它是"刷新本地数据"，不是分析报告的一节。
    int run_pull();

    /// 把当前设置写成启动配置档。返回 false 表示失败（原因已打印）。
    bool save_profile_now() const;

    /// 分节编号，形如 "[3/7]"。开了智能体研判总节数才是 7。
    std::string stage(int index) const;

    static PyEngine::Config build_engine_config(const Options& o);

    static std::string fmt(double v, int decimals = 2);
    static std::string fmt_int(long long v);
    static std::string fmt_pct(double v, bool signed_ = true);

    Options  opt_;
    PyEngine engine_;
};

}  // namespace fp
