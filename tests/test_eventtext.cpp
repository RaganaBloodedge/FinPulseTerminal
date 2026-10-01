// FinPulse Terminal — 智能体事件的展示文案测试
//
// 这组测试存在的理由是一个**已经发生过一次**的静默失效缺陷：
//
//   描述表的键写的是语义名（``role.start``），而线上跑的事件名是
//   ``agent.role.start``。于是每一条分支都不命中，命令行只剩光秃秃的
//   事件名 —— 打印没报错、事件也确实收到了、退出码是 0，唯一的现象是
//   "说明文字不见了"。这种缺陷靠跑一遍程序是发现不了的（输出看起来
//   "就是没写说明"），只能靠把"每个事件都该有说明"写成断言。
//
// 所以这里不但测具体的格式化结果，还测一条元性质：
// **引擎推的每一种事件名都必须能匹配到一条说明**（见最后一个用例）。
// 将来 Python 侧新增了一种事件、忘了在 C++ 侧加文案，就会红在这里，
// 而不是等到有人盯着终端输出发呆。

#include "TestFramework.h"

#include "app/EventText.h"

#include <initializer_list>
#include <string>
#include <utility>
#include <vector>

using namespace fp;

namespace {

/// 造一条事件数据的便利函数。
Json data(std::initializer_list<std::pair<const char*, Json>> kv) {
    Json j = Json::object();
    for (const auto& [k, v] : kv) j.set(k, v);
    return j;
}

/// 剥了前缀再描述，跟生产路径一致。（调用点也是这两步。）
std::string text_of(const std::string& wire_name, const Json& d = Json::object()) {
    return event_text::describe(event_text::strip_prefix(wire_name), d);
}

}  // namespace

// ── 前缀剥离 ──────────────────────────────────────────────────────

FP_TEST(eventtext, "带前缀的事件名能被剥掉") {
    FP_CHECK_EQ(event_text::strip_prefix("agent.role.start"), std::string("role.start"));
    FP_CHECK_EQ(event_text::strip_prefix("agent.run.done"), std::string("run.done"));
    // 只剥一层，不是把所有点都切掉。
    FP_CHECK_EQ(event_text::strip_prefix("agent.a.b.c"), std::string("a.b.c"));
}

FP_TEST(eventtext, "不带前缀的名字原样返回") {
    FP_CHECK_EQ(event_text::strip_prefix("role.start"), std::string("role.start"));
    FP_CHECK_EQ(event_text::strip_prefix(""), std::string(""));
    // 只是恰好含 "agent." 但不是以它开头 —— 不能误伤。
    FP_CHECK_EQ(event_text::strip_prefix("market.agent.x"), std::string("market.agent.x"));
    // 光秃秃的前缀本身：剥完会是空串，没有意义，就当它没前缀。
    FP_CHECK_EQ(event_text::strip_prefix("agent."), std::string("agent."));
}

// ── 逐事件文案 ────────────────────────────────────────────────────

FP_TEST(eventtext, "角色事件优先用中文名") {
    const Json d = data({{"role", Json("risk_officer")}, {"role_name", Json("风险官")}});
    FP_CHECK_EQ(text_of("agent.role.start", d), std::string("风险官 开始研判"));
    FP_CHECK_EQ(text_of("agent.role.done", d), std::string("风险官 完成"));

    // 没有中文名时退到 id，不能退成一个空字符串。
    const Json only_id = data({{"role", Json("risk_officer")}});
    FP_CHECK_EQ(text_of("agent.role.start", only_id), std::string("risk_officer 开始研判"));
}

FP_TEST(eventtext, "投委会开场写清 quorum 与轮数") {
    const Json d = data({{"panel", Json("default_committee")},
                         {"panel_name", Json("标准投委会")},
                         {"quorum", Json(2)},
                         {"rounds", Json(2)}});
    const std::string s = text_of("agent.debate.start", d);
    FP_CHECK(s.find("标准投委会") != std::string::npos);
    FP_CHECK(s.find("quorum=2") != std::string::npos);
    FP_CHECK(s.find("轮数 2") != std::string::npos);
}

FP_TEST(eventtext, "轮次事件区分有人改口与无人改口") {
    // **空数组与字段缺失结论不同，这不是疏漏，是有意的**：
    //   changed = []  引擎**明确断言**"这一轮没人改口"→ 可以写出来；
    //   changed 缺失  引擎**什么都没说** → 只能报"第 N 轮结束"。
    // 把后者也写成"（无人改口）"是凭空替引擎下结论 —— 和"弃权被写成
    // NEUTRAL"是同一类错误，只是方向相反。
    // 这条断言的作用是防止将来有人把两个分支"顺手合并"。
    const Json none   = data({{"round", Json(1)}, {"changed", Json::array()}});
    const Json absent = data({{"round", Json(1)}});
    FP_CHECK_EQ(text_of("agent.round.done", none),
                std::string("第 1 轮结束（无人改口）"));
    FP_CHECK_EQ(text_of("agent.round.done", absent),
                std::string("第 1 轮结束"));
    // 类型不对（比如给了个对象）也走"什么都没说"这条路，不能崩。
    const Json wrong_type = data({{"round", Json(1)}, {"changed", Json::object()}});
    FP_CHECK_EQ(text_of("agent.round.done", wrong_type),
                std::string("第 1 轮结束"));

    Json list = Json::array();
    list.push(Json("risk_officer"));
    list.push(Json("flow_analyst"));
    const Json changed = data({{"round", Json(2)}, {"changed", std::move(list)}});
    const std::string s = text_of("agent.round.done", changed);
    FP_CHECK(s.find("第 2 轮") != std::string::npos);
    FP_CHECK(s.find("改口 2 人") != std::string::npos);
    // 改口名单要能看出是谁 —— 这是"交叉质证起没起作用"的直接证据。
    FP_CHECK(s.find("risk_officer") != std::string::npos);
    FP_CHECK(s.find("flow_analyst") != std::string::npos);
}

FP_TEST(eventtext, "结束事件不会把弃权读成中性") {
    // direction 为 null（= 弃权）时，绝不能渲染成 "NEUTRAL"。
    const Json abstain = data({{"direction", Json(nullptr)}});
    const std::string d1 = text_of("agent.debate.done", abstain);
    FP_CHECK(d1.find("NEUTRAL") == std::string::npos);
    FP_CHECK(d1.find("未形成有效决议") != std::string::npos);

    FP_CHECK(text_of("agent.run.done", abstain).find("NEUTRAL") == std::string::npos);

    // 有方向时如实写出来。
    const Json bull = data({{"direction", Json("BULLISH")}});
    FP_CHECK(text_of("agent.debate.done", bull).find("BULLISH") != std::string::npos);
    FP_CHECK(text_of("agent.run.done", bull).find("BULLISH") != std::string::npos);
}

FP_TEST(eventtext, "认不出的事件返回空串而不是瞎编") {
    // 返回空串时调用方只打事件名。换成"未知事件"之类的占位符会把
    // 噪音混进报告，而且会掩盖"文案忘了加"这件事。
    FP_CHECK_EQ(text_of("agent.some.future.event"), std::string(""));
    FP_CHECK_EQ(text_of("agent."), std::string(""));
}

// ── 元性质：不许再出现"事件收到了但没文案" ────────────────────────

FP_TEST(eventtext, "引擎推的每种事件名都有对应文案") {
    // 这份清单与 python/finpulse_engine/agent/api.py 里 self.sink.emit(...)
    // 以及 orchestrator.py 里 emit_progress(...) 的事件名一一对应。
    // **Python 侧新增事件时这里要跟着加** —— 忘了加就会红在这条，
    // 而不会表现成"终端上某一行莫名其妙没有说明"。
    const std::vector<std::string> wire_names = {
        "agent.run.start",  "agent.run.done",  "agent.run.error",
        "agent.team.start", "agent.team.done",
        "agent.role.start", "agent.role.done",
        "agent.debate.start", "agent.debate.done",
        "agent.chair.start",
        "agent.round.done",
        // 数据域（批量拉取）走单独的前缀，同样必须在文案表里有一席之地。
        "data.pull.start",  "data.pull.symbol", "data.pull.done",
    };

    // 给一份"尽可能填满"的数据：只要文案里读了某个字段就一定能读到，
    // 于是非空结果只可能来自"匹配上了某条分支"。反过来，若某个事件名
    // 没有对应分支，describe 必然返回空串 —— 正是要抓的那个缺陷。
    const Json rich = data({{"role", Json("r")},
                            {"role_name", Json("角色")},
                            {"panel", Json("p")},
                            {"panel_name", Json("投委会")},
                            {"kind", Json("debate")},
                            {"error", Json("boom")},
                            {"direction", Json("BULLISH")},
                            {"round", Json(1)},
                            {"count", Json(3)},
                            {"quorum", Json(2)},
                            {"rounds", Json(2)},
                            {"symbol", Json("600519.SH")},
                            {"source", Json("tushare")},
                            {"bars", Json(250)},
                            {"index", Json(3)},
                            {"total", Json(14)},
                            {"ok", Json(13)},
                            {"failed", Json(1)},
                            {"changed", Json::array()}});

    for (const std::string& wire : wire_names) {
        const std::string s = text_of(wire, rich);
        FP_CHECK_MSG(!s.empty(), "事件 " + wire + " 没有对应的说明文案 —— "
                                 "描述表的键很可能又写成了不带前缀的名字");
    }

    // 反向也查一下：前缀剥错会出现"事件名匹配上了、但键写错"的形态，
    // 所以顺便确认剥离后的名字确实是描述表用的那套。
    FP_CHECK_EQ(event_text::strip_prefix("agent.round.done"), std::string("round.done"));
    FP_CHECK_EQ(event_text::strip_prefix("data.pull.done"), std::string("pull.done"));

    // 序号必须打出来：批量拉取最需要的信息是"还剩几只"。
    const std::string line = text_of("data.pull.symbol", rich);
    FP_CHECK_MSG(line.find("[3/14]") != std::string::npos, "拉取进度没带序号: " + line);
    FP_CHECK_MSG(line.find("600519.SH") != std::string::npos, "拉取进度没带标的: " + line);
}
