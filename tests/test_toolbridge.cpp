#include "TestFramework.h"

#include "agent/ToolBridge.h"
#include "core/DataHub.h"

#include <algorithm>
#include <chrono>
#include <cstdlib>
#include <string>

#if defined(_WIN32)
#  ifndef WIN32_LEAN_AND_MEAN
#    define WIN32_LEAN_AND_MEAN
#  endif
#  include <winsock2.h>
#  include <ws2tcpip.h>
using test_socket_t = SOCKET;
#else
#  include <arpa/inet.h>
#  include <netinet/in.h>
#  include <sys/socket.h>
#  include <unistd.h>
using test_socket_t = int;
#endif

using namespace fp;

namespace {

// ── 一个 60 行的最小 HTTP 客户端 ──
//
// 存在的理由：`handle_http_request()` 的离线测试验证不了 bind/listen/accept
// 这一整段。而 Python 侧用的是真正的 urllib，它要面对的是真 socket。
// 只测解析器会漏掉"服务端根本没起来"这种最致命的故障。

struct HttpResponse {
    int         status{0};
    std::string body;
};

/// 发一条 HTTP 请求到 127.0.0.1:port，返回状态码与响应体。
/// 连不上时 status = 0。
HttpResponse http_roundtrip(std::uint16_t port, const std::string& raw_request) {
    HttpResponse out;
#if defined(_WIN32)
    static bool wsa = [] {
        WSADATA d{};
        return WSAStartup(MAKEWORD(2, 2), &d) == 0;
    }();
    if (!wsa) return out;
#endif

    test_socket_t s = ::socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
    if (s == static_cast<test_socket_t>(-1)
#if defined(_WIN32)
        || s == INVALID_SOCKET
#endif
    ) {
        return out;
    }

    sockaddr_in addr{};
    addr.sin_family      = AF_INET;
    addr.sin_port        = ::htons(port);
    addr.sin_addr.s_addr = ::htonl(INADDR_LOOPBACK);
    if (::connect(s, reinterpret_cast<sockaddr*>(&addr), sizeof(addr)) != 0) {
#if defined(_WIN32)
        ::closesocket(s);
#else
        ::close(s);
#endif
        return out;
    }

    std::size_t sent = 0;
    while (sent < raw_request.size()) {
        const int n = ::send(s, raw_request.data() + sent,
                             static_cast<int>(raw_request.size() - sent), 0);
        if (n <= 0) break;
        sent += static_cast<std::size_t>(n);
    }

    std::string raw;
    char buf[4096];
    for (;;) {
        const int n = ::recv(s, buf, static_cast<int>(sizeof(buf)), 0);
        if (n <= 0) break;
        raw.append(buf, static_cast<std::size_t>(n));
    }
#if defined(_WIN32)
    ::closesocket(s);
#else
    ::close(s);
#endif

    const auto first_space = raw.find(' ');
    if (first_space != std::string::npos) {
        out.status = std::atoi(raw.c_str() + first_space + 1);
    }
    const auto sep = raw.find("\r\n\r\n");
    if (sep != std::string::npos) out.body = raw.substr(sep + 4);
    return out;
}

std::string post_tool(const std::string& token, const std::string& name,
                      const std::string& args_json = "{}",
                      const std::string& host = "127.0.0.1") {
    const std::string body = "{\"name\":\"" + name + "\",\"arguments\":" + args_json + "}";
    return "POST /tool HTTP/1.1\r\nHost: " + host +
           "\r\nX-FinPulse-Token: " + token +
           "\r\nContent-Type: application/json\r\nContent-Length: " +
           std::to_string(body.size()) + "\r\n\r\n" + body;
}

std::string get_tools(const std::string& token,
                      const std::string& host = "127.0.0.1") {
    return "GET /tools HTTP/1.1\r\nHost: " + host +
           "\r\nX-FinPulse-Token: " + token + "\r\n\r\n";
}

Json json_of(const std::string& text) {
    try {
        return Json::parse(text);
    } catch (const std::exception&) {
        return Json::object();
    }
}

/// 就地装配一个自包含的桥：注册默认工具 + 注入 provider。
///
/// 不返回 ToolBridge：它持有线程与 socket，有用户声明的析构函数，
/// 因此既不可拷贝也不可移动 —— 值返回会编译不过（这是对的，
/// 让一个正在监听的桥被移动走本来就不合理）。
void setup_bridge(ToolBridge& bridge) {
    bridge.register_default_tools();
    bridge.set_quote_provider([](const std::string& symbol) {
        Json q = Json::object();
        q.set("symbol", symbol.empty() ? "SYNTH" : symbol);
        q.set("last", 123.5);
        q.set("change_pct", 1.25);
        return q;
    });
    bridge.set_series_provider([](const std::string& symbol) {
        Json s = Json::object();
        s.set("symbol", symbol.empty() ? "SYNTH" : symbol);
        s.set("bars", 250);
        s.set("issues", Json::array());
        return s;
    });
}

}  // namespace

// ── 工具注册 ──────────────────────────────────────────────────────

FP_TEST(toolbridge, "默认工具都已注册") {
    DataHub    hub;
    ToolBridge bridge(&hub);
    setup_bridge(bridge);

    const auto names = bridge.tool_names();
    FP_CHECK_EQ(names.size(), 4u);
    for (const char* expected : {"terminal.bus_stats", "terminal.data_quality",
                                 "terminal.live_quote", "terminal.subscriptions"}) {
        FP_CHECK(std::find(names.begin(), names.end(), expected) != names.end());
    }
    // 幂等：重复注册不该抛，也不该变成 8 个。
    bridge.register_default_tools();
    FP_CHECK_EQ(bridge.tool_count(), 4u);
}

FP_TEST(toolbridge, "工具名重复注册被拒绝") {
    DataHub    hub;
    ToolBridge bridge(&hub);
    ToolBridge::Tool t;
    t.name        = "terminal.test";
    t.description = "d";
    t.handler     = [](const Json&) { return Json::object(); };
    bridge.add_tool(t);
    FP_CHECK_THROWS(bridge.add_tool(t), std::invalid_argument);

    ToolBridge::Tool empty;
    empty.name    = "";
    empty.handler = [](const Json&) { return Json::object(); };
    FP_CHECK_THROWS(bridge.add_tool(empty), std::invalid_argument);

    ToolBridge::Tool no_impl;
    no_impl.name = "terminal.x";
    FP_CHECK_THROWS(bridge.add_tool(no_impl), std::invalid_argument);
}

FP_TEST(toolbridge, "白名单之外的工具不派发") {
    // 正向白名单：注册了不等于会出现在模型面前。
    FP_CHECK(ToolBridge::is_exposed("terminal.bus_stats"));
    FP_CHECK(ToolBridge::is_exposed("terminal.live_quote"));
    FP_CHECK(!ToolBridge::is_exposed("terminal.delete_everything"));
    FP_CHECK(!ToolBridge::is_exposed(""));

    DataHub    hub;
    ToolBridge bridge(&hub);
    ToolBridge::Tool t;
    t.name        = "terminal.not_exposed";
    t.description = "不该被派发";
    t.handler     = [](const Json&) { return Json::object(); };
    bridge.add_tool(t);

    const std::string resp =
        bridge.handle_http_request(post_tool(bridge.token(), "terminal.not_exposed"));
    FP_CHECK(resp.find("404") != std::string::npos);
    FP_CHECK_EQ(bridge.stats().unknown_tools, 1u);
}

// ── 鉴权 ──────────────────────────────────────────────────────────

FP_TEST(toolbridge, "没有令牌一律 401") {
    DataHub    hub;
    ToolBridge bridge(&hub);
    setup_bridge(bridge);

    const std::string no_token =
        "GET /tools HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n";
    std::string resp = bridge.handle_http_request(no_token);
    FP_CHECK(resp.find("401") != std::string::npos);
    FP_CHECK(Json::parse(resp.substr(resp.find("\r\n\r\n") + 4))["error"]
                 .as_string_or("")
                 .find("缺少") != std::string::npos);

    resp = bridge.handle_http_request(get_tools("deadbeef"));
    FP_CHECK(resp.find("401") != std::string::npos);
    FP_CHECK_EQ(bridge.stats().rejected_auth, 2u);
}

FP_TEST(toolbridge, "进程令牌可以调所有工具") {
    DataHub    hub;
    ToolBridge bridge(&hub);
    setup_bridge(bridge);
    const std::string token = bridge.token();
    FP_CHECK_EQ(token.size(), 32u);
    FP_CHECK(token.find_first_not_of("0123456789abcdef") == std::string::npos);

    const std::string resp =
        bridge.handle_http_request(post_tool(token, "terminal.bus_stats"));
    FP_CHECK(resp.find("200") != std::string::npos);
    const Json body = Json::parse(resp.substr(resp.find("\r\n\r\n") + 4));
    FP_CHECK(body["ok"].as_bool_or(false));
    FP_CHECK(body["result"].has("published"));
}

FP_TEST(toolbridge, "作用域令牌可限定工具集") {
    DataHub    hub;
    ToolBridge bridge(&hub);
    setup_bridge(bridge);

    const auto scoped = bridge.issue_token({"terminal.bus_stats"}, "仅统计");
    FP_CHECK_EQ(scoped.remaining, 256);            ///< 默认上限
    FP_CHECK_EQ(bridge.stats().active_tokens, 1u);

    // 允许的那个能过。
    std::string resp =
        bridge.handle_http_request(post_tool(scoped.token, "terminal.bus_stats"));
    FP_CHECK(resp.find("200") != std::string::npos);

    // 没授权的那个被拒。注意：**不能**回 404 —— 那等于告诉调用方
    // "这个工具存在，只是你没权限"，凭这个就能把目录枚举出来。
    resp = bridge.handle_http_request(post_tool(scoped.token, "terminal.live_quote"));
    FP_CHECK(resp.find("401") != std::string::npos);
    FP_CHECK(resp.find("404") == std::string::npos);
}

FP_TEST(toolbridge, "作用域令牌次数用尽后失效") {
    DataHub         hub;
    ToolBridgeConfig cfg;
    cfg.token_max_uses = 2;
    ToolBridge bridge(&hub, cfg);
    setup_bridge(bridge);

    const auto scoped = bridge.issue_token({}, "只准两次");
    FP_CHECK(bridge.handle_http_request(post_tool(scoped.token, "terminal.bus_stats"))
                 .find("200") != std::string::npos);
    FP_CHECK(bridge.handle_http_request(post_tool(scoped.token, "terminal.bus_stats"))
                 .find("200") != std::string::npos);
    // 第三次应当被拒。
    FP_CHECK(bridge.handle_http_request(post_tool(scoped.token, "terminal.bus_stats"))
                 .find("401") != std::string::npos);
}

FP_TEST(toolbridge, "吊销后的令牌立刻失效") {
    DataHub    hub;
    ToolBridge bridge(&hub);
    setup_bridge(bridge);
    const auto scoped = bridge.issue_token({}, "待吊销");
    FP_CHECK(bridge.revoke_token(scoped.token));
    FP_CHECK(!bridge.revoke_token(scoped.token));   // 重复吊销返回 false，不抛
    FP_CHECK(bridge.handle_http_request(post_tool(scoped.token, "terminal.bus_stats"))
                 .find("401") != std::string::npos);
}

// ── DNS rebinding 防护 ────────────────────────────────────────────

FP_TEST(toolbridge, "非回环 Host 被拒") {
    DataHub    hub;
    ToolBridge bridge(&hub);
    setup_bridge(bridge);

    // 攻击者控制的域名指向 127.0.0.1 时，请求确实落在回环上，
    // 只绑回环挡不住 —— 所以要看 Host。
    std::string resp =
        bridge.handle_http_request(get_tools(bridge.token(), "evil.example.com"));
    FP_CHECK(resp.find("403") != std::string::npos);
    FP_CHECK_EQ(bridge.stats().rejected_host, 1u);
    FP_CHECK(bridge.last_reject_reason().find("evil.example.com") != std::string::npos);

    // 各种合法的回环写法都要放行。
    for (const char* host : {"127.0.0.1", "localhost", "127.0.0.1:8080",
                             "[::1]:8080", "::1"}) {
        resp = bridge.handle_http_request(get_tools(bridge.token(), host));
        FP_CHECK(resp.find("200") != std::string::npos);
    }
}

// ── 路由与错误 ────────────────────────────────────────────────────

FP_TEST(toolbridge, "GET tools 只列出白名单内的工具") {
    DataHub    hub;
    ToolBridge bridge(&hub);
    bridge.register_default_tools();

    ToolBridge::Tool hidden;
    hidden.name        = "terminal.hidden";
    hidden.description = "注册了但不在白名单里";
    hidden.handler     = [](const Json&) { return Json::object(); };
    bridge.add_tool(hidden);

    const std::string resp =
        bridge.handle_http_request(get_tools(bridge.token()));
    FP_CHECK(resp.find("200") != std::string::npos);
    const Json body = Json::parse(resp.substr(resp.find("\r\n\r\n") + 4));
    FP_CHECK_EQ(body["count"].as_int_or(0), 4);
    for (const Json& t : body["tools"].items()) {
        FP_CHECK(ToolBridge::is_exposed(t["name"].as_string_or("")));
        FP_CHECK(t.has("parameters"));
    }
}

FP_TEST(toolbridge, "未知路径与坏请求") {
    DataHub    hub;
    ToolBridge bridge(&hub);
    setup_bridge(bridge);

    std::string resp = bridge.handle_http_request(
        "GET /secret HTTP/1.1\r\nHost: 127.0.0.1\r\nX-FinPulse-Token: " +
        bridge.token() + "\r\n\r\n");
    FP_CHECK(resp.find("404") != std::string::npos);

    // 头不完整
    resp = bridge.handle_http_request("GET /tools");
    FP_CHECK(resp.find("400") != std::string::npos);

    // 请求行缺字段
    resp = bridge.handle_http_request("bad\r\nHost: 127.0.0.1\r\n\r\n");
    FP_CHECK(resp.find("400") != std::string::npos);

    // HTTP/2 不支持
    resp = bridge.handle_http_request(
        "GET /tools HTTP/2\r\nHost: 127.0.0.1\r\n\r\n");
    FP_CHECK(resp.find("400") != std::string::npos);

    // body 不是 JSON
    resp = bridge.handle_http_request(
        "POST /tool HTTP/1.1\r\nHost: 127.0.0.1\r\nX-FinPulse-Token: " +
        bridge.token() + "\r\nContent-Length: 3\r\n\r\nxxx");
    FP_CHECK(resp.find("400") != std::string::npos);

    // 缺 name
    resp = bridge.handle_http_request(
        "POST /tool HTTP/1.1\r\nHost: 127.0.0.1\r\nX-FinPulse-Token: " +
        bridge.token() + "\r\nContent-Length: 2\r\n\r\n{}");
    FP_CHECK(resp.find("400") != std::string::npos);

    FP_CHECK(bridge.stats().rejected_bad_request >= 4u);
}

FP_TEST(toolbridge, "工具失败与桥不可用是两件事") {
    DataHub hub;
    // 故意**不注入** provider：桥本身是好的，但终端没有提供那样数据。
    // 这两种状态必须能被区分开，否则调用方不知道该重试还是该修配置。
    ToolBridge fresh(&hub);
    fresh.register_default_tools();

    const std::string resp =
        fresh.handle_http_request(post_tool(fresh.token(), "terminal.live_quote"));
    FP_CHECK(resp.find("200") != std::string::npos);   // 桥是好的
    const Json body = Json::parse(resp.substr(resp.find("\r\n\r\n") + 4));
    FP_CHECK(body["ok"].as_bool_or(false));            // 调用成功
    // 但结果如实说明"终端没提供"，而不是返回空对象假装查过了。
    FP_CHECK_EQ(body["result"]["available"].as_bool_or(true), false);
    FP_CHECK(body["result"]["reason"].as_string_or("").find("未注入") !=
             std::string::npos);
}

FP_TEST(toolbridge, "provider 返回空时报不可用并说明原因") {
    DataHub    hub;
    ToolBridge bridge(&hub);
    bridge.register_default_tools();
    bridge.set_quote_provider([](const std::string&) { return Json::object(); });

    const std::string resp =
        bridge.handle_http_request(post_tool(bridge.token(), "terminal.live_quote",
                                            "{\"symbol\":\"600519\"}"));
    const Json body = Json::parse(resp.substr(resp.find("\r\n\r\n") + 4));
    FP_CHECK_EQ(body["result"]["available"].as_bool_or(true), false);
    FP_CHECK(body["result"]["reason"].as_string_or("").find("600519") !=
             std::string::npos);
}

// ── 真实 socket 往返 ──────────────────────────────────────────────

FP_TEST(toolbridge, "真实监听端口可用") {
    DataHub hub;
    // 先建订阅再发，这样 bus_stats 里 unmatched 应当是 0。
    Subscription sub(&hub, hub.subscribe("market.quote.**",
                                         [](const Topic&, const Json&) {},
                                         "测试订阅"));
    hub.publish(Topic::unchecked("market.quote.SYNTH"), Json::object());

    ToolBridge bridge(&hub);
    setup_bridge(bridge);
    bridge.start();   // port = 0 → 内核挑端口
    FP_CHECK(bridge.running());
    FP_CHECK(bridge.port() != 0);
    FP_CHECK_EQ(bridge.endpoint(),
                "http://127.0.0.1:" + std::to_string(bridge.port()));

    // 1) 目录
    HttpResponse r = http_roundtrip(bridge.port(), get_tools(bridge.token()));
    FP_CHECK_EQ(r.status, 200);
    const Json tools = json_of(r.body);
    FP_CHECK(tools["count"].as_int_or(0) > 0);

    // 2) 无令牌
    r = http_roundtrip(bridge.port(), "GET /tools HTTP/1.1\r\nHost: 127.0.0.1\r\n\r\n");
    FP_CHECK_EQ(r.status, 401);

    // 3) 调用工具，且结果真的是从 DataHub 读出来的
    r = http_roundtrip(bridge.port(),
                       post_tool(bridge.token(), "terminal.bus_stats"));
    FP_CHECK_EQ(r.status, 200);
    const Json stats = json_of(r.body);
    FP_CHECK(stats["ok"].as_bool_or(false));
    FP_CHECK(stats["result"]["published"].as_int_or(0) >= 1);
    FP_CHECK(stats["result"]["active_subscriptions"].as_int_or(0) >= 1);
    FP_CHECK_EQ(stats["result"]["unmatched"].as_int_or(-1), 0);

    // 4) 行情快照走的是注入的 provider
    r = http_roundtrip(bridge.port(),
                       post_tool(bridge.token(), "terminal.live_quote",
                                 "{\"symbol\":\"600519\"}"));
    FP_CHECK_EQ(r.status, 200);
    const Json quote = json_of(r.body);
    FP_CHECK(quote["result"]["available"].as_bool_or(false));
    FP_CHECK_NEAR(quote["result"]["last"].as_double_or(0.0), 123.5, 1e-9);

    // 5) stop() 之后端口应当不再接受连接
    bridge.stop();
    FP_CHECK(!bridge.running());
    FP_CHECK_EQ(http_roundtrip(bridge.port(), get_tools(bridge.token())).status, 0);
}

FP_TEST(toolbridge, "超限请求被拒而不是吃内存") {
    DataHub hub;
    ToolBridgeConfig cfg;
    cfg.max_header_bytes = 1024;
    cfg.max_body_bytes   = 4096;
    ToolBridge bridge(&hub, cfg);
    setup_bridge(bridge);
    bridge.start();

    // 声明一个超过上限的 Content-Length。**不真的发那么多字节** ——
    // 那样测试本身就不安全了。服务端应当在读完头之后就拒绝。
    const std::string req =
        "POST /tool HTTP/1.1\r\nHost: 127.0.0.1\r\nX-FinPulse-Token: " +
        bridge.token() + "\r\nContent-Length: 99999999\r\n\r\n";
    HttpResponse r = http_roundtrip(bridge.port(), req);
    FP_CHECK_EQ(r.status, 413);

    // 超长请求头
    std::string big = "GET /tools HTTP/1.1\r\nHost: 127.0.0.1\r\nX-Pad: ";
    big.append(4000, 'a');
    big += "\r\n\r\n";
    r = http_roundtrip(bridge.port(), big);
    FP_CHECK_EQ(r.status, 431);

    bridge.stop();
    const auto stats = bridge.stats();
    FP_CHECK(stats.oversize >= 2u);
    FP_CHECK_EQ(stats.tool_calls, 0u);
}
