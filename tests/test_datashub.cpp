#include "TestFramework.h"

#include "core/DataHub.h"

#include <atomic>
#include <stdexcept>
#include <thread>
#include <vector>

using namespace fp;

namespace {

Json quote_payload(double last) {
    Json j = Json::object();
    j.set("last", last);
    return j;
}

}  // namespace

FP_TEST(datashub, "基本订阅与投递") {
    DataHub hub;
    int    hits = 0;
    double last = 0.0;

    const auto id = hub.subscribe("market.quote.*",
                                  [&](const Topic&, const Json& p) {
                                      ++hits;
                                      last = p["last"].as_double_or(0.0);
                                  });
    FP_CHECK(id > 0);
    FP_CHECK_EQ(hub.subscription_count(), 1u);

    hub.publish("market.quote.AAPL", quote_payload(123.5));
    FP_CHECK_EQ(hits, 1);
    FP_CHECK_NEAR(last, 123.5, 1e-9);

    // 不匹配的主题不该触发
    hub.publish("market.kline.AAPL", quote_payload(999.0));
    FP_CHECK_EQ(hits, 1);

    const auto st = hub.stats();
    FP_CHECK_EQ(st.published, 2u);
    FP_CHECK_EQ(st.delivered, 1u);
    FP_CHECK_EQ(st.unmatched, 1u);
}

FP_TEST(datashub, "多订阅者都收到") {
    DataHub hub;
    int a = 0, b = 0;
    hub.subscribe("t.**", [&](const Topic&, const Json&) { ++a; });
    hub.subscribe("t.x",  [&](const Topic&, const Json&) { ++b; });

    hub.publish("t.x", Json(1));
    FP_CHECK_EQ(a, 1);
    FP_CHECK_EQ(b, 1);
}

// 这是整套设计里最关键的一条不变量。
// 如果 publish 在持锁状态下回调，下面第一个用例会直接死锁——
// 所以把它固化成测试，任何人重构 DataHub 时都会立刻发现。
FP_TEST(datashub, "回调中退订自己不会死锁") {
    DataHub        hub;
    std::uint64_t  id = 0;
    int            calls = 0;

    id = hub.subscribe("x.y", [&](const Topic&, const Json&) {
        ++calls;
        hub.unsubscribe(id);  // 在回调里把自己摘掉
    });

    hub.publish("x.y", Json(1));
    FP_CHECK_EQ(calls, 1);

    hub.publish("x.y", Json(2));  // 已经退订了，不该再被调
    FP_CHECK_EQ(calls, 1);
    FP_CHECK_EQ(hub.subscription_count(), 0u);
}

FP_TEST(datashub, "回调中新增订阅不会死锁") {
    DataHub hub;
    int  late_calls = 0;
    bool hooked     = false;

    hub.subscribe("trigger", [&](const Topic&, const Json&) {
        if (hooked) return;
        hooked = true;
        hub.subscribe("trigger", [&](const Topic&, const Json&) { ++late_calls; });
    });

    hub.publish("trigger", Json(1));
    FP_CHECK_EQ(late_calls, 0);  // 快照在派发前就取好了，本轮看不到新订阅
    hub.publish("trigger", Json(2));
    FP_CHECK_EQ(late_calls, 1);
}

FP_TEST(datashub, "回调中发布其它主题不会死锁") {
    DataHub hub;
    int a = 0, b = 0;
    hub.subscribe("chain.a", [&](const Topic&, const Json&) {
        ++a;
        hub.publish("chain.b", Json(1));
    });
    hub.subscribe("chain.b", [&](const Topic&, const Json&) { ++b; });

    hub.publish("chain.a", Json(1));
    FP_CHECK_EQ(a, 1);
    FP_CHECK_EQ(b, 1);
}

FP_TEST(datashub, "环形触发被深度上限掐断") {
    DataHub hub;
    int calls = 0;
    hub.subscribe("loop", [&](const Topic&, const Json&) {
        ++calls;
        hub.publish("loop", Json(1));  // 自己触发自己
    });

    hub.publish("loop", Json(1));

    // 深度上限是 8。没有这个保护，这个用例就是一次栈溢出。
    FP_CHECK(calls > 0);
    FP_CHECK(calls <= 8);
}

FP_TEST(datashub, "订阅者异常被隔离") {
    DataHub hub;
    int good = 0;

    hub.subscribe("t", [](const Topic&, const Json&) {
        throw std::runtime_error("故意炸一个订阅者");
    });
    hub.subscribe("t", [&](const Topic&, const Json&) { ++good; });

    hub.publish("t", Json(1));

    FP_CHECK_EQ(good, 1);  // 后者仍然被投递了
    const auto st = hub.stats();
    FP_CHECK_EQ(st.failed, 1u);
    FP_CHECK_EQ(st.delivered, 1u);
}

FP_TEST(datashub, "重复退订不报错") {
    DataHub hub;
    const auto id = hub.subscribe("t", [](const Topic&, const Json&) {});
    FP_CHECK(hub.unsubscribe(id));
    FP_CHECK(!hub.unsubscribe(id));         // 第二次返回 false，但不抛
    FP_CHECK(!hub.unsubscribe(999999));     // 不存在的 id 同样静默
}

FP_TEST(datashub, "RAII 句柄自动退订") {
    DataHub hub;
    FP_CHECK_EQ(hub.subscription_count(), 0u);
    {
        Subscription s(&hub, hub.subscribe("t", [](const Topic&, const Json&) {}));
        FP_CHECK_EQ(hub.subscription_count(), 1u);
        FP_CHECK(s.valid());
        FP_CHECK(s.id() > 0);
    }
    FP_CHECK_EQ(hub.subscription_count(), 0u);
}

FP_TEST(datashub, "RAII 句柄可移动不可拷贝") {
    DataHub hub;
    Subscription a(&hub, hub.subscribe("t", [](const Topic&, const Json&) {}));
    FP_CHECK_EQ(hub.subscription_count(), 1u);

    Subscription b(std::move(a));
    FP_CHECK(b.valid());
    FP_CHECK(!a.valid());
    FP_CHECK_EQ(hub.subscription_count(), 1u);  // 移动不该新增订阅

    b.reset();
    FP_CHECK_EQ(hub.subscription_count(), 0u);
}

FP_TEST(datashub, "空 handler 被拒绝") {
    DataHub hub;
    FP_CHECK_THROWS(hub.subscribe("t", DataHub::Handler{}), std::invalid_argument);
    FP_CHECK_THROWS(hub.subscribe("bad pattern ** middle", [](const Topic&, const Json&) {}),
                    std::invalid_argument);
}

FP_TEST(datashub, "跨线程发布不丢不乱") {
    DataHub hub;
    std::atomic<int> received{0};
    hub.subscribe("mt.**", [&](const Topic&, const Json&) {
        received.fetch_add(1, std::memory_order_relaxed);
    });

    constexpr int kThreads = 4;
    constexpr int kPerThread = 250;
    std::vector<std::thread> workers;
    workers.reserve(kThreads);

    for (int t = 0; t < kThreads; ++t) {
        workers.emplace_back([&hub, t]() {
            for (int i = 0; i < kPerThread; ++i) {
                hub.publish("mt.t" + std::to_string(t), quote_payload(i));
            }
        });
    }
    for (auto& w : workers) w.join();

    FP_CHECK_EQ(received.load(), kThreads * kPerThread);
    FP_CHECK_EQ(hub.stats().published, static_cast<std::uint64_t>(kThreads * kPerThread));
}

FP_TEST(datashub, "活跃订阅清单") {
    DataHub hub;
    hub.subscribe("a.*", [](const Topic&, const Json&) {}, "甲");
    hub.subscribe("b.**", [](const Topic&, const Json&) {}, "乙");

    const auto list = hub.active_subscriptions();
    FP_CHECK_EQ(list.size(), 2u);
    FP_CHECK_EQ(std::get<1>(list[0]), std::string("a.*"));
    FP_CHECK_EQ(std::get<2>(list[1]), std::string("乙"));
}
