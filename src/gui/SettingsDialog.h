// FinPulse Terminal — 「AI 研判」的参数设置界面
//
// ## 这个界面只需要填一样东西：API 密钥
//
// 早先这里要用户填四样：服务商、模型名、端点、密钥。这对用户不公平 ——
// 「模型名」是服务商文档里的知识，「端点」更是，而这两样**服务商自己的
// ``GET /v1/models`` 接口就答得出来**。Cherry Studio / Open WebUI / Cline
// 的做法都是一样的：填密钥 → 点一下 → 模型列表自己出来，从下拉里挑。
// 这里对齐的就是这个交互。
//
// 于是流程变成：
//   1. 粘密钥 → 界面按密钥格式**本地**预选服务商（一个网络请求都不发）
//   2. 点「测试并获取模型」→ 只向**用户选定的那一家**要模型列表
//   3. 从下拉里挑模型 —— 不手打
//
// ## 为什么服务商必须由用户显式声明，而不是替他挨家试
//
// 「自动识别你在用哪家」听着更省事：拿密钥依次问 OpenAI、DeepSeek、
// Moonshot，谁答话就是谁。**但那会把用户的密钥发给它不属于的公司** ——
// 对方用不了它，可它已经泄露了。所以这里不做这件事；代价只是用户在下拉
// 里点一下名字。这也是本界面里唯一一个"必须由人来答"的问题。
//
// ## 密钥
//
// 默认**不落盘**。想记住得主动勾选「记住到启动配置档」—— 那是知情选择。
// 没勾就一个字都不写。
#pragma once

#include <QDialog>
#include <QString>

#include <string>
#include <vector>

class QCheckBox;
class QComboBox;
class QDoubleSpinBox;
class QFormLayout;
class QLabel;
class QLineEdit;
class QPushButton;
class QSpinBox;
class QToolButton;

namespace fp::gui {

/// 研判相关的全部可配置项。跨界面传递的可拷贝纯数据。
struct AgentSettings {
    // ── 模型服务 ──
    /// **必填**：用户显式声明的服务商。空 = 没配好，对话会退回规则后端。
    /// 不做"留空即用角色配置"那套 —— 那正是"我明明填了密钥却说没连模型"
    /// 的来源（密钥填了、provider 为空、引擎照角色配置走 rule_based）。
    std::string provider;
    /// 模型 id。**从服务商的 /v1/models 里选**，不是手打。
    std::string model;
    /// 高级：自建端点。选「自定义端点」时必填，其余留空走官方地址。
    std::string base_url;
    std::string api_key;
    double      temperature{0.3};
    int         max_tokens{1200};
    /// 是否把密钥写进启动配置档。**默认假** —— 见文件头。
    bool remember_key{false};

    // ── 投委会 ──
    std::string panel;   ///< 空 = 默认投委会
    std::string role;    ///< 非空 = 只跑这一个角色（不组会）
    int         rounds{0};   ///< 0 = 用配置里的轮数

    // ── 数据源 ──
    std::string tushare_token;
    bool        remember_tushare{false};
};

/// 引擎 ``agent.llm.probe`` 的结果。原样搬过来，界面只负责显示。
///
/// 这里用普通结构体而不是 ``Json``：对话框不必知道 RPC 的形状，
/// 解析放在 MainWindow（那边本来就在跟 ``Json`` 打交道）。
struct ProbeResult {
    bool ok{false};
    /// true = 引擎只做了本地推断、没发请求，需要用户先选服务商。
    bool needs_choice{false};
    /// 本地推断是否确定（如密钥前缀能唯一对上某一家）。
    bool confident{false};
    std::string provider;        ///< 识别/选定的服务商
    std::string provider_name;   ///< 展示名
    std::string note;            ///< 引擎的解释（为什么需要你来选）
    std::vector<std::string> candidates;  ///< 需要用户从这些里选
    std::vector<std::string> models;      ///< 可用模型 id
    std::string error;           ///< 非空 = 失败原因（可直接显示）
    std::string hint;            ///< 下一步该怎么做的建议
    int total{0};
    int filtered{0};             ///< 滤掉了几个非对话模型（embedding 之类）
};

class SettingsDialog : public QDialog {
    Q_OBJECT

public:
    /// ``providers`` 是引擎自述的服务商清单。**不在界面里写死** —— 引擎
    /// 加了新后端而界面不知道，就会出现"命令行能用、界面里选不到"。
    struct ProviderItem {
        std::string name;
        std::string description;
        std::string default_base_url;   ///< 官方端点，供「自定义端点」预填
        std::string key_env;            ///< 该家的密钥环境变量名
        bool        needs_network{true};
    };
    struct NamedItem {
        std::string id;
        std::string name;
    };

    SettingsDialog(const AgentSettings& current,
                   const std::vector<ProviderItem>& providers,
                   const std::vector<NamedItem>& panels,
                   const std::vector<NamedItem>& roles,
                   QWidget* parent = nullptr);

    /// 确定之后取回编辑结果。取消时调用方不该读它。
    AgentSettings settings() const;

    /// 把探测结果画到界面上：填模型下拉、写状态行、必要时提示先选服务商。
    void applyProbeResult(const ProbeResult& r);

    /// 正在探测 / 结束。用于禁用按钮、显示"测试中…"。
    void setProbing(bool busy);

Q_SIGNALS:
    /// 用户点了「测试并获取模型」。
    ///
    /// 对话框**自己不发**这个请求：一次探测要等几秒，而对话框跑在主线程上，
    /// 直接发就是界面卡死。所以它把这件事交给外层，由外层在自己的线程里做。
    void probeRequested(const QString& provider, const QString& apiKey,
                        const QString& baseUrl);

private:
    /// 三个分组各自建部件。
    ///
    /// 参数类型写 `QFormLayout*` 而不是 `class QFormLayout*`：后者是一个
    /// **详细类型说明符**，写在命名空间里会把 `QFormLayout` 声明成
    /// `fp::gui::QFormLayout` —— 于是它和全局那个永远不是同一个类型，
    /// .cpp 里一调 `addRow` 就是 "invalid use of incomplete type"。
    void buildServiceGroup(QFormLayout* form);
    void buildCommitteeGroup(QFormLayout* form);
    void buildDataGroup(QFormLayout* form);

    /// 「高级」那一块（端点 / 温度 / 最大输出）的折叠面板。
    QWidget* buildAdvancedPanel();

    void requestProbe();
    void setStatus(const QString& text, const QString& tone);
    /// 服务商变了之后：端点占位符、密钥环境变量提示、自定义端点时的展开。
    void onProviderChanged();
    /// 按密钥格式**本地**预选服务商。不发请求。
    void preselectFromKey(const QString& key);
    void fillModelCombo(const std::vector<std::string>& models, const QString& keep);

    /// 引擎自述的清单。在构造函数体里拷进来再交给三个 build* ——
    /// 建造顺序是「先建部件、后填当前值」，中途读的是这几个成员，
    /// 所以必须在建部件**之前**就填好。
    std::vector<ProviderItem> providers_;
    std::vector<NamedItem>    panels_;
    std::vector<NamedItem>    roles_;

    QComboBox*      provider_combo_{nullptr};
    QLineEdit*      key_edit_{nullptr};
    QPushButton*    probe_button_{nullptr};
    QLabel*         status_label_{nullptr};
    QComboBox*      model_combo_{nullptr};
    QLabel*         model_hint_{nullptr};
    QCheckBox*      remember_key_{nullptr};

    QToolButton*    advanced_toggle_{nullptr};
    QWidget*        advanced_panel_{nullptr};
    QLineEdit*      base_url_edit_{nullptr};
    QDoubleSpinBox* temperature_spin_{nullptr};
    QSpinBox*       max_tokens_spin_{nullptr};

    QComboBox* panel_combo_{nullptr};
    QComboBox* role_combo_{nullptr};
    QSpinBox*  rounds_spin_{nullptr};

    QLineEdit* tushare_edit_{nullptr};
    QCheckBox* remember_tushare_{nullptr};
};

}  // namespace fp::gui
