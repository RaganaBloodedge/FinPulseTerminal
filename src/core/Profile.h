// FinPulse Terminal — 启动配置档（profile）
//
// 解决的问题很小但很烦：每开一次终端，都要重新选数据源、重新敲标的代码。
// 有了它，"打开就看宁德时代的实时日线"变成默认行为，而不是每次的六个动作。
//
// **为什么放在用户目录而不是仓库里**：
//   * 这是"我的偏好"，不是项目的属性 —— 配置跟着人走，不跟着 checkout 走；
//   * token 就在里面。仓库里的文件会被提交、会被截图、会被拷来拷去，
//     密钥不该待在那样的地方。`~/.finpulse/profile.json` 只有本人可读。
//
// **密钥仍然有优先级**：命令行/运行时参数 > 环境变量 > 配置档。
// 配置档只是"省一次输入"，不是唯一入口 —— 不想让 token 落盘的人
// 完全可以继续只用 TUSHARE_TOKEN 环境变量，其余字段照样从配置档来。
#pragma once

#include <cstddef>
#include <string>
#include <vector>

namespace fp {

/// 启动默认值。每个字段都可以为空 —— 空表示"这一项没设置"，
/// 由调用方自己的默认值兜底，而不是在这里硬塞一个默认值进来。
/// 不这么做的话，"没配"和"配成了内置默认"就无法区分了。
struct Profile {
    std::string source;         ///< 默认数据源，如 tushare
    std::string symbol;         ///< 默认标的，如 600519.SH
    std::size_t bars{0};        ///< 默认根数；0 = 未设置
    std::string csv_path;       ///< source=csv 时的默认文件
    std::string tushare_token;  ///< Tushare 密钥（明文；见文件头关于存放位置的说明）
    /// 「拉取数据」要拉的清单（代码数组）。空 = 用内置的上证成分股清单。
    std::vector<std::string> watchlist;

    // ── 推理后端（AI 研判）────────────────────────────────
    //
    // 与 tushare_token 同样的取舍：默认**不写**，只有当用户在设置界面
    // 明确勾了「记住密钥」才会落盘。勾了就是知情选择，没勾就一个字都不写。
    std::string llm_provider;
    std::string llm_model;
    std::string llm_base_url;
    std::string llm_api_key;

    std::string path;           ///< 实际读取/写入的路径（空 = 没找到文件）
    std::string error;          ///< 文件在但读不动时的原因；空 = 正常
    bool        loaded{false};  ///< 是否真的读到了一个配置文件

    bool has_token() const noexcept { return !tushare_token.empty(); }
    bool has_llm_key() const noexcept { return !llm_api_key.empty(); }
    /// 一项都没设 —— 调用方可以据此完全忽略这个配置档。
    bool empty() const noexcept;
};

/// 配置档的默认位置：`$FINPULSE_PROFILE` 优先，其次是 `~/.finpulse/profile.json`。
/// Windows 上 `~` 取 `%USERPROFILE%`。
std::string default_profile_path();

/// 读取配置档。
///
/// **文件不存在不是错误**：返回 loaded=false 的空档，error 保持为空。
/// 只有"文件在、但坏了"（读不动 / 不是 JSON / 字段类型不对）才写 error ——
/// 那种情况必须让用户看见，否则会出现"我明明配了却不生效"这种最难查的问题。
Profile load_profile(const std::string& path = "");

/// 写入配置档：自动建目录、写紧凑缩进的 JSON、把文件权限设成 0600。
/// 返回空串表示成功，否则是给人看的失败原因。
///
/// 会**完整覆盖**文件内容（而不是合并），因为要支持"把某项清空"。
/// 调用方负责先把旧值并进来（GUI 的自动保存就是这么做的）。
std::string save_profile(const Profile& p, const std::string& path = "");

}  // namespace fp
