#include "data/Watchlist.h"

#include <algorithm>
#include <cctype>

namespace fp {
namespace {

/// 上证主要成分股（上证 50 / 上证 180 里的代表性标的）。
///
/// 名称用交易所公布的中文简称，不带"股份""集团"之类的尾巴：
/// 界面上要的是能一眼认出来的那个词。
const std::vector<WatchItem>& table() {
    static const std::vector<WatchItem> kItems = {
        {"600519.SH", "贵州茅台"},
        {"601318.SH", "中国平安"},
        {"600036.SH", "招商银行"},
        {"601398.SH", "工商银行"},
        {"600030.SH",   "中信证券"},
        {"600276.SH",   "恒瑞医药"},
        {"600887.SH",   "伊利股份"},
        {"601888.SH",   "中国中免"},
        {"600900.SH",   "长江电力"},
        {"601899.SH",   "紫金矿业"},
        {"600309.SH",   "万华化学"},
        {"601088.SH",   "中国神华"},
        {"600585.SH",   "海螺水泥"},
        {"601012.SH",   "隆基绿能"},
    };
    return kItems;
}

std::string upper(std::string s) {
    std::transform(s.begin(), s.end(), s.begin(),
                   [](unsigned char c) { return static_cast<char>(std::toupper(c)); });
    return s;
}

/// 六个数字 → 交易所后缀。规则来自 A 股代码分配，是确定性的：
/// 6/9 开头是沪市，0/2/3 是深市，4/8 是北交所。
std::string infer_suffix(const std::string& digits) {
    if (digits.empty()) return {};
    const char c = digits.front();
    if (c == '6' || c == '9') return ".SH";
    if (c == '0' || c == '2' || c == '3') return ".SZ";
    if (c == '4' || c == '8') return ".BJ";
    return {};
}

}  // namespace

const std::vector<WatchItem>& sse_watchlist() { return table(); }

std::string watch_label(const WatchItem& item) {
    if (item.name.empty()) return item.code;
    return item.code + "  " + item.name;
}

std::string symbol_from_text(const std::string& text) {
    // 1. 掐掉首尾空白，再取到第一个空白为止 —— 于是带中文名的一行
    //    和纯代码走的是同一条路。
    const std::size_t begin = text.find_first_not_of(" \t\r\n");
    if (begin == std::string::npos) return {};
    const std::size_t end = text.find_first_of(" \t\r\n", begin);
    std::string token = text.substr(begin, end == std::string::npos ? std::string::npos
                                                                   : end - begin);

    // 2. 去掉常见的分隔写法：600519-SH / 600519_SH → 统一成点号。
    for (char& c : token) {
        if (c == '_') c = '.';
    }

    std::string up = upper(token);

    // 3. sh600519 / sz000001 这种"前缀式"（通达信、新浪的写法）→ 后缀式。
    if (up.size() > 2 && (up.compare(0, 2, "SH") == 0 || up.compare(0, 2, "SZ") == 0 ||
                          up.compare(0, 2, "BJ") == 0)) {
        const std::string digits = up.substr(2);
        const bool all_digit =
            !digits.empty() &&
            std::all_of(digits.begin(), digits.end(),
                        [](unsigned char c) { return std::isdigit(c) != 0; });
        if (all_digit) return digits + "." + up.substr(0, 2);
    }

    // 4. 带后缀的：只把后缀大写，代码部分原样保留。
    const std::size_t dot = up.find('.');
    if (dot != std::string::npos && dot + 1 < up.size()) {
        return up;
    }

    // 5. 纯六位数字：补上交易所后缀。少了这一步，用户手输 "600519"
    //    会被引擎原样拿去查 —— Tushare 认不出，报"没有这只票"。
    if (up.size() == 6 &&
        std::all_of(up.begin(), up.end(),
                    [](unsigned char c) { return std::isdigit(c) != 0; })) {
        const std::string suffix = infer_suffix(up);
        if (!suffix.empty()) return up + suffix;
    }

    return token;  // 认不出的形状原样返回，让引擎去报错
}

std::string name_for(const std::string& symbol) {
    const std::string want = upper(symbol);
    for (const WatchItem& item : table()) {
        if (upper(item.code) == want) return item.name;
    }
    return {};
}

}  // namespace fp
