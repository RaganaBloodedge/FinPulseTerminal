#include "agent/AgentService.h"

#include <algorithm>
#include <utility>

#include "core/Log.h"

namespace fp {

namespace {

constexpr const char* kTag = "agent";

/// 把 Json 数组里的字符串抽出来。**非字符串元素一律丢弃并计数**，
/// 而不是静默转成 "1" 之类 —— 那种脏数据会让 UI 上出现一个不存在的角色名。
std::vector<std::string> string_array(const Json& j) {
    std::vector<std::string> out;
    if (!j.is_array()) return out;
    for (const Json& item : j.items()) {
        if (item.is_string()) out.push_back(item.as_string());
    }
    return out;
}

std::string str_or_empty(const Json& j, std::string_view key) {
    return j[key].as_string_or("");
}

/// 把护栏摘要摊成人类可读的行，追加到 ``out`` 里。
///
/// 报告里的数字没有出处时，正文看起来完全正常 —— 只有把护栏结果一并显示，
/// 用户才知道这份报告该不该信。所以每次都带上，不做"严重才显示"的筛选。
void append_guardrails(std::vector<std::string>& out, const Json& guardrails) {
    const Json& items = guardrails["items"];
    if (!items.is_array()) return;
    const long long total = guardrails["total"].as_int_or(0);
    if (total == 0) return;
    for (const Json& g : items.items()) {
        const std::string sev = str_or_empty(g, "severity");
        const std::string msg = str_or_empty(g, "message");
        const std::string who = str_or_empty(g, "guardrail");
        if (msg.empty()) continue;
        out.push_back("[" + sev + "] " + (who.empty() ? "" : who + "：") + msg);
    }
}

}  // namespace

// ── 生命周期 ──────────────────────────────────────────────────────

AgentService::AgentService(PyEngine& engine, DataHub* hub)
    : engine_(engine),
      hub_(hub ? hub : &DataHub::instance()),
      sink_(std::make_shared<Sink>()) {
    sink_->hub = hub_;
    install_event_handler();
}

AgentService::~AgentService() {
    // 不主动拆事件处理器：RpcClient 的读线程可能**正在**回调它，拆掉就要
    // 处理"读线程调用中"的竞态。这里之所以可以不拆，是因为处理器捕获的是
    // :struct:`Sink` 的 weak_ptr —— 本对象一析构，weak 就失效，残留的
    // 处理器退化成空操作，既不碰已释放的内存，也不需要回捕 SetEvent 的时序。
    //
    // 注意 sink_ 是 shared_ptr：它由本对象与"可能还活着的处理器"共享，
    // 所以那点计数状态不会跟着本对象一起消失。
}

// 事件通路：引擎 → 本类 → DataHub。
//
// 这里是整个"联合能力"里方向反转的那一环 —— 通常 C++ 发请求、Python 应答；
// 现在 Python 在执行**过程中**主动往回推，C++ 壳实时把进度渲染出来。
void AgentService::install_event_handler() {
    // **只捕获 weak_ptr，绝不捕获 this。** 引擎可能比本对象活得久，
    // 捕获 this 的话"服务已析构、引擎又推来一条事件"就是 use-after-free。
    // 见 AgentService.h 里 :struct:`Sink` 的说明。
    std::weak_ptr<Sink> weak = sink_;

    engine_.set_event_handler([weak](const std::string& event, const Json& data) {
        const std::shared_ptr<Sink> s = weak.lock();
        if (!s) return;   // 服务已析构：安全地什么都不做

        const std::string   run_id = str_or_empty(data, "run_id");
        const std::uint64_t seq =
            s->seq.fetch_add(1, std::memory_order_relaxed) + 1;

        Json payload = Json::object();
        payload.set("event", event);
        payload.set("seq", static_cast<long long>(seq));
        payload.set("run_id", run_id);
        payload.set("data", data);

        // 主题里带上 run_id：订阅 `agent.stream.**` 收全部，
        // 订阅 `agent.stream.<id>` 只看某一次研判。
        //
        // 用**校验过的** Topic 构造函数而不是 unchecked：run_id 是从 Python
        // 那边传过来的外部数据，万一它含非法字符（空格、控制字符），
        // unchecked 会造出一个永远匹配不上的主题 —— 表现为"事件凭空消失"，
        // 而这是最难查的一类问题。校验不过就退到一个稳定的兜底主题。
        std::string topic = kTopicPrefix;
        topic += run_id.empty() ? "unknown" : run_id;

        {
            std::lock_guard<std::mutex> lk(s->last_topic_mu);
            s->last_topic = topic;
        }
        s->forwarded.fetch_add(1, std::memory_order_relaxed);
        try {
            s->hub->publish(Topic(topic), std::move(payload));
        } catch (const std::invalid_argument& e) {
            FP_ERROR(kTag, "事件主题非法（run_id=" << run_id << "）: " << e.what());
            s->hub->publish(std::string(kTopicPrefix) + "unknown", std::move(payload));
        }
    });
}

std::string AgentService::last_event_topic() const {
    std::lock_guard<std::mutex> lk(sink_->last_topic_mu);
    return sink_->last_topic;
}

// ── 调用封装 ──────────────────────────────────────────────────────

Json AgentService::call(const std::string& method, Json params, int timeout_ms) {
    try {
        return engine_.rpc().call(method, std::move(params), timeout_ms);
    } catch (const RpcError& e) {
        // RpcError 的 code_name 就是 Python 侧的异常类名。补上一次 discover()
        // 之后 Python 侧会返回 NotFound / BadParams / BadData，这里原样带出去，
        // 上层就能区分"角色名写错"和"数据不够"，而不是笼统的"调用失败"。
        //
        // kRemote 是引擎**显式**报的业务错误（可信），其余是本地/传输层的问题。
        if (e.code() == RpcError::kRemote && !e.code_name().empty()) {
            throw AgentError(e.what(), e.code_name());
        }
        throw AgentError(e.what(), e.code() == RpcError::kTimeout ? "Timeout"
                                                                 : "TransportError");
    }
}

// ── 配置 ──────────────────────────────────────────────────────────

AgentService::RoleInfo AgentService::parse_role(const Json& j) {
    RoleInfo r;
    r.id       = str_or_empty(j, "id");
    r.name     = str_or_empty(j, "name");
    r.description = str_or_empty(j, "description");
    r.category    = str_or_empty(j, "category");
    r.provider    = str_or_empty(j, "provider");
    r.model_id    = str_or_empty(j, "model_id");
    r.tools            = string_array(j["tools"]);
    r.output_sections  = string_array(j["output_sections"]);
    r.direction_sections = string_array(j["direction_sections"]);
    r.memory       = j["memory"].as_bool_or(false);
    r.reasoning    = j["reasoning"].as_bool_or(false);
    r.max_tool_calls = static_cast<int>(j["max_tool_calls"].as_int_or(6));
    return r;
}

AgentService::PanelInfo AgentService::parse_panel(const Json& j) {
    PanelInfo p;
    p.id          = str_or_empty(j, "id");
    p.name        = str_or_empty(j, "name");
    p.description = str_or_empty(j, "description");
    p.chair       = str_or_empty(j, "chair");
    p.quorum      = static_cast<int>(j["quorum"].as_int_or(2));
    p.rounds      = static_cast<int>(j["rounds"].as_int_or(2));
    p.valid       = j["valid"].as_bool_or(true);

    for (const Json& m : j["members"].items()) {
        PanelMember pm;
        // 键名是 ``role``，与投委会配置 JSON 里人写的那份同名。
        // Python 侧曾经发 ``role_id``（它的属性名），两边不一致时这里会
        // 静静地解析出 0 个委员 —— 界面上一片空白，而没有任何报错。
        pm.role          = str_or_empty(m, "role");
        pm.weight        = m["weight"].as_double_or(1.0);
        pm.cross_examine = m["cross_examine"].as_bool_or(true);
        if (!pm.role.empty()) p.members.push_back(std::move(pm));
    }
    for (const Json& pr : j["problems"].items()) {
        const std::string lv = str_or_empty(pr, "level");
        const std::string msg = str_or_empty(pr, "message");
        if (!msg.empty()) p.problems.push_back("[" + lv + "] " + msg);
    }
    return p;
}

const std::vector<AgentService::RoleInfo>& AgentService::roles() {
    {
        std::lock_guard<std::mutex> lk(mu_);
        if (roles_loaded_) return roles_;
    }

    const Json reply = call("agent.roles", Json::object());
    std::vector<RoleInfo> parsed;
    for (const Json& item : reply["roles"].items()) {
        RoleInfo r = parse_role(item);
        if (!r.id.empty()) parsed.push_back(std::move(r));
    }
    // 引擎说某个角色声明了不存在的工具 —— 这不是致命错误，但必须让用户看见，
    // 否则他会在报告里读到"该工具不可用"却找不到原因。
    for (const auto& kv : reply["missing_tools"].members()) {
        FP_WARN(kTag, "角色 " << kv.first << " 声明了未注册的工具: " << kv.second.dump());
    }

    std::lock_guard<std::mutex> lk(mu_);
    roles_ = std::move(parsed);
    roles_loaded_ = true;
    return roles_;
}

Json AgentService::role_detail(const std::string& role_id) {
    Json params = Json::object();
    params.set("role_id", role_id);
    return call("agent.role.get", std::move(params));
}

const std::vector<AgentService::PanelInfo>& AgentService::panels() {
    {
        std::lock_guard<std::mutex> lk(mu_);
        if (panels_loaded_) return panels_;
    }

    const Json reply = call("agent.panels", Json::object());
    std::vector<PanelInfo> parsed;
    for (const Json& item : reply["panels"].items()) {
        PanelInfo p = parse_panel(item);
        if (p.id.empty()) continue;
        // 配置本身有问题（比如主席同时也是委员）时，把原因带出去 ——
        // 直接禁用它比让用户跑到一半才发现好。
        if (!p.valid) {
            for (const std::string& msg : p.problems) {
                FP_ERROR(kTag, "投委会 " << p.id << " 配置无效: " << msg);
            }
        }
        parsed.push_back(std::move(p));
    }

    std::lock_guard<std::mutex> lk(mu_);
    panels_ = std::move(parsed);
    panels_loaded_ = true;
    return panels_;
}

void AgentService::reload() {
    call("agent.reload", Json::object());
    std::lock_guard<std::mutex> lk(mu_);
    roles_.clear();
    panels_.clear();
    roles_loaded_ = false;
    panels_loaded_ = false;
}

Json AgentService::bridge_status(const std::string& endpoint, const std::string& token) {
    Json params = Json::object();
    if (!endpoint.empty() || !token.empty()) {
        Json tb = Json::object();
        tb.set("endpoint", endpoint);
        tb.set("token", token);
        params.set("tool_bridge", std::move(tb));
    }
    return call("agent.bridge.status", std::move(params));
}

// ── 参数构造 ──────────────────────────────────────────────────────

Json AgentService::bars_param(const CandleSeries& series) {
    Json arr = Json::array();
    for (const Candle& c : series.bars()) {
        arr.push(c.to_json());
    }
    return arr;
}

Json AgentService::base_params(const CandleSeries& series, const Options& opt) const {
    Json params = Json::object();
    params.set("bars", bars_param(series));
    params.set("symbol", series.symbol());
    params.set("method", opt.method);
    params.set("horizon", opt.horizon);
    params.set("folds", opt.folds);
    params.set("min_train", opt.min_train);
    params.set("risk_free", opt.risk_free);
    params.set("include_text", opt.include_text);
    // 后端覆盖：只在**非空**时才发。发一个空串过去会让引擎把
    // "空 provider" 当成一次显式覆盖，于是每一场研判都降级到规则后端。
    if (!opt.provider.empty()) params.set("provider", opt.provider);
    if (!opt.model.empty())    params.set("model", opt.model);
    if (!opt.base_url.empty()) params.set("base_url", opt.base_url);
    if (!opt.api_key.empty())  params.set("api_key", opt.api_key);
    if (!opt.bridge_endpoint.empty() || !opt.bridge_token.empty()) {
        Json tb = Json::object();
        tb.set("endpoint", opt.bridge_endpoint);
        tb.set("token", opt.bridge_token);
        params.set("tool_bridge", std::move(tb));
    }
    return params;
}

// ── 结果解析 ──────────────────────────────────────────────────────

AgentService::Verdict AgentService::parse_verdict(const std::string& role_id,
                                                 const Json& j) {
    Verdict v;
    v.role     = role_id.empty() ? str_or_empty(j, "role_id") : role_id;
    v.role_name = str_or_empty(j, "role_name");
    // 方向缺省为空 —— **弃权**。绝不回落成 "NEUTRAL"：那会把"没表态"
    // 变成"表态了中性"，主席的计票分母当场就错了。
    v.direction  = str_or_empty(j, "direction");
    v.confidence = str_or_empty(j, "confidence");
    v.weight     = j["weight"].as_double_or(1.0);
    v.round      = static_cast<int>(j["round"].as_int_or(1));
    v.ok         = j["ok"].as_bool_or(false);
    v.text       = str_or_empty(j, "text");

    // 推理后端与用量。引擎侧每个角色都会回报这三样，之前收了但没往上传，
    // 于是"agent 到底在拿什么思考"在界面上完全看不出来。
    v.provider        = str_or_empty(j, "provider");
    v.fallback_reason = str_or_empty(j, "fallback_reason");
    const Json& usage = j["usage"];
    v.prompt_tokens     = usage["prompt_tokens"].as_int_or(0);
    v.completion_tokens = usage["completion_tokens"].as_int_or(0);
    return v;
}

AgentService::Outcome AgentService::parse_outcome(const Json& j) {
    Outcome out;
    out.valid       = j["valid"].as_bool_or(false);
    out.direction   = str_or_empty(j, "direction");
    out.confidence  = str_or_empty(j, "confidence");
    out.run_id      = str_or_empty(j, "run_id");
    out.intent      = str_or_empty(j, "intent");
    out.duration_ms = j["duration_ms"].as_double_or(0.0);

    const Json& panel = j["panel"];
    out.panel_id   = str_or_empty(panel, "id");
    out.panel_name = str_or_empty(panel, "name");

    for (const Json& r : j["rounds"].items()) {
        RoundInfo ri;
        ri.index       = static_cast<int>(r["round"].as_int_or(1));
        ri.duration_ms = r["duration_ms"].as_double_or(0.0);
        ri.changed     = string_array(r["changed"]);
        for (const auto& kv : r["members"].members()) {
            ri.verdicts.push_back(parse_verdict(kv.first, kv.second));
        }
        out.rounds.push_back(std::move(ri));
    }

    if (j.has("chair")) out.chair = parse_verdict("", j["chair"]);

    // 最终方向表取自**最后一轮**：那才是质证之后的立场。
    if (!out.rounds.empty()) {
        for (const Verdict& v : out.rounds.back().verdicts) {
            out.final_directions.emplace_back(v.role, v.direction);
        }
    }

    for (const Json& pr : j["problems"].items()) {
        const std::string lv = str_or_empty(pr, "level");
        const std::string msg = str_or_empty(pr, "message");
        if (!msg.empty()) out.problems.push_back("[" + lv + "] " + msg);
    }
    append_guardrails(out.problems, j["guardrails"]);

    // 「没出决议」和「决议是 NEUTRAL」是两件事。前者必须能被调用方识别出来，
    // 否则界面会显示一个看起来像结论的东西。
    if (!out.valid && out.direction.empty() && out.problems.empty()) {
        out.problems.push_back("[error] 引擎未给出决议原因");
    }
    return out;
}

// ── 运行 ──────────────────────────────────────────────────────────

AgentService::Outcome AgentService::run_role(const std::string& role_id,
                                             const CandleSeries& series,
                                             const Options& opt) {
    if (role_id.empty()) throw AgentError("role_id 不能为空", "BadParams");
    Json params = base_params(series, opt);
    params.set("role", role_id);

    // 单角色返回的是**一个 RoleRun**，不是一场会议：没有 rounds / panel / valid。
    // 直接丢给 parse_outcome 会得到 valid=false（字段不存在）—— 那会让调用方
    // 以为研判失败了，而实际上报告好好的。所以这里单独映射。
    const Json j = call("agent.run", std::move(params));

    Outcome out;
    out.run_id      = str_or_empty(j, "run_id");
    out.intent      = str_or_empty(j, "intent");
    out.duration_ms = j["duration_ms"].as_double_or(0.0);

    const Verdict v = parse_verdict(role_id, j);
    out.valid      = v.ok;
    out.direction  = v.direction;
    out.confidence = v.confidence;
    out.chair      = v;
    out.final_directions.emplace_back(role_id, v.direction);

    RoundInfo ri;
    ri.index       = 1;
    ri.duration_ms = out.duration_ms;
    ri.verdicts.push_back(v);
    out.rounds.push_back(std::move(ri));

    if (!v.ok) {
        const std::string why = str_or_empty(j, "error");
        out.problems.push_back("[error] 角色未产出结论：" +
                               (why.empty() ? std::string("正文为空") : why));
    }
    append_guardrails(out.problems, j["guardrails"]);
    return out;
}

AgentService::Outcome AgentService::debate(const CandleSeries& series,
                                           const Options& opt) {
    Json params = base_params(series, opt);
    if (!opt.panel.empty()) params.set("panel", opt.panel);
    if (opt.rounds > 0)    params.set("rounds", opt.rounds);
    return parse_outcome(call("agent.debate", std::move(params)));
}

std::uint64_t AgentService::debate_async(const CandleSeries& series, const Options& opt,
                                        RunCallback cb) {
    Json params = base_params(series, opt);
    if (!opt.panel.empty()) params.set("panel", opt.panel);
    if (opt.rounds > 0)    params.set("rounds", opt.rounds);

    return engine_.rpc().call_async(
        "agent.debate", std::move(params),
        [cb = std::move(cb)](const Json& result, const RpcError* err) {
            if (!cb) return;
            if (err != nullptr) {
                const std::string code =
                    (err->code() == RpcError::kRemote && !err->code_name().empty())
                        ? err->code_name()
                        : (err->code() == RpcError::kTimeout ? "Timeout" : "TransportError");
                AgentError e(err->what(), code);
                cb(Outcome{}, &e);
                return;
            }
            cb(parse_outcome(result), nullptr);
        });
}

AgentService::Outcome AgentService::team(const std::vector<std::string>& role_ids,
                                         const CandleSeries& series,
                                         const Options& opt) {
    if (role_ids.empty()) throw AgentError("role_ids 不能为空", "BadParams");
    Json params = base_params(series, opt);

    Json arr = Json::array();
    for (const std::string& id : role_ids) arr.push(id);
    params.set("roles", std::move(arr));

    const Json j = call("agent.team", std::move(params));

    // 团队没有主席，也没有"轮"的概念。这里把成员结论放进一轮里，
    // 让调用方可以用同一套渲染逻辑处理 debate 和 team 的结果。
    Outcome out;
    out.run_id      = str_or_empty(j, "run_id");
    out.panel_name  = str_or_empty(j, "panel");
    out.duration_ms = j["duration_ms"].as_double_or(0.0);
    // valid / effective 由引擎判定 —— "形成了有效判断"的规则只在 Python 侧
    // 实现一份（见 orchestrator.is_effective）。壳侧再算一遍必然与它漂移。
    out.valid       = j["valid"].as_bool_or(false);

    RoundInfo ri;
    ri.index       = 1;
    ri.duration_ms = out.duration_ms;
    for (const auto& kv : j["members"].members()) {
        ri.verdicts.push_back(parse_verdict(kv.first, kv.second));
    }
    out.rounds.push_back(std::move(ri));

    for (const std::string& rid : string_array(j["ineffective"])) {
        out.problems.push_back(
            "[error] 委员 " + rid + " 未形成有效判断（未拿到工具数据或护栏报错），"
            "已从结论里排除");
    }
    append_guardrails(out.problems, j["guardrails"]);

    if (!out.valid && out.problems.empty()) {
        out.problems.push_back("[error] 无任何委员形成有效判断");
    }
    return out;
}

// ── 回查 ──────────────────────────────────────────────────────────

Json AgentService::trace(const std::string& run_id, bool include_events,
                         bool include_result) {
    if (run_id.empty()) throw AgentError("run_id 不能为空", "BadParams");
    Json params = Json::object();
    params.set("run_id", run_id);
    params.set("include_events", include_events);
    params.set("include_result", include_result);
    return call("agent.trace", std::move(params));
}

Json AgentService::recent_runs(int limit) {
    Json params = Json::object();
    params.set("limit", limit);
    return call("agent.runs", std::move(params));
}

Json AgentService::memory(const std::string& symbol, int limit) {
    Json params = Json::object();
    if (!symbol.empty()) params.set("symbol", symbol);
    params.set("limit", limit);
    return call("agent.memory", std::move(params));
}

Json AgentService::consistency(const std::string& symbol,
                              const std::vector<std::string>& roles) {
    if (symbol.empty()) throw AgentError("symbol 不能为空", "BadParams");
    Json params = Json::object();
    params.set("symbol", symbol);
    if (!roles.empty()) {
        Json arr = Json::array();
        for (const std::string& r : roles) arr.push(r);
        params.set("roles", std::move(arr));
    }
    return call("agent.consistency", std::move(params));
}

Json AgentService::stream_stats() {
    return call("agent.stream.stats", Json::object());
}

}  // namespace fp
