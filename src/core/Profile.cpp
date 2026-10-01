#include "core/Profile.h"

#include "core/Json.h"
#include "core/Log.h"

#include <cstdlib>
#include <filesystem>
#include <fstream>
#include <sstream>
#include <system_error>

namespace fp {
namespace {

constexpr const char* kTag = "profile";

/// 配置档文件名。放在 `~/.finpulse/` 下 —— 见头文件里关于"为什么不进仓库"的说明。
constexpr const char* kDirName  = ".finpulse";
constexpr const char* kFileName = "profile.json";

/// 家目录。Windows 上没有 HOME，用 USERPROFILE。
std::string home_dir() {
    for (const char* name : {"HOME", "USERPROFILE"}) {
        if (const char* v = std::getenv(name)) {
            if (*v != '\0') return v;
        }
    }
    return {};
}

/// 读一个可能带引号的字符串字段。类型不对时**记一笔并跳过**，不整份作废：
/// 一个字段写错不该让其它三项一起失效，但也不能假装没这回事。
///
/// 顺带说一句：token 从不进日志。这个函数只回值，不回显。
bool take_string(const Json& obj, const char* key, std::string& out, std::string& problems) {
    const Json& v = obj[key];
    if (v.is_null()) return false;
    if (!v.is_string()) {
        problems += std::string(problems.empty() ? "" : "；") + key + " 不是字符串（已忽略该项）";
        return false;
    }
    out = v.as_string();
    return true;
}

}  // namespace

bool Profile::empty() const noexcept {
    return source.empty() && symbol.empty() && bars == 0 && csv_path.empty() &&
           tushare_token.empty() && watchlist.empty() &&
           llm_provider.empty() && llm_model.empty() && llm_base_url.empty() &&
           llm_api_key.empty();
}

std::string default_profile_path() {
    if (const char* env = std::getenv("FINPULSE_PROFILE")) {
        if (*env != '\0') return env;
    }
    const std::string home = home_dir();
    if (home.empty()) return {};   // 连家目录都问不到，就当没有配置档
    return (std::filesystem::path(home) / kDirName / kFileName).string();
}

Profile load_profile(const std::string& path) {
    Profile p;
    p.path = path.empty() ? default_profile_path() : path;
    if (p.path.empty()) return p;

    std::error_code ec;
    if (!std::filesystem::exists(p.path, ec) || ec) {
        // 没配过 —— 这是常态，不是错误。首次运行的用户不该被一条报错吓到。
        FP_DEBUG(kTag, "未找到启动配置档 " << p.path << "（不影响使用，走内置默认值）");
        return p;
    }

    std::ifstream in(p.path, std::ios::binary);
    if (!in) {
        p.error = "打不开文件（检查权限）: " + p.path;
        return p;
    }
    std::ostringstream buf;
    buf << in.rdbuf();

    Json root;
    try {
        root = Json::parse(buf.str());
    } catch (const std::exception& e) {
        // 坏文件必须报出去。静默忽略的后果是"我明明配了却不生效"。
        p.error = std::string("不是合法 JSON（") + e.what() + "）: " + p.path;
        return p;
    }
    if (!root.is_object()) {
        p.error = "顶层必须是 JSON 对象: " + p.path;
        return p;
    }

    std::string problems;
    take_string(root, "source", p.source, problems);
    take_string(root, "symbol", p.symbol, problems);
    take_string(root, "csv_path", p.csv_path, problems);
    take_string(root, "tushare_token", p.tushare_token, problems);
    take_string(root, "llm_provider", p.llm_provider, problems);
    take_string(root, "llm_model", p.llm_model, problems);
    take_string(root, "llm_base_url", p.llm_base_url, problems);
    take_string(root, "llm_api_key", p.llm_api_key, problems);

    const Json& bars = root["bars"];
    if (bars.is_number()) {
        const long long b = bars.as_int_or(0);
        if (b > 0) {
            p.bars = static_cast<std::size_t>(b);
        } else {
            problems += std::string(problems.empty() ? "" : "；") + "bars 必须为正整数（已忽略该项）";
        }
    } else if (!bars.is_null()) {
        problems += std::string(problems.empty() ? "" : "；") + "bars 不是数字（已忽略该项）";
    }

    const Json& watch = root["watchlist"];
    if (watch.is_array()) {
        for (const Json& item : watch.items()) {
            if (item.is_string() && !item.as_string().empty()) {
                p.watchlist.push_back(item.as_string());
            }
        }
        if (p.watchlist.empty()) {
            problems += std::string(problems.empty() ? "" : "；") + "watchlist 里没有有效代码（已忽略该项）";
        }
    } else if (!watch.is_null()) {
        problems += std::string(problems.empty() ? "" : "；") + "watchlist 不是数组（已忽略该项）";
    }

    p.loaded = true;
    p.error  = problems;

    // 日志里只出现"有没有 token"，绝不出现 token 本身 —— 日志会被贴到 issue 里、
    // 会被截图、会被拿去做演示。
    FP_INFO(kTag, "读取启动配置档 " << p.path
                    << "（source=" << (p.source.empty() ? "未设置" : p.source)
                    << " symbol=" << (p.symbol.empty() ? "未设置" : p.symbol)
                    << " watchlist=" << p.watchlist.size() << " 项"
                    << " tushare_token=" << (p.has_token() ? "已配置" : "未配置")
                    << " llm=" << (p.llm_provider.empty() ? "未设置" : p.llm_provider)
                    << "/" << (p.has_llm_key() ? "密钥已配置" : "密钥未配置") << "）");
    if (!problems.empty()) FP_WARN(kTag, "配置档有字段被忽略：" << problems);
    return p;
}

std::string save_profile(const Profile& p, const std::string& path) {
    const std::string target = path.empty() ? (p.path.empty() ? default_profile_path() : p.path) : path;
    if (target.empty()) return "无法确定配置档路径（家目录读不到，也没设 FINPULSE_PROFILE）";

    Json root = Json::object();
    root.set("source", Json(p.source));
    root.set("symbol", Json(p.symbol));
    if (p.bars > 0) root.set("bars", Json(static_cast<long long>(p.bars)));
    if (!p.csv_path.empty()) root.set("csv_path", Json(p.csv_path));
    root.set("tushare_token", Json(p.tushare_token));
    // 推理后端：只写非空项。空着就不出现，配置文件里干干净净 ——
    // 一眼能看出"我到底配了什么"，而不是一堆空字符串。
    if (!p.llm_provider.empty()) root.set("llm_provider", Json(p.llm_provider));
    if (!p.llm_model.empty()) root.set("llm_model", Json(p.llm_model));
    if (!p.llm_base_url.empty()) root.set("llm_base_url", Json(p.llm_base_url));
    if (!p.llm_api_key.empty()) root.set("llm_api_key", Json(p.llm_api_key));
    if (!p.watchlist.empty()) {
        Json arr = Json::array();
        for (const std::string& code : p.watchlist) arr.push(Json(code));
        root.set("watchlist", std::move(arr));
    }

    std::error_code ec;
    const std::filesystem::path fs_path(target);
    if (fs_path.has_parent_path()) {
        std::filesystem::create_directories(fs_path.parent_path(), ec);
        if (ec) return "建目录失败: " + fs_path.parent_path().string() + "（" + ec.message() + "）";
    }

    std::ofstream out(target, std::ios::binary | std::ios::trunc);
    if (!out) return "写不了文件（检查权限）: " + target;
    out << root.dump(2) << "\n";
    out.close();
    if (!out) return "写入过程中出错: " + target;

    // 只有本人可读写。token 就在这个文件里，其它权限都是多余的敞口。
    // Windows 上这一步基本是空操作，但代码不该因此分叉成两套。
    std::filesystem::permissions(target,
                                 std::filesystem::perms::owner_read |
                                     std::filesystem::perms::owner_write,
                                 std::filesystem::perm_options::replace, ec);
    if (ec) FP_WARN(kTag, "未能收紧文件权限（" << ec.message() << "）: " << target);

    FP_INFO(kTag, "已保存启动配置档 " << target);
    return {};
}

}  // namespace fp
