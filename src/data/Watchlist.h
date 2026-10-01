// FinPulse Terminal — 默认关注清单（上证指数主要成分股）
//
// 为什么是一份固定清单，而不是去抓"全市场所有 A 股"：
//   * 这是一个演示终端，选择列表要能一眼看完。几千项的滚动框不如 14 项；
//   * 真正高频的痛点不是"找不到某只票"，而是"记不清它的代码" ——
//     所以每一项都带中文名，界面上显示"600519.SH  贵州茅台"。
//
// 清单可以被启动配置档的 ``watchlist`` 字段整体替换（见 core/Profile.h）。
// 换句话说：这里只是**默认值**，不是硬编码的真理。
#pragma once

#include <string>
#include <vector>

namespace fp {

/// 清单里的一项：代码 + 中文名。
/// 名字只用于界面展示 —— 传给引擎、写进配置档的永远只有代码。
struct WatchItem {
    std::string code;  ///< Tushare 规范代码，如 600519.SH
    std::string name;  ///< 中文简称，仅用于显示
};

/// 上证（SSE）主要成分股，覆盖金融、消费、医药、能源、新能源等主要板块。
/// 顺序即界面上的显示顺序，第一项是启动时的默认标的。
const std::vector<WatchItem>& sse_watchlist();

/// 界面上显示的一行，形如 ``600519.SH  贵州茅台``。
std::string watch_label(const WatchItem& item);

/// 从界面上的一行反推代码。
///
/// 用户可能输入的东西比想象中多，这几种都要能用：
///   ``600519.SH``  /  ``600519.SH 贵州茅台``  /  ``600519``  /  ``sh600519``
/// 归一化的规则是**确定性**的，不是猜：6/9 开头 → ``.SH``，0/2/3 → ``.SZ``，
/// 4/8 → ``.BJ``。用户真输入了不认识的形状时原样返回，交给引擎去报
/// "没有这只票" —— 比在这里悄悄编一个代码好。
std::string symbol_from_text(const std::string& text);

/// 代码 → 中文名；不认识返回空串。调用方据此只显示代码，
/// 而不是显示一个 "?" 让人以为数据出了问题。
std::string name_for(const std::string& symbol);

}  // namespace fp
