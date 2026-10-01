#include "model/Types.h"

#include <algorithm>
#include <cmath>
#include <cstdio>
#include <ctime>

namespace fp {

namespace {

constexpr Timestamp kMsPerDay = 86400LL * 1000LL;

void civil_from_ts(Timestamp ts_ms, std::tm& out) {
    const std::time_t secs = static_cast<std::time_t>(ts_ms / 1000);
#if defined(_WIN32)
    gmtime_s(&out, &secs);
#else
    gmtime_r(&secs, &out);
#endif
}

Json point_to_json(const ForecastPoint& p) {
    Json j = Json::object();
    j.set("ts", p.ts_ms);
    j.set("value", p.value);
    j.set("lower", p.lower);
    j.set("upper", p.upper);
    return j;
}

ForecastPoint point_from_json(const Json& j) {
    ForecastPoint p;
    p.ts_ms = j["ts"].as_int_or(0);
    p.value = j["value"].as_double_or(0.0);
    // 引擎可能不给区间，此时退化成点预测
    p.lower = j.has("lower") ? j["lower"].as_double_or(p.value) : p.value;
    p.upper = j.has("upper") ? j["upper"].as_double_or(p.value) : p.value;
    return p;
}

}  // namespace

// ── 时间工具 ──────────────────────────────────────────────

std::string format_date(Timestamp ts_ms) {
    std::tm tm{};
    civil_from_ts(ts_ms, tm);
    // 缓冲区按 32 留：tm_year+1900 在编译器眼里是任意 int，
    // 按理论最坏情况（11 位数字）算，16 字节会被 -Wformat-truncation 盯上。
    char buf[32];
    std::snprintf(buf, sizeof(buf), "%04d-%02d-%02d",
                  tm.tm_year + 1900, tm.tm_mon + 1, tm.tm_mday);
    return buf;
}

std::string format_datetime(Timestamp ts_ms) {
    std::tm tm{};
    civil_from_ts(ts_ms, tm);
    char buf[48];
    std::snprintf(buf, sizeof(buf), "%04d-%02d-%02d %02d:%02d:%02d",
                  tm.tm_year + 1900, tm.tm_mon + 1, tm.tm_mday,
                  tm.tm_hour, tm.tm_min, tm.tm_sec);
    return buf;
}

Timestamp today_start_ms() {
    const std::time_t now = std::time(nullptr);
    std::tm tm{};
#if defined(_WIN32)
    gmtime_s(&tm, &now);
#else
    gmtime_r(&now, &tm);
#endif
    tm.tm_hour = 0;
    tm.tm_min  = 0;
    tm.tm_sec  = 0;
#if defined(_WIN32)
    return static_cast<Timestamp>(_mkgmtime(&tm)) * 1000;
#else
    return static_cast<Timestamp>(timegm(&tm)) * 1000;
#endif
}

Timestamp parse_date(const std::string& s) {
    if (s.empty()) return -1;

    int y = 0, m = 0, d = 0;
    if (std::sscanf(s.c_str(), "%4d-%2d-%2d", &y, &m, &d) != 3) return -1;
    // "%4d" 对 "24-05-01" 也会成功解析出 24，所以年份范围必须自己卡。
    // 两位年份几乎一定是上游格式不统一，静默接受会算出错误的年份。
    if (y < 1900 || y > 2999) return -1;
    if (m < 1 || m > 12 || d < 1 || d > 31) return -1;

    std::tm tm{};
    tm.tm_year = y - 1900;
    tm.tm_mon  = m - 1;
    tm.tm_mday = d;
#if defined(_WIN32)
    return static_cast<Timestamp>(_mkgmtime(&tm)) * 1000;
#else
    return static_cast<Timestamp>(timegm(&tm)) * 1000;
#endif
}

// ── Quote ────────────────────────────────────────────────

Json Quote::to_json() const {
    Json j = Json::object();
    j.set("symbol", symbol);
    j.set("ts", ts_ms);
    j.set("last", last);
    j.set("prev_close", prev_close);
    j.set("open", open);
    j.set("high", high);
    j.set("low", low);
    j.set("volume", volume);
    // 涨跌幅在 C++ 侧算好一起发出去，避免每个订阅者各算一遍、算法还可能不一致
    j.set("change", change());
    j.set("change_pct", change_pct());
    return j;
}

Quote Quote::from_json(const Json& j) {
    Quote q;
    q.symbol     = j["symbol"].as_string_or("");
    q.ts_ms      = j["ts"].as_int_or(0);
    q.last       = j["last"].as_double_or(0.0);
    q.prev_close = j["prev_close"].as_double_or(j["last"].as_double_or(0.0));
    q.open       = j["open"].as_double_or(0.0);
    q.high       = j["high"].as_double_or(0.0);
    q.low        = j["low"].as_double_or(0.0);
    q.volume     = j["volume"].as_int_or(0);
    return q;
}

// ── Candle ───────────────────────────────────────────────

Json Candle::to_json() const {
    Json j = Json::object();
    j.set("ts", ts_ms);
    j.set("open", open);
    j.set("high", high);
    j.set("low", low);
    j.set("close", close);
    j.set("volume", volume);
    return j;
}

Candle Candle::from_json(const Json& j) {
    Candle c;
    c.ts_ms  = j["ts"].as_int_or(0);
    c.open   = j["open"].as_double_or(0.0);
    c.high   = j["high"].as_double_or(0.0);
    c.low    = j["low"].as_double_or(0.0);
    c.close  = j["close"].as_double_or(0.0);
    c.volume = j["volume"].as_int_or(0);
    return c;
}

// ── CandleSeries ─────────────────────────────────────────

std::vector<double> CandleSeries::closes() const {
    std::vector<double> out;
    out.reserve(bars_.size());
    for (const auto& b : bars_) out.push_back(b.close);
    return out;
}

std::vector<Timestamp> CandleSeries::timestamps() const {
    std::vector<Timestamp> out;
    out.reserve(bars_.size());
    for (const auto& b : bars_) out.push_back(b.ts_ms);
    return out;
}

std::vector<double> CandleSeries::close_slice(std::size_t from, std::size_t count) const {
    std::vector<double> out;
    if (from >= bars_.size()) return out;
    const std::size_t end = std::min(bars_.size(), from + count);
    out.reserve(end - from);
    for (std::size_t i = from; i < end; ++i) out.push_back(bars_[i].close);
    return out;
}

void CandleSeries::normalize() {
    std::stable_sort(bars_.begin(), bars_.end(),
                     [](const Candle& a, const Candle& b) { return a.ts_ms < b.ts_ms; });

    // 同一时间戳保留**最后**出现的那一条。
    // 之所以这么定：CSV 源是追加写的，同一天被重写一次时新行在文件末尾，
    // 而"后写的覆盖先写的"符合直觉。注意 std::unique 保留的是第一个，
    // 直接用会得到相反的结果——这条差异有测试覆盖。
    std::vector<Candle> deduped;
    deduped.reserve(bars_.size());
    for (auto it = bars_.rbegin(); it != bars_.rend(); ++it) {
        if (deduped.empty() || deduped.back().ts_ms != it->ts_ms) {
            deduped.push_back(*it);
        }
    }
    std::reverse(deduped.begin(), deduped.end());
    bars_ = std::move(deduped);
}

std::vector<std::string> CandleSeries::validate() const {
    std::vector<std::string> issues;
    if (bars_.empty()) {
        issues.push_back("序列为空");
        return issues;
    }
    for (std::size_t i = 0; i < bars_.size(); ++i) {
        const auto& b = bars_[i];
        if (b.high < b.low)               issues.push_back("第 " + std::to_string(i) + " 根: high < low");
        if (b.close <= 0 || b.open <= 0)  issues.push_back("第 " + std::to_string(i) + " 根: 价格非正");
        if (b.high < b.close || b.low > b.close)
            issues.push_back("第 " + std::to_string(i) + " 根: close 落在 [low, high] 之外");
        if (i > 0 && bars_[i].ts_ms <= bars_[i - 1].ts_ms)
            issues.push_back("第 " + std::to_string(i) + " 根: 时间戳未严格递增");
        if (issues.size() > 20) { issues.push_back("... 其余问题已省略"); break; }
    }
    return issues;
}

Json CandleSeries::to_json() const {
    Json j = Json::object();
    j.set("symbol", symbol_);
    Json arr = Json::array();
    for (const auto& b : bars_) arr.push(b.to_json());
    j.set("bars", std::move(arr));
    j.set("count", static_cast<long long>(bars_.size()));
    return j;
}

CandleSeries CandleSeries::from_json(const Json& j) {
    CandleSeries cs(j["symbol"].as_string_or(""));
    const Json& arr = j["bars"];
    if (arr.is_array()) {
        cs.reserve(arr.size());
        for (const auto& item : arr.items()) cs.push(Candle::from_json(item));
    }
    return cs;
}

// ── ForecastResult ───────────────────────────────────────

Json ForecastResult::to_json() const {
    Json j = Json::object();
    j.set("method", method);
    j.set("symbol", symbol);
    j.set("last_close", last_close);
    Json arr = Json::array();
    for (const auto& p : points) arr.push(point_to_json(p));
    j.set("points", std::move(arr));
    j.set("meta", meta);
    return j;
}

ForecastResult ForecastResult::from_json(const Json& j) {
    ForecastResult r;
    r.method     = j["method"].as_string_or("");
    r.symbol     = j["symbol"].as_string_or("");
    r.last_close = j["last_close"].as_double_or(0.0);
    r.meta       = j["meta"];
    const Json& arr = j["points"];
    if (arr.is_array()) {
        r.points.reserve(arr.size());
        for (const auto& item : arr.items()) r.points.push_back(point_from_json(item));
    }
    return r;
}

// ── BacktestMetrics ──────────────────────────────────────

Json BacktestMetrics::to_json() const {
    Json j = Json::object();
    j.set("method", method);
    j.set("folds", static_cast<long long>(folds));
    j.set("horizon", static_cast<long long>(horizon));
    j.set("mae", mae);
    j.set("rmse", rmse);
    j.set("mape", mape);
    j.set("dir_acc", dir_acc);
    j.set("base_mae", base_mae);
    j.set("base_rmse", base_rmse);
    j.set("base_dir_acc", base_dir_acc);
    j.set("skill", skill);
    return j;
}

BacktestMetrics BacktestMetrics::from_json(const Json& j) {
    BacktestMetrics m;
    m.method       = j["method"].as_string_or("");
    m.folds        = static_cast<std::size_t>(j["folds"].as_int_or(0));
    m.horizon      = static_cast<std::size_t>(j["horizon"].as_int_or(1));
    m.mae          = j["mae"].as_double_or(0.0);
    m.rmse         = j["rmse"].as_double_or(0.0);
    m.mape         = j["mape"].as_double_or(0.0);
    m.dir_acc      = j["dir_acc"].as_double_or(0.0);
    m.base_mae     = j["base_mae"].as_double_or(0.0);
    m.base_rmse    = j["base_rmse"].as_double_or(0.0);
    m.base_dir_acc = j["base_dir_acc"].as_double_or(0.0);
    m.skill        = j["skill"].as_double_or(0.0);
    return m;
}

}  // namespace fp
