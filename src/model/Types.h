// FinPulse Terminal — 领域数据模型（行情、K 线、预测、回测指标）
//
// 这些结构处在"C++ 壳"和"Python 引擎"的边界上，所以每一个都自带
// to_json / from_json。序列化格式就是 docs/bridge-protocol.md 里冻结的那份，
// 改这里等于改协议，两边必须同步。
#pragma once

#include <cstdint>
#include <string>
#include <vector>

#include "core/Json.h"

namespace fp {

/// 毫秒级 epoch。全项目统一用它，不混用 time_t / 秒级。
using Timestamp = std::int64_t;

/// "2024-03-15" / "2024-03-15 09:30:00" 形式的本地（UTC）时间串。
std::string format_date(Timestamp ts_ms);
std::string format_datetime(Timestamp ts_ms);
/// 今天的 00:00:00 UTC。
Timestamp today_start_ms();
/// 解析 "YYYY-MM-DD"。失败返回 -1 —— 不能用 0 当失败哨兵，
/// 因为 0 本身是合法的（正是 epoch 那一刻）。两位数年份（如 "24-05-01"）
/// 一律拒绝：它几乎总是数据源格式不统一导致的，静默接受会算出错误的年份。
Timestamp parse_date(const std::string& s);

/// 实时/最近一笔行情快照。
struct Quote {
    std::string  symbol;
    Timestamp    ts_ms{0};
    double       last{0.0};
    double       prev_close{0.0};
    double       open{0.0};
    double       high{0.0};
    double       low{0.0};
    std::int64_t volume{0};

    double change() const { return last - prev_close; }
    double change_pct() const {
        return prev_close != 0.0 ? (last - prev_close) / prev_close * 100.0 : 0.0;
    }

    Json to_json() const;
    static Quote from_json(const Json& j);
};

/// 单根 K 线。
struct Candle {
    Timestamp    ts_ms{0};
    double       open{0.0};
    double       high{0.0};
    double       low{0.0};
    double       close{0.0};
    std::int64_t volume{0};

    double range() const { return high - low; }
    double typical() const { return (high + low + close) / 3.0; }  ///< ATR / 布林中轨常用
    bool   is_bullish() const { return close >= open; }

    Json to_json() const;
    static Candle from_json(const Json& j);
};

/// OHLCV 序列。指标计算、回测、图表都吃这个。
class CandleSeries {
public:
    CandleSeries() = default;
    explicit CandleSeries(std::string symbol) : symbol_(std::move(symbol)) {}

    void push(Candle c) { bars_.push_back(std::move(c)); }
    void reserve(std::size_t n) { bars_.reserve(n); }

    const std::string&              symbol() const noexcept { return symbol_; }
    void                            set_symbol(std::string s) { symbol_ = std::move(s); }
    const std::vector<Candle>&      bars() const noexcept { return bars_; }
    std::vector<Candle>&            bars() noexcept { return bars_; }
    std::size_t                     size() const noexcept { return bars_.size(); }
    bool                            empty() const noexcept { return bars_.empty(); }
    const Candle&                   at(std::size_t i) const { return bars_.at(i); }
    const Candle&                   back(std::size_t n = 0) const { return bars_.at(bars_.size() - 1 - n); }

    /// 只取收盘价，喂给指标 / 统计 / 预测。
    std::vector<double>             closes() const;
    /// 只取时间戳。
    std::vector<Timestamp>          timestamps() const;
    /// [from, end) 的收盘价切片，回测里按窗口滚动调用。
    std::vector<double>             close_slice(std::size_t from, std::size_t count) const;

    /// 按时间戳去重排序（CSV 源里偶尔有重复行）。
    void normalize();
    /// 数据质量检查：非正价格、high < low、时间倒序等。
    std::vector<std::string> validate() const;

    Json to_json() const;
    static CandleSeries from_json(const Json& j);

private:
    std::string         symbol_;
    std::vector<Candle> bars_;
};

/// 单个预测点。
struct ForecastPoint {
    Timestamp ts_ms{0};
    double    value{0.0};
    double    lower{0.0};   ///< 置信下界（引擎不支持时与 value 相同）
    double    upper{0.0};   ///< 置信上界
};

/// 一次预测的完整结果。
struct ForecastResult {
    std::string                method;
    std::string                symbol;
    double                     last_close{0.0};
    std::vector<ForecastPoint> points;
    Json                       meta;   ///< 方法特有信息（AR 阶数、AIC、随机游走漂移等）

    Json to_json() const;
    static ForecastResult from_json(const Json& j);
};

/// 滚动回测指标。baseline_* 一律指"随机游走"基线 —— 时间序列预测里
/// 不跟随机游走比的准确率没有意义（Meese–Rogoff 的教训）。
struct BacktestMetrics {
    std::string method;
    std::size_t folds{0};
    std::size_t horizon{1};

    // 模型
    double mae{0.0};
    double rmse{0.0};
    double mape{0.0};        ///< 平均绝对百分比误差（%）
    double dir_acc{0.0};     ///< 方向命中率（%）
    // 随机游走基线
    double base_mae{0.0};
    double base_rmse{0.0};
    double base_dir_acc{0.0};
    /// 技能分 = 1 - MSE_model / MSE_baseline。>0 说明跑赢随机游走，<=0 说明白干。
    double skill{0.0};

    Json to_json() const;
    static BacktestMetrics from_json(const Json& j);
};

}  // namespace fp
