#include "core/DataHub.h"

#include "core/Log.h"

#include <exception>
#include <stdexcept>

namespace fp {

namespace {

constexpr const char* kTag = "datahub";

// 一个订阅者在 handler 里再 publish 同一个主题，会形成环形触发。
// 递归 8 层还没收敛基本可以断定是 bug，直接掐断而不是等栈溢出。
thread_local int g_dispatch_depth = 0;
constexpr int   kMaxDispatchDepth = 8;

struct DepthGuard {
    DepthGuard()  { ++g_dispatch_depth; }
    ~DepthGuard() { --g_dispatch_depth; }
};

}  // namespace

DataHub& DataHub::instance() {
    static DataHub hub;
    return hub;
}

std::uint64_t DataHub::subscribe(std::string pattern, Handler handler, std::string label) {
    if (!handler) throw std::invalid_argument("[datahub] handler 不能为空");

    // 模式编译（含校验）放在锁外，非法模式在真正改动任何状态之前就抛出去
    TopicPattern compiled(pattern);

    auto entry = std::make_shared<Entry>();
    entry->pattern = std::move(compiled);
    entry->label   = label.empty() ? entry->pattern.pattern() : std::move(label);
    entry->handler = std::move(handler);

    {
        std::lock_guard<std::mutex> lk(mu_);
        entry->id = next_id_++;
        entries_.push_back(entry);
        stats_.subscriptions = entries_.size();
    }

    FP_DEBUG(kTag, "订阅 #" << entry->id << " → " << entry->pattern.pattern()
                            << (label.empty() ? "" : (" (" + entry->label + ")")));
    return entry->id;
}

bool DataHub::unsubscribe(std::uint64_t id) {
    std::lock_guard<std::mutex> lk(mu_);
    for (auto it = entries_.begin(); it != entries_.end(); ++it) {
        if ((*it)->id == id) {
            entries_.erase(it);
            stats_.subscriptions = entries_.size();
            return true;
        }
    }
    return false;  // 重复退订不该崩，也不该报错
}

void DataHub::publish(const Topic& topic, Json payload) {
    if (g_dispatch_depth >= kMaxDispatchDepth) {
        FP_ERROR(kTag, "派发深度已达 " << kMaxDispatchDepth << " 层，疑似环形触发，丢弃: " << topic.name());
        return;
    }
    DepthGuard guard;

    // ── 第一段：锁内只做匹配 + 抄句柄 ──────────────────────
    // 用户代码一行都不在这里执行，这是整套并发语义的支点。
    std::vector<std::shared_ptr<Entry>> hits;
    {
        std::lock_guard<std::mutex> lk(mu_);
        ++stats_.published;
        for (const auto& e : entries_) {
            if (e->pattern.matches(topic)) hits.push_back(e);
        }
        if (hits.empty()) ++stats_.unmatched;
    }

    if (hits.empty()) {
        FP_TRACE(kTag, "无订阅者，已丢弃: " << topic.name());
        return;
    }

    // ── 第二段：锁外派发 ──────────────────────────────────
    for (const auto& e : hits) {
        try {
            e->handler(topic, payload);
            std::lock_guard<std::mutex> lk(mu_);
            ++stats_.delivered;
        } catch (const std::exception& ex) {
            std::lock_guard<std::mutex> lk(mu_);
            ++stats_.failed;
            // 一个订阅者炸了不能连累其它订阅者，记下来继续派发
            FP_ERROR(kTag, "订阅者 #" << e->id << " (" << e->label << ") 抛异常: " << ex.what());
        } catch (...) {
            std::lock_guard<std::mutex> lk(mu_);
            ++stats_.failed;
            FP_ERROR(kTag, "订阅者 #" << e->id << " (" << e->label << ") 抛出未知异常");
        }
    }
}

std::size_t DataHub::subscription_count() const {
    std::lock_guard<std::mutex> lk(mu_);
    return entries_.size();
}

DataHubStats DataHub::stats() const {
    std::lock_guard<std::mutex> lk(mu_);
    DataHubStats s = stats_;
    s.subscriptions = entries_.size();
    return s;
}

void DataHub::reset_stats() {
    std::lock_guard<std::mutex> lk(mu_);
    const std::size_t keep = entries_.size();
    stats_ = DataHubStats{};
    stats_.subscriptions = keep;
}

std::vector<std::tuple<std::uint64_t, std::string, std::string>> DataHub::active_subscriptions() const {
    std::lock_guard<std::mutex> lk(mu_);
    std::vector<std::tuple<std::uint64_t, std::string, std::string>> out;
    out.reserve(entries_.size());
    for (const auto& e : entries_) {
        out.emplace_back(e->id, e->pattern.pattern(), e->label);
    }
    return out;
}

}  // namespace fp
