#include "data/ReplaySource.h"

#include "core/Log.h"

#include <algorithm>
#include <chrono>
#include <thread>

namespace fp {

namespace {
constexpr const char* kTag = "replay";
}  // namespace

std::string ReplaySource::normalize_symbol(std::string s) {
    std::replace(s.begin(), s.end(), '.', '-');
    if (s.empty()) s = "UNKNOWN";
    return s;
}

void ReplaySource::load(CandleSeries series) {
    series_ = std::move(series);
    series_.normalize();
    cursor_ = 0;
    stop_.store(false, std::memory_order_release);
    last_quote_ = Quote{};

    FP_INFO(kTag, "装载 " << series_.size() << " 根 " << symbol_ << " 的历史数据"
                          << (series_.empty() ? "" : ("，区间 " + format_date(series_.at(0).ts_ms) +
                                                      " ~ " + format_date(series_.back().ts_ms))));
}

Quote ReplaySource::make_quote(const std::string& symbol, const Candle& bar, const Candle& prev) {
    Quote q;
    q.symbol     = symbol;
    q.ts_ms      = bar.ts_ms;
    q.last       = bar.close;
    q.prev_close = prev.close;
    q.open       = bar.open;
    q.high       = bar.high;
    q.low        = bar.low;
    q.volume     = bar.volume;
    return q;
}

void ReplaySource::sleep_for_step() const {
    if (cursor_ < 2) return;

    int delay_ms = opt_.interval_ms;

    if (opt_.speed > 0.0) {
        const auto dt = series_.at(cursor_ - 1).ts_ms - series_.at(cursor_ - 2).ts_ms;
        if (dt > 0) {
            delay_ms += static_cast<int>(static_cast<double>(dt) / opt_.speed);
        }
    }

    if (delay_ms > 0) {
        std::this_thread::sleep_for(std::chrono::milliseconds(delay_ms));
    }
}

bool ReplaySource::step() {
    if (cursor_ >= series_.size()) return false;

    const Candle& bar  = series_.at(cursor_);
    const Candle& prev = cursor_ > 0 ? series_.at(cursor_ - 1) : bar;

    last_quote_ = make_quote(symbol_, bar, prev);

    // 行情快照与 K 线分成两个主题：表格只关心 quote，图表两个都要。
    // 把它们合成一条消息会强迫所有订阅者都去解析自己不用的字段。
    hub_.publish("market.quote." + symbol_, last_quote_.to_json());
    if (opt_.emit_kline) {
        hub_.publish("market.kline." + symbol_, bar.to_json());
    }

    ++cursor_;
    return true;
}

std::size_t ReplaySource::run_to_end() {
    const std::size_t begin = cursor_;

    while (!stop_.load(std::memory_order_acquire)) {
        sleep_for_step();
        if (!step()) break;
    }

    const std::size_t count = cursor_ - begin;
    hub_.publish("market.replay.done." + symbol_,
                 Json(static_cast<long long>(count)));
    FP_INFO(kTag, "回放结束，共发布 " << count << " 根");
    return count;
}

}  // namespace fp
