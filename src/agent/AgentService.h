// FinPulse Terminal — 智能体服务（C++ 侧门面）
//
// 这一层是"C++ 壳"与"Python 智能体"之间唯一的语义接缝。它做三件事：
//
//   1. 把 CandleSeries 转成引擎要的 bars 数组，构造 agent.* 的调用参数；
//   2. 把引擎推回来的**单向事件**（agent.role.start / agent.round.done /
//      agent.debate.done ...）转成进程总线上的主题
//          agent.stream.<run_id>
//      于是任何模块都能订阅"某个正在跑的研判"，而不需要认识 Python；
//   3. 把 RPC 层的 Json 翻译成强类型结构（RoleInfo / PanelInfo / DebateOutcome），
//      让 GUI 不必自己满 JSON 里捞字段。
//
// ── 为什么事件要绕一圈走总线
//
// RpcClient 只接受**一个**事件处理器（set_event_handler）。如果 AgentService
// 和 MainWindow 都想要事件，谁后设置谁生效，先设置的那个静默失效 —— 这类
// "看起来接上了但收不到"的问题很难查。
//
// 所以约定：AgentService 是事件通道的唯一持有者，收到之后一律发到 DataHub 上。
// 订阅者用 DataHub 订阅，可以任意多、任意退订，互不影响。
//
// ── 与 Fincept 的对应关系
//
// Fincept 的 AgentService 自建 QProcess（因为要往 stdin 写大 payload 并逐行
// 解析 THINKING:/TOKEN:/DONE: 前缀），代价是它绕开了 PythonRunner 的健康检查，
// 于是没有看门狗。本项目不用这套：RpcClient 已经把"请求-响应关联 + 超时 +
// 意外断开回调"做完了，事件走的是**帧协议里的结构化事件**而不是前缀行，
// 所以不需要第二套进程管理。
#pragma once

#include <atomic>
#include <chrono>
#include <cstdint>
#include <functional>
#include <memory>
#include <mutex>
#include <string>
#include <vector>

#include "bridge/PyEngine.h"
#include "core/DataHub.h"
#include "core/Json.h"
#include "model/Types.h"

namespace fp {

/// 智能体调用失败。刻意与 RpcError 分开：RpcError 的 code 是**传输层**的
/// 分类（超时/断开/协议），这里是**语义层**的（角色不存在/数据不够/quorum 不足）。
class AgentError : public std::runtime_error {
public:
    AgentError(std::string msg, std::string code = "AgentError")
        : std::runtime_error(std::move(msg)), code_(std::move(code)) {}

    /// 与 Python 侧 rpc.RpcError.code 同名：NotFound / BadParams / BadData / ...
    const std::string& code() const noexcept { return code_; }

private:
    std::string code_;
};

/// 一次研判的运行参数。默认值就是"照配置来"。
///
/// **为什么定义在命名空间作用域而不是嵌套在类里**：GCC 不允许把
/// ``= Options{}`` 这类默认实参指向"当前正在定义的类"的嵌套类型 ——
/// 因为那个类型的默认成员初始化器在类的右大括号之前还不算完整
/// （报错原文：default member initializer required before the end of its
/// enclosing class）。放到命名空间作用域就绕开了，而且这几组参数结构
/// 本来就不依赖 AgentService 的内部状态。
struct AgentOptions {
    std::string panel;             ///< 空 = 用默认投委会
    std::string provider;          ///< 空 = 用角色配置里的 provider

    // ── 推理后端的显式接入参数 ──────────────────────────────
    //
    // 有这三个，接入大模型就不必改 JSON、也不必事先导出环境变量，命令行
    // 直接给：--provider deepseek --api-key <密钥> --model deepseek-chat。
    //
    // 它们只作用于**本次运行**，不写回任何配置文件 —— 密钥写进被提交、
    // 被截图、被拷来拷去的 JSON 里是件很糟的事。
    std::string model;             ///< 空 = 用角色配置里的 model_id
    std::string base_url;          ///< 空 = 用该 provider 的官方端点
    std::string api_key;           ///< 空 = 回落到对应环境变量

    int         rounds{0};         ///< 0 = 用 panel 配置的轮数
    std::string method{"ar"};      ///< 喂给角色的预测方法
    int         horizon{5};
    int         folds{5};
    int         min_train{60};
    double      risk_free{0.0};
    bool        include_text{true};
    /// 终端工具桥端点。空 = Python 侧用 NullToolClient（如实报"工具不可用"）。
    std::string bridge_endpoint;
    std::string bridge_token;
};

class AgentService {
public:
    /// 类内别名，让调用点仍然可以写成 ``AgentService::Options``。
    using Options = AgentOptions;

    /// 角色定义（GUI 用来列出可选的研判角色）。
    struct RoleInfo {
        std::string              id;
        std::string              name;
        std::string              description;
        std::string              category;
        std::string              provider;
        std::string              model_id;
        std::vector<std::string> tools;
        std::vector<std::string> output_sections;
        /// 允许承载**本角色自己**方向标签的段落（默认只有结论段）。
        std::vector<std::string> direction_sections;
        bool                     memory{false};
        bool                     reasoning{false};
        int                      max_tool_calls{6};
    };

    /// 投委会成员。
    struct PanelMember {
        std::string role;
        double      weight{1.0};
        bool        cross_examine{true};
    };

    /// 投委会定义。
    struct PanelInfo {
        std::string              id;
        std::string              name;
        std::string              description;
        std::string              chair;
        std::vector<PanelMember> members;
        int                      quorum{2};
        int                      rounds{2};
        bool                     valid{true};
        std::vector<std::string> problems;   ///< 配置体检的告警/错误（人类可读）
    };

    /// 一个委员在某一轮的结论。
    struct Verdict {
        std::string role;
        std::string role_name;
        std::string direction;      ///< 空表示**弃权**（未给出方向），不是 NEUTRAL
        std::string confidence;
        double      weight{1.0};
        int         round{1};
        bool        ok{false};
        std::string text;           ///< 报告正文（include_text=false 时为空）

        /// 这个角色**实际**用的推理后端（引擎回报的真值）。
        ///
        /// 单列出来的理由：配置里写的是什么，与实际生效的是什么，是两件事
        /// —— 缺密钥就会降级。不把"实际用了什么、降级了没有"摆出来，
        /// 用户永远无法确认自己填的密钥到底有没有被用上。
        std::string provider;
        std::string fallback_reason;   ///< 非空 = 发生了降级，这里是原因
        long long   prompt_tokens{0};
        long long   completion_tokens{0};

        long long total_tokens() const { return prompt_tokens + completion_tokens; }
        /// 是否真的跑在外部模型上（而不是内置的规则后端）。
        bool used_external() const { return !provider.empty() && provider != "rule_based"; }
        bool has_direction() const { return !direction.empty(); }
    };

    /// 一轮研判。
    struct RoundInfo {
        int                   index{1};
        std::vector<Verdict>  verdicts;
        std::vector<std::string> changed;   ///< 相对上一轮改了方向/置信度的角色
        double                duration_ms{0.0};
    };

    /// 一场投委会的结论。
    struct Outcome {
        bool                     valid{false};
        std::string              direction;      ///< 最终方向，空表示未出决议
        std::string              confidence;
        std::string              run_id;
        std::string              panel_id;
        std::string              panel_name;
        double                   duration_ms{0.0};
        /// 这场会在**议什么**（本次运行参数：标的 / 样本区间 / 预测设置）。
        ///
        /// 由引擎的 describe_intent 生成，与喂给模型的提示词同源。不参与
        /// 任何计算，只负责回答"它被问的是什么" —— 报告头部此前没有这一项，
        /// 读者看不到结论是在什么口径下得出的，也就无从复核。
        std::string              intent;
        std::vector<RoundInfo>   rounds;
        /// 会议过程中的**告警与故障**（配置体检、quorum 不足、沿用上一轮、
        /// 各角色护栏）。**不是**会议议题 —— 议题在 intent 里。
        std::vector<std::string> problems;
        Verdict                  chair;          ///< 主席的完整结论
        /// 各角色最终方向。空字符串 = 弃权。**不是** NEUTRAL。
        std::vector<std::pair<std::string, std::string>> final_directions;
    };

    /// 运行完成的回调。err 为 nullptr 表示成功。
    using RunCallback = std::function<void(const Outcome&, const AgentError*)>;

    /// ``hub`` 为 nullptr 时用进程级单例（DataHub::instance()）。
    /// 允许注入局部总线是为了单测能隔离断言 —— 否则测试之间会通过
    /// 全局总线互相干扰，而那种失败是"随机顺序下偶尔红"。
    explicit AgentService(PyEngine& engine, DataHub* hub = nullptr);
    ~AgentService();

    AgentService(const AgentService&)            = delete;
    AgentService& operator=(const AgentService&) = delete;

    // ── 配置 ────────────────────────────────────────────────

    /// 列出全部角色。首次调用会拉一次并缓存；reload() 才重新拉。
    const std::vector<RoleInfo>& roles();
    /// 取单个角色的完整定义（含 instructions 原文，字段放在 Json 里返回）。
    Json role_detail(const std::string& role_id);

    const std::vector<PanelInfo>& panels();
    /// 重新从引擎加载角色/投委会配置（改完 JSON 后调用，不必重启引擎）。
    void reload();

    /// 终端工具桥的状态。**跑研判前先调这个**：桥不通时角色会如实写
    /// "该工具不可用"，与其让用户困惑于报告里的这句话，不如先把状态显示出来。
    Json bridge_status(const std::string& endpoint = {}, const std::string& token = {});

    // ── 运行 ────────────────────────────────────────────────

    /// 单角色研判（同步）。失败抛 AgentError。
    Outcome run_role(const std::string& role_id, const CandleSeries& series,
                     const Options& opt = AgentOptions{});

    /// 投委会（同步）。**这是主入口**：独立研判 → 交叉质证 → 主席综合。
    Outcome debate(const CandleSeries& series, const Options& opt = AgentOptions{});

    /// 投委会（异步）。事件照常推总线，结果通过 cb 回调。
    /// 返回请求 id（0 表示引擎未运行，cb 已被就地以错误调用过）。
    std::uint64_t debate_async(const CandleSeries& series, const Options& opt,
                               RunCallback cb);

    /// 多角色并行独立研判（没有主席）。
    Outcome team(const std::vector<std::string>& role_ids, const CandleSeries& series,
                 const Options& opt = AgentOptions{});

    // ── 回查 ────────────────────────────────────────────────

    /// 一次运行的进度事件流。include_result 为 true 时连完整报告一起返回。
    Json trace(const std::string& run_id, bool include_events = true,
               bool include_result = false);
    /// 最近若干次运行。
    Json recent_runs(int limit = 10);
    /// 历次决议记忆（看同一标的的方向稳不稳定）。
    Json memory(const std::string& symbol = {}, int limit = 10);
    /// 各角色在最近若干次决议里的方向一致性。
    Json consistency(const std::string& symbol,
                     const std::vector<std::string>& roles = {});

    /// 事件统计（推送数、丢弃数）。界面说"没收到事件"时先看这个。
    Json stream_stats();

    // ── 事件 ────────────────────────────────────────────────

    /// 进度事件的主题前缀。订阅 "agent.stream.**" 收全部，
    /// 订阅 "agent.stream." + run_id 只收某一次。
    static constexpr const char* kTopicPrefix = "agent.stream.";

    /// 最近一次事件的主题（诊断用）。
    std::string last_event_topic() const;

    /// 已转发到总线的事件数。
    std::uint64_t events_forwarded() const noexcept { return sink_->forwarded.load(); }

private:
    /// 事件处理器真正需要碰的那点状态，**独立于 AgentService 本身**。
    ///
    /// 单独拎出来的原因是一个真实的内存安全问题：处理器注册在 PyEngine 上，
    /// 而引擎的生命周期由调用方掌握 —— 引擎完全可能比 AgentService 活得久。
    /// 早先的写法让 lambda 直接捕获 ``this``，于是"AgentService 析构 →
    /// 引擎又推来一条事件"就是一次 use-after-free。原来那里的注释写的是
    /// "留一个失效的 handler 是安全的（它只是往总线发消息）"—— 这个理由
    /// 是错的：处理器要读 ``seq_``、要写 ``forwarded_``、要拿 ``hub_``，
    /// 桩桩件件都要经过 ``this``。
    ///
    /// 改法：状态装进 shared_ptr，处理器只捕获 **weak_ptr**。AgentService
    /// 析构 → weak 失效 → 残留的处理器变成一个什么都不做的空操作。
    /// 既不拆处理器（那要处理"读线程正在调用中"的竞态），也不会再碰
    /// 任何已释放的内存。
    struct Sink {
        DataHub*                   hub{nullptr};
        std::atomic<std::uint64_t> seq{0};
        std::atomic<std::uint64_t> forwarded{0};
        mutable std::mutex         last_topic_mu;
        std::string                last_topic;
    };

    void install_event_handler();
    Json call(const std::string& method, Json params, int timeout_ms = -1);

    static RoleInfo    parse_role(const Json& j);
    static PanelInfo   parse_panel(const Json& j);
    static Outcome     parse_outcome(const Json& j);
    static Verdict     parse_verdict(const std::string& role_id, const Json& j);
    static Json        bars_param(const CandleSeries& series);
    Json               base_params(const CandleSeries& series, const Options& opt) const;

    PyEngine&  engine_;
    /// 不是 nullptr：构造时若传入 nullptr 会落到 DataHub::instance()。
    DataHub*   hub_;        ///< 允许注入局部总线是为了单测隔离

    mutable std::mutex mu_;          ///< 保护下面的缓存
    std::vector<RoleInfo>  roles_;
    std::vector<PanelInfo> panels_;
    bool                   roles_loaded_{false};
    bool                   panels_loaded_{false};

    /// 事件状态。**必须在引擎之前构造、在引擎之后析构不了** —— 用
    /// shared_ptr 持有正是为了让"引擎活着、服务死了"这个状态是安全的。
    const std::shared_ptr<Sink> sink_;
};

}  // namespace fp
