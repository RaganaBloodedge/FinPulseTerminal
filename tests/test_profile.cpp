// FinPulse Terminal — 启动配置档读写测试
//
// 配置档的失败模式很特别：**它错了不会崩，只会让用户觉得"我明明配了"**。
// 这是最难自查的一类问题 —— 没有报错、没有异常、程序照常启动，
// 只是打开的不是你要的那只票。
//
// 所以这里守的是三条边界：
//   1. 文件不存在 = 常态，不是错误（绝不能第一次启动就弹报错）；
//   2. 文件存在但**坏了** = 必须留下 error（绝不能静默忽略）；
//   3. 单个字段类型不对 = 只忽略那一项，其余照读（一个手滑不该让
//      token 和 watchlist 一起失效），但也要记一笔。
//
// 另外有一条只有在类 Unix 上才有意义的断言：写入后权限必须是 0600。
// token 就在这个文件里，0644 意味着同机器上任何账号都能读走它。

#include "TestFramework.h"

#include "core/Json.h"
#include "core/Profile.h"

#include <filesystem>
#include <fstream>
#include <string>

using namespace fp;

namespace {

/// 每个用例一个干净的目录。用不同名字而不是共用目录再清理：
/// 用例之间通过文件系统互相影响时，失败会表现为"单独跑能过、一起跑不行"。
std::filesystem::path scratch(const std::string& name) {
    const auto dir = std::filesystem::temp_directory_path() / "finpulse-tests" / name;
    std::error_code ec;
    std::filesystem::remove_all(dir, ec);
    std::filesystem::create_directories(dir, ec);
    return dir;
}

void write_raw(const std::filesystem::path& p, const std::string& text) {
    std::filesystem::create_directories(p.parent_path());
    std::ofstream out(p, std::ios::binary | std::ios::trunc);
    out << text;
}

std::string read_raw(const std::filesystem::path& p) {
    std::ifstream in(p, std::ios::binary);
    return std::string((std::istreambuf_iterator<char>(in)),
                       std::istreambuf_iterator<char>());
}

Profile sample() {
    Profile p;
    p.source        = "tushare";
    p.symbol        = "600519.SH";
    p.bars          = 250;
    p.csv_path      = "data/demo.csv";
    p.tushare_token = "token-abc123";
    p.watchlist     = {"600519.SH", "601318.SH"};
    p.llm_provider  = "deepseek";
    p.llm_model     = "deepseek-chat";
    p.llm_base_url  = "https://api.deepseek.com/v1";
    p.llm_api_key   = "sk-not-a-real-key";
    return p;
}

}  // namespace

// ── 不存在 / 空 ───────────────────────────────────────────

FP_TEST(profile, "文件不存在不是错误") {
    const auto path = scratch("missing") / "profile.json";
    const Profile p = load_profile(path.string());

    FP_CHECK(!p.loaded);
    FP_CHECK(p.error.empty());           // ← 关键：不能报错
    FP_CHECK(p.path == path.string());
    FP_CHECK(p.empty());
    FP_CHECK(!p.has_token());
    FP_CHECK(!p.has_llm_key());
}

FP_TEST(profile, "空配置档被判定为空") {
    FP_CHECK(Profile{}.empty());
    Profile p;
    p.bars = 1;                          // 只有一项非默认
    FP_CHECK(!p.empty());
}

FP_TEST(profile, "只配了密钥也不算空档") {
    // empty() 漏掉 llm_api_key 的后果是：一个只存了密钥的配置档会被
    // 调用方当成"没配过"整体忽略，于是密钥白存了。
    Profile p;
    p.llm_api_key = "sk-x";
    FP_CHECK(!p.empty());
    FP_CHECK(p.has_llm_key());
    FP_CHECK(!p.has_token());
}

// ── 往返 ──────────────────────────────────────────────────

FP_TEST(profile, "写入再读回逐字段保真") {
    const auto  path = scratch("roundtrip") / "profile.json";
    const Profile src = sample();

    FP_CHECK_EQ(save_profile(src, path.string()), std::string());

    const Profile got = load_profile(path.string());
    FP_CHECK(got.loaded);
    FP_CHECK(got.error.empty());
    FP_CHECK_EQ(got.source, src.source);
    FP_CHECK_EQ(got.symbol, src.symbol);
    FP_CHECK_EQ(got.bars, src.bars);
    FP_CHECK_EQ(got.csv_path, src.csv_path);
    FP_CHECK_EQ(got.tushare_token, src.tushare_token);
    FP_CHECK_EQ(got.llm_provider, src.llm_provider);
    FP_CHECK_EQ(got.llm_model, src.llm_model);
    FP_CHECK_EQ(got.llm_base_url, src.llm_base_url);
    FP_CHECK_EQ(got.llm_api_key, src.llm_api_key);
    FP_CHECK_EQ(got.watchlist.size(), std::size_t(2));
    FP_CHECK_EQ(got.watchlist[0], std::string("600519.SH"));
    FP_CHECK_EQ(got.watchlist[1], std::string("601318.SH"));
}

FP_TEST(profile, "保存会自动建出父目录") {
    // 首次使用的用户目录里根本没有 ~/.finpulse/。不自动建的话，
    // 第一次 --save-profile 就直接失败 —— 而那正是唯一一次需要它成功的时候。
    const auto path = scratch("mkdir") / "a" / "b" / "profile.json";
    FP_CHECK_EQ(save_profile(sample(), path.string()), std::string());
    FP_CHECK(std::filesystem::exists(path));
}

FP_TEST(profile, "空字段不落盘") {
    // 配置是给人看的。一堆 "" 会把"我到底配了什么"淹掉。
    const auto path = scratch("omit") / "profile.json";
    Profile p;
    p.source = "tushare";

    FP_CHECK_EQ(save_profile(p, path.string()), std::string());

    const std::string text = read_raw(path);
    FP_CHECK(text.find("tushare") != std::string::npos);
    FP_CHECK(text.find("llm_provider") == std::string::npos);
    FP_CHECK(text.find("csv_path") == std::string::npos);
    FP_CHECK(text.find("watchlist") == std::string::npos);
}

FP_TEST(profile, "覆盖写不会留下上一版的字段") {
    // save_profile 是**完整覆盖**而不是合并 —— 这是为了支持"把某项清空"。
    // 若旧值残留，用户在界面上取消了勾选也删不掉它。
    const auto path = scratch("overwrite") / "profile.json";
    FP_CHECK_EQ(save_profile(sample(), path.string()), std::string());

    Profile lean;
    lean.source = "csv";
    FP_CHECK_EQ(save_profile(lean, path.string()), std::string());

    const Profile got = load_profile(path.string());
    FP_CHECK_EQ(got.source, std::string("csv"));
    FP_CHECK(!got.has_token());
    FP_CHECK_EQ(got.llm_provider, std::string());
    FP_CHECK(!got.has_llm_key());
    FP_CHECK(got.watchlist.empty());
}

#ifndef _WIN32
FP_TEST(profile, "写入后权限收紧到仅本人可读") {
    // token 明文在这个文件里。0644 意味着同机器上任何账号都能读走。
    const auto path = scratch("perm") / "profile.json";
    FP_CHECK_EQ(save_profile(sample(), path.string()), std::string());

    std::error_code ec;
    const auto perms = std::filesystem::status(path, ec).permissions();
    FP_CHECK(!ec);
    FP_CHECK((perms & std::filesystem::perms::group_all) == std::filesystem::perms::none);
    FP_CHECK((perms & std::filesystem::perms::others_all) == std::filesystem::perms::none);
    FP_CHECK((perms & std::filesystem::perms::owner_read) != std::filesystem::perms::none);
}
#endif

// ── 坏文件必须报出来 ──────────────────────────────────────

FP_TEST(profile, "不是合法 JSON 时报错而不是当作没配过") {
    // 静默忽略的后果就是"我明明配了却不生效"。
    const auto path = scratch("badjson") / "profile.json";
    write_raw(path, "{ \"source\": \"tushare\", ");   // 截断

    const Profile p = load_profile(path.string());
    FP_CHECK(!p.loaded);
    FP_CHECK(!p.error.empty());
    FP_CHECK(p.error.find("JSON") != std::string::npos);
}

FP_TEST(profile, "顶层不是对象时报错") {
    const auto path = scratch("notobject") / "profile.json";
    write_raw(path, "[1, 2, 3]\n");

    const Profile p = load_profile(path.string());
    FP_CHECK(!p.loaded);
    FP_CHECK(p.error.find("对象") != std::string::npos);
}

// ── 单字段容错 ────────────────────────────────────────────

FP_TEST(profile, "字段类型错只忽略那一项") {
    // 手滑把一个字段写成数字，不该让 token 和 watchlist 一起失效。
    const auto path = scratch("fieldtype") / "profile.json";
    write_raw(path,
              "{\n"
              "  \"source\": 123,\n"               // 该忽略
              "  \"symbol\": \"600519.SH\",\n"     // 该保留
              "  \"tushare_token\": \"tok\",\n"     // 该保留
              "  \"llm_provider\": null\n"          // null = 没设置，不算错
              "}\n");

    const Profile p = load_profile(path.string());
    FP_CHECK(p.loaded);
    FP_CHECK_EQ(p.source, std::string());            // 被忽略
    FP_CHECK_EQ(p.symbol, std::string("600519.SH")); // 保住了
    FP_CHECK_EQ(p.tushare_token, std::string("tok"));// 保住了
    FP_CHECK_EQ(p.llm_provider, std::string());
    FP_CHECK(p.error.find("source") != std::string::npos);
    FP_CHECK(p.error.find("llm_provider") == std::string::npos);  // null 不该记问题
}

FP_TEST(profile, "bars 非正数被忽略并记一笔") {
    const auto path = scratch("bars") / "profile.json";
    write_raw(path, "{ \"bars\": -5, \"symbol\": \"600519.SH\" }\n");

    const Profile p = load_profile(path.string());
    FP_CHECK(p.loaded);
    FP_CHECK_EQ(p.bars, std::size_t(0));
    FP_CHECK_EQ(p.symbol, std::string("600519.SH"));
    FP_CHECK(p.error.find("bars") != std::string::npos);
}

FP_TEST(profile, "watchlist 不是数组时忽略并记一笔") {
    const auto path = scratch("watch") / "profile.json";
    write_raw(path, "{ \"watchlist\": \"600519.SH\", \"source\": \"tushare\" }\n");

    const Profile p = load_profile(path.string());
    FP_CHECK(p.loaded);
    FP_CHECK(p.watchlist.empty());
    FP_CHECK_EQ(p.source, std::string("tushare"));
    FP_CHECK(p.error.find("watchlist") != std::string::npos);
}

FP_TEST(profile, "watchlist 只收非空字符串") {
    // 混进来 null / 数字 / 空串都是常事（手编 JSON 的时候）。
    // 它们必须被丢掉，而不是变成"代码是空串"的标的去拉数据。
    const auto path = scratch("watchmix") / "profile.json";
    write_raw(path,
              "{ \"watchlist\": [\"600519.SH\", \"\", null, 42, \"601318.SH\"] }\n");

    const Profile p = load_profile(path.string());
    FP_CHECK(p.loaded);
    FP_CHECK_EQ(p.watchlist.size(), std::size_t(2));
    FP_CHECK_EQ(p.watchlist[0], std::string("600519.SH"));
    FP_CHECK_EQ(p.watchlist[1], std::string("601318.SH"));
}

FP_TEST(profile, "空 watchlist 数组也记一笔") {
    // 一个空数组和"没写"在语义上不同：前者多半是编辑坏了一半。
    const auto path = scratch("watchempty") / "profile.json";
    write_raw(path, "{ \"watchlist\": [] }\n");

    const Profile p = load_profile(path.string());
    FP_CHECK(p.loaded);
    FP_CHECK(p.watchlist.empty());
    FP_CHECK(p.error.find("watchlist") != std::string::npos);
}

FP_TEST(profile, "多个字段出问题时问题串用分号连起来") {
    // 只报第一个的话，用户改完一个发现还有下一个，来回好几轮。
    const auto path = scratch("multi") / "profile.json";
    write_raw(path, "{ \"bars\": 0, \"watchlist\": 7 }\n");

    const Profile p = load_profile(path.string());
    FP_CHECK(p.error.find("bars") != std::string::npos);
    FP_CHECK(p.error.find("watchlist") != std::string::npos);
    FP_CHECK(p.error.find("；") != std::string::npos);
}
