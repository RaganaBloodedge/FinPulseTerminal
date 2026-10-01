#include "SettingsDialog.h"

#include <QCheckBox>
#include <QComboBox>
#include <QDialogButtonBox>
#include <QDoubleSpinBox>
#include <QFormLayout>
#include <QGroupBox>
#include <QHBoxLayout>
#include <QLabel>
#include <QLineEdit>
#include <QPushButton>
#include <QSignalBlocker>
#include <QSpinBox>
#include <QToolButton>
#include <QVBoxLayout>

namespace fp::gui {
namespace {

/// 提示文字统一样式：小一号、灰色。它们是解释，不是内容。
QLabel* hint(const QString& text, QWidget* parent) {
    auto* label = new QLabel(text, parent);
    label->setWordWrap(true);
    label->setStyleSheet(QStringLiteral("color:#666;font-size:11px;"));
    return label;
}

/// 状态行的三种语气 + 空闲。
///
/// 用颜色区分是因为这里的消息天生分三类、而用户的下一步动作完全不同：
/// 绿 = 可以继续，黄 = 需要你补一个选择，红 = 出错了得改配置。
QString tone_style(const QString& tone) {
    if (tone == QStringLiteral("ok"))    return QStringLiteral("color:#1a7f37;font-size:11px;");
    if (tone == QStringLiteral("warn"))  return QStringLiteral("color:#9a6700;font-size:11px;");
    if (tone == QStringLiteral("error")) return QStringLiteral("color:#c05050;font-size:11px;");
    return QStringLiteral("color:#666;font-size:11px;");
}

}  // namespace

SettingsDialog::SettingsDialog(const AgentSettings& current,
                               const std::vector<ProviderItem>& providers,
                               const std::vector<NamedItem>& panels,
                               const std::vector<NamedItem>& roles,
                               QWidget* parent)
    : QDialog(parent) {
    setWindowTitle(QStringLiteral("AI 研判 · 参数设置"));
    setMinimumWidth(580);

    // 清单必须在建部件之前拷进来：三个 build* 会读它们。
    providers_ = providers;
    panels_    = panels;
    roles_     = roles;

    auto* layout = new QVBoxLayout(this);

    auto* service_box = new QGroupBox(QStringLiteral("模型服务"), this);
    auto* service_form = new QFormLayout(service_box);
    buildServiceGroup(service_form);
    layout->addWidget(service_box);

    advanced_toggle_ = new QToolButton(this);
    advanced_toggle_->setText(QStringLiteral("高级选项（端点 / 温度 / 最大输出）"));
    advanced_toggle_->setCheckable(true);
    advanced_toggle_->setChecked(false);
    advanced_toggle_->setArrowType(Qt::RightArrow);
    // QToolButton 默认是 ToolButtonIconOnly —— 只画箭头不画字，
    // 折叠按钮就剩一个孤零零的三角。显式指明"文字放图标旁边"。
    advanced_toggle_->setToolButtonStyle(Qt::ToolButtonTextBesideIcon);
    advanced_toggle_->setAutoRaise(true);
    layout->addWidget(advanced_toggle_);

    advanced_panel_ = buildAdvancedPanel();
    advanced_panel_->setVisible(false);
    layout->addWidget(advanced_panel_);

    connect(advanced_toggle_, &QToolButton::toggled, this, [this](bool on) {
        advanced_panel_->setVisible(on);
        advanced_toggle_->setArrowType(on ? Qt::DownArrow : Qt::RightArrow);
    });

    auto* committee_box = new QGroupBox(QStringLiteral("投委会"), this);
    auto* committee_form = new QFormLayout(committee_box);
    buildCommitteeGroup(committee_form);
    layout->addWidget(committee_box);

    auto* data_box = new QGroupBox(QStringLiteral("数据源（Tushare）"), this);
    auto* data_form = new QFormLayout(data_box);
    buildDataGroup(data_form);
    layout->addWidget(data_box);

    layout->addStretch(1);

    // ── 用当前值填充 ──────────────────────────────────────
    if (!current.provider.empty()) {
        const int idx = provider_combo_->findData(QString::fromStdString(current.provider));
        if (idx >= 0) provider_combo_->setCurrentIndex(idx);
    }
    key_edit_->setText(QString::fromStdString(current.api_key));
    remember_key_->setChecked(current.remember_key);
    fillModelCombo({}, QString::fromStdString(current.model));
    base_url_edit_->setText(QString::fromStdString(current.base_url));
    temperature_spin_->setValue(current.temperature);
    max_tokens_spin_->setValue(current.max_tokens);

    if (!current.panel.empty()) {
        const int idx = panel_combo_->findData(QString::fromStdString(current.panel));
        if (idx >= 0) panel_combo_->setCurrentIndex(idx);
    }
    if (!current.role.empty()) {
        const int idx = role_combo_->findData(QString::fromStdString(current.role));
        if (idx >= 0) role_combo_->setCurrentIndex(idx);
    }
    rounds_spin_->setValue(current.rounds);

    if (!current.tushare_token.empty()) tushare_edit_->setText(QString::fromStdString(current.tushare_token));
    remember_tushare_->setChecked(current.remember_tushare);

    // 已经有端点 / 非默认采样参数的话，高级区就直接摊开 —— 藏起来会让
    // 用户以为自己的设置丢了。
    if (!current.base_url.empty()
        || current.temperature != AgentSettings{}.temperature
        || current.max_tokens != AgentSettings{}.max_tokens) {
        advanced_toggle_->setChecked(true);
    }

    onProviderChanged();
    if (!current.model.empty()) {
        setStatus(QStringLiteral("模型：%1（上次用的）。改密钥后点「测试并获取模型」重新拉取。")
                      .arg(QString::fromStdString(current.model)),
                  QStringLiteral("idle"));
    }

    auto* buttons = new QDialogButtonBox(QDialogButtonBox::Ok | QDialogButtonBox::Cancel, this);
    buttons->button(QDialogButtonBox::Ok)->setText(QStringLiteral("保存"));
    buttons->button(QDialogButtonBox::Cancel)->setText(QStringLiteral("取消"));
    connect(buttons, &QDialogButtonBox::accepted, this, &QDialog::accept);
    connect(buttons, &QDialogButtonBox::rejected, this, &QDialog::reject);
    layout->addWidget(buttons);
}

// ── 模型服务 ─────────────────────────────────────────────────

void SettingsDialog::buildServiceGroup(QFormLayout* form) {
    // ① 服务商 —— 本界面里唯一一个**必须由人来答**的问题。
    //    引擎不替用户挨家试密钥（那等于把密钥发给无关的公司），
    //    所以这一格必须有人显式声明。代价只是一次点击。
    provider_combo_ = new QComboBox(this);
    // objectName 供无头截图钩子定位（--settings-probe 走的是用户路径，
    // 但它得能找到控件）。不给 findChild 一个稳定的名字，钩子就只能
    // 按控件类型遍历 —— 那会误抓别的下拉框。
    provider_combo_->setObjectName(QStringLiteral("settingsProvider"));
    provider_combo_->addItem(QStringLiteral("（请选择服务商…）"), QString());
    for (const ProviderItem& p : providers_) {
        const QString name = QString::fromStdString(p.name);
        const QString desc = QString::fromStdString(p.description);
        provider_combo_->addItem(QStringLiteral("%1 — %2").arg(name, desc), name);

        QString tip = desc;
        if (!p.default_base_url.empty()) {
            tip += QStringLiteral("\n默认端点：%1").arg(QString::fromStdString(p.default_base_url));
        } else {
            tip += QStringLiteral("\n**没有内置端点**：请在「高级选项」里填端点地址。");
        }
        if (!p.key_env.empty()) {
            tip += QStringLiteral("\n密钥环境变量：%1").arg(QString::fromStdString(p.key_env));
        }
        tip += QStringLiteral("\n\n密钥只会发给这里选定的服务商 —— 界面不会拿它去试别家。");
        provider_combo_->setItemData(provider_combo_->count() - 1, tip, Qt::ToolTipRole);
    }
    connect(provider_combo_, &QComboBox::currentIndexChanged,
            this, [this](int) { onProviderChanged(); });
    form->addRow(QStringLiteral("服务商"), provider_combo_);

    // ② 密钥 + 测试按钮。这是全界面唯一**必填**的东西。
    auto* key_row = new QWidget(this);
    auto* key_layout = new QHBoxLayout(key_row);
    key_layout->setContentsMargins(0, 0, 0, 0);

    key_edit_ = new QLineEdit(key_row);
    key_edit_->setObjectName(QStringLiteral("settingsKey"));
    key_edit_->setEchoMode(QLineEdit::Password);
    key_edit_->setPlaceholderText(QStringLiteral("在这里粘贴你的 API 密钥"));
    key_edit_->setToolTip(QStringLiteral(
        "整个界面只有这一项是必填的。\n"
        "模型名与端点都不用你填 —— 点右边的按钮，它们会自己从服务商那里拉回来。\n\n"
        "密钥只发给上面选定的那一家；不会拿去依次试探其他服务商。\n"
        "不勾选「记住」时，它只存在于本次会话的内存里。"));
    connect(key_edit_, &QLineEdit::textChanged, this, [this](const QString& text) {
        // 本地预选：只读字符串，不发任何网络请求。
        if (provider_combo_->currentData().toString().isEmpty()) preselectFromKey(text);
    });
    connect(key_edit_, &QLineEdit::returnPressed, this, [this] { requestProbe(); });
    key_layout->addWidget(key_edit_, 1);

    probe_button_ = new QPushButton(QStringLiteral("测试并获取模型"), key_row);
    probe_button_->setObjectName(QStringLiteral("settingsProbe"));
    probe_button_->setToolTip(QStringLiteral(
        "向选定的服务商请求 GET /v1/models。\n"
        "成功的话，模型下拉会自动填上它支持的模型 —— 你不用去文档里抄模型名。"));
    connect(probe_button_, &QPushButton::clicked, this, [this] { requestProbe(); });
    key_layout->addWidget(probe_button_);
    form->addRow(QStringLiteral("API 密钥"), key_row);

    // ③ 状态行：探测的结果都落在这里。
    status_label_ = new QLabel(this);
    status_label_->setWordWrap(true);
    setStatus(QStringLiteral("粘贴密钥 → 点「测试并获取模型」→ 从下面的下拉里挑一个模型。"),
              QStringLiteral("idle"));
    form->addRow(QString(), status_label_);

    // ④ 模型 —— 可编辑下拉。默认是"选"，允许手输纯粹是兜底：
    //    有些服务商不提供 /models（返回 404），那时候总得让人能填上。
    model_combo_ = new QComboBox(this);
    model_combo_->setObjectName(QStringLiteral("settingsModel"));
    model_combo_->setEditable(true);
    model_combo_->setInsertPolicy(QComboBox::NoInsert);
    model_combo_->lineEdit()->setPlaceholderText(
        QStringLiteral("点上面的按钮获取列表"));
    model_combo_->setToolTip(QStringLiteral(
        "列表来自服务商的 /v1/models，所以它一定是你这份密钥真能用的模型。\n"
        "服务商不提供该接口时可以手动输入模型名。"));
    form->addRow(QStringLiteral("模型"), model_combo_);

    model_hint_ = hint(QStringLiteral("模型列表由服务商提供，不需要你记模型名。"), this);
    form->addRow(QString(), model_hint_);

    // ⑤ 记住密钥 —— 默认不勾。
    remember_key_ = new QCheckBox(QStringLiteral("记住到启动配置档（下次打开就不用重填）"), this);
    remember_key_->setToolTip(QStringLiteral(
        "勾选后密钥会以明文写进 ~/.finpulse/profile.json（权限 0600，不进版本控制）。\n"
        "好处是打开程序就能用；代价是它落到了磁盘上。\n"
        "不勾选则每次启动都要重填，或改用环境变量。"));
    form->addRow(QString(), remember_key_);
}

QWidget* SettingsDialog::buildAdvancedPanel() {
    auto* panel = new QGroupBox(QStringLiteral("高级选项"), this);
    auto* form = new QFormLayout(panel);

    base_url_edit_ = new QLineEdit(panel);
    base_url_edit_->setObjectName(QStringLiteral("settingsBaseUrl"));
    base_url_edit_->setPlaceholderText(QStringLiteral("留空 = 用官方端点"));
    base_url_edit_->setToolTip(QStringLiteral(
        "只在自建网关 / 本地 vLLM / LM Studio / Ollama，或服务商给的地址不是官方地址时才填。\n"
        "例：http://127.0.0.1:11434/v1\n\n"
        "写 https://xxx.com、https://xxx.com/v1、https://xxx.com/v1/chat/completions\n"
        "都能认，程序会自己规整。"));
    form->addRow(QStringLiteral("端点"), base_url_edit_);

    temperature_spin_ = new QDoubleSpinBox(panel);
    temperature_spin_->setRange(0.0, 2.0);
    temperature_spin_->setSingleStep(0.1);
    temperature_spin_->setDecimals(1);
    temperature_spin_->setToolTip(QStringLiteral(
        "越低越稳定、越像照本宣科；越高越发散。\n投资分析建议 0.1~0.4：这里的价值在"
        "稳定可复现，不在文采。"));
    form->addRow(QStringLiteral("温度"), temperature_spin_);

    max_tokens_spin_ = new QSpinBox(panel);
    max_tokens_spin_->setRange(128, 8192);
    max_tokens_spin_->setSingleStep(128);
    max_tokens_spin_->setToolTip(QStringLiteral(
        "单次回复的上限。设得太小会把回答截断（界面会提示 finish_reason=length）。"));
    form->addRow(QStringLiteral("最大输出"), max_tokens_spin_);

    return panel;
}

// ── 服务商 / 密钥的联动 ──────────────────────────────────────

void SettingsDialog::onProviderChanged() {
    const QString name = provider_combo_->currentData().toString();
    if (name.isEmpty()) {
        base_url_edit_->setPlaceholderText(QStringLiteral("先选服务商"));
        return;
    }

    // 找出这一条的默认端点，用它当占位符：用户一眼就知道"留空会连到哪里"。
    for (const ProviderItem& p : providers_) {
        if (QString::fromStdString(p.name) != name) continue;
        if (p.default_base_url.empty()) {
            base_url_edit_->setPlaceholderText(QStringLiteral("这家必须填端点地址"));
            // 必须显式给端点的那种（自定义网关），直接把高级区摊开并聚焦 ——
            // 否则用户点了保存才发现缺东西。
            if (!advanced_toggle_->isChecked()) advanced_toggle_->setChecked(true);
            base_url_edit_->setFocus();
        } else {
            base_url_edit_->setPlaceholderText(
                QStringLiteral("留空 = %1").arg(QString::fromStdString(p.default_base_url)));
        }
        break;
    }
}

void SettingsDialog::preselectFromKey(const QString& key) {
    // 这里只做**最保守**的两条本地匹配：能唯一对上、且对上了几乎不会错。
    // 完整的推断规则在引擎侧（llm/discovery.py），界面不复制那一份 ——
    // 复制出来的第二份规则迟早会和第一份不一致。
    //
    // 通用 `sk-` 前缀这里**故意不猜**：OpenAI / DeepSeek / Moonshot 的密钥
    // 格式完全一样，猜错了就会把密钥发给错的那家。宁可让用户点一下下拉。
    const QString k = key.trimmed();
    QString guess;
    if (k.startsWith(QStringLiteral("sk-proj-")))      guess = QStringLiteral("openai");
    else if (k.startsWith(QStringLiteral("gsk_")))     guess = QStringLiteral("groq");

    if (guess.isEmpty()) return;
    const int idx = provider_combo_->findData(guess);
    if (idx < 0) return;
    provider_combo_->setCurrentIndex(idx);
    setStatus(QStringLiteral("已按密钥格式预选 %1。点「测试并获取模型」确认。")
                  .arg(provider_combo_->currentText()),
              QStringLiteral("idle"));
}

void SettingsDialog::requestProbe() {
    const QString provider = provider_combo_->currentData().toString();
    const QString key      = key_edit_->text();
    const QString base_url = base_url_edit_->text().trimmed();

    if (key.trimmed().isEmpty() && base_url.isEmpty()) {
        setStatus(QStringLiteral("先粘贴 API 密钥（本地端点不校验密钥的话，请在「高级选项」里填端点地址）。"),
                  QStringLiteral("warn"));
        key_edit_->setFocus();
        return;
    }
    if (provider.isEmpty()) {
        // 没选服务商就不发请求 —— 密钥该发给谁，只有用户知道。
        setStatus(QStringLiteral("请先在上面选择服务商。密钥只会发给选定的那一家，"
                                 "所以这一步不能替你猜。"),
                  QStringLiteral("warn"));
        provider_combo_->setFocus();
        provider_combo_->showPopup();
        return;
    }

    setProbing(true);
    setStatus(QStringLiteral("正在向「%1」请求模型列表…").arg(provider_combo_->currentText()),
              QStringLiteral("idle"));
    Q_EMIT probeRequested(provider, key, base_url);
}

void SettingsDialog::setProbing(bool busy) {
    if (probe_button_) {
        probe_button_->setEnabled(!busy);
        probe_button_->setText(busy ? QStringLiteral("测试中…")
                                    : QStringLiteral("测试并获取模型"));
    }
}

void SettingsDialog::fillModelCombo(const std::vector<std::string>& models, const QString& keep) {
    QComboBox* box = model_combo_;
    const QString wanted = keep.isEmpty() ? box->currentText() : keep;

    box->clear();
    for (const std::string& m : models) {
        box->addItem(QString::fromStdString(m), QString::fromStdString(m));
    }
    if (!wanted.isEmpty()) {
        const int idx = box->findText(wanted);
        if (idx >= 0) box->setCurrentIndex(idx);
        else if (models.empty()) box->setEditText(wanted);   // 拉不到就保留手填值
    } else if (box->count() > 0) {
        box->setCurrentIndex(0);
    }
}

void SettingsDialog::applyProbeResult(const ProbeResult& r) {
    setProbing(false);

    // ── 需要用户先选服务商（引擎只做了本地推断，没发请求）──
    if (r.needs_choice) {
        QString text = QString::fromStdString(r.note);
        if (text.isEmpty()) {
            text = QStringLiteral("无法从密钥格式判断服务商，请在上面选一个。");
        }
        setStatus(text, r.confident ? QStringLiteral("idle") : QStringLiteral("warn"));
        if (!r.candidates.empty()) {
            QStringList names;
            for (const std::string& c : r.candidates) names << QString::fromStdString(c);
            model_hint_->setText(QStringLiteral("常见候选：%1").arg(names.join(QStringLiteral(" / "))));
        }
        provider_combo_->setFocus();
        provider_combo_->showPopup();
        return;
    }

    // ── 失败 ──
    if (!r.ok) {
        QString text = QString::fromStdString(r.error);
        if (text.isEmpty()) text = QStringLiteral("获取模型列表失败。");
        if (!r.hint.empty()) text += QStringLiteral("\n") + QString::fromStdString(r.hint);
        setStatus(text, QStringLiteral("error"));
        return;
    }

    // ── 成功 ──
    // 引擎回的是识别/规整后的服务商，把它同步回下拉：用户可能选的是
    // 「自定义端点」，而真正生效的是这里面推断出来的那家。
    if (!r.provider.empty()) {
        const int idx = provider_combo_->findData(QString::fromStdString(r.provider));
        if (idx >= 0 && idx != provider_combo_->currentIndex()) {
            QSignalBlocker block(provider_combo_);
            provider_combo_->setCurrentIndex(idx);
        }
    }

    fillModelCombo(r.models, QString());

    const QString name = QString::fromStdString(
        r.provider_name.empty() ? r.provider : r.provider_name);
    QString text = QStringLiteral("✓ 已连接 %1 · 可用模型 %2 个").arg(name).arg(r.total);
    if (r.filtered > 0) {
        // 说清楚"为什么比官网上看到的少"：不然用户会以为列表拉漏了。
        text += QStringLiteral("（已滤掉 %1 个非对话模型，如 embedding / 语音）").arg(r.filtered);
    }
    setStatus(text, QStringLiteral("ok"));

    if (r.total == 0) {
        model_hint_->setText(QStringLiteral("服务商没有返回可用模型，请手动输入模型名。"));
    } else {
        model_hint_->setText(
            QStringLiteral("模型列表来自 %1 的 /v1/models，可直接用。").arg(name));
    }
}

// ── 投委会 / 数据源 ──────────────────────────────────────────

void SettingsDialog::buildCommitteeGroup(QFormLayout* form) {
    panel_combo_ = new QComboBox(this);
    panel_combo_->addItem(QStringLiteral("（默认投委会）"), QString());
    for (const NamedItem& p : panels_) {
        panel_combo_->addItem(QStringLiteral("%1 — %2").arg(QString::fromStdString(p.id),
                                                            QString::fromStdString(p.name)),
                              QString::fromStdString(p.id));
    }
    form->addRow(QStringLiteral("投委会"), panel_combo_);

    role_combo_ = new QComboBox(this);
    role_combo_->addItem(QStringLiteral("（整场投委会）"), QString());
    for (const NamedItem& r : roles_) {
        role_combo_->addItem(QStringLiteral("%1 — %2").arg(QString::fromStdString(r.id),
                                                           QString::fromStdString(r.name)),
                             QString::fromStdString(r.id));
    }
    role_combo_->setToolTip(QStringLiteral(
        "选了单个角色就**不组会**：只有这一个角色发言，速度快很多，适合"
        "快速问一个视角。"));
    form->addRow(QStringLiteral("角色"), role_combo_);

    rounds_spin_ = new QSpinBox(this);
    rounds_spin_->setRange(0, 10);
    rounds_spin_->setSpecialValueText(QStringLiteral("用配置（0）"));
    rounds_spin_->setToolTip(QStringLiteral(
        "轮数。1 = 只独立研判；2 = 独立研判 + 一轮交叉质证（默认）。"));
    form->addRow(QStringLiteral("轮数"), rounds_spin_);
}

void SettingsDialog::buildDataGroup(QFormLayout* form) {
    tushare_edit_ = new QLineEdit(this);
    tushare_edit_->setEchoMode(QLineEdit::Password);
    tushare_edit_->setPlaceholderText(QStringLiteral("留空 = 读环境变量 / 回落本地缓存"));
    tushare_edit_->setToolTip(QStringLiteral(
        "拉取实时行情用（这是行情数据源的 token，和上面的大模型密钥是两回事）。\n"
        "也可以在命令行上写一次：finpulse-cli --tushare-token <token> --save-profile"));
    form->addRow(QStringLiteral("Tushare token"), tushare_edit_);

    remember_tushare_ = new QCheckBox(QStringLiteral("记住到启动配置档"), this);
    form->addRow(QString(), remember_tushare_);
    form->addRow(QString(), hint(QStringLiteral(
        "写入位置：~/.finpulse/profile.json（权限 0600，不进版本控制）。"), this));
}

void SettingsDialog::setStatus(const QString& text, const QString& tone) {
    status_label_->setText(text);
    status_label_->setStyleSheet(tone_style(tone));
}

// ── 取值 ─────────────────────────────────────────────────────

AgentSettings SettingsDialog::settings() const {
    AgentSettings s;
    s.provider = provider_combo_->currentData().toString().toStdString();
    s.model = model_combo_->currentText().trimmed().toStdString();
    s.base_url = base_url_edit_->text().trimmed().toStdString();
    // 优先级：界面上当下填的 > 探测时用的那个。用户在探测之后又改了输入框，
    // 以他改的为准；没改过（输入框内容与探测时一致）也自然是同一个值。
    s.api_key = key_edit_->text().trimmed().toStdString();
    s.remember_key = remember_key_->isChecked();
    s.temperature = temperature_spin_->value();
    s.max_tokens = max_tokens_spin_->value();

    s.panel = panel_combo_->currentData().toString().toStdString();
    s.role = role_combo_->currentData().toString().toStdString();
    s.rounds = rounds_spin_->value();

    s.tushare_token = tushare_edit_->text().trimmed().toStdString();
    s.remember_tushare = remember_tushare_->isChecked();
    return s;
}

}  // namespace fp::gui
