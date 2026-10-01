// FinPulse Terminal — 行情回放
//
// 把一段历史 K 线按时间轴"重放"成实时行情流，投递到 DataHub。
//
// 它的价值在于让整个终端在**离线**状态下也能跑通完整链路：
// 数据从回放源出来、经过总线、被表格和图表消费——和接真实行情时
// 走的是同一条路。这样"实时"就只是一个数据源的区别，
// 而不是一套需要单独维护和调试的代码路径。
//
// 这也是本项目没有引入假 tick 生成器、或者让 UI 直接读文件的原因：
// 一旦 UI 允许绕过总线拿数据，总线的设计就会被逐渐侵蚀。
#pragma once

#include <atomic>
#include <cstddef>
#include <string>

#include "core/DataHub.h"
#include "model/Types.h"

namespace fp {

class ReplaySource {
public:
    struct Options {
        /// 历史时间与现实时间的比例。0 或负数表示全速（不 sleep）。
        double speed{0.0};
        /// 每根之间额外固定的间隔（毫秒），用于肉眼观察。
        int interval_ms{0};
        /// 是否同时发布 market.kline.* 主题。
        bool emit_kline{true};
    };

    ReplaySource(DataHub& hub, std::string symbol)
        : ReplaySource(hub, std::move(symbol), Options{}) {}

    ReplaySource(DataHub& hub, std::string symbol, Options opt)
        : hub_(hub), symbol_(normalize_symbol(std::move(symbol))), opt_(opt) {}

    /// 主题里的符号不能含 '.'（会被当成层级分隔），统一替换成 '-'。
    static std::string normalize_symbol(std::string s);

    void load(CandleSeries series);

    /// 按设定节奏播完全部 K 线，返回实际发布的 tick 数。
    std::size_t run_to_end();

    /// 只播下一根。已播完返回 false。
    bool step();

    /// 从另一个线程请求提前停止（run_to_end 会在下一根之前退出）。
    void request_stop() { stop_.store(true, std::memory_order_release); }

    std::size_t emitted() const noexcept { return cursor_; }
    std::size_t remaining() const noexcept { return series_.size() - cursor_; }
    const CandleSeries& series() const noexcept { return series_; }
    std::size_t cursor() const noexcept { return cursor_; }

    /// 当前时刻的行情快照（尚未开始回放时是空 Quote）。
    Quote current_quote() const { return last_quote_; }

private:
    static Quote make_quote(const std::string& symbol, const Candle& bar, const Candle& prev);
    void sleep_for_step() const;

    DataHub&          hub_;
    std::string       symbol_;
    Options           opt_;
    CandleSeries      series_;
    std::size_t       cursor_{0};
    Quote             last_quote_{};
    std::atomic<bool> stop_{false};
};

}  // namespace fp
