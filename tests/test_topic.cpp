#include "TestFramework.h"

#include "core/Topic.h"

#include <stdexcept>

using namespace fp;

FP_TEST(topic, "精确匹配") {
    const TopicPattern p("market.quote.AAPL");
    FP_CHECK(p.matches(Topic::unchecked("market.quote.AAPL")));
    FP_CHECK(!p.matches(Topic::unchecked("market.quote.MSFT")));
    FP_CHECK(!p.matches(Topic::unchecked("market.quote")));
    FP_CHECK(!p.matches(Topic::unchecked("market.quote.AAPL.1m")));
}

FP_TEST(topic, "单层通配只吃一段") {
    const TopicPattern p("market.quote.*");
    FP_CHECK(p.matches(Topic::unchecked("market.quote.AAPL")));
    FP_CHECK(p.matches(Topic::unchecked("market.quote.BRK-B")));
    FP_CHECK(!p.matches(Topic::unchecked("market.quote.AAPL.1m")));
    FP_CHECK(!p.matches(Topic::unchecked("market.quote")));
    FP_CHECK(!p.matches(Topic::unchecked("market.kline.AAPL")));
}

FP_TEST(topic, "尾部多段通配") {
    const TopicPattern p("market.**");
    FP_CHECK(p.matches(Topic::unchecked("market.quote.AAPL")));
    FP_CHECK(p.matches(Topic::unchecked("market.kline.AAPL.1m.raw")));
    FP_CHECK(p.matches(Topic::unchecked("market")));  // ** 允许匹配零段
    FP_CHECK(!p.matches(Topic::unchecked("engine.status")));
    FP_CHECK(!p.matches(Topic::unchecked("marketing.x")));  // 前缀必须按段对齐
}

FP_TEST(topic, "通配与字面量混用") {
    const TopicPattern p("market.*.AAPL");
    // 中段被 * 吃掉
    FP_CHECK(p.matches(Topic::unchecked("market.quote.AAPL")));
    FP_CHECK(p.matches(Topic::unchecked("market.kline.AAPL")));
    // 末段是字面量，必须精确相等
    FP_CHECK(!p.matches(Topic::unchecked("market.quote.MSFT")));
    // 段数也必须对上
    FP_CHECK(!p.matches(Topic::unchecked("market.quote.AAPL.1m")));
    FP_CHECK(!p.matches(Topic::unchecked("market.AAPL")));
    FP_CHECK(!p.matches(Topic::unchecked("other.quote.AAPL")));
}

FP_TEST(topic, "非法模式被拒绝") {
    // ** 只能在末尾 —— 允许出现在中间会让匹配退化成正则级别的复杂度
    FP_CHECK_THROWS(TopicPattern("a.**.b"), std::invalid_argument);
    // 不支持段内部分通配
    FP_CHECK_THROWS(TopicPattern("a.b*"), std::invalid_argument);
    FP_CHECK_THROWS(TopicPattern("a.*x"), std::invalid_argument);
    FP_CHECK_THROWS(TopicPattern(""), std::invalid_argument);
    FP_CHECK_THROWS(TopicPattern("a..b"), std::invalid_argument);
    FP_CHECK_THROWS(TopicPattern(".a"), std::invalid_argument);
}

FP_TEST(topic, "捕获通配段") {
    const TopicPattern p("market.*.AAPL");
    const auto caps = p.capture(Topic::unchecked("market.quote.AAPL"));
    FP_CHECK_EQ(caps.size(), 1u);
    FP_CHECK_EQ(caps[0], std::string("quote"));

    const TopicPattern q("market.**");
    const auto c2 = q.capture(Topic::unchecked("market.kline.AAPL.1m"));
    FP_CHECK_EQ(c2.size(), 1u);
    FP_CHECK_EQ(c2[0], std::string("kline.AAPL.1m"));  // ** 捕获压成一个点分串

    // 不匹配时捕获为空
    FP_CHECK_EQ(p.capture(Topic::unchecked("engine.status")).size(), 0u);
}

FP_TEST(topic, "主题名合法性校验") {
    FP_CHECK_THROWS(Topic("has space"), std::invalid_argument);
    FP_CHECK_THROWS(Topic("with*wild"), std::invalid_argument);
    FP_CHECK_THROWS(Topic(""), std::invalid_argument);
    FP_CHECK_THROWS(Topic("a..b"), std::invalid_argument);
    FP_CHECK_THROWS(Topic("tab\there"), std::invalid_argument);

    const Topic ok("market.quote.BRK-B");
    FP_CHECK_EQ(ok.depth(), 3u);
    FP_CHECK_EQ(ok.segments().size(), 3u);
    FP_CHECK_EQ(ok.segments()[2], std::string("BRK-B"));
}

FP_TEST(topic, "超长主题名被拒绝") {
    const std::string huge(300, 'a');
    FP_CHECK_THROWS(Topic(huge), std::invalid_argument);
}
