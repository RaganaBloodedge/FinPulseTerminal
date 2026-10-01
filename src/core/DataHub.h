// FinPulse Terminal — 进程内发布订阅总线
//
// 设计目标
//   1. 生产者不关心谁来消费（行情回放线程、Python 引擎事件、定时器都能发）；
//   2. 消费者不关心数据从哪来（表格、图表、日志、策略都能订）；
//   3. 单条恶意的 or 有 bug 的订阅者不能拖垮其他人。
//
// 并发语义（重点，也是这套实现里最容易踩坑的地方）
//   publish() 在锁内只做一件事：把命中的订阅者 shared_ptr 抄一份出来，然后立刻解锁。
//   真正的 handler 调用全部发生在锁外。这样有三个好处：
//     a) handler 里再调 subscribe/unsubscribe/publish 不会自死锁；
//     b) 慢订阅者不会阻塞其它线程的 publish；
//     c) 遍历过程中有人退订也不会让迭代器失效 —— shared_ptr 保住了 Entry 的命。
//
//   代价是：一个刚刚 unsubscribe 的订阅者，仍可能收到"已经在途"的那一次投递。
//   这是刻意的取舍 —— 退订语义定义为"不再接收此后发布的主题"，
//   而不是"绝对不会再被调用"。需要严格保证的订阅者应当自己检查一个 valid 标志。
//   （对标 Qt 的 QObject::disconnect 在跨线程直连时也有类似语义。）
#pragma once

#include <cstdint>
#include <functional>
#include <memory>
#include <mutex>
#include <string>
#include <string_view>
#include <tuple>
#include <utility>
#include <vector>

#include "core/Json.h"
#include "core/Topic.h"

namespace fp {

struct DataHubStats {
    std::uint64_t published{0};    ///< 累计 publish 次数
    std::uint64_t delivered{0};    ///< 累计成功投递给订阅者的次数
    std::uint64_t failed{0};       ///< handler 抛异常的次数（已捕获，不影响其它订阅者）
    std::uint64_t unmatched{0};    ///< 没有任何订阅者命中的次数（用于发现"发出去没人听"的 bug）
    std::size_t   subscriptions{0};///< 当前活跃订阅数
};

class DataHub {
public:
    using Handler = std::function<void(const Topic&, const Json&)>;

    /// 进程级单例。测试里如果要隔离，直接构造局部的 DataHub 即可。
    static DataHub& instance();

    DataHub() = default;
    DataHub(const DataHub&) = delete;
    DataHub& operator=(const DataHub&) = delete;

    /// 订阅。pattern 见 TopicPattern；label 只用于日志定位。
    /// 非法 pattern 抛 std::invalid_argument。
    std::uint64_t subscribe(std::string pattern, Handler handler, std::string label = {});

    /// 退订。对不存在的 id 静默返回 false（重复退订不该崩）。
    bool unsubscribe(std::uint64_t id);

    void publish(const Topic& topic, Json payload);
    void publish(std::string_view topic, Json payload) {
        publish(Topic::unchecked(std::string(topic)), std::move(payload));
    }

    std::size_t subscription_count() const;
    DataHubStats stats() const;
    void reset_stats();

    /// 当前所有活跃订阅的 (id, pattern, label)，用于诊断接口和测试断言。
    std::vector<std::tuple<std::uint64_t, std::string, std::string>> active_subscriptions() const;

private:
    struct Entry {
        std::uint64_t id{0};
        TopicPattern  pattern;
        std::string   label;
        Handler       handler;
    };

    mutable std::mutex mu_;
    // 订阅数规模很小（本项目稳态 <30），线性扫描比哈希表更快，也省掉一层索引失效的复杂度。
    std::vector<std::shared_ptr<Entry>> entries_;
    std::uint64_t                       next_id_{1};
    DataHubStats                        stats_;
};

/// RAII 订阅句柄。析构自动退订，避免"忘了 unsubscribe"这类泄漏。
/// move-only：拷贝语义下两个句柄共用一个 id，析构顺序会变得难以推理。
class Subscription {
public:
    Subscription() = default;
    Subscription(DataHub* hub, std::uint64_t id) : hub_(hub), id_(id) {}

    Subscription(const Subscription&)            = delete;
    Subscription& operator=(const Subscription&) = delete;

    Subscription(Subscription&& o) noexcept
        : hub_(std::exchange(o.hub_, nullptr)), id_(std::exchange(o.id_, 0)) {}

    Subscription& operator=(Subscription&& o) noexcept {
        if (this != &o) {
            reset();
            hub_ = std::exchange(o.hub_, nullptr);
            id_   = std::exchange(o.id_, 0);
        }
        return *this;
    }

    ~Subscription() { reset(); }

    void reset() {
        if (hub_ && id_ != 0) {
            hub_->unsubscribe(id_);
        }
        hub_ = nullptr;
        id_  = 0;
    }

    std::uint64_t id() const noexcept { return id_; }
    bool          valid() const noexcept { return hub_ != nullptr && id_ != 0; }

private:
    DataHub*      hub_{nullptr};
    std::uint64_t id_{0};
};

}  // namespace fp
