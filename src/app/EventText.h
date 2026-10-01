// FinPulse Terminal — 智能体进度事件的展示文案
//
// 从 CliApp.cpp 里抽出来的，理由很具体：这段代码出过一次**静默失效**的
// 缺陷 —— 描述表的键写的是语义名（``role.start``），而线上跑的事件名是
// ``agent.role.start``。于是每一条分支都不命中，命令行只剩光秃秃的事件名，
// 看起来"事件通道通了"，实际上说明文字一个字都没出来，也没有任何报错。
//
// 放在匿名命名空间里的函数没法被测试，改用一次就没人敢动。所以提到头文件：
// 它是纯函数（输入事件名 + 数据，输出一行字），没有 IO、没有状态，
// 正适合被直接断言。
#pragma once

#include <cstddef>
#include <string>
#include <vector>

#include "core/Json.h"

namespace fp {
namespace event_text {

/// 传输层前缀。事件名在线上是 ``agent.role.start`` / ``data.pull.start``
/// 这种形式，用它和别的**事件域**区分开。
inline constexpr const char* kPrefix = "agent.";

/// 数据域。批量拉取（``source.pull``）的事件走这一支 —— 它不是智能体
/// 的进度，混用 ``agent.`` 前缀会让"运维类事件"和"推理类事件"分不开。
inline constexpr const char* kDataPrefix = "data.";

/// 剥掉传输层前缀，得到语义名。不是已知前缀开头时原样返回。
inline std::string strip_prefix(const std::string& name) {
    for (const char* prefix : {kPrefix, kDataPrefix}) {
        const std::size_t len = std::char_traits<char>::length(prefix);
        if (name.size() > len && name.compare(0, len, prefix) == 0) {
            return name.substr(len);
        }
    }
    return name;
}

inline std::string join(const std::vector<std::string>& v, const char* sep = ", ") {
    std::string out;
    for (std::size_t i = 0; i < v.size(); ++i) {
        if (i) out += sep;
        out += v[i];
    }
    return out;
}

/// 把一条事件压成一行可读的说明。入参是**剥过前缀**的语义名
/// （见 :func:`strip_prefix`）。认不出的事件返回空串 —— 调用方据此决定
/// 只打事件名，而不是打一句"未知事件"把噪音塞进报告。
///
/// 为什么文案在 C++ 侧而不是让 Python 直接推一句人话：事件是**给所有前端
/// 用的结构化数据**（GUI 要拿它画轮次表、算耗时），把中文句子焊进 payload
/// 会让 GUI 不得不去解析人类语言。文案属于展示层，就留在展示层。
inline std::string describe(const std::string& ev, const Json& d) {
    // fallback 按值收 string 而不是 const char*：这样 s("a", s("b", "x"))
    // 这种"取不到就退到另一个字段、再退到常量"的写法才成立。
    const auto s = [&d](const char* k, std::string fallback = {}) {
        return d[k].as_string_or(std::move(fallback));
    };

    if (ev == "role.start") return s("role_name", s("role", "角色")) + " 开始研判";
    if (ev == "role.done")  return s("role_name", s("role", "角色")) + " 完成";
    if (ev == "team.start") return "并行独立研判 " + std::to_string(d["count"].as_int_or(0)) + " 位角色";
    if (ev == "team.done")  return "团队研判结束";
    if (ev == "debate.start") {
        return s("panel_name", s("panel", "投委会"))
             + " 开会（quorum=" + std::to_string(d["quorum"].as_int_or(0))
             + "，轮数 " + std::to_string(d["rounds"].as_int_or(0)) + "）";
    }
    if (ev == "chair.start") return "主席开始综合各位委员的结论";
    if (ev == "round.done") {
        std::string out = "第 " + std::to_string(d["round"].as_int_or(0)) + " 轮结束";
        const Json& changed = d["changed"];
        // 只在 changed **确实是个数组**时才谈改口。三种情况的区别是有意的：
        //   []      引擎明确断言"这一轮没人改口" → 写出来，这是个有意义的结论；
        //   非空    列出改口的人（交叉质证起没起作用的直接证据）；
        //   缺失/类型不对   引擎什么都没说 → 只报轮次，不替它下结论。
        // 把最后一种也当成"无人改口"，就是凭空捏造一个事实 —— 和"弃权
        // 被写成 NEUTRAL"是同一类错误。
        if (changed.is_array()) {
            if (changed.size() == 0) {
                out += "（无人改口）";
            } else {
                std::vector<std::string> who;
                for (const auto& c : changed.items()) who.push_back(c.as_string_or("?"));
                out += "（改口 " + std::to_string(who.size()) + " 人: " + join(who) + "）";
            }
        }
        return out;
    }
    // direction 为空是**弃权**，不是 NEUTRAL。这里必须显式区分，否则命令行
    // 会把"没出决议"读成"看中性"——而这两件事在投委会里含义完全相反。
    if (ev == "debate.done") {
        const std::string dir = s("direction");
        return std::string("会议结束：") + (dir.empty() ? "未形成有效决议" : "方向 " + dir);
    }
    if (ev == "run.done") {
        const std::string dir = s("direction");
        return std::string("运行结束（") + (dir.empty() ? "无方向" : dir) + "）";
    }
    if (ev == "run.start") return std::string("开始运行（") + s("kind", "?") + "）";
    if (ev == "run.error") return "运行失败: " + s("error", "?");

    // ── 数据域：批量拉取 ──────────────────────────────────
    if (ev == "pull.start") {
        return "开始批量拉取 " + std::to_string(d["total"].as_int_or(0))
             + " 只标的（" + s("source", "?") + "，每只 "
             + std::to_string(d["bars"].as_int_or(0)) + " 根）";
    }
    if (ev == "pull.symbol") {
        // 带上序号，让人一眼看出还剩几只 —— 批量任务最需要的就是这个。
        return "[" + std::to_string(d["index"].as_int_or(0)) + "/"
             + std::to_string(d["total"].as_int_or(0)) + "] " + s("symbol", "?");
    }
    if (ev == "pull.done") {
        return "拉取结束：成功 " + std::to_string(d["ok"].as_int_or(0))
             + " 只 / 失败 " + std::to_string(d["failed"].as_int_or(0)) + " 只";
    }
    return {};
}

}  // namespace event_text
}  // namespace fp
