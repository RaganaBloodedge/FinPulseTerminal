#include "app/CliApp.h"

#include "app/EventText.h"
#include "agent/AgentService.h"
#include "agent/ToolBridge.h"
#include "core/Profile.h"
#include "data/ReplaySource.h"
#include "data/Watchlist.h"
#include "core/DataHub.h"
#include "core/Log.h"
#include "core/Version.h"

#include <algorithm>
#include <atomic>
#include <chrono>
#include <cmath>
#include <cstdio>
#include <map>
#include <set>
#include <string>
#include <thread>
#include <vector>

namespace fp {

namespace {

using Clock = std::chrono::steady_clock;

double elapsed_ms(Clock::time_point t0) {
    return std::chrono::duration<double, std::milli>(Clock::now() - t0).count();
}

/// 终端显示宽度。CJK 字符占两列，按字节数对齐会错位。
/// 这里只做粗略分类：ASCII 算 1 列，其余按 2 列处理。
std::size_t display_width(const std::string& s) {
    std::size_t w = 0;
    for (std::size_t i = 0; i < s.size();) {
        const unsigned char c = static_cast<unsigned char>(s[i]);
        if (c < 0x80)            { w += 1; i += 1; }
        else if ((c >> 5) == 0x6){ w += 1; i += 2; }
        else if ((c >> 4) == 0xE){ w += 2; i += 3; }
        else                     { w += 2; i += 4; }
    }
    return w;
}

std::string pad_right(const std::string& s, std::size_t width) {
    const std::size_t w = display_width(s);
    if (w >= width) return s;
    return s + std::string(width - w, ' ');
}

std::string pad_left(const std::string& s, std::size_t width) {
    const std::size_t w = display_width(s);
    if (w >= width) return s;
    return std::string(width - w, ' ') + s;
}

/// 从可能是 null 的数组里取最后一个有值的数字。
double last_valid(const Json& arr) {
    if (!arr.is_array()) return std::nan("");
    for (std::size_t i = arr.size(); i-- > 0;) {
        if (arr.at(i).is_number()) return arr.at(i).as_double();
    }
    return std::nan("");
}

std::string join(const std::vector<std::string>& v, const char* sep = ", ") {
    std::string out;
    for (std::size_t i = 0; i < v.size(); ++i) {
        if (i) out += sep;
        out += v[i];
    }
    return out;
}

}  // namespace

// ── 格式化 ────────────────────────────────────────────────

std::string CliApp::fmt(double v, int decimals) {
    if (!std::isfinite(v)) return "n/a";
    char buf[64];
    std::snprintf(buf, sizeof(buf), "%.*f", decimals, v);
    return buf;
}

std::string CliApp::fmt_int(long long v) {
    char buf[32];
    std::snprintf(buf, sizeof(buf), "%lld", v);

    std::string s(buf);
    const bool neg = !s.empty() && s[0] == '-';
    std::string digits = neg ? s.substr(1) : s;

    std::string rev;
    int count = 0;
    for (auto it = digits.rbegin(); it != digits.rend(); ++it) {
        if (count != 0 && count % 3 == 0) rev.push_back(',');
        rev.push_back(*it);
        ++count;
    }
    std::reverse(rev.begin(), rev.end());
    return neg ? "-" + rev : rev;
}

std::string CliApp::fmt_pct(double v, bool signed_) {
    if (!std::isfinite(v)) return "n/a";
    char buf[64];
    std::snprintf(buf, sizeof(buf), signed_ ? "%+.2f%%" : "%.2f%%", v);
    return buf;
}

// ── 输出辅助 ──────────────────────────────────────────────

// ── 启动配置档 ────────────────────────────────────────────

bool CliApp::save_profile_now() const {
    // 先把已有的读回来再合并：调用方只想改其中几项，不该顺手把
    // 手写过的 watchlist（或别人加的字段）抹掉。
    Profile p = load_profile();
    p.source = opt_.source;
    p.symbol = opt_.symbol;
    p.bars   = opt_.bars;
    if (!opt_.csv_path.empty()) p.csv_path = opt_.csv_path;
    if (!opt_.tushare_token.empty()) p.tushare_token = opt_.tushare_token;

    const std::string err = save_profile(p);
    if (!err.empty()) {
        std::printf("\n  [!!] 保存启动配置档失败：%s\n", err.c_str());
        return false;
    }
    std::printf("\n  [OK] 已保存启动配置档：%s\n", p.path.c_str());
    std::printf("  %s  %s\n", pad_right("数据源:", 22).c_str(), p.source.c_str());
    std::printf("  %s  %s\n", pad_right("标的:", 22).c_str(), p.symbol.c_str());
    std::printf("  %s  %s 根\n", pad_right("根数:", 22).c_str(), fmt_int(static_cast<long long>(p.bars)).c_str());
    std::printf("  %s  %s\n", pad_right("Tushare token:", 22).c_str(),
                p.has_token() ? "已写入（文件权限 0600，不进版本控制）" : "未写入");
    return true;
}

// ── 批量拉取 ──────────────────────────────────────────────

int CliApp::run_pull() {
    // 清单优先级：命令行给的 > 配置档里的 > 内置的上证成分股。
    std::vector<std::string> symbols = opt_.pull_symbols;
    if (symbols.empty()) {
        const Profile p = load_profile();
        symbols = p.watchlist;
    }
    if (symbols.empty()) {
        for (const WatchItem& item : sse_watchlist()) symbols.push_back(item.code);
    }

    section("批量拉取行情");
    kv("数据源", opt_.source);
    kv("标的数", fmt_int(static_cast<long long>(symbols.size())) + " 只");
    kv("每只根数", fmt_int(static_cast<long long>(opt_.bars)));

    Json params = Json::object();
    Json arr = Json::array();
    for (const std::string& s : symbols) arr.push(Json(s));
    params.set("symbols", std::move(arr));
    params.set("source", opt_.source);
    params.set("bars", static_cast<long long>(opt_.bars));
    if (!opt_.tushare_token.empty()) params.set("token", opt_.tushare_token);

    // 一只股票一次往返，14 只就是 14 次。给足超时，但别给到"看起来像卡死"。
    const Json res = engine_.rpc().call("source.pull", std::move(params), 180000);

    const Json& items = res["items"];
    std::printf("\n  %s %s %s %s %s\n",
                pad_right("标的", 14).c_str(), pad_right("名称", 12).c_str(),
                pad_right("根数", 6).c_str(), pad_right("数据截至", 12).c_str(), "文件");
    for (const Json& it : items.items()) {
        const std::string sym  = it["symbol"].as_string_or("");
        const std::string name = name_for(sym);
        if (it["ok"].as_bool_or(false)) {
            const long long as_of = it["as_of"].as_int_or(0);
            std::printf("  %s %s %s %s %s\n",
                        pad_right(sym, 14).c_str(),
                        pad_right(name.empty() ? "—" : name, 12).c_str(),
                        pad_right(fmt_int(it["rows"].as_int_or(0)), 6).c_str(),
                        pad_right(as_of > 0 ? format_date(as_of) : "—", 12).c_str(),
                        it["path"].as_string_or("").c_str());
        } else {
            std::printf("  %s %s %s\n",
                        pad_right(sym, 14).c_str(),
                        pad_right(name.empty() ? "—" : name, 12).c_str(),
                        ("[!!] " + it["error"].as_string_or("失败")).c_str());
        }
    }

    const long long ok = res["ok"].as_int_or(0);
    const long long failed = res["failed"].as_int_or(0);
    std::printf("\n");
    kv("拉取结果", fmt_int(ok) + " 成功 / " + fmt_int(failed) + " 失败");
    const std::string dir = res["dir"].as_string_or("");
    if (!dir.empty()) kv("落盘目录", dir);

    if (failed > 0) {
        // 失败最常见的原因是 token 没配 —— 那种情况下每只都会退到缓存或
        // 演示数据，报告里全是"[!!] 未取到实时数据"。这一行是给这种局面
        // 准备的：不要让人逐条去猜。
        note("失败项多为「未取到实时数据」时，先确认 token：");
        note("  环境变量 TUSHARE_TOKEN，或配置档 ~/.finpulse/profile.json 的 tushare_token");
    }
    std::printf("\n");
    return ok > 0 ? 0 : 2;
}

void CliApp::section(const std::string& title) const {
    std::printf("\n%s\n", title.c_str());
    std::printf("  %s\n", std::string(72, '-').c_str());
}

void CliApp::kv(const std::string& key, const std::string& value) const {
    std::printf("  %s  %s\n", pad_right(key + ":", 22).c_str(), value.c_str());
}

void CliApp::note(const std::string& text) const {
    std::printf("  %s  %s\n", std::string(22, ' ').c_str(), text.c_str());
}

/// 分母跟着实际会跑的节数走。写死 "[3/6]" 然后多插一节，读者第一眼
/// 看到的就是"6 节里的第 5 节之后又冒出第 7 节"这种自相矛盾的编号。
std::string CliApp::stage(int index) const {
    const int total = opt_.agent ? 7 : 6;
    return "[" + std::to_string(index) + "/" + std::to_string(total) + "]";
}

// ── 各节输出 ──────────────────────────────────────────────

void CliApp::print_series(const CandleSeries& cs, const Json& raw) const {
    if (cs.empty()) {
        kv("数据", "(空)");
        return;
    }

    const Candle& last = cs.back();
    const Candle& prev = cs.size() > 1 ? cs.at(cs.size() - 2) : last;
    const double chg = last.close - prev.close;
    const double chg_pct = prev.close != 0.0 ? chg / prev.close * 100.0 : 0.0;
    const double span = cs.at(0).close != 0.0
                            ? (last.close / cs.at(0).close - 1.0) * 100.0
                            : 0.0;

    kv("数据源", opt_.source + " / " + cs.symbol());
    kv("K 线数", fmt_int(static_cast<long long>(cs.size())) + " 根");
    kv("区间", format_date(cs.at(0).ts_ms) + "  ~  " + format_date(last.ts_ms));
    kv("最新一根", "O=" + fmt(last.open) + "  H=" + fmt(last.high) +
                       "  L=" + fmt(last.low) + "  C=" + fmt(last.close));
    kv("成交量", fmt_int(last.volume));
    kv("较前收盘", fmt(chg) + "  (" + fmt_pct(chg_pct) + ")");
    kv("区间涨跌", fmt_pct(span));

    // ── 数据出处 ────────────────────────────────────────────
    //
    // 能回落的数据源（tushare）会在这里交代它到底走了哪条路。这一段
    // **不是可选的装饰**：拿到的是本地缓存却以为在看实时行情，是最容易
    // 出事的一种误解 —— 而回落本身恰恰是设计出来的正常行为，所以只能
    // 靠"如实说出来"来防，不能靠"不回落"来防。
    const Json& prov = raw["provenance"];
    if (prov.is_object()) {
        const std::string mode   = prov["mode"].as_string_or("");
        const std::string detail = prov["detail"].as_string_or("");
        const std::string reason = prov["reason"].as_string_or("");
        const long long   as_of  = prov["as_of"].as_int_or(0);

        if (mode == "live_api") {
            kv("数据出处", "实时接口（已连上远端行情服务）");
        } else if (mode == "local_cache") {
            kv("数据出处", "[!] 本地缓存 —— 不是实时数据");
        } else if (mode == "demo_synthetic") {
            // 最坏的一档，必须最刺眼。合成数据看起来和真实行情一样"正常"，
            // 一旦没标出来，所有结论都建立在一份假数据上而没人知道。
            kv("数据出处", "[!!] 合成演示数据 —— 既不是实时行情，也不是本地缓存");
        } else if (!mode.empty()) {
            kv("数据出处", mode);
        }
        if (as_of > 0) kv("数据截至", format_date(as_of) + "（最后一根日线）");
        if (!detail.empty()) note(detail);
        if (!reason.empty()) note("未能取实时数据的原因: " + reason);
        if (mode == "local_cache" || mode == "demo_synthetic") {
            note("要让这里显示实时数据（按优先级二选一）：");
            note("  1) 设置环境变量 TUSHARE_TOKEN");
            note("  2) 把 token 写进启动配置档 ~/.finpulse/profile.json 的 tushare_token");
            note("     一次性写入：finpulse-cli --tushare-token <你的token> --save-profile");
        }
    }
}

void CliApp::print_indicators(const Json& res) const {
    const Json& results = res["results"];
    if (!results.is_array()) return;

    for (const auto& item : results.items()) {
        const std::string spec = item["spec"].as_string_or("?");
        const Json& lines = item["lines"];

        std::string detail;
        if (lines.is_object()) {
            for (const auto& kvp : lines.members()) {
                const double v = last_valid(kvp.second);
                if (!detail.empty()) detail += "   ";
                detail += kvp.first + "=" + fmt(v, 4);
            }
        }
        kv(spec, detail);
    }
}

void CliApp::print_stats(const Json& s) const {
    const auto num = [&s](const char* k) { return s[k].as_double_or(std::nan("")); };

    kv("年化收益 (CAGR)", fmt_pct(num("cagr_pct")));
    kv("年化波动率", fmt_pct(num("ann_vol_pct"), false));
    kv("夏普比率", fmt(num("sharpe"), 3));
    kv("索提诺比率", fmt(num("sortino"), 3));

    std::string dd = fmt_pct(num("max_drawdown_pct"));
    const std::string peak = s["max_drawdown_peak_date"].as_string_or("");
    const std::string trough = s["max_drawdown_trough_date"].as_string_or("");
    if (!peak.empty()) {
        const bool recovered = s["max_drawdown_recovery_date"].is_string();
        dd += "   (" + peak + " → " + trough + (recovered ? "，已收复)" : "，尚未收复)");
    }
    kv("最大回撤", dd);

    kv("VaR / CVaR (95%)", fmt_pct(num("var95_pct")) + "   /   " + fmt_pct(num("cvar95_pct")));
    kv("偏度 / 超额峰度", fmt(num("skew"), 3) + "   /   " + fmt(num("excess_kurtosis"), 3));
    kv("自相关 lag-1", fmt(num("autocorr_lag1"), 4));
    kv("上涨日占比", fmt_pct(num("positive_days_pct"), false));
    kv("单日最大涨 / 跌", fmt_pct(num("best_day_pct")) + "   /   " + fmt_pct(num("worst_day_pct")));
}

void CliApp::print_forecast(const ForecastResult& r) const {
    kv("方法", r.method);
    kv("最后收盘", fmt(r.last_close));

    const Json& m = r.meta;
    if (m.has("order")) {
        std::string how = m["auto_selected"].as_bool_or(false) ? "（AIC 自动选阶）" : "（指定）";
        kv("AR 阶数", std::to_string(m["order"].as_int_or(0)) + " " + how);
    }
    if (m.has("phi") && m["phi"].is_array()) {
        std::string coeff;
        for (const auto& v : m["phi"].items()) {
            if (!coeff.empty()) coeff += ", ";
            coeff += fmt(v.as_double_or(0.0), 4);
        }
        kv("系数 φ", coeff);
    }
    if (m.has("sigma")) kv("残差标准差 σ", fmt(m["sigma"].as_double_or(0.0), 4));
    if (m.has("aic") && m["aic"].is_number()) kv("AIC", fmt(m["aic"].as_double_or(0.0), 3));

    std::printf("\n  %s %s %s %s\n",
                pad_right("步", 6).c_str(),
                pad_left("预测值", 14).c_str(),
                pad_left("下界(95%)", 14).c_str(),
                pad_left("上界(95%)", 14).c_str());

    for (std::size_t i = 0; i < r.points.size(); ++i) {
        const auto& p = r.points[i];
        std::printf("  %s %s %s %s\n",
                    pad_right(std::to_string(i + 1), 6).c_str(),
                    pad_left(fmt(p.value), 14).c_str(),
                    pad_left(fmt(p.lower), 14).c_str(),
                    pad_left(fmt(p.upper), 14).c_str());
    }
}

void CliApp::print_backtest(const Json& raw, const BacktestMetrics& m) const {
    kv("折数 × 步长", std::to_string(m.folds) + " 折 × " + std::to_string(m.horizon) + " 步"
                          + "   （扩张窗口，无前视）");
    kv("模型 MAE / RMSE", fmt(m.mae) + "   /   " + fmt(m.rmse));
    kv("随机游走 MAE / RMSE", fmt(m.base_mae) + "   /   " + fmt(m.base_rmse));
    if (raw["mape"].is_number()) kv("MAPE", fmt(raw["mape"].as_double_or(0.0), 3) + "%");

    const bool win = m.skill > 0.0;
    kv("技能分 (1-MSE/MSE_rw)",
       fmt(m.skill, 4) + "   " + (win ? "[OK] 跑赢随机游走" : "[!!] 不如随机游走"));

    if (raw["dir_acc"].is_number()) {
        kv("方向命中率", fmt(m.dir_acc, 2) + "%   （随机游走无方向信息，按 50% 计）");
    }
    if (raw["interval_coverage"].is_number()) {
        const double cov = raw["interval_coverage"].as_double_or(0.0);
        kv("预测区间覆盖率", fmt(cov * 100.0, 1) + "%" + "   （名义 95%）");
        if (cov < 0.90) {
            // 这个数字偏低**不是 bug**，而是 AR 模型的内生局限，值得说清楚。
            // 区间宽度按同方差假设算（σ 为常数），但价格水平大幅变动时
            // 绝对误差会跟着放大，固定宽度的区间自然包不住。
            kv("说明", "区间按同方差假设（σ 恒定）计算。价格水平显著变动时绝对误差会随之放大，");
            note("固定宽度的区间包不住 —— 这是 AR 的已知局限，不是计算错误。");
        }
    }

    const Json& pf = raw["per_fold"];
    if (pf.is_array() && pf.size() > 0) {
        std::printf("\n  %s %s %s %s\n",
                    pad_right("折", 6).c_str(),
                    pad_left("训练样本", 12).c_str(),
                    pad_left("本折 RMSE", 14).c_str(),
                    pad_left("基线 RMSE", 14).c_str());
        for (const auto& f : pf.items()) {
            std::printf("  %s %s %s %s\n",
                        pad_right(std::to_string(f["index"].as_int_or(0) + 1), 6).c_str(),
                        pad_left(fmt_int(f["train_size"].as_int_or(0)), 12).c_str(),
                        pad_left(fmt(f["rmse"].as_double_or(std::nan(""))), 14).c_str(),
                        pad_left(fmt(f["base_rmse"].as_double_or(std::nan(""))), 14).c_str());
        }
    }
}

void CliApp::run_bus_demo(CandleSeries cs) {
    section("[总线] DataHub 事件流回放");

    DataHub& hub = DataHub::instance();
    hub.reset_stats();

    std::size_t alerts = 0;
    std::size_t quotes = 0;

    // 订阅 1：总量计数。用 ** 匹配所有符号，演示多层通配。
    Subscription counter(&hub, hub.subscribe("market.quote.**",
        [&quotes](const Topic&, const Json&) { ++quotes; }, "行情计数"));

    // 订阅 2：异动提醒。演示"总线上挂一个策略"的写法——
    // 这个 lambda 完全不知道数据是从回放来的还是从实时行情来的。
    Subscription watcher(&hub, hub.subscribe(
        "market.quote." + ReplaySource::normalize_symbol(opt_.symbol),
        [&alerts](const Topic&, const Json& payload) {
            const Quote q = Quote::from_json(payload);
            if (std::fabs(q.change_pct()) >= 3.0) {
                ++alerts;
                if (alerts <= 5) {
                    std::printf("  · %s  %-8s %10s   %s\n",
                                format_date(q.ts_ms).c_str(),
                                q.symbol.c_str(),
                                CliApp::fmt(q.last).c_str(),
                                CliApp::fmt_pct(q.change_pct()).c_str());
                } else if (alerts == 6) {
                    std::printf("  · …（后续异动省略）\n");
                }
            }
        },
        "异动提醒"));

    Subscription done(&hub, hub.subscribe("market.replay.done.**",
        [](const Topic&, const Json&) {}, "回放完成"));

    ReplaySource replay(hub, opt_.symbol);
    ReplaySource::Options ro;
    ro.speed = opt_.replay_speed;

    // 回放需要 OHLCV，所以这里把 CandleSeries 移交给它。
    // 注意上面那些订阅在 load 之前就建好了——顺序反了会漏掉前面几根。
    replay.load(std::move(cs));

    const auto t0 = Clock::now();
    const std::size_t n = replay.run_to_end();
    const double ms = elapsed_ms(t0);

    const DataHubStats st = hub.stats();

    std::printf("\n");
    kv("订阅者", std::to_string(hub.subscription_count()) + " 个（回放期间保持活跃）");
    kv("发布 / 投递", fmt_int(static_cast<long long>(st.published)) + " 条  /  "
                          + fmt_int(static_cast<long long>(st.delivered)) + " 次");
    kv("无订阅丢弃", fmt_int(static_cast<long long>(st.unmatched)) + " 条");
    kv("订阅者异常", fmt_int(static_cast<long long>(st.failed)) + " 次");
    kv("回放吞吐", fmt_int(static_cast<long long>(n)) + " 根 / " + fmt(ms, 1) + " ms   →   "
                       + fmt(ms > 0 ? n / (ms / 1000.0) : 0.0, 0) + " 根/秒");
    kv("异动提醒命中", std::to_string(alerts) + " 次（阈值 ±3%）");
}

CliApp::AgentSummary CliApp::run_agent(CandleSeries cs) {
    section(stage(7) + " 智能体研判（C++ 壳 ⇄ Python 智能体）");

    DataHub& hub = DataHub::instance();
    hub.reset_stats();

    // ── 工具桥 ────────────────────────────────────────────────
    //
    // 这是"C++/Python 联合能力"里方向反转的那一环：Python 在推理**过程中**
    // 回调 C++ 侧，问"终端现在正在收哪只票、总线投递了多少条"。这些状态
    // 只存在于本进程，Python 分析进程看不见 —— 不架这座桥，智能体就只能
    // 凭它自己从 bars 算出来的东西说话。
    ToolBridge bridge(&hub);
    if (!opt_.agent_no_bridge) {
        bridge.register_default_tools();

        // 注入的是**真数据**：行情就是上面 [2/6] 打印过的那一根。
        // 不塞桩值 —— 塞桩能让命令行看起来一切正常，但报告里的数字是假的，
        // 而"数字可验证"正是这个 CLI 存在的理由。
        const CandleSeries* series = &cs;
        bridge.set_quote_provider([series](const std::string& symbol) -> Json {
            if (series == nullptr || series->empty()) return Json::object();
            const Candle& last = series->back();
            const double prev = series->size() > 1
                                    ? series->at(series->size() - 2).close
                                    : last.close;
            Json q = Json::object();
            q.set("symbol", symbol.empty() ? series->symbol() : symbol);
            q.set("ts_ms", static_cast<long long>(last.ts_ms));
            q.set("last", last.close);
            q.set("open", last.open);
            q.set("high", last.high);
            q.set("low", last.low);
            q.set("volume", static_cast<long long>(last.volume));
            q.set("change_pct", prev != 0.0 ? (last.close / prev - 1.0) * 100.0 : 0.0);
            q.set("source", "C++ CliApp 内存");
            return q;
        });

        bridge.set_series_provider([series](const std::string& symbol) -> Json {
            Json s = Json::object();
            s.set("symbol", symbol.empty() ? series->symbol() : symbol);
            s.set("bars", static_cast<long long>(series ? series->size() : 0));
            s.set("loaded", series != nullptr && !series->empty());
            return s;
        });

        try {
            bridge.start();
            kv("工具桥", bridge.endpoint() + "   （只绑回环）");
            kv("已注册工具", join(bridge.tool_names()));
        } catch (const std::exception& e) {
            // 桥起不来不该让研判失败：终端工具是锦上添花，不是前提。
            // 角色会如实写"该工具不可用"——这里只需要把原因说出来。
            kv("工具桥", std::string("[!!] 启动失败: ") + e.what());
            note("角色会如实报告终端工具不可用，研判继续。");
        }
    } else {
        kv("工具桥", "已按 --no-bridge 关闭");
        note("本次研判中终端工具一律报不可用 —— 这是刻意演示降级路径。");
    }

    // ── 配置自检 ──────────────────────────────────────────────
    AgentService svc(engine_, &hub);
    {
        const auto& roles = svc.roles();
        kv("可用角色", std::to_string(roles.size()) + " 个");
        for (const auto& p : svc.panels()) {
            kv("投委会", p.name + "  (" + p.id + ")   委员 "
                             + std::to_string(p.members.size()) + " 位   quorum="
                             + std::to_string(p.quorum) + "   轮数 "
                             + std::to_string(p.rounds)
                             + (p.valid ? "" : "   [!!] 配置有问题"));
            for (const auto& prob : p.problems) note("[!] " + prob);
        }
    }
    {
        // 引擎侧视角才是定论：C++ 这边 start() 成功，但 Python 那边可能
        // 仍然连不上（端口被占、令牌过期）。跑之前先把这件事说清楚。
        const Json st = svc.bridge_status(bridge.running() ? bridge.endpoint() : std::string{},
                                          bridge.running() ? bridge.token() : std::string{});
        const bool ok = st["available"].as_bool_or(false);
        kv("桥(引擎侧视角)",
           std::string(ok ? "[OK] 可用" : "[--] 不可用")
               + "   工具 " + std::to_string(st["tool_count"].as_int_or(0)) + " 个"
               + (ok ? "" : "   原因: " + st["reason"].as_string_or("未提供原因")));
    }

    // ── 事件订阅 ──────────────────────────────────────────────
    //
    // 必须在调用**之前**订阅：研判是同步跑完的，事后再订阅只会收到空白。
    std::atomic<int>  events{0};
    std::atomic<bool> run_done{false};
    const auto        t_sub = Clock::now();
    Subscription      sub(&hub, hub.subscribe(
        "agent.stream.**",
        [&events, &run_done, t_sub](const Topic&, const Json& p) {
            ++events;
            // 打印时用**原始全名**（脚本 grep 得到），说明文字用语义名。
            const std::string full = p["event"].as_string_or("?");
            const std::string ev   = event_text::strip_prefix(full);
            if (ev == "run.done" || ev == "run.error") run_done.store(true);

            const std::string what = event_text::describe(ev, p["data"]);
            std::printf("  · %s  %s\n",
                        pad_right(fmt(elapsed_ms(t_sub), 0) + "ms", 9).c_str(),
                        (full + (what.empty() ? "" : "  " + what)).c_str());
        },
        "CLI 研判进度"));

    // ── 开跑 ──────────────────────────────────────────────────
    AgentService::Options aopt;
    aopt.panel        = opt_.agent_panel;
    aopt.provider     = opt_.agent_provider;
    aopt.model        = opt_.agent_model;
    aopt.base_url     = opt_.agent_base_url;
    aopt.api_key      = opt_.agent_api_key;
    aopt.rounds       = opt_.agent_rounds;
    aopt.method       = opt_.forecast_method;
    aopt.horizon      = static_cast<int>(opt_.horizon);
    aopt.folds        = static_cast<int>(opt_.folds);
    aopt.min_train    = static_cast<int>(opt_.min_train);
    aopt.include_text = opt_.agent_report;
    if (bridge.running()) {
        aopt.bridge_endpoint = bridge.endpoint();
        aopt.bridge_token    = bridge.token();
    }

    const bool single = !opt_.agent_role.empty();
    kv("研判模式", single ? ("单角色 " + opt_.agent_role)
                          : "投委会辩论（独立研判 → 交叉质证 → 主席综合）");
    std::printf("\n  ── 进度事件流（Python 边跑边推，经总线转发）──\n");

    const auto t0  = Clock::now();
    const AgentService::Outcome out =
        single ? svc.run_role(opt_.agent_role, cs, aopt) : svc.debate(cs, aopt);
    const double ms = elapsed_ms(t0);

    // 结果比事件先到（事件是异步推的）。不等一下就打印，上面的耗时统计
    // 会和事件条数对不上，而"数字对不上"会让读者怀疑整段输出。
    for (int i = 0; i < 400 && !run_done.load(); ++i) {
        std::this_thread::sleep_for(std::chrono::milliseconds(5));
    }

    // ── 结果 ──────────────────────────────────────────────────
    std::printf("\n  ── 研判结果 ──\n");
    kv("运行编号", out.run_id);
    kv("形态", single ? "单角色研判"
                      : ("投委会 " + out.panel_name + "  (" + out.panel_id + ")"));
    kv("耗时", fmt(ms, 1) + " ms   （引擎自计 " + fmt(out.duration_ms, 1) + " ms）");
    // 三种状态必须分开写：有效有方向 / 有效但弃权 / 根本没成会。
    // 把它们压成一个"未出决议"会让"数据不够所以没表态"和"配置坏了"
    // 看起来一模一样。
    if (!out.valid) {
        kv("决议", "[--] 未形成有效决议");
    } else if (out.direction.empty()) {
        kv("决议", "[OK] 有效，但主席未给出方向");
    } else {
        kv("决议", out.direction);
    }
    if (!out.confidence.empty()) kv("置信度", out.confidence);
    // 「议题」与「运行告警」是两件事，标签不能合用一个。
    //
    // 此前只有一行"会议问题"，装着配置体检、quorum 不足、沿用上一轮、
    // 各角色护栏告警 —— 语义是"会议过程中出的问题"。但中文里"会议问题"
    // 另一种同样自然的读法是"会议**要议的**问题"，真跑一次就会把用户
    // 带到"这块地方是不是该显示议题"上去。牌子换掉，顺带把议题补上。
    if (!out.intent.empty()) kv("议题", out.intent);
    for (const auto& prob : out.problems) kv("运行告警", prob);

    // 每轮表态。方向为空 = **弃权**，显式写出来而不是回落成 NEUTRAL ——
    // 那是"没表态"被读成"表态了中性"，主席的计票分母当场就错。
    for (const auto& rnd : out.rounds) {
        std::printf("\n  ── 第 %d 轮 ──\n", rnd.index);
        std::printf("  %s %s %s %s\n",
                    pad_right("委员", 20).c_str(),
                    pad_left("方向", 10).c_str(),
                    pad_left("置信度", 10).c_str(),
                    pad_left("权重", 6).c_str());
        for (const auto& v : rnd.verdicts) {
            const std::string dir = v.has_direction() ? v.direction : std::string("弃权");
            std::printf("  %s %s %s %s\n",
                        pad_right(v.role_name.empty() ? v.role : v.role_name, 20).c_str(),
                        pad_left(dir, 10).c_str(),
                        pad_left(v.confidence.empty() ? std::string("—") : v.confidence, 10).c_str(),
                        pad_left(fmt(v.weight, 1), 6).c_str());
        }
        if (!rnd.changed.empty()) {
            // 改口名单是"交叉质证起没起作用"的直接证据：一条都没有说明
            // 第二轮什么都没改变，那这一轮就是白跑的。
            std::printf("  %s  %s\n", pad_right("改口", 20).c_str(),
                        join(rnd.changed).c_str());
        }
        if (!rnd.verdicts.empty()) {
            std::printf("  %s  %s\n", pad_right("本轮耗时", 20).c_str(),
                        (fmt(rnd.duration_ms, 1) + " ms").c_str());
        }
    }

    if (!out.final_directions.empty()) {
        std::printf("\n  ── 最终立场（取自最后一轮）──\n");
        for (const auto& [role, dir] : out.final_directions) {
            std::printf("  %s  %s\n", pad_right(role, 24).c_str(),
                        dir.empty() ? "弃权（未表态，不计入分母）" : dir.c_str());
        }
    }

    if (opt_.agent_report && !out.chair.text.empty()) {
        const std::string who = out.chair.role_name.empty() ? out.chair.role : out.chair.role_name;
        std::printf("\n  ── %s 的报告 ──\n", who.c_str());
        std::printf("%s\n", out.chair.text.c_str());
    }

    // ── 推理后端：它到底在拿什么思考 ──────────────────────────
    //
    // 这一段回答的是"这个 agent 是真在用大模型，还是在跑内置的规则后端"。
    // 在这之前这个信息**已经**在 RPC 返回值里了，只是界面从不显示 ——
    // 而默认配置写的是 rule_based，于是它看起来就像"根本没连模型"。
    // 摆出来之后，"填了密钥却没生效（被降级）"这种问题也能一眼看见。
    {
        std::printf("\n  ── 推理后端（每个角色实际用的模型）──\n");

        std::map<std::string, int> counts;
        long long tin = 0, tout = 0;
        bool external = false;
        // 去重：同一个角色跑两轮会贡献两条，同一个降级原因也会被每个角色
        // 各报一次。原样打印出来就是六行一模一样的字 —— 有用的信息被
        // 淹没在重复里，等于没打印。
        std::set<std::string> degraded;
        std::set<std::string> reasons;

        const auto tally = [&](const AgentService::Verdict& v) {
            const std::string back = v.provider.empty() ? "未报告" : v.provider;
            ++counts[back];
            tin  += v.prompt_tokens;
            tout += v.completion_tokens;
            if (v.used_external()) external = true;
            if (!v.fallback_reason.empty()) {
                degraded.insert(v.role_name.empty() ? v.role : v.role_name);
                reasons.insert(v.fallback_reason);
            }
        };
        for (const auto& rnd : out.rounds) {
            for (const auto& v : rnd.verdicts) tally(v);
        }
        if (!out.chair.provider.empty()) tally(out.chair);

        std::vector<std::string> parts;
        for (const auto& kvp : counts) parts.push_back(kvp.first + " ×" + std::to_string(kvp.second));
        kv("后端分布", join(parts));
        kv("Token 用量", "输入 " + fmt_int(tin) + "   输出 " + fmt_int(tout)
                             + "   合计 " + fmt_int(tin + tout));

        if (external) {
            kv("外部模型", "[OK] 本轮确实跑在大模型上");
        } else {
            kv("外部模型", "[--] 全部跑在内置规则后端上 —— 未连接任何大模型");
            note("规则后端不联网、不做推理：它按工具输出的**真实数值**把各段落渲染成");
            note("规定格式。结构一致、可离线复现，但没有自然语言推理。");
            note("接上大模型（任选其一）：");
            note("  1) 命令行: --provider deepseek --api-key <你的密钥> --model deepseek-chat");
            note("  2) 配置  : 改 python/finpulse_engine/agent/configs/<角色>.json 的 config.model");
            note("  3) 环境变量: 导出 DEEPSEEK_API_KEY / OPENAI_API_KEY / MOONSHOT_API_KEY 等");
        }
        if (!degraded.empty()) {
            kv("降级", join(std::vector<std::string>(degraded.begin(), degraded.end()))
                           + " 配置的后端未生效，已回落规则后端");
            for (const auto& r : reasons) note(r);
        }
    }

    // ── 反向工具通道的实测证据 ────────────────────────────────
    //
    // 这一段是刻意留的。"C++ 与 Python 双向联动"这句话只有把**真实发生的
    // 调用次数**打出来才算数 —— 数字是 0 就说明通道没通，而这种情况在
    // 界面上表现为"报告里少了几个数字"，非常容易被当成正常输出放过。
    std::printf("\n  ── 反向工具通道（Python 回调 C++ 终端）──\n");
    const ToolBridge::Stats bs = bridge.stats();
    kv("工具调用", fmt_int(static_cast<long long>(bs.tool_calls)) + " 次");
    kv("收到 HTTP 请求", fmt_int(static_cast<long long>(bs.requests)) + " 次");
    kv("拒绝 令牌/主机/格式",
       fmt_int(static_cast<long long>(bs.rejected_auth)) + " / "
           + fmt_int(static_cast<long long>(bs.rejected_host)) + " / "
           + fmt_int(static_cast<long long>(bs.rejected_bad_request)));
    kv("未知工具", fmt_int(static_cast<long long>(bs.unknown_tools)) + " 次");
    if (bridge.running() && bs.tool_calls == 0) {
        note("调用为 0：本轮委员可能都没声明终端工具（不是故障）");
    }

    const Json ss = svc.stream_stats();
    kv("事件", "总线收到 " + std::to_string(events.load()) + " 条"
                   + "   引擎转发 " + fmt_int(static_cast<long long>(svc.events_forwarded())) + " 条");
    kv("引擎侧推送", fmt_int(ss["emitted"].as_int_or(0)) + " 条   丢弃 "
                         + fmt_int(ss["dropped"].as_int_or(0)) + " 条");

    AgentSummary sum;
    sum.valid      = out.valid;
    sum.run_id     = out.run_id;
    sum.direction  = out.direction;
    sum.confidence = out.confidence;
    sum.tool_calls = static_cast<std::size_t>(bs.tool_calls);
    return sum;
}

// ── 主流程 ────────────────────────────────────────────────

PyEngine::Config CliApp::build_engine_config(const Options& o) {
    PyEngine::Config cfg;
    cfg.python      = o.python;
    cfg.python_root = o.python_root;
    cfg.verbose     = o.verbose;
    // 智能体研判一场要跑 3 位委员 × 2 轮 + 主席，是这里最慢的一类调用。
    // 30 秒对指标/回测富余，对研判则可能在慢机器上踩线；踩线的表现是
    // "投委会随机失败"，最难查。开了 agent 就放宽。
    cfg.call_timeout_ms = o.agent ? 120000 : 30000;
    return cfg;
}

CliApp::CliApp(Options opt)
    : opt_(std::move(opt)), engine_(build_engine_config(opt_)) {}

int CliApp::run() {
    const auto t_start = Clock::now();

    std::printf("\n");
    std::printf("  %s   v%s\n", kAppName, kVersion);
    std::printf("  C++20 / Qt6 桌面壳  +  嵌入式 Python 分析引擎\n");

    // "它为什么自己去拉了茅台" —— 必须有个能读到的答案，否则配置档
    // 就成了一个看不见的隐形输入。
    if (!opt_.profile_note.empty()) note(opt_.profile_note);

    // 保存配置档**在启动引擎之前**：这一步不需要 Python，
    // 引擎起不来时也不该拦住"把设置存下来"这件事。
    if (opt_.save_profile && !save_profile_now()) return 2;

    try {
        // ── 引擎 ──────────────────────────────────────────
        section(stage(1) + " 启动分析引擎");
        auto t0 = Clock::now();
        engine_.start();
        const auto& inf = engine_.info();

        kv("解释器", engine_.interpreter());
        kv("引擎", std::string(inf.name) + " v" + inf.version
                       + "  (Python " + inf.python_version + ")");
        kv("协议版本", "v" + std::to_string(inf.protocol));
        kv("可用数据源", join(inf.sources));
        kv("可用预测方法", join(inf.forecasters));
        kv("冷启动耗时", fmt(elapsed_ms(t0), 1) + " ms");

        // ── 只拉数据就退出 ────────────────────────────────
        //
        // 放在 --list 之前：拉数据与分析报告是两件事，用户敲 --pull 想要的
        // 就是"把本地数据刷一遍"，再顺手跑一份 6 节报告只会淹掉他要看的
        // 那张汇总表。
        if (opt_.pull) return run_pull();

        // ── 只列配置就退出 ────────────────────────────────
        // 挑 --role / --panel 的取值时不该被逼着先跑完一次完整分析。
        if (opt_.agent_list) {
            AgentService svc(engine_, &DataHub::instance());

            section("可用角色（--role <id>）");
            std::printf("  %s %s %s %s\n",
                        pad_right("id", 24).c_str(), pad_right("名称", 22).c_str(),
                        pad_right("类别", 14).c_str(), "方向来源段");
            for (const auto& r : svc.roles()) {
                std::printf("  %s %s %s %s\n",
                            pad_right(r.id, 24).c_str(),
                            pad_right(r.name, 22).c_str(),
                            pad_right(r.category, 14).c_str(),
                            join(r.direction_sections).c_str());
            }

            section("可用投委会（--panel <id>）");
            for (const auto& p : svc.panels()) {
                std::printf("  %s  %s\n", p.id.c_str(), p.name.c_str());
                std::printf("    主席 %s   quorum=%d   轮数 %d   %s\n",
                            p.chair.c_str(), p.quorum, p.rounds,
                            p.valid ? "[OK] 配置有效" : "[!!] 配置有问题");
                for (const auto& m : p.members) {
                    std::printf("      · %s   权重 %.2f   交叉质证 %s\n",
                                m.role.c_str(), m.weight,
                                m.cross_examine ? "是" : "否");
                }
                for (const auto& prob : p.problems) std::printf("      [!] %s\n", prob.c_str());
            }
            std::printf("\n");
            return 0;
        }

        // ── 数据 ──────────────────────────────────────────
        section(stage(2) + " 装载行情");
        t0 = Clock::now();

        Json load_params = Json::object();
        load_params.set("source", opt_.source);
        load_params.set("symbol", opt_.symbol);
        load_params.set("bars", static_cast<long long>(opt_.bars));
        if (!opt_.csv_path.empty()) load_params.set("path", opt_.csv_path);
        if (opt_.source == "synthetic" && opt_.seed >= 0) load_params.set("seed", opt_.seed);
        // token 只对 tushare 有意义；其余数据源的 load 都收 **kwargs，
        // 多传一个也用不上，但为了报告里不出现"无意义的参数"这里判断一下。
        if (opt_.source == "tushare" && !opt_.tushare_token.empty()) {
            load_params.set("token", opt_.tushare_token);
        }

        const Json loaded = engine_.rpc().call("source.load", std::move(load_params), 30000);
        CandleSeries series = CandleSeries::from_json(loaded);
        series.set_symbol(opt_.symbol);

        print_series(series, loaded);
        kv("载入耗时", fmt(elapsed_ms(t0), 1) + " ms");

        if (!loaded["warnings"].is_array() || loaded["warnings"].size() == 0) {
            kv("数据质量", "[OK] 未见异常");
        } else {
            kv("数据质量", "[!!] " + std::to_string(loaded["warnings"].size()) + " 项提示，首个："
                                + loaded["warnings"].at(0).as_string_or(""));
        }

        // ── 指标 ──────────────────────────────────────────
        section(stage(3) + " 技术指标（引擎侧计算）");
        t0 = Clock::now();

        Json ind_params = Json::object();
        Json bars_json = Json::array();
        for (const auto& b : series.bars()) bars_json.push(b.to_json());
        ind_params.set("bars", std::move(bars_json));
        Json spec_arr = Json::array();
        for (const auto& s : opt_.indicators) spec_arr.push(Json(s));
        ind_params.set("specs", std::move(spec_arr));

        const Json ind_res = engine_.rpc().call("analysis.indicators", std::move(ind_params), 30000);
        print_indicators(ind_res);
        kv("计算耗时", fmt(elapsed_ms(t0), 1) + " ms");

        // ── 风险 ──────────────────────────────────────────
        section(stage(4) + " 风险概览");
        t0 = Clock::now();

        Json stats_params = Json::object();
        Json bars2 = Json::array();
        for (const auto& b : series.bars()) bars2.push(b.to_json());
        stats_params.set("bars", std::move(bars2));

        const Json stats_res = engine_.rpc().call("analysis.stats", std::move(stats_params), 30000);
        print_stats(stats_res);
        kv("计算耗时", fmt(elapsed_ms(t0), 1) + " ms");

        // ── 预测 ──────────────────────────────────────────
        section(stage(5) + " 预测（含 95% 置信区间）");
        t0 = Clock::now();

        const ForecastResult fc = engine_.forecast(series.bars(), opt_.forecast_method,
                                                   opt_.horizon);
        print_forecast(fc);
        kv("耗时", fmt(elapsed_ms(t0), 1) + " ms");

        // ── 回测 ──────────────────────────────────────────
        section(stage(6) + " 滚动回测（walk-forward，随机游走作对照）");
        t0 = Clock::now();

        Json bt_params = Json::object();
        Json bars3 = Json::array();
        for (const auto& b : series.bars()) bars3.push(b.to_json());
        bt_params.set("bars", std::move(bars3));
        bt_params.set("method", opt_.forecast_method);
        bt_params.set("horizon", static_cast<long long>(opt_.horizon));
        bt_params.set("folds", static_cast<long long>(opt_.folds));
        bt_params.set("min_train", static_cast<long long>(opt_.min_train));

        const Json bt_raw = engine_.rpc().call("forecast.backtest", std::move(bt_params), 60000);
        print_backtest(bt_raw, BacktestMetrics::from_json(bt_raw));
        kv("耗时", fmt(elapsed_ms(t0), 1) + " ms");

        // ── 智能体研判 ────────────────────────────────────
        //
        // 复用同一个引擎进程：分析方法与 agent 方法装在**同一把** RPC 通道上，
        // 所以研判用的行情就是上面刚装载、刚算完指标和回测的那一份，不需要
        // 重新喂一遍，也不会有"两个进程看到两份数据"的问题。
        AgentSummary agent;
        if (opt_.agent) {
            agent = run_agent(series);
        }

        // ── 总线演示 ──────────────────────────────────────
        if (opt_.bus_demo) {
            run_bus_demo(series);
        }

        // ── 汇总 ──────────────────────────────────────────
        section("完成");
        const auto rs = engine_.rpc().stats();
        kv("RPC 调用", fmt_int(static_cast<long long>(rs.requests_sent)) + " 次"
                           + "   成功 " + fmt_int(static_cast<long long>(rs.responses_ok))
                           + "   引擎报错 " + fmt_int(static_cast<long long>(rs.responses_err))
                           + "   超时 " + fmt_int(static_cast<long long>(rs.timeouts)));
        kv("引擎重启次数", std::to_string(engine_.restart_count()));
        kv("总耗时", fmt(elapsed_ms(t_start), 1) + " ms");

        if (opt_.agent) {
            // 三种结果分开写，理由同 run_agent 里那段。
            const std::string verdict =
                !agent.valid ? "未形成有效决议"
                             : (agent.direction.empty() ? "有效但未给方向" : agent.direction);
            kv("智能体决议", verdict + "   运行 " + agent.run_id
                                 + "   反向工具调用 "
                                 + fmt_int(static_cast<long long>(agent.tool_calls)) + " 次");
        }

        if (opt_.json_out) {
            Json summary = Json::object();
            summary.set("symbol", opt_.symbol);
            summary.set("source", opt_.source);
            summary.set("bars", static_cast<long long>(series.size()));
            summary.set("last_close", series.empty() ? 0.0 : series.back().close);
            summary.set("skill", BacktestMetrics::from_json(bt_raw).skill);
            summary.set("rpc_calls", static_cast<long long>(rs.requests_sent));
            summary.set("elapsed_ms", elapsed_ms(t_start));
            if (opt_.agent) {
                summary.set("agent_valid", agent.valid);
                summary.set("agent_direction", agent.direction);
                summary.set("agent_run_id", agent.run_id);
                summary.set("agent_tool_calls", static_cast<long long>(agent.tool_calls));
            }
            std::printf("\n%s\n", summary.dump().c_str());
        }

        std::printf("\n");
        return 0;
    } catch (const RpcError& e) {
        std::fprintf(stderr, "\n[失败] 引擎调用出错: %s", e.what());
        if (!e.code_name().empty()) std::fprintf(stderr, "  (%s)", e.code_name().c_str());
        std::fprintf(stderr, "\n");
        if (!e.detail().empty()) std::fprintf(stderr, "  细节: %s\n", e.detail().c_str());
        return 2;
    } catch (const EngineError& e) {
        std::fprintf(stderr, "\n[失败] %s\n", e.what());
        return 3;
    } catch (const std::exception& e) {
        std::fprintf(stderr, "\n[失败] %s\n", e.what());
        return 4;
    }
}

void CliApp::print_usage(const char* argv0) {
    std::printf(R"(FinPulse Terminal — 命令行模式

用法:
  %s [选项]

数据:
  --source <name>      数据源: tushare / csv / synthetic  (默认 用配置档，否则 synthetic)
  --symbol <SYM>       标的代码，如 600519.SH            (默认 用配置档，否则 SYNTH)
  --bars <n>           取多少根 K 线                     (默认 250)
  --csv <path>         指定 CSV 文件（source=csv 时用）
  --seed <n>           合成数据随机种子，-1 表示随机     (默认 42)
  --tushare-token <t>  Tushare 密钥（也可用环境变量 TUSHARE_TOKEN，
                       或写进启动配置档；命令行会留在 shell 历史里）

启动配置档（让"打开就是上证的实时数据"不必每次重配）:
  --save-profile       把当前 --source/--symbol/--bars/--tushare-token
                       写成 ~/.finpulse/profile.json（权限 0600，不进仓库）
  --no-profile         本次完全忽略配置档（复现、CI 用）

批量拉取:
  --pull               只做批量拉取：把清单逐个取数落成 data/<代码>.csv，
                       然后退出（默认清单是配置档的 watchlist，否则内置上证成分股）
  --pull-symbols <s>   指定要拉的标的，逗号或空白分隔，可重复
                       例: --pull-symbols 600519.SH,601318.SH

分析:
  --indicator <spec>   指标规格，可重复。例: ma:5,20      (可多次指定)
  --method <name>      预测方法: ar / randomwalk         (默认 ar)
  --horizon <n>        预测步长                          (默认 5)
  --folds <n>          回测折数                          (默认 5)
  --min-train <n>      最小训练窗口                      (默认 60)

智能体研判:
  --agent              跑一场投委会研判（独立研判 → 交叉质证 → 主席综合）
  --role <id>          只跑这一个角色，不组会。唯一值见 --agent --list
  --panel <id>         指定投委会                          (默认 default_committee)
  --rounds <n>         覆盖投委会配置的轮数                (默认 用配置)
  --no-bridge          不起终端工具桥，验证"工具不可用"的降级路径
  --brief              不打印报告正文，只看结论与投票
  --list               列出可用角色与投委会后退出

推理后端（接大模型；不填则用角色配置里的，默认是内置规则后端）:
  --provider <name>    后端: rule_based / openai / deepseek / moonshot /
                       dashscope / openrouter / groq / ollama / openai_compat
  --model <id>         模型名，例如 deepseek-chat、gpt-4o-mini
  --base-url <url>     自定义端点（本地 vLLM / LM Studio / 自建网关）
  --api-key <key>      API 密钥。**只作用于本次运行，不写入任何文件**；
                       留空则回落到对应环境变量（如 DEEPSEEK_API_KEY）

  例: --provider deepseek --model deepseek-chat --api-key <你的密钥>
      --provider ollama --model qwen2.5:7b --base-url http://127.0.0.1:11434/v1

运行:
  --python <path>      指定 Python 解释器（默认自动探测）
  --python-root <dir>  引擎包所在目录
  --replay             回放模式：把行情按时间轴投递到 DataHub
  --replay-speed <x>   回放倍速，0 表示全速              (默认 0)
  --bus-demo           展示 DataHub 的订阅与投递统计
  --json               结束时额外输出一行汇总 JSON
  -v, --verbose        打开引擎与总线的调试日志
  -h, --help           显示本帮助

示例:
  %s --bars 500 --method ar --folds 8 --horizon 5
  %s --source tushare --symbol 600519.SH --bars 250
  %s --pull --pull-symbols 600519.SH,601318.SH
  %s --tushare-token <你的token> --save-profile
  %s --source csv --symbol AAPL --csv data/AAPL.csv --bus-demo
  %s --replay --replay-speed 200 --bus-demo
  %s --agent --bars 300 --json
  %s --agent --role risk_officer --bars 300 --brief
  %s --agent --provider deepseek --model deepseek-chat --api-key <密钥>
)",
                argv0, argv0, argv0, argv0, argv0, argv0, argv0, argv0, argv0, argv0);
}

}  // namespace fp
