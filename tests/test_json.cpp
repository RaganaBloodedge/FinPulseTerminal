#include "TestFramework.h"

#include "core/Json.h"

#include <limits>

using namespace fp;

FP_TEST(json, "解析标量") {
    FP_CHECK(Json::parse("null").is_null());
    FP_CHECK(Json::parse("true").as_bool());
    FP_CHECK(!Json::parse("false").as_bool());
    FP_CHECK_EQ(Json::parse("42").as_int(), 42);
    FP_CHECK_NEAR(Json::parse("3.5").as_double(), 3.5, 1e-12);
    FP_CHECK_EQ(Json::parse(R"("hi")").as_string(), std::string("hi"));
    FP_CHECK(Json::parse("17").is_integral());
    FP_CHECK(!Json::parse("17.0").is_integral());
}

FP_TEST(json, "解析嵌套结构") {
    const auto j = Json::parse(R"({
        "name": "AAPL",
        "bars": [1, 2, 3],
        "meta": {"ok": true, "nested": {"deep": [{"x": 1.5}]}}
    })");

    FP_CHECK(j.is_object());
    FP_CHECK_EQ(j["name"].as_string(), std::string("AAPL"));
    FP_CHECK_EQ(j["bars"].size(), 3u);
    FP_CHECK_EQ(j["bars"].at(1).as_int(), 2);
    FP_CHECK(j["meta"]["ok"].as_bool());
    FP_CHECK_NEAR(j["meta"]["nested"]["deep"].at(0)["x"].as_double(), 1.5, 1e-12);
}

FP_TEST(json, "空白与紧凑格式都能解") {
    FP_CHECK_EQ(Json::parse("  {  \"a\" :  1  }  ")["a"].as_int(), 1);
    FP_CHECK_EQ(Json::parse(R"({"a":1,"b":[true,null]})")["b"].at(1).is_null(), true);
}

FP_TEST(json, "字符串转义往返") {
    const std::string src = "line1\nline2\ttab\"quote\"\\back";
    Json j = Json::object();
    j.set("s", src);

    const std::string wire = j.dump();
    FP_CHECK(wire.find('\n') == std::string::npos);  // 换行必须被转义
    FP_CHECK_EQ(Json::parse(wire)["s"].as_string(), src);
}

FP_TEST(json, "控制字符被转义") {
    Json j = Json::object();
    j.set("s", std::string("\x01\x02"));
    const std::string wire = j.dump();
    FP_CHECK(wire.find("\\u0001") != std::string::npos);
    FP_CHECK(wire.find("\\u0002") != std::string::npos);
    FP_CHECK_EQ(Json::parse(wire)["s"].as_string(), std::string("\x01\x02"));
}

FP_TEST(json, "unicode 基本转义") {
    const auto j = Json::parse(R"("\u4e2d\u6587")");
    FP_CHECK_EQ(j.as_string(), std::string("中文"));
}

FP_TEST(json, "unicode 代理对合成四字节") {
    // U+1F4C8 📈 在 JSON 里是 \uD83D\uDCC8
    const auto j = Json::parse(R"("\uD83D\uDCC8")");
    FP_CHECK_EQ(j.as_string(), std::string("\xF0\x9F\x93\x88"));
}

FP_TEST(json, "中文直接透传不做转义") {
    Json j = Json::object();
    j.set("s", std::string("涨跌幅"));
    const std::string wire = j.dump();
    FP_CHECK(wire.find("\xE6\xB6\xA8") != std::string::npos);  // 原始 UTF-8 字节在
    FP_CHECK(wire.find("\\u") == std::string::npos);           // 没有被转义
    FP_CHECK_EQ(Json::parse(wire)["s"].as_string(), std::string("涨跌幅"));
}

FP_TEST(json, "非法输入抛 JsonError") {
    FP_CHECK_THROWS(Json::parse("{"), JsonError);
    FP_CHECK_THROWS(Json::parse("[1,]"), JsonError);
    FP_CHECK_THROWS(Json::parse(R"({"a":})"), JsonError);
    FP_CHECK_THROWS(Json::parse("nul"), JsonError);
    FP_CHECK_THROWS(Json::parse(R"("未闭合)"), JsonError);
    FP_CHECK_THROWS(Json::parse("{} extra"), JsonError);
    FP_CHECK_THROWS(Json::parse(""), JsonError);
}

FP_TEST(json, "错误带字节偏移") {
    try {
        Json::parse(R"({"a": @})");
        FP_CHECK(false);
    } catch (const JsonError& e) {
        FP_CHECK(e.offset() > 0);
    }
}

FP_TEST(json, "数值格式化保持最短往返") {
    FP_CHECK_EQ(Json(0.1).dump(), std::string("0.1"));
    FP_CHECK_EQ(Json(1.0 / 3.0).dump().substr(0, 5), std::string("0.333"));
    FP_CHECK_EQ(Json(42).dump(), std::string("42"));
    FP_CHECK_EQ(Json(-7).dump(), std::string("-7"));
    FP_CHECK_EQ(Json(2.5).dump(), std::string("2.5"));

    // 往返必须精确
    for (double v : {0.1, 1e-9, 123456.789, 1.0 / 3.0, 2.718281828459045}) {
        FP_CHECK_NEAR(Json::parse(Json(v).dump()).as_double(), v, 1e-15);
    }
}

FP_TEST(json, "特殊浮点退化为 null") {
    // JSON 规范里没有 NaN/Inf，写出去会让对端解析失败，所以退化成 null
    FP_CHECK_EQ(Json(std::nan("")).dump(), std::string("null"));
    FP_CHECK_EQ(Json(std::numeric_limits<double>::infinity()).dump(), std::string("null"));
}

FP_TEST(json, "字段缺失返回 null 单例可链式读取") {
    const auto j = Json::parse(R"({"a": {"b": 1}})");
    FP_CHECK(j["a"]["nonexistent"].is_null());
    FP_CHECK(j["missing"]["deeper"]["deepest"].is_null());  // 不抛异常
    FP_CHECK(j["a"]["b"].is_number());
}

FP_TEST(json, "类型不符抛 JsonTypeError") {
    const auto j = Json::parse(R"({"s": "text", "n": 5})");
    FP_CHECK_THROWS(j["s"].as_int(), JsonTypeError);
    FP_CHECK_THROWS(j["n"].as_string(), JsonTypeError);
    FP_CHECK_THROWS(j["n"].as_bool(), JsonTypeError);
    // 宽松取值不抛
    FP_CHECK_EQ(j["s"].as_int_or(99), 99);
}

FP_TEST(json, "数组越界抛 out_of_range") {
    const auto j = Json::parse("[1,2]");
    FP_CHECK_THROWS(j.at(5), std::out_of_range);
    // 对非数组调用下标访问是类型错误，不是越界
    FP_CHECK_THROWS(Json::parse("{}").at(0), JsonTypeError);
}

FP_TEST(json, "构造与写入") {
    Json j = Json::object();
    j.set("a", 1);
    j.set("a", 2);  // 覆盖
    FP_CHECK_EQ(j.size(), 1u);
    FP_CHECK_EQ(j["a"].as_int(), 2);

    Json arr = Json::array();
    arr.push(1);
    arr.push("x");
    FP_CHECK_EQ(arr.size(), 2u);
    FP_CHECK_EQ(arr.at(1).as_string(), std::string("x"));
}

FP_TEST(json, "缩进输出可被重新解析") {
    Json j = Json::object();
    j.set("n", 1);
    Json arr = Json::array();
    arr.push(1);
    arr.push(2);
    j.set("arr", arr);

    const std::string pretty = j.dump(2);
    FP_CHECK(pretty.find('\n') != std::string::npos);
    const auto back = Json::parse(pretty);
    FP_CHECK_EQ(back["arr"].size(), 2u);
    FP_CHECK_EQ(back["arr"].at(0).as_int(), 1);
}

FP_TEST(json, "深嵌套有上限保护") {
    std::string deep;
    for (int i = 0; i < 100; ++i) deep += "[";
    for (int i = 0; i < 100; ++i) deep += "]";
    // 100 层超过 kMaxDepth(64)，必须拒绝而不是把栈打爆
    FP_CHECK_THROWS(Json::parse(deep), JsonError);
}
