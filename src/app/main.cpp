// FinPulse Terminal — 命令行入口
//
// 参数解析刻意手写而不是引 CLI11 / cxxopts：
// 一共十来个开关，手写 80 行就能覆盖，还省掉一个构建期依赖。
// 等开关数量上到几十个再换库也不迟。

#include "app/CliApp.h"

#include "core/Log.h"
#include "core/Profile.h"
#include "core/Version.h"

#include <cstdio>
#include <cstdlib>
#include <exception>
#include <string>
#include <vector>

// CMake 会把源码树里的 python/ 目录路径注进来。
// 运行时也可以被环境变量 FINPULSE_PYTHON_ROOT 覆盖，
// 这样把二进制拷到别处去跑也能找到引擎。
#ifndef FINPULSE_PYTHON_ROOT
#  define FINPULSE_PYTHON_ROOT ""
#endif

namespace {

std::string default_python_root() {
    if (const char* env = std::getenv("FINPULSE_PYTHON_ROOT")) {
        if (*env != '\0') return env;
    }
    return FINPULSE_PYTHON_ROOT;
}

void die(const char* fmt, const std::string& arg) {
    std::fprintf(stderr, fmt, arg.c_str());
    std::fprintf(stderr, "\n");
}

std::string join(const std::vector<std::string>& v, const char* sep) {
    std::string out;
    for (std::size_t i = 0; i < v.size(); ++i) {
        if (i) out += sep;
        out += v[i];
    }
    return out;
}

/// 逗号分隔的标的清单：``--pull-symbols 600519.SH,601318.SH``。
/// 空白也当分隔符 —— 用户从表格里复制粘贴时，分隔符往往是空白。
std::vector<std::string> split_symbols(const std::string& text) {
    std::vector<std::string> out;
    std::string cur;
    for (char c : text) {
        if (c == ',' || c == ' ' || c == '\t' || c == ';') {
            if (!cur.empty()) { out.push_back(cur); cur.clear(); }
        } else {
            cur.push_back(c);
        }
    }
    if (!cur.empty()) out.push_back(cur);
    return out;
}

}  // namespace

int main(int argc, char** argv) {
    fp::CliApp::Options opt;
    opt.python_root = default_python_root();

    // 只有用户显式给了 --indicator 时才丢掉默认指标集
    bool indicators_given = false;

    // 哪些参数**用户真的给了**。启动配置档只负责兜底，不能覆盖用户
    // 在命令行上写下的东西 —— "我明明指定了 --symbol 却被配置文件改掉"
    // 是一类没法解释的行为。
    bool source_given = false, symbol_given = false, bars_given = false;
    bool csv_given = false, token_given = false;

    for (int i = 1; i < argc; ++i) {
        const std::string a = argv[i];

        const auto next = [&](const char* what) -> std::string {
            if (i + 1 >= argc) {
                die("参数 %s 缺少取值", what);
                std::exit(2);
            }
            return argv[++i];
        };

        const auto as_size = [&](const char* what) -> std::size_t {
            const std::string v = next(what);
            try {
                return static_cast<std::size_t>(std::stoull(v));
            } catch (const std::exception&) {
                die(std::string("%s 的取值不是合法整数: ").append(what).c_str(), v);
                std::exit(2);
            }
        };

        if (a == "-h" || a == "--help") {
            fp::CliApp::print_usage(argv[0]);
            return 0;
        } else if (a == "--source") {
            opt.source = next("--source");
            source_given = true;
        } else if (a == "--symbol") {
            opt.symbol = next("--symbol");
            symbol_given = true;
        } else if (a == "--bars") {
            opt.bars = as_size("--bars");
            bars_given = true;
        } else if (a == "--csv") {
            opt.csv_path = next("--csv");
            csv_given = true;
        } else if (a == "--tushare-token") {
            opt.tushare_token = next("--tushare-token");
            token_given = true;
        } else if (a == "--save-profile") {
            opt.save_profile = true;
        } else if (a == "--no-profile") {
            opt.use_profile = false;
        } else if (a == "--pull") {
            opt.pull = true;
        } else if (a == "--pull-symbols") {
            const std::vector<std::string> more = split_symbols(next("--pull-symbols"));
            opt.pull_symbols.insert(opt.pull_symbols.end(), more.begin(), more.end());
        } else if (a == "--seed") {
            opt.seed = std::stoll(next("--seed"));
        } else if (a == "--indicator") {
            if (!indicators_given) {
                opt.indicators.clear();
                indicators_given = true;
            }
            opt.indicators.push_back(next("--indicator"));
        } else if (a == "--method") {
            opt.forecast_method = next("--method");
        } else if (a == "--horizon") {
            opt.horizon = as_size("--horizon");
        } else if (a == "--folds") {
            opt.folds = as_size("--folds");
        } else if (a == "--min-train") {
            opt.min_train = as_size("--min-train");
        } else if (a == "--python") {
            opt.python = next("--python");
        } else if (a == "--python-root") {
            opt.python_root = next("--python-root");
        } else if (a == "--replay-speed") {
            opt.replay_speed = std::stod(next("--replay-speed"));
        } else if (a == "--replay" || a == "--bus-demo") {
            // 回放本身就是总线演示，两个开关等同
            opt.bus_demo = true;
        } else if (a == "--agent") {
            opt.agent = true;
        } else if (a == "--role") {
            opt.agent       = true;
            opt.agent_role  = next("--role");
        } else if (a == "--panel") {
            opt.agent       = true;
            opt.agent_panel = next("--panel");
        } else if (a == "--rounds") {
            opt.agent        = true;
            opt.agent_rounds = static_cast<int>(as_size("--rounds"));
        } else if (a == "--provider") {
            opt.agent          = true;
            opt.agent_provider = next("--provider");
        } else if (a == "--model") {
            opt.agent       = true;
            opt.agent_model = next("--model");
        } else if (a == "--base-url") {
            opt.agent          = true;
            opt.agent_base_url = next("--base-url");
        } else if (a == "--api-key") {
            opt.agent         = true;
            opt.agent_api_key = next("--api-key");
        } else if (a == "--no-bridge") {
            opt.agent_no_bridge = true;
        } else if (a == "--brief") {
            opt.agent_report = false;
        } else if (a == "--list") {
            opt.agent      = true;
            opt.agent_list = true;
        } else if (a == "--json") {
            opt.json_out = true;
        } else if (a == "-v" || a == "--verbose") {
            opt.verbose = true;
        } else {
            die("未知参数: %s", a);
            fp::CliApp::print_usage(argv[0]);
            return 2;
        }
    }

    // ── 启动配置档兜底 ────────────────────────────────────
    //
    // 规则只有一条：**命令行 > 配置档 > 内置默认**。于是"打开就用什么数据"
    // 这件事只需配置一次，而任何一次临时覆盖都不会被写回去。
    if (opt.use_profile) {
        const fp::Profile prof = fp::load_profile();
        if (!prof.error.empty()) {
            // 配置档坏了必须说出来。静默忽略的后果是"我明明配了却不生效"。
            std::fprintf(stderr, "[!] 启动配置档有问题：%s\n", prof.error.c_str());
        }
        std::vector<std::string> applied;
        if (!source_given && !prof.source.empty()) {
            opt.source = prof.source;
            applied.push_back("数据源=" + prof.source);
        }
        if (!symbol_given && !prof.symbol.empty()) {
            opt.symbol = prof.symbol;
            applied.push_back("标的=" + prof.symbol);
        }
        if (!bars_given && prof.bars > 0) {
            opt.bars = prof.bars;
            applied.push_back("根数=" + std::to_string(prof.bars));
        }
        if (!csv_given && !prof.csv_path.empty()) {
            opt.csv_path = prof.csv_path;
            applied.push_back("csv=" + prof.csv_path);
        }
        if (!token_given && prof.has_token()) {
            opt.tushare_token = prof.tushare_token;
            applied.push_back("tushare_token=已配置");
        }
        if (!applied.empty()) {
            opt.profile_note = "启动配置档 " + prof.path + "\n→ " +
                               join(applied, "、") + "（命令行参数优先于此档）";
        }
    }

    // 默认只显示 Warn 及以上：CLI 的分节报告本身已经把引擎信息、耗时、
    // 统计量都列全了，再叠一层 INFO 日志只会让报告变脏。
    // 排查问题时加 -v 打开完整日志。
    fp::log_set_level(opt.verbose ? fp::LogLevel::Debug : fp::LogLevel::Warn);

    fp::CliApp app(std::move(opt));
    return app.run();
}
