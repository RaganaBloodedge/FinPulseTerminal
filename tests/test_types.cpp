#include "TestFramework.h"

#include "model/Types.h"

#include <string>

using namespace fp;

// ── 时间 ──────────────────────────────────────────────────

FP_TEST(types, "日期往返") {
    const Timestamp ts = parse_date("2024-03-15");
    FP_CHECK(ts > 0);
    FP_CHECK_EQ(format_date(ts), std::string("2024-03-15"));
    FP_CHECK_EQ(format_datetime(ts), std::string("2024-03-15 00:00:00"));
}

FP_TEST(types, "纪元起点") {
    // 0 是合法时间戳（epoch 本身），所以解析失败必须用另一个哨兵值，
    // 不能用 0 —— 否则 1970-01-01 会被误判成"解析失败"。
    FP_CHECK_EQ(parse_date("1970-01-01"), 0LL);
    FP_CHECK_EQ(parse_date("1970-01-02"), 86400000LL);
}

FP_TEST(types, "非法日期一律返回 -1") {
    FP_CHECK_EQ(parse_date(""), -1LL);
    FP_CHECK_EQ(parse_date("not-a-date"), -1LL);
    FP_CHECK_EQ(parse_date("2024-13-01"), -1LL);   // 月份越界
    FP_CHECK_EQ(parse_date("2024-00-10"), -1LL);   // 月份为 0
    FP_CHECK_EQ(parse_date("2024-05-32"), -1LL);   // 日越界
    FP_CHECK_EQ(parse_date("24-05-01"), -1LL);     // 两位年份不接受
    FP_CHECK_EQ(parse_date("1899-12-31"), -1LL);   // 早于合理范围
}

// ── Quote ─────────────────────────────────────────────────

FP_TEST(types, "quote 涨跌计算") {
    Quote q;
    q.symbol     = "AAPL";
    q.last       = 110.0;
    q.prev_close = 100.0;
    FP_CHECK_NEAR(q.change(), 10.0, 1e-9);
    FP_CHECK_NEAR(q.change_pct(), 10.0, 1e-9);

    q.last = 95.0;
    FP_CHECK_NEAR(q.change(), -5.0, 1e-9);
    FP_CHECK_NEAR(q.change_pct(), -5.0, 1e-9);
}

FP_TEST(types, "quote 前收为 0 不除零") {
    Quote q;
    q.last = 50.0;
    q.prev_close = 0.0;
    FP_CHECK_NEAR(q.change_pct(), 0.0, 1e-12);
}

FP_TEST(types, "quote JSON 往返") {
    Quote q;
    q.symbol     = "BRK-B";
    q.ts_ms      = 1700000000000LL;
    q.last       = 371.25;
    q.prev_close = 369.0;
    q.open       = 370.0;
    q.high       = 372.5;
    q.low        = 368.75;
    q.volume     = 3456789;

    const Json  j    = q.to_json();
    const Quote back = Quote::from_json(j);

    FP_CHECK_EQ(back.symbol, q.symbol);
    FP_CHECK_EQ(back.ts_ms, q.ts_ms);
    FP_CHECK_NEAR(back.last, q.last, 1e-9);
    FP_CHECK_NEAR(back.prev_close, q.prev_close, 1e-9);
    FP_CHECK_EQ(back.volume, q.volume);

    // 派生字段也随 payload 一起发出，免得每个订阅者各算一遍
    FP_CHECK(j.has("change"));
    FP_CHECK(j.has("change_pct"));
}

FP_TEST(types, "quote 缺字段时回落") {
    Json j = Json::object();
    j.set("symbol", "X");
    j.set("last", 10.0);
    const Quote q = Quote::from_json(j);
    FP_CHECK_EQ(q.symbol, std::string("X"));
    FP_CHECK_NEAR(q.prev_close, 10.0, 1e-9);  // 缺前收时退化成最新价，涨跌为 0
    FP_CHECK_NEAR(q.change_pct(), 0.0, 1e-9);
}

// ── Candle ────────────────────────────────────────────────

FP_TEST(types, "candle 派生量") {
    Candle c;
    c.open  = 10.0;
    c.high  = 12.0;
    c.low   = 9.0;
    c.close = 11.0;

    FP_CHECK_NEAR(c.range(), 3.0, 1e-9);
    FP_CHECK_NEAR(c.typical(), (12.0 + 9.0 + 11.0) / 3.0, 1e-9);
    FP_CHECK(c.is_bullish());

    c.close = 9.5;
    FP_CHECK(!c.is_bullish());
}

FP_TEST(types, "candle JSON 往返") {
    Candle c;
    c.ts_ms  = 1700000000000LL;
    c.open   = 100.5;
    c.high   = 103.25;
    c.low    = 99.75;
    c.close  = 102.0;
    c.volume = 1234567;

    const Candle back = Candle::from_json(c.to_json());
    FP_CHECK_EQ(back.ts_ms, c.ts_ms);
    FP_CHECK_NEAR(back.open, c.open, 1e-9);
    FP_CHECK_NEAR(back.high, c.high, 1e-9);
    FP_CHECK_NEAR(back.low, c.low, 1e-9);
    FP_CHECK_NEAR(back.close, c.close, 1e-9);
    FP_CHECK_EQ(back.volume, c.volume);
}

// ── CandleSeries ──────────────────────────────────────────

FP_TEST(types, "序列归一化按时间排序") {
    CandleSeries cs("T");
    cs.push(Candle{3000, 3, 3, 3, 3, 0});
    cs.push(Candle{1000, 1, 1, 1, 1, 0});
    cs.push(Candle{2000, 2, 2, 2, 2, 0});
    cs.normalize();

    FP_CHECK_EQ(cs.size(), 3u);
    FP_CHECK_EQ(cs.at(0).ts_ms, 1000LL);
    FP_CHECK_EQ(cs.at(1).ts_ms, 2000LL);
    FP_CHECK_EQ(cs.at(2).ts_ms, 3000LL);
}

FP_TEST(types, "序列归一化保留最后一条重复") {
    // CSV 是追加写的，同一天重写时新行在末尾 —— 应当由新行覆盖旧行。
    // 用 std::unique 会得到相反结果，所以这条专门测。
    CandleSeries cs("T");
    cs.push(Candle{1000, 1, 1, 1, 1.0, 0});
    cs.push(Candle{2000, 2, 2, 2, 2.0, 0});
    cs.push(Candle{2000, 9, 9, 9, 9.0, 0});  // 重复，应胜出
    cs.push(Candle{3000, 3, 3, 3, 3.0, 0});
    cs.normalize();

    FP_CHECK_EQ(cs.size(), 3u);
    FP_CHECK_NEAR(cs.at(1).close, 9.0, 1e-9);
    FP_CHECK_NEAR(cs.at(1).open, 9.0, 1e-9);
}

FP_TEST(types, "序列校验能发现问题") {
    CandleSeries cs("T");
    cs.push(Candle{1000, 10, 9, 11, 10, 0});  // high(9) < low(11)
    const auto issues = cs.validate();
    FP_CHECK(!issues.empty());
}

FP_TEST(types, "序列校验通过干净数据") {
    CandleSeries cs("T");
    cs.push(Candle{1000, 10, 12, 9, 11, 100});
    cs.push(Candle{2000, 11, 13, 10, 12, 120});
    FP_CHECK_EQ(cs.validate().size(), 0u);
}

FP_TEST(types, "空序列校验给出提示") {
    CandleSeries cs("T");
    const auto   issues = cs.validate();
    FP_CHECK_EQ(issues.size(), 1u);
}

FP_TEST(types, "时间戳未严格递增会被发现") {
    CandleSeries cs("T");
    cs.push(Candle{2000, 10, 12, 9, 11, 0});
    cs.push(Candle{2000, 11, 13, 10, 12, 0});  // 与上一根同级
    const auto issues = cs.validate();
    FP_CHECK(!issues.empty());
}

FP_TEST(types, "closes 与切片") {
    CandleSeries cs("T");
    for (int i = 0; i < 10; ++i) {
        cs.push(Candle{1000LL * (i + 1), 0, 0, 0, static_cast<double>(i), 0});
    }

    const auto cl = cs.closes();
    FP_CHECK_EQ(cl.size(), 10u);
    FP_CHECK_NEAR(cl[9], 9.0, 1e-9);

    const auto sl = cs.close_slice(2, 4);
    FP_CHECK_EQ(sl.size(), 4u);
    FP_CHECK_NEAR(sl[0], 2.0, 1e-9);
    FP_CHECK_NEAR(sl[3], 5.0, 1e-9);

    // 越界的切片返回空而不是抛
    FP_CHECK_EQ(cs.close_slice(100, 4).size(), 0u);
    // 跨过末尾时截断
    FP_CHECK_EQ(cs.close_slice(8, 100).size(), 2u);
}

FP_TEST(types, "back 取倒数第 n 根") {
    CandleSeries cs("T");
    for (int i = 0; i < 5; ++i) {
        cs.push(Candle{1000LL * (i + 1), 0, 0, 0, static_cast<double>(i), 0});
    }
    FP_CHECK_NEAR(cs.back().close, 4.0, 1e-9);
    FP_CHECK_NEAR(cs.back(1).close, 3.0, 1e-9);
    FP_CHECK_EQ(cs.back().ts_ms, 5000LL);
}

FP_TEST(types, "序列 JSON 往返") {
    CandleSeries cs("AAPL");
    cs.push(Candle{1000, 1, 2, 0.5, 1.5, 10});
    cs.push(Candle{2000, 1.5, 2.5, 1.0, 2.0, 20});

    const Json        j    = cs.to_json();
    const CandleSeries back = CandleSeries::from_json(j);

    FP_CHECK_EQ(back.symbol(), std::string("AAPL"));
    FP_CHECK_EQ(back.size(), 2u);
    FP_CHECK_NEAR(back.at(1).close, 2.0, 1e-9);
    FP_CHECK_EQ(j["count"].as_int(), 2LL);
}

// ── 预测与回测结果 ────────────────────────────────────────

FP_TEST(types, "预测结果往返") {
    ForecastResult r;
    r.method     = "ar";
    r.symbol     = "SYNTH";
    r.last_close = 100.0;
    r.points.push_back(ForecastPoint{1000, 101.0, 99.0, 103.0});
    r.points.push_back(ForecastPoint{2000, 102.0, 98.5, 105.5});
    r.meta = Json::object();
    r.meta.set("order", 3);

    const ForecastResult back = ForecastResult::from_json(r.to_json());
    FP_CHECK_EQ(back.method, std::string("ar"));
    FP_CHECK_EQ(back.points.size(), 2u);
    FP_CHECK_NEAR(back.points[1].value, 102.0, 1e-9);
    FP_CHECK_NEAR(back.points[0].lower, 99.0, 1e-9);
    FP_CHECK_EQ(back.meta["order"].as_int(), 3LL);
}

FP_TEST(types, "预测点缺区间时退化为点估计") {
    Json r   = Json::object();
    Json arr = Json::array();
    Json p   = Json::object();
    p.set("ts", 1000);
    p.set("value", 42.5);
    arr.push(p);
    r.set("points", arr);

    const ForecastResult fr = ForecastResult::from_json(r);
    FP_CHECK_EQ(fr.points.size(), 1u);
    FP_CHECK_NEAR(fr.points[0].value, 42.5, 1e-9);
    FP_CHECK_NEAR(fr.points[0].lower, 42.5, 1e-9);
    FP_CHECK_NEAR(fr.points[0].upper, 42.5, 1e-9);
}

FP_TEST(types, "回测指标往返") {
    BacktestMetrics m;
    m.method       = "ar";
    m.folds        = 5;
    m.horizon      = 5;
    m.mae          = 1.23;
    m.rmse         = 2.34;
    m.mape         = 3.45;
    m.dir_acc      = 52.0;
    m.base_mae     = 1.30;
    m.base_rmse    = 2.40;
    m.base_dir_acc = 50.0;
    m.skill        = 0.0321;

    const BacktestMetrics back = BacktestMetrics::from_json(m.to_json());
    FP_CHECK_EQ(back.method, std::string("ar"));
    FP_CHECK_EQ(back.folds, 5u);
    FP_CHECK_EQ(back.horizon, 5u);
    FP_CHECK_NEAR(back.rmse, 2.34, 1e-9);
    FP_CHECK_NEAR(back.skill, 0.0321, 1e-9);
}
