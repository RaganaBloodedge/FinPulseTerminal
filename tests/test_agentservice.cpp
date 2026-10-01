// FinPulse Terminal — 智能体服务集成测试
//
// 这组测试**真的会拉起一个 Python 子进程**，所以它比其余测试慢（首次握手
// 一两秒），但它验证的是别处验证不了的东西：
//
//   1. C++ → Python 的调用契约（方法名、参数名、返回结构）真的对得上。
//      单侧的单元测试只能证明"我这一侧自洽"，两边字段名不一致照样全绿。
//   2. **反向工具通道真的通了**：Python 在推理过程中回调 C++ 起的那几个
//      HTTP 工具，而 C++ 侧的总线统计里能看到这些调用。
//      这是本项目"C++/Python 联合能力"的核心主张，必须是实测出来的，
//      不能是文档里写的。
//   3. 进度事件真的从 Python 一路流到 DataHub 上。
//
// 找不到 Python 解释器时**跳过**（打印醒目提示），而不是失败：这个项目的
// 构建不要求目标机器装 Python（C++ 核心库可以独立编译）。但解释器在、
// 引擎却起不来，那是真故障，必须红。设 FINPULSE_REQUIRE_ENGINE=1 可以把
// 跳过变成失败，CI 上用这个。

#include "TestFramework.h"

#include "agent/AgentService.h"
#include "agent/ToolBridge.h"
#include "bridge/PyEngine.h"
#include "core/DataHub.h"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdlib>
#include <iostream>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

using namespace fp;

namespace {

/// 整个文件共用一个引擎 —— 握手要一两秒，每个用例起一次没有意义。
struct Fixture {
    DataHub     hub;      ///< 局部总线：不与进程级单例互相干扰
    PyEngine    engine;
    ToolBridge  bridge;
    bool        started{false};
    bool        skipped{false};
    std::string skip_reason;

    /// 事件计数：由 DataHub 订阅累加。
    std::atomic<int> stream_events{0};
    std::atomic<int> round_done_events{0};
    std::atomic<int> run_done_events{0};
    /// 事件回调跑在 RpcClient 的读线程里，字符串要在锁下存取。
    mutable std::mutex event_mu;
    std::string        last_event_run_id;
    std::string        last_round_directions_dump;

    static PyEngine::Config engine_config() {
        PyEngine::Config cfg;
        cfg.python_root          = FINPULSE_PYTHON_ROOT;
        cfg.handshake_timeout_ms = 20000;
        cfg.call_timeout_ms      = 60000;   // 投委会两轮 + 主席，留足余量
        return cfg;
    }

    static ToolBridgeConfig bridge_config() {
        ToolBridgeConfig c;
        c.port           = 0;       // 内核挑端口，测试之间不会撞
        c.token_max_uses = 4096;    // 一场投委会会调很多次
        return c;
    }

    // PyEngine 不可拷贝也不可移动（持有子进程与读线程），所以在初始化列表里
    // 直接构造，不能先默认构造再赋值。
    Fixture() : engine(engine_config()), bridge(&hub, bridge_config()) {}

    /// 惰性启动。返回 false 表示"环境不具备"，调用方应跳过。
    bool ensure() {
        if (started) return true;
        if (skipped) return false;

        try {
            engine.start();
        } catch (const std::exception& e) {
            skipped     = true;
            skip_reason = e.what();
            std::cout << "\n  [跳过] 无法启动 Python 引擎：" << skip_reason << "\n";
            std::cout << "  [跳过] 这一组是 C++/Python 联合能力的集成测试，"
                         "缺少解释器时无法验证。\n";
            if (std::getenv("FINPULSE_REQUIRE_ENGINE")) {
                std::cout << "  [跳过] 但设置了 FINPULSE_REQUIRE_ENGINE=1，"
                             "按失败处理。\n";
            }
            return false;
        }

        // 工具桥：注入终端状态，然后起来。
        bridge.register_default_tools();
        bridge.set_quote_provider([](const std::string& symbol) {
            Json q = Json::object();
            q.set("symbol", symbol.empty() ? "SYNTH-A" : symbol);
            q.set("last", 123.5);
            q.set("change_pct", 1.25);
            q.set("source", "C++ 终端内存");
            return q;
        });
        bridge.set_series_provider([](const std::string& symbol) {
            Json s = Json::object();
            s.set("symbol", symbol.empty() ? "SYNTH-A" : symbol);
            s.set("bars", 260);
            s.set("issues", Json::array());
            return s;
        });
        bridge.start();

        // 订阅全部智能体事件。用局部 hub，断言不会受其它测试影响。
        hub.subscribe(
            "agent.stream.**",
            [this](const Topic&, const Json& payload) {
                ++stream_events;
                const std::string ev = payload["event"].as_string_or("");
                const std::string rid = payload["run_id"].as_string_or("");
                std::lock_guard<std::mutex> lk(event_mu);
                if (ev == "agent.round.done") {
                    ++round_done_events;
                    last_round_directions_dump = payload["data"]["directions"].dump();
                }
                if (ev == "agent.run.done") ++run_done_events;
                if (!rid.empty()) last_event_run_id = rid;
            },
            "集成测试");

        started = true;
        return true;
    }

    /// 把桥的信息塞进运行参数，让角色能回调 C++。
    AgentOptions options(bool use_bridge = true) const {
        AgentOptions o;
        o.include_text = true;
        if (use_bridge) {
            o.bridge_endpoint = bridge.endpoint();
            o.bridge_token    = bridge.token();
        }
        return o;
    }
};

Fixture& fixture() {
    static Fixture f;
    return f;
}

/// 需要引擎的用例统一用它开头。返回 false 就让用例直接返回。
bool require_engine() {
    Fixture& f = fixture();
    if (f.ensure()) return true;
    if (std::getenv("FINPULSE_REQUIRE_ENGINE")) {
        FP_CHECK_MSG(false, "FINPULSE_REQUIRE_ENGINE=1 但引擎未能启动: " + f.skip_reason);
    }
    return false;
}

/// 确定性的合成行情。不用随机数：失败时能一模一样地复现。
CandleSeries synth_series(std::size_t n = 260, const std::string& symbol = "SYNTH-A") {
    CandleSeries s(symbol);
    for (std::size_t i = 0; i < n; ++i) {
        const double d    = static_cast<double>(i);
        const double wave = 0.02 * std::sin(d / 11.0) + 0.008 * std::cos(d / 3.7);
        const double c    = 100.0 * (1.0 + 0.0004 * d + wave);

        Candle bar;
        bar.ts_ms  = 1'700'000'000'000LL + static_cast<Timestamp>(i) * 86'400'000LL;
        bar.open   = c * 0.999;
        bar.high   = c * 1.006;
        bar.low    = c * 0.994;
        bar.close  = c;
        bar.volume = static_cast<std::int64_t>(1'500'000.0 * (1.0 + 10.0 * std::fabs(wave)));
        s.push(bar);
    }
    return s;
}

bool is_direction(const std::string& d) {
    return d == "BULLISH" || d == "BEARISH" || d == "NEUTRAL";
}

/// 把序列转成引擎要的 bars 数组。刻意手写而不是调 AgentService 的私有
/// 辅助 —— 下面那个用例要**绕过 C++ 门面**直接发 RPC，用它自己那条路。
Json bars_json(const CandleSeries& s) {
    Json arr = Json::array();
    for (const Candle& c : s.bars()) arr.push(c.to_json());
    return arr;
}

}  // namespace

// ── 配置层 ────────────────────────────────────────────────────────

FP_TEST(agentservice, "角色与投委会配置能从引擎拉回来") {
    if (!require_engine()) return;
    AgentService svc(fixture().engine, &fixture().hub);

    const auto& roles = svc.roles();
    FP_CHECK(roles.size() >= 4u);

    bool saw_chair = false;
    for (const AgentService::RoleInfo& r : roles) {
        FP_CHECK(!r.id.empty());
        FP_CHECK(!r.name.empty());
        FP_CHECK(!r.category.empty());
        FP_CHECK(!r.output_sections.empty());
        // **每个角色的方向来源都必须是它自己声明的段落**。这是那条
        // "引述别人的方向被当成自己的立场"缺陷的最终防线 —— 上一版
        // 风险官的方向来源被解析成了全部段落。
        FP_CHECK(!r.direction_sections.empty());
        for (const std::string& s : r.direction_sections) {
            FP_CHECK(std::find(r.output_sections.begin(), r.output_sections.end(), s) !=
                     r.output_sections.end());
        }
        if (r.id == "committee_chair") saw_chair = true;
    }
    FP_CHECK(saw_chair);

    const auto& panels = svc.panels();
    FP_CHECK(panels.size() >= 1u);
    for (const AgentService::PanelInfo& p : panels) {
        FP_CHECK(p.valid);
        FP_CHECK(!p.chair.empty());
        FP_CHECK(p.members.size() >= 2u);
        FP_CHECK(p.quorum >= 1);
        // 主席不能同时是委员 —— 那会让它给自己的结论计票。
        for (const AgentService::PanelMember& m : p.members) {
            FP_CHECK(m.role != p.chair);
        }
    }
}

FP_TEST(agentservice, "单个角色的完整定义可以取回") {
    if (!require_engine()) return;
    AgentService svc(fixture().engine, &fixture().hub);
    svc.roles();   // 确保加载过

    const Json detail = svc.role_detail("technical_analyst");
    FP_CHECK_EQ(detail["id"].as_string_or(""), std::string("technical_analyst"));
    FP_CHECK(detail["instructions"].as_string_or("").size() > 80);
    FP_CHECK(detail["tools_detail"].is_array());
    FP_CHECK(detail["tools_detail"].size() > 0);

    // 未知角色要给出可用列表，而不是一句"调用失败"。
    bool threw = false;
    try {
        svc.role_detail("no_such_role");
    } catch (const AgentError& e) {
        threw = true;
        FP_CHECK_EQ(e.code(), std::string("NotFound"));
        FP_CHECK(e.what() != nullptr);
    }
    FP_CHECK(threw);
}

// ── 单角色研判 ────────────────────────────────────────────────────

FP_TEST(agentservice, "单角色研判产出完整报告") {
    if (!require_engine()) return;
    AgentService svc(fixture().engine, &fixture().hub);

    AgentService::Options opt;
    opt.include_text = true;
    const auto out = svc.run_role("technical_analyst", synth_series(), opt);

    FP_CHECK(out.valid);
    FP_CHECK(out.run_id.size() == 12u);       // 引擎侧 uuid4().hex[:12]
    FP_CHECK(!out.chair.text.empty());
    FP_CHECK(is_direction(out.direction));
    FP_CHECK(!out.confidence.empty());
    FP_CHECK_EQ(out.rounds.size(), 1u);
    FP_CHECK_EQ(out.rounds[0].verdicts.size(), 1u);
    FP_CHECK(out.rounds[0].verdicts[0].ok);
    FP_CHECK(out.rounds[0].verdicts[0].role == "technical_analyst");

    // 单角色路径也不能漏掉议题：它走的是 agent.run（返回一个 RoleRun，
    // 没有 DebateResult 可挂），议题由门面层补上 —— 正是最容易漏的一处。
    FP_CHECK(!out.intent.empty());
    FP_CHECK(out.intent.find("SYNTH-A") != std::string::npos);

    // 报告里必须出现它声明的每个段落 —— 段落名是给程序用的，缺一个
    // 下游的抽取就少一块。
    for (const AgentService::RoleInfo& r : svc.roles()) {
        if (r.id != "technical_analyst") continue;
        for (const std::string& sec : r.output_sections) {
            FP_CHECK(out.chair.text.find(sec) != std::string::npos);
        }
    }

    // 轨迹能回查。
    const Json trace = svc.trace(out.run_id, true, false);
    FP_CHECK_EQ(trace["kind"].as_string_or(""), std::string("role"));
    FP_CHECK(trace["event_count"].as_int_or(0) > 0);
}

// ── 投委会 ────────────────────────────────────────────────────────

FP_TEST(agentservice, "投委会两轮研判与主席决议") {
    if (!require_engine()) return;
    AgentService svc(fixture().engine, &fixture().hub);

    const auto out = svc.debate(synth_series(), fixture().options(false));

    FP_CHECK(out.valid);
    FP_CHECK(is_direction(out.direction));
    FP_CHECK(!out.confidence.empty());
    FP_CHECK_EQ(out.rounds.size(), 2u);
    FP_CHECK_EQ(out.panel_id, std::string("default_committee"));
    FP_CHECK(!out.panel_name.empty());

    // 第一轮应当三个委员都跑；第二轮至少跑过。
    FP_CHECK_EQ(out.rounds[0].verdicts.size(), 3u);
    FP_CHECK(out.rounds[1].verdicts.size() >= 2u);

    // 每个委员都要给出报告正文（include_text=true）。
    for (const auto& rnd : out.rounds) {
        for (const AgentService::Verdict& v : rnd.verdicts) {
            FP_CHECK(v.ok);
            FP_CHECK(!v.text.empty());
            FP_CHECK(!v.role_name.empty());
        }
    }

    // 主席的报告必须写全声明的段落。
    FP_CHECK(!out.chair.text.empty());
    for (const AgentService::RoleInfo& r : svc.roles()) {
        if (r.id != "committee_chair") continue;
        for (const std::string& sec : r.output_sections) {
            FP_CHECK(out.chair.text.find(sec) != std::string::npos);
        }
    }

    // 最终方向表来自最后一轮，且人数与委员数一致（这正是那条
    // "3 位分析师中 1 看多、0 看空、1 中性"自相矛盾的防线）。
    FP_CHECK_EQ(out.final_directions.size(), out.rounds.back().verdicts.size());
    std::size_t with_dir = 0;
    for (const auto& [role, dir] : out.final_directions) {
        FP_CHECK(!role.empty());
        if (!dir.empty()) {
            FP_CHECK(is_direction(dir));
            ++with_dir;
        }
    }
    FP_CHECK(with_dir >= 1u);

    // 议题（本次运行参数）必须原样带回来。报告头部要能回答"这场会在议
    // 什么" —— 少了这一项，一份再正确的报告也无法被复核：读者不知道
    // 结论是在什么样本口径下得出的。断言具体值而不是"非空"，是因为
    // 只查非空的话，引擎漏了标的、只回一句"样本 260 根"照样能绿。
    FP_CHECK(!out.intent.empty());
    FP_CHECK(out.intent.find("SYNTH-A") != std::string::npos);
    FP_CHECK(out.intent.find("260") != std::string::npos);
}

FP_TEST(agentservice, "弃权不落成 NEUTRAL") {
    if (!require_engine()) return;
    AgentService svc(fixture().engine, &fixture().hub);
    const auto out = svc.debate(synth_series(), fixture().options(false));

    // 风险官按定义不产出方向。它必须表现为**空字符串（弃权）**，
    // 绝不能回落成 NEUTRAL —— 那会把"没表态"变成"表态了中性"，
    // 主席的计票分母当场就错了。
    bool saw_risk_officer = false;
    for (const auto& [role, dir] : out.final_directions) {
        if (role != "risk_officer") continue;
        saw_risk_officer = true;
        FP_CHECK(dir.empty());
    }
    FP_CHECK(saw_risk_officer);
}

FP_TEST(agentservice, "轮次事件与最终结论对得上") {
    if (!require_engine()) return;
    Fixture&   f = fixture();
    AgentService svc(f.engine, &f.hub);

    const int before_rounds = f.round_done_events.load();
    const int before_stream = f.stream_events.load();
    const int before_done   = f.run_done_events.load();

    const auto out = svc.debate(synth_series(), f.options(false));
    FP_CHECK(out.valid);

    // 事件是真异步推过来的，给它一点时间。不用 sleep 死等：这里只断言
    // "至少发生了"，不要求精确时序。
    for (int i = 0; i < 200 && f.round_done_events.load() < before_rounds + 2; ++i) {
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }

    FP_CHECK(f.stream_events.load() > before_stream);
    FP_CHECK_EQ(f.round_done_events.load(), before_rounds + 2);
    FP_CHECK_EQ(f.run_done_events.load(), before_done + 1);
    {
        std::lock_guard<std::mutex> lk(f.event_mu);
        FP_CHECK_EQ(f.last_event_run_id, out.run_id);
        // 轮次事件里带着方向表 —— 界面画"谁在第几轮说了什么"全靠它。
        FP_CHECK(f.last_round_directions_dump.find("risk_officer") !=
                 std::string::npos);
    }
}

// ── 反向工具通道 ──────────────────────────────────────────────────

FP_TEST(agentservice, "Python 真的回调了 C++ 的工具桥") {
    if (!require_engine()) return;
    Fixture&   f = fixture();
    AgentService svc(f.engine, &f.hub);

    // 先确认桥是活的，并且引擎侧也认为它可用 —— 后者才是关键：
    // 只有 Python 侧的 bridge.status 说 available，工具调用才会走真桥
    // 而不是 NullToolClient。
    const Json status = svc.bridge_status(f.bridge.endpoint(), f.bridge.token());
    FP_CHECK(status["available"].as_bool_or(false));
    FP_CHECK_EQ(status["tool_count"].as_int_or(0), 4);

    const auto before = f.bridge.stats().tool_calls;

    // flow_analyst 的配置里声明了 terminal.bus_stats，所以这一场
    // 投委会必定会去调 C++ 侧。
    const auto out = svc.debate(synth_series(), f.options(true));
    FP_CHECK(out.valid);

    const auto after = f.bridge.stats();
    FP_CHECK(after.tool_calls > before);
    FP_CHECK_EQ(after.rejected_auth, 0u);      // 令牌得是对的
    FP_CHECK_EQ(after.rejected_host, 0u);
    FP_CHECK_EQ(after.unknown_tools, 0u);

    // 报告里应当出现终端侧提供的数据痕迹。flow_analyst 的报告会引用
    // 总线统计里的数字；这里不硬编码字段名，只查"有没有用到"。
    bool mentioned = false;
    for (const auto& rnd : out.rounds) {
        for (const AgentService::Verdict& v : rnd.verdicts) {
            if (v.text.find("bus_stats") != std::string::npos ||
                v.text.find("终端") != std::string::npos ||
                v.text.find("published") != std::string::npos) {
                mentioned = true;
            }
        }
    }
    FP_CHECK_MSG(mentioned,
                 "报告里没有任何终端侧数据的痕迹 —— 反向工具通道可能没生效。"
                 "工具调用数: " + std::to_string(after.tool_calls));
}

FP_TEST(agentservice, "没有工具桥时如实报不可用") {
    if (!require_engine()) return;
    AgentService svc(fixture().engine, &fixture().hub);

    // 不给 endpoint：Python 侧会退化成 NullToolClient，每个远程工具
    // 都返回 {"available": false, "reason": ...}，报告里会如实写出来，
    // 而不是抛异常或假装查过了。
    const Json status = svc.bridge_status();
    FP_CHECK(!status["available"].as_bool_or(true));
    FP_CHECK(status["reason"].as_string_or("").size() > 0);

    const auto out = svc.debate(synth_series(), fixture().options(false));
    // 关键：**依然要能出决议**。终端工具是锦上添花，不是前提。
    FP_CHECK(out.valid);
    FP_CHECK(is_direction(out.direction));
}

// ── 参数校验与错误翻译 ────────────────────────────────────────────

FP_TEST(agentservice, "样本不足时报错说清原因") {
    if (!require_engine()) return;
    AgentService svc(fixture().engine, &fixture().hub);

    bool threw = false;
    try {
        svc.debate(synth_series(30), fixture().options(false));
    } catch (const AgentError& e) {
        threw = true;
        // 引擎侧抛的是 BadData；C++ 侧必须把它翻译出来，而不是笼统的
        // "调用失败" —— 调用方要据此判断该补数据还是该改代码。
        FP_CHECK_EQ(e.code(), std::string("BadData"));
        FP_CHECK(std::string(e.what()).find("60") != std::string::npos);
    }
    FP_CHECK(threw);
}

FP_TEST(agentservice, "未知投委会是参数错误") {
    if (!require_engine()) return;
    AgentService svc(fixture().engine, &fixture().hub);

    AgentService::Options opt = fixture().options(false);
    opt.panel = "no_such_panel";

    bool threw = false;
    try {
        svc.debate(synth_series(), opt);
    } catch (const AgentError& e) {
        threw = true;
        FP_CHECK_EQ(e.code(), std::string("BadParams"));
    }
    FP_CHECK(threw);
}

FP_TEST(agentservice, "未知角色的错误码是 NotFound") {
    if (!require_engine()) return;
    AgentService svc(fixture().engine, &fixture().hub);
    svc.roles();

    bool threw = false;
    try {
        svc.run_role("no_such_role", synth_series());
    } catch (const AgentError& e) {
        threw = true;
        FP_CHECK_EQ(e.code(), std::string("NotFound"));
    }
    FP_CHECK(threw);
}

// ── 回查 ──────────────────────────────────────────────────────────

FP_TEST(agentservice, "运行记录与决策记忆可回查") {
    if (!require_engine()) return;
    AgentService svc(fixture().engine, &fixture().hub);

    const auto out = svc.debate(synth_series(260, "SYNTH-MEM"), fixture().options(false));
    FP_CHECK(out.valid);

    const Json runs = svc.recent_runs(5);
    FP_CHECK(runs["runs"].is_array());
    FP_CHECK(runs["runs"].size() >= 1);

    const Json mem = svc.memory("SYNTH-MEM", 5);
    FP_CHECK(mem["decisions"].is_array());
    FP_CHECK(mem["count"].as_int_or(0) >= 1);

    const Json cons = svc.consistency("SYNTH-MEM", {"committee_chair"});
    FP_CHECK(cons["reports"].is_array());
    FP_CHECK_EQ(cons["reports"].size(), 1u);

    // 不存在的 run_id 要报 NotFound 而不是返回空对象。
    bool threw = false;
    try {
        svc.trace("000000000000");
    } catch (const AgentError& e) {
        threw = true;
        FP_CHECK_EQ(e.code(), std::string("NotFound"));
    }
    FP_CHECK(threw);

    const Json stats = svc.stream_stats();
    FP_CHECK(stats["emitted"].as_int_or(0) > 0);
    FP_CHECK(svc.events_forwarded() > 0);
}

// ── 生命周期安全 ──────────────────────────────────────────────────

FP_TEST(agentservice, "服务已析构时残留的事件处理器不会碰已释放内存") {
    if (!require_engine()) return;
    Fixture& f = fixture();

    // 场景：引擎比 AgentService 活得久。
    //
    //   1. 建一个服务 → 它在引擎上装了一个事件处理器；
    //   2. 立刻销毁它 → 处理器**仍然留在引擎上**。这是有意的：拆掉它就要
    //      处理"读线程正在回调中"的竞态，而那种竞态比留着更难搞；
    //   3. 绕过 C++ 门面、直接用 RPC 触发一次智能体运行 —— Python 于是会
    //      往这条已经"没有主人"的处理器上推事件。
    //
    // 早先的实现让处理器直接捕获 this，第 3 步就是一次 use-after-free
    // （读的是已释放的 seq_ / hub_，写的是已释放的 forwarded_）。
    // 现在处理器只捕获 weak_ptr：第 3 步**必须什么都不发生**，也不能崩。
    // 这条用例就是那个修复的看门人。
    {
        AgentService doomed(f.engine, &f.hub);
        doomed.roles();   // 确保处理器已经装上
    }                     // ← 析构

    // 先给在途事件一点时间落地，免得把上一个用例的尾巴算进来 ——
    // 那样这条断言会变成"偶尔红"，而偶尔红的测试等于没有测试。
    std::this_thread::sleep_for(std::chrono::milliseconds(200));
    const int before = f.stream_events.load();

    Json params = Json::object();
    params.set("bars", bars_json(synth_series(260, "SYNTH-DOOM")));
    params.set("symbol", std::string("SYNTH-DOOM"));
    params.set("method", std::string("ar"));
    params.set("horizon", 5);
    params.set("folds", 5);
    params.set("min_train", 60);
    params.set("include_text", false);

    // 没有任何 AgentService 活着，事件无人接管。
    const Json res = f.engine.rpc().call("agent.debate", std::move(params), 60000);
    FP_CHECK(res["run_id"].as_string_or("").size() > 0);   // 运行本身是成功的

    std::this_thread::sleep_for(std::chrono::milliseconds(400));

    // 关键断言：那批事件被安全丢弃，一条都没到总线上。
    // （而不是"崩了"或"把野数据发到总线上"。）
    FP_CHECK_MSG(f.stream_events.load() == before,
                 "服务已析构，事件却仍被转发到总线上 —— 处理器很可能还在用"
                 "一个悬空的对象。收到 " +
                     std::to_string(f.stream_events.load() - before) + " 条。");
}

FP_TEST(agentservice, "异步投委会通过回调返回结果") {
    if (!require_engine()) return;
    Fixture&   f = fixture();
    AgentService svc(f.engine, &f.hub);

    std::atomic<bool> done{false};
    std::atomic<bool> got_error{false};
    AgentService::Outcome result;
    std::string err_text;

    const std::uint64_t id = svc.debate_async(
        synth_series(), f.options(false),
        [&](const AgentService::Outcome& out, const AgentError* err) {
            if (err) {
                got_error.store(true);
                err_text = err->what();
            } else {
                result = out;
            }
            done.store(true);
        });
    FP_CHECK(id > 0);

    // 回调是引擎读线程里跑的，这里等它。
    for (int i = 0; i < 3000 && !done.load(); ++i) {
        std::this_thread::sleep_for(std::chrono::milliseconds(10));
    }
    FP_CHECK(done.load());
    FP_CHECK_MSG(!got_error.load(), "异步调用失败: " + err_text);
    FP_CHECK(result.valid);
    FP_CHECK(is_direction(result.direction));
    FP_CHECK_EQ(result.rounds.size(), 2u);
}
