#include "MainWindow.h"

#include "CandleChartWidget.h"
#include "ChatRender.h"
#include "QuoteTableModel.h"

#include "agent/AgentService.h"
#include "agent/ToolBridge.h"
#include "app/EventText.h"
#include "core/DataHub.h"
#include "core/Log.h"
#include "core/Version.h"
#include "data/ReplaySource.h"
#include "data/Watchlist.h"

#include <QCheckBox>
#include <QComboBox>
#include <QDateTime>
#include <QDialog>
#include <QFormLayout>
#include <QGroupBox>
#include <QHBoxLayout>
#include <QHeaderView>
#include <QLabel>
#include <QLineEdit>
#include <QMessageBox>
#include <QMetaObject>
#include <QPlainTextEdit>
#include <QPointer>
#include <QPushButton>
#include <QScrollBar>
#include <QSignalBlocker>
#include <QSpinBox>
#include <QSplitter>
#include <QStatusBar>
#include <QTabWidget>
#include <QTableView>
#include <QTextBrowser>
#include <QTimer>
#include <QToolBar>
#include <QVBoxLayout>

#include <algorithm>
#include <chrono>
#include <cmath>
#include <cstdlib>
#include <map>
#include <exception>
#include <map>
#include <set>

#ifndef FINPULSE_PYTHON_ROOT
#  define FINPULSE_PYTHON_ROOT ""
#endif

namespace fp::gui {

namespace {

std::string default_python_root() {
    if (const char* env = std::getenv("FINPULSE_PYTHON_ROOT")) {
        if (*env != '\0') return env;
    }
    return FINPULSE_PYTHON_ROOT;
}

PyEngine::Config build_engine_config() {
    PyEngine::Config cfg;
    cfg.python_root     = default_python_root();
    cfg.call_timeout_ms = 60000;  // 回测在大序列上可能跑几秒，留足余量
    return cfg;
}

QString mono_family() {
    return QStringLiteral("Consolas, Menlo, DejaVu Sans Mono, monospace");
}

/// 从 analysis.indicators 的返回里抠出一条线。
/// 返回与输入等长，无值处填 NaN（图表据此断线）。
std::vector<double> extract_line(const Json& indicators, const std::string& line_name) {
    std::vector<double> out;
    const Json&         results = indicators["results"];
    if (!results.is_array()) return out;

    for (const auto& item : results.items()) {
        const Json& lines = item["lines"];
        if (!lines.is_object()) continue;
        for (const auto& kv : lines.members()) {
            if (kv.first != line_name) continue;
            if (!kv.second.is_array()) continue;
            out.reserve(kv.second.size());
            for (const auto& v : kv.second.items()) {
                out.push_back(v.is_number() ? v.as_double() : std::nan(""));
            }
            return out;
        }
    }
    return out;
}

QVector<double> to_qt(const std::vector<double>& v) {
    QVector<double> out;
    out.reserve(static_cast<qsizetype>(v.size()));
    for (double d : v) out.append(d);
    return out;
}

bool has_finite(const std::vector<double>& v) {
    for (double d : v) {
        if (std::isfinite(d)) return true;
    }
    return false;
}

void write_pairs(QPlainTextEdit* view, const std::vector<std::pair<QString, QString>>& rows) {
    QString text;
    int     width = 0;
    for (const auto& r : rows) width = std::max(width, static_cast<int>(r.first.size()));

    for (const auto& r : rows) {
        text += r.first.leftJustified(width, ' ') + QStringLiteral("   ") + r.second + QLatin1Char('\n');
    }
    view->setPlainText(text);
}

QString num(double v, int decimals = 2, bool signed_ = false) {
    if (!std::isfinite(v)) return QStringLiteral("—");
    return (signed_ && v > 0 ? QStringLiteral("+") : QString()) +
           QString::number(v, 'f', decimals);
}

QString pct(double v, bool signed_ = true) {
    if (!std::isfinite(v)) return QStringLiteral("—");
    return num(v, 2, signed_) + QStringLiteral("%");
}

QString json_num(const Json& j, const char* key) {
    const Json& v = j[key];
    if (v.is_number()) return QString::number(v.as_double(), 'f', 4);
    return QStringLiteral("—");
}

QString qs(const std::string& s) { return QString::fromStdString(s); }

/// 等宽视图里的**显示列宽**：CJK / 全角字符占两列，其余占一列。
///
/// 不能用 QString::arg(fieldWidth) 代替：它数的是 QChar 个数，"技术面分析师"
/// 会被当成 6 个字符补齐，而屏幕上占了 12 列 —— 整张表就歪了。CLI 那边
/// 有一模一样的处理（按 UTF-8 首字节分类），这里是它在 Qt 侧的对应物。
int display_cols(const QString& s) {
    int w = 0;
    for (const QChar c : s) {
        // 0x1100 以下是拉丁/西里尔/希腊等单宽字符；再往上是 CJK、假名、
        // 谚文等双宽区。粗略但够用：这个视图里只可能出现中英文和数字。
        w += (c.unicode() < 0x1100) ? 1 : 2;
    }
    return w;
}

QString pad_cols(const QString& s, int cols, bool left_align = true) {
    const int w = display_cols(s);
    if (w >= cols) return s;
    const QString fill(cols - w, QLatin1Char(' '));
    return left_align ? s + fill : fill + s;
}

/// 汇总本次**实际**使用的推理后端。
///
/// 单列出来，是因为"配置里写的"和"实际生效的"是两件事：缺密钥会**静默**
/// 降级到内置规则后端。不把实际值摆出来，用户就永远没法确认自己填的密钥
/// 有没有被用上 —— 而"我明明填了啊"是最难自查的一类问题。
QString describe_backends(const AgentService::Outcome& out) {
    std::map<std::string, int> counts;
    std::set<std::string>      reasons;
    long long                  tin = 0, tout = 0;
    bool                       external = false;

    const auto tally = [&](const AgentService::Verdict& v) {
        ++counts[v.provider.empty() ? std::string("未报告") : v.provider];
        tin  += v.prompt_tokens;
        tout += v.completion_tokens;
        if (v.used_external()) external = true;
        if (!v.fallback_reason.empty()) reasons.insert(v.fallback_reason);
    };
    for (const AgentService::RoundInfo& r : out.rounds) {
        for (const AgentService::Verdict& v : r.verdicts) tally(v);
    }
    if (!out.chair.provider.empty()) tally(out.chair);

    QStringList parts;
    for (const auto& kvp : counts) {
        parts << QStringLiteral("%1 ×%2").arg(qs(kvp.first)).arg(kvp.second);
    }

    QString s = parts.join(QStringLiteral("  "));
    if (tin + tout > 0) {
        s += QStringLiteral("    token %1 → %2").arg(tin).arg(tout);
    }
    if (!external) {
        s += QStringLiteral("   [未连接大模型，跑在内置规则后端上]");
    }
    return s;
}

/// 把一场研判的结果渲染成 AI 研判页上的一段文字。
///
/// **弃权必须和 NEUTRAL 分开显示**：方向为空表示"这个角色没表态"，NEUTRAL
/// 表示"它表态了、认为中性"。这两件事在投委会里含义完全相反，混在一起会
/// 让主席的计票看起来莫名其妙（"3 位分析师里 2 位有方向，为什么说支持
/// 面不足半数"）。
///
/// 放在 .cpp 的匿名命名空间里，而不是做成 MainWindow 的成员：它不碰任何
/// 部件，只是拼字符串。
///
/// **更正**：这里原先写着"因此可以被单独测试（见 tests/test_eventtext.cpp）"，
/// 是错的 —— 匿名命名空间的东西别的翻译单元根本看不见，而 test_eventtext.cpp
/// 测的是 EventText.h（事件说明文案），与这个函数无关。所以这段渲染**没有
/// 直接的单测**，它的正确性靠 integration 用例（tests/test_agentservice.cpp）
/// 间接覆盖：那边压的是 Outcome 的字段（含 intent），断的是"引擎有没有把
/// 东西带回来"，不是"这行字怎么排"。要真正测排版，得先把它挪成
/// ChatRender.h 那样的纯函数头文件。
QString render_outcome(const AgentService::Outcome& out, double elapsed_ms) {
    QString text;

    text += QStringLiteral("\n=== 研判结果 ===\n");
    text += QStringLiteral("运行编号    %1\n").arg(qs(out.run_id));
    if (!out.panel_id.empty()) {
        text += QStringLiteral("投委会      %1 (%2)\n").arg(qs(out.panel_name), qs(out.panel_id));
    }
    text += QStringLiteral("耗时        %1 ms（引擎自计 %2 ms）\n")
                .arg(elapsed_ms, 0, 'f', 1)
                .arg(out.duration_ms, 0, 'f', 1);

    // 三种状态分开写：有效有方向 / 有效但弃权 / 根本没成会。压成一个
    // "未出决议"会让"数据不够所以没表态"和"配置坏了"长得一模一样。
    if (!out.valid) {
        text += QStringLiteral("决议        [未形成有效决议]\n");
    } else if (out.direction.empty()) {
        text += QStringLiteral("决议        [有效，但主席未给出方向]\n");
    } else {
        text += QStringLiteral("决议        %1\n").arg(qs(out.direction));
    }
    if (!out.confidence.empty()) {
        text += QStringLiteral("置信度      %1\n").arg(qs(out.confidence));
    }
    // 议题排在最前、告警在后：读者先要知道"这场会在议什么"，再去看过程
    // 里出了什么问题。此前这里只有一行"会议问题"装着告警 —— 那个词在
    // 中文里同时能读成"会议要议的问题"，用户真会以为它该显示议题。
    if (!out.intent.empty()) {
        text += QStringLiteral("议题        %1\n").arg(qs(out.intent));
    }
    for (const std::string& p : out.problems) {
        text += QStringLiteral("运行告警    %1\n").arg(qs(p));
    }

    // 推理后端：回答"它到底拿什么在思考"。这一段同时承担一个提示职责 ——
    // 默认跑在规则后端上时给出"怎么接大模型"的一句话，否则用户只会看到
    // 一份像模像样的报告，根本不知道背后没有模型。
    text += QStringLiteral("推理后端    %1\n").arg(describe_backends(out));
    {
        const bool any_external = std::any_of(
            out.rounds.begin(), out.rounds.end(),
            [](const AgentService::RoundInfo& r) {
                return std::any_of(r.verdicts.begin(), r.verdicts.end(),
                                   [](const AgentService::Verdict& v) {
                                       return v.used_external();
                                   });
            });
        if (!any_external) {
            text += QStringLiteral(
                "            上面那行选一个后端并填模型名/密钥，再跑一次即可真正调用大模型。\n");
        }
        std::set<std::string> reasons;
        for (const AgentService::RoundInfo& r : out.rounds) {
            for (const AgentService::Verdict& v : r.verdicts) {
                if (!v.fallback_reason.empty()) reasons.insert(v.fallback_reason);
            }
        }
        for (const std::string& why : reasons) {
            text += QStringLiteral("降级原因    %1\n").arg(qs(why));
        }
    }

    for (const AgentService::RoundInfo& rnd : out.rounds) {
        text += QStringLiteral("\n--- 第 %1 轮 ---\n").arg(rnd.index);
        text += QStringLiteral("  %1 %2 %3 %4\n")
                    .arg(pad_cols(QStringLiteral("委员"), 20),
                         pad_cols(QStringLiteral("方向"), 10, false),
                         pad_cols(QStringLiteral("置信度"), 10, false),
                         pad_cols(QStringLiteral("权重"), 6, false));
        for (const AgentService::Verdict& v : rnd.verdicts) {
            const QString who = v.role_name.empty() ? qs(v.role) : qs(v.role_name);
            text += QStringLiteral("  %1 %2 %3 %4\n")
                        .arg(pad_cols(who, 20),
                             pad_cols(v.has_direction() ? qs(v.direction)
                                                        : QStringLiteral("弃权"), 10, false),
                             pad_cols(v.confidence.empty() ? QStringLiteral("—")
                                                           : qs(v.confidence), 10, false),
                             pad_cols(QString::number(v.weight, 'f', 1), 6, false));
        }
        if (!rnd.changed.empty()) {
            QStringList who;
            for (const std::string& c : rnd.changed) who << qs(c);
            text += QStringLiteral("  改口        %1 人：%2\n")
                        .arg(who.size())
                        .arg(who.join(QStringLiteral(", ")));
        }
        text += QStringLiteral("  本轮耗时    %1 ms\n").arg(rnd.duration_ms, 0, 'f', 1);
    }

    if (!out.final_directions.empty()) {
        text += QStringLiteral("\n--- 最终立场（取自最后一轮）---\n");
        for (const auto& [role, dir] : out.final_directions) {
            text += QStringLiteral("  %1  %2\n")
                        .arg(pad_cols(qs(role), 22))
                        .arg(dir.empty() ? QStringLiteral("弃权（未表态，不计入分母）") : qs(dir));
        }
    }

    if (!out.chair.text.empty()) {
        const QString who = out.chair.role_name.empty() ? qs(out.chair.role)
                                                        : qs(out.chair.role_name);
        text += QStringLiteral("\n--- %1 的报告 ---\n%2\n").arg(who, qs(out.chair.text));
    }
    return text;
}

}  // namespace

// ══════════════════════════════════════════════════════════

MainWindow::MainWindow(QWidget* parent)
    : QMainWindow(parent), engine_(build_engine_config()) {
    setWindowTitle(QStringLiteral("%1  v%2  —  C++20 / Qt6 桌面壳 + 嵌入式 Python 分析引擎")
                       .arg(QString::fromLatin1(kAppName), QString::fromLatin1(kVersion)));
    resize(1360, 860);

    buildUi();
    wireDataHub();

    // 启动默认值必须在 buildUi 之后、startEngine 之前落到位：
    // refreshSourceCombo() 会重建数据源下拉框（保留 currentText），
    // 所以先设好 tushare，引擎连上之后它会被正确地保留下来。
    applyStartupProfile();

    // 研判参数的初值来自配置档。密钥字段尤其重要：配过并且勾了「记住」的
    // 人，重开程序不该被要求再填一遍。
    agent_settings_.provider    = profile_.llm_provider;
    agent_settings_.model       = profile_.llm_model;
    agent_settings_.base_url    = profile_.llm_base_url;
    agent_settings_.api_key     = profile_.llm_api_key;
    agent_settings_.tushare_token = profile_.tushare_token;
    // 配置档里已经有值 = 上次是勾着「记住」存下来的，那就把勾保持住；
    // 否则每次打开设置界面都要重新勾一次，很快就会没人愿意勾。
    agent_settings_.remember_key     = profile_.has_llm_key();
    agent_settings_.remember_tushare = profile_.has_token();

    startEngine();
    appendLog(QStringLiteral("引擎就绪：%1 v%2 (Python %3)")
                  .arg(QString::fromStdString(engine_.info().name),
                       QString::fromStdString(engine_.info().version),
                       QString::fromStdString(engine_.info().python_version)));

    // 对话页的第一条消息。它同时承担"这一页怎么用"的说明职责 ——
    // 一个只有输入框的对话页，用户不会知道还能跑投委会研判。
    appendChatTurn(QStringLiteral("FinPulse"),
                   QStringLiteral(
                       "这里是和终端对话的地方。直接问就行，比如「现在这个位置的风险在哪」。\n"
                       "\n"
                       "回答问题的是你配置的推理后端；**没接大模型时它会直说**，"
                       "不会拿模板假装回答。\n"
                       "命令：/debate 跑投委会研判 · /clear 清空 · /help 更多\n"
                       "参数（后端 / 模型 / 密钥 / 投委会 / token）都在「设置…」里。"),
                   QStringLiteral("assistant"));

    runAnalysisAsync();
}

MainWindow::~MainWindow() {
    // ── 析构顺序是有讲究的，改之前先读完这段 ──
    //
    // 1. 先停回放和几路后台工作线程。它们都可能正在调 engine_.rpc()，
    //    不 join 就往下走，就会在引擎已经被停掉之后还在发请求。
    stopReplay();
    if (agent_thread_.joinable())    agent_thread_.join();
    if (chat_thread_.joinable())     chat_thread_.join();
    if (pull_thread_.joinable())     pull_thread_.join();
    if (settings_thread_.joinable()) settings_thread_.join();

    // 2. 退订。此后没人再往部件上写事件。
    quote_sub_.reset();
    kline_sub_.reset();
    done_sub_.reset();
    agent_sub_.reset();

    // 3. 停工具桥：它的 accept 线程持有 this（provider 捕获了 this），
    //    必须在我们开始拆自己之前 join 掉。
    bridge_.reset();

    if (worker_.joinable()) worker_.join();

    // 4. 停引擎 —— 到此为止，那个可能回调 AgentService 的读线程没了。
    engine_.stop();

    // 5. **最后**才销毁 AgentService。它的析构本身不碰引擎，但它留在引擎
    //    上的那个事件处理器要等到没人调用它时才安全。实际上处理器持有的是
    //    weak_ptr，即使顺序反了也只是空操作；这里仍然按最保守的顺序写，
    //    免得将来有人把它改成别的实现时踩到。
    agent_svc_.reset();
}

// ── UI 构建 ───────────────────────────────────────────────

void MainWindow::buildUi() {
    buildToolBar();

    chart_ = new CandleChartWidget(this);
    chart_->setTitle(QStringLiteral("等待数据…"));
    // 必须在这里连：上面的 buildToolBar() 跑在 chart_ 创建之前，
    // 那时 chart_ 还是 nullptr，connect 会静默失败并只打一行警告。
    connect(chart_, &CandleChartWidget::hoveredIndexChanged, this, [this](int) { updateStatus(); });

    // 拖动模式是"点一下切换"的模式态，不给出反馈的话用户会怀疑自己点没点上。
    // 光靠光标从箭头变小手太容易被忽略，所以状态栏和日志都写一句。
    connect(chart_, &CandleChartWidget::dragModeChanged, this, [this](bool on) {
        appendLog(on ? QStringLiteral("图表：进入拖动模式（再点一下图表退出）")
                     : QStringLiteral("图表：退出拖动模式"));
        updateStatus();
    });

    side_tabs_ = new QTabWidget(this);

    // 行情表格
    quote_model_ = new QuoteTableModel(this);
    quote_view_  = new QTableView(this);
    quote_view_->setModel(quote_model_);
    quote_view_->setAlternatingRowColors(true);
    quote_view_->verticalHeader()->setVisible(false);
    quote_view_->horizontalHeader()->setStretchLastSection(true);
    quote_view_->setSelectionBehavior(QAbstractItemView::SelectRows);
    quote_view_->setEditTriggers(QAbstractItemView::NoEditTriggers);
    quote_view_->setFont(QFont(mono_family()));
    side_tabs_->addTab(quote_view_, QStringLiteral("行情"));

    // 风险概览
    stats_view_ = new QPlainTextEdit(this);
    stats_view_->setReadOnly(true);
    stats_view_->setFont(QFont(mono_family()));
    side_tabs_->addTab(stats_view_, QStringLiteral("风险"));

    // 回测
    backtest_view_ = new QPlainTextEdit(this);
    backtest_view_->setReadOnly(true);
    backtest_view_->setFont(QFont(mono_family()));
    side_tabs_->addTab(backtest_view_, QStringLiteral("回测"));

    // AI 研判
    buildAgentTab();

    // 日志
    log_view_ = new QPlainTextEdit(this);
    log_view_->setReadOnly(true);
    log_view_->setMaximumBlockCount(500);
    log_view_->setFont(QFont(mono_family()));
    side_tabs_->addTab(log_view_, QStringLiteral("日志"));

    auto* splitter = new QSplitter(Qt::Horizontal, this);
    splitter->addWidget(chart_);
    splitter->addWidget(side_tabs_);
    splitter->setStretchFactor(0, 3);
    splitter->setStretchFactor(1, 2);
    splitter->setSizes({820, 520});

    setCentralWidget(splitter);

    status_label_ = new QLabel(QStringLiteral("就绪"), this);
    statusBar()->addWidget(status_label_);
}

void MainWindow::buildAgentTab() {
    auto* page   = new QWidget(this);
    auto* layout = new QVBoxLayout(page);
    layout->setContentsMargins(6, 6, 6, 6);
    layout->setSpacing(6);

    // ── 动作行 ───────────────────────────────────────────────
    //
    // 参数一行都不放这里。它们属于「配置」（设一次、长期不变），而这些
    // 属于「操作」（每轮都要点）。混在一起的结果是常用的按钮被不常用的
    // 输入框挤到看不见 —— 这正是这一页原先的样子。
    auto* actions = new QHBoxLayout();

    debate_button_ = new QPushButton(QStringLiteral("跑投委会研判"), page);
    debate_button_->setToolTip(QStringLiteral(
        "独立研判 → 交叉质证 → 主席综合，产出一份有契约的结构化报告。\n"
        "同时会启动终端工具桥：Python 在推理过程中会反过来调用本进程，\n"
        "读取这里正在展示的行情与总线统计。\n"
        "投委会与角色在「设置…」里选。"));
    connect(debate_button_, &QPushButton::clicked, this, [this] { runDebateAsync(); });
    actions->addWidget(debate_button_);

    settings_button_ = new QPushButton(QStringLiteral("设置…"), page);
    settings_button_->setToolTip(QStringLiteral(
        "推理后端（模型 / 端点 / 密钥 / 温度）、投委会与角色、Tushare token。\n"
        "密钥默认只在内存里；想让它记住，得在设置界面里主动勾选。"));
    connect(settings_button_, &QPushButton::clicked, this, [this] { openSettings(); });
    actions->addWidget(settings_button_);

    clear_button_ = new QPushButton(QStringLiteral("清空对话"), page);
    clear_button_->setToolTip(QStringLiteral(
        "只清记录区的显示与多轮历史，不动后端设置，也不动已拉到的数据。"));
    connect(clear_button_, &QPushButton::clicked, this, [this] { clearChat(); });
    actions->addWidget(clear_button_);

    actions->addStretch(1);
    layout->addLayout(actions);

    // ── 记录区 ───────────────────────────────────────────────
    chat_view_ = new QTextBrowser(page);
    // 给出稳定的 objectName：`main_gui --ask` 要按**用户路径**（切页签 →
    // 输入 → 点发送）驱动一次真实对话来出图。靠"按文本找控件"同一个
    // 页面里很快就分不清谁是谁，而 objectName 不会因为改文案就失效。
    chat_view_->setObjectName(QStringLiteral("chatView"));
    chat_view_->setOpenExternalLinks(false);   // 报告里出现网址也不该把浏览器拉起来
    chat_view_->setPlaceholderText(QStringLiteral(
        "在下面输入问题，回车发送。\n\n"
        "这里会按时间顺序累积：你的提问、模型的回答、研判的进度事件与结论。"));
    layout->addWidget(chat_view_, 1);

    // ── 输入行 ───────────────────────────────────────────────
    auto* input_row = new QHBoxLayout();

    chat_input_ = new QLineEdit(page);
    chat_input_->setObjectName(QStringLiteral("chatInput"));
    chat_input_->setPlaceholderText(QStringLiteral(
        "问点什么…（回车发送；/debate 跑投委会研判，/clear 清空，/help 帮助）"));
    chat_input_->setToolTip(QStringLiteral(
        "问的是**当前荷载的那份行情**：标的、根数、最新收盘、区间涨跌、"
        "年化波动率、最大回撤和数据出处都会带进提示词。\n"
        "所以「它现在贵不贵」这种问题，它会基于真实数字回答，而不是凭印象估算。"));
    connect(chat_input_, &QLineEdit::returnPressed, this, [this] { sendChat(); });
    input_row->addWidget(chat_input_, 1);

    chat_send_button_ = new QPushButton(QStringLiteral("发送"), page);
    chat_send_button_->setObjectName(QStringLiteral("chatSend"));
    chat_send_button_->setDefault(true);
    connect(chat_send_button_, &QPushButton::clicked, this, [this] { sendChat(); });
    input_row->addWidget(chat_send_button_);

    layout->addLayout(input_row);

    side_tabs_->addTab(page, QStringLiteral("AI 研判"));
}

void MainWindow::buildToolBar() {
    auto* bar = addToolBar(QStringLiteral("主工具栏"));
    bar->setMovable(false);

    bar->addWidget(new QLabel(QStringLiteral("  数据源 ")));
    // ── 部件 ──
    source_combo_ = new QComboBox(this);
    // 这只是"引擎起来之前"的占位清单，引擎一连上就会被 refreshSourceCombo()
    // 换成引擎自述的那一份。tushare 放在最前 —— 它现在是默认数据源。
    source_combo_->addItems({QStringLiteral("tushare"), QStringLiteral("csv"),
                             QStringLiteral("synthetic")});
    bar->addWidget(source_combo_);

    bar->addWidget(new QLabel(QStringLiteral("  标的 ")));
    symbol_combo_ = new QComboBox(this);
    // 可编辑：清单里的票直接选，清单外的代码手输（会做代码归一化）。
    symbol_combo_->setEditable(true);
    symbol_combo_->setMinimumWidth(190);
    bar->addWidget(symbol_combo_);

    bar->addWidget(new QLabel(QStringLiteral("  根数 ")));
    bars_spin_ = new QSpinBox(this);
    bars_spin_->setRange(60, 10000);
    bars_spin_->setValue(500);
    bars_spin_->setSingleStep(50);
    bar->addWidget(bars_spin_);

    bar->addSeparator();

    bar->addWidget(new QLabel(QStringLiteral(" 预测 ")));
    method_combo_ = new QComboBox(this);
    method_combo_->addItems({QStringLiteral("ar"), QStringLiteral("randomwalk")});
    bar->addWidget(method_combo_);

    bar->addWidget(new QLabel(QStringLiteral(" 步长 ")));
    horizon_spin_ = new QSpinBox(this);
    horizon_spin_->setRange(1, 30);
    horizon_spin_->setValue(5);
    bar->addWidget(horizon_spin_);

    bar->addWidget(new QLabel(QStringLiteral(" 回测折数 ")));
    folds_spin_ = new QSpinBox(this);
    folds_spin_->setRange(1, 20);
    folds_spin_->setValue(5);
    bar->addWidget(folds_spin_);

    boll_check_ = new QCheckBox(QStringLiteral("布林带"), this);
    boll_check_->setChecked(true);
    bar->addWidget(boll_check_);

    bar->addWidget(new QLabel(QStringLiteral(" 回放速度 ")));
    speed_combo_ = new QComboBox(this);
    // userData 就是每根之间的间隔毫秒数；0 表示全速不等待。
    speed_combo_->addItem(QStringLiteral("中 (60ms)"), 60);
    speed_combo_->addItem(QStringLiteral("快 (20ms)"), 20);
    speed_combo_->addItem(QStringLiteral("慢 (200ms)"), 200);
    speed_combo_->addItem(QStringLiteral("全速"), 0);
    speed_combo_->setToolTip(QStringLiteral(
        "回放时每根 K 线之间停多久。停得越久越像实时行情，整段放完也越久。\n"
        "默认 60ms：250 根大约 15 秒，能看清表格跳价和图表生长。"));
    bar->addWidget(speed_combo_);

    bar->addSeparator();

    reload_button_ = new QPushButton(QStringLiteral("重新分析"), this);
    connect(reload_button_, &QPushButton::clicked, this, [this] { runAnalysisAsync(); });
    bar->addWidget(reload_button_);

    replay_button_ = new QPushButton(QStringLiteral("回放行情"), this);
    connect(replay_button_, &QPushButton::clicked, this, [this] { startReplay(); });
    bar->addWidget(replay_button_);

    stop_button_ = new QPushButton(QStringLiteral("停止回放"), this);
    stop_button_->setEnabled(false);
    connect(stop_button_, &QPushButton::clicked, this, [this] { stopReplay(); });
    bar->addWidget(stop_button_);

    // 「拉取数据」：把清单里的标的逐个取数落成 CSV 缓存。它是**数据准备**
    // 动作（刷新本地数据），不是分析动作 —— 所以和分析/研判互不占用 RPC
    // 通道的假设不成立，它照样要抢 engine_.rpc()，统一由 updateActionState 管。
    pull_button_ = new QPushButton(QStringLiteral("拉取数据"), this);
    pull_button_->setToolTip(QStringLiteral(
        "把关注清单里的标的逐个拉一遍，落成 data/<代码>.csv。\n"
        "配好 token 之后，即使以后断网，也有真实历史可看。\n"
        "清单来自启动配置档的 watchlist；没配则用内置的上证成分股。"));
    connect(pull_button_, &QPushButton::clicked, this, [this] { runPullAsync(); });
    bar->addWidget(pull_button_);

    // ── 参数控件 → 自动重跑 ──────────────────────────────────
    //
    // 接在**末尾**而不是每建一个控件就接一次：到这里所有初值都已经设好，
    // 连接之后不会再被初始化时的赋值打到，省掉一堆"是不是程序化赋值"的判断。
    //
    // 布林带勾选框和回放速度**故意不接**：前者只改图表的叠加显示，后者只管
    // 回放节奏 —— 都不参与分析参数，改了没必要重跑四个 RPC。
    reanalyze_timer_ = new QTimer(this);
    reanalyze_timer_->setSingleShot(true);
    reanalyze_timer_->setInterval(250);
    connect(reanalyze_timer_, &QTimer::timeout, this, [this] { runAnalysisAsync(); });

    const auto hook = [this](auto* widget, auto signal) {
        connect(widget, signal, this, [this](auto...) { scheduleReanalysis(); });
    };
    hook(method_combo_, &QComboBox::activated);
    hook(bars_spin_,    &QSpinBox::valueChanged);
    hook(horizon_spin_, &QSpinBox::valueChanged);
    hook(folds_spin_,   &QSpinBox::valueChanged);

    // 换数据源要**顺手把标的清单换掉**，不能只接 scheduleReanalysis：
    // 从 tushare 切到 csv 之后，"600519.SH"这个标的在本地根本没有对应文件，
    // 界面会把用户留在一个必然失败的组合上。
    connect(source_combo_, &QComboBox::activated, this, [this] {
        refreshSymbolCombo(source_combo_->currentText(), symbol_combo_->currentText());
    });
    // 标的切换（下拉选 or 手输回车）走同一条重跑路径。
    connect(symbol_combo_, &QComboBox::activated, this, [this] { scheduleReanalysis(); });
    if (QLineEdit* edit = symbol_combo_->lineEdit()) {
        connect(edit, &QLineEdit::editingFinished, this, [this] { scheduleReanalysis(); });
    }
}

void MainWindow::wireDataHub() {
    DataHub& hub = DataHub::instance();

    // 这个回调跑在**发布线程**（回放线程）上，不是主线程。
    // 所以里面第一件事就是把数据丢回主线程 —— 直接改部件会随机崩溃。
    quote_sub_ = std::make_unique<Subscription>(
        &hub,
        hub.subscribe(
            "market.quote.**",
            [this](const Topic&, const Json& payload) {
                const Quote q = Quote::from_json(payload);
                quotes_seen_.fetch_add(1, std::memory_order_relaxed);
                QMetaObject::invokeMethod(
                    this,
                    [this, q]() {
                        quote_model_->upsertQuote(q);
                        updateStatus();
                    },
                    Qt::QueuedConnection);
            },
            "行情表格"));

    // K 线单独订阅。行情快照（quote）和蜡烛本体（kline）被拆成两个主题，
    // 就是为了这一刻：表格只吃 quote，图表还要 OHLC，
    // 谁也不用去解析自己不用的字段。订阅者也是同样各取所需。
    kline_sub_ = std::make_unique<Subscription>(
        &hub,
        hub.subscribe(
            "market.kline.**",
            [this](const Topic&, const Json& payload) {
                const Candle bar = Candle::from_json(payload);
                bars_seen_.fetch_add(1, std::memory_order_relaxed);
                // 同样跑在回放线程上，所以照样得投回主线程再碰部件。
                QMetaObject::invokeMethod(
                    this,
                    [this, bar]() {
                        if (!replaying_.load()) return;  // 停止后的在途消息直接丢掉
                        chart_->appendBar(bar);
                        updateStatus();
                    },
                    Qt::QueuedConnection);
            },
            "K 线图表"));

    done_sub_ = std::make_unique<Subscription>(
        &hub,
        hub.subscribe(
            "market.replay.done.**",
            [this](const Topic&, const Json&) {
                QMetaObject::invokeMethod(
                    this,
                    [this]() {
                        replaying_.store(false);
                        updateActionState();
                        // 回放把图表切成了流式模式，这里把分析视图贴回去，
                        // 省掉一次重新跑引擎的往返（也就省掉几百毫秒）。
                        if (last_bundle_) applyChartView(*last_bundle_);
                        appendLog(QStringLiteral("回放完成：发布 %1 根 K 线 / %2 条行情，"
                                                 "图表已恢复为分析视图")
                                      .arg(bars_seen_.load())
                                      .arg(quotes_seen_.load()));
                        updateStatus();
                    },
                    Qt::QueuedConnection);
            },
            "回放完成"));
}

// ── 引擎与分析 ────────────────────────────────────────────

void MainWindow::startEngine() {
    try {
        engine_.start();
    } catch (const std::exception& e) {
        showError(QStringLiteral("引擎启动失败"),
                  QString::fromUtf8(e.what()) +
                      QStringLiteral("\n\n请确认 python/ 目录可用，"
                                     "或用环境变量 FINPULSE_PYTHON_ROOT 指定它的位置。"));
        return;
    }
    refreshSourceCombo();

    // 后端清单在这里拉一次并缓存：设置对话框打开时再发请求就要阻塞界面，
    // 而这份清单在进程生命周期内不会变。失败不致命 —— 对话框会退化成
    // 只剩「（用角色配置）」，用户仍然能用配置文件那条路。
    refreshLlmProviders();
}

void MainWindow::refreshLlmProviders() {
    llm_providers_.clear();
    if (!engine_.alive()) return;

    try {
        const Json st = engine_.rpc().call("agent.llm.status", Json::object(), 15000);

        // 先按名字建一张「规格」表：默认端点、密钥环境变量。
        // 用 provider_specs 而不是从 providers[] 里猜 —— 默认端点这件事
        // 只有引擎知道，界面猜不出来。
        std::map<std::string, const Json*> specs;
        for (const Json& s : st["provider_specs"].items()) {
            const std::string name = s["name"].as_string_or("");
            if (!name.empty()) specs[name] = &s;
        }

        for (const Json& p : st["providers"].items()) {
            SettingsDialog::ProviderItem item;
            item.name        = p["name"].as_string_or("");
            item.description = p["description"].as_string_or("");
            if (item.name.empty()) continue;

            const auto it = specs.find(item.name);
            if (it != specs.end()) {
                item.default_base_url = (*it->second)["default_base_url"].as_string_or("");
                item.key_env          = (*it->second)["key_env"].as_string_or("");
                item.needs_network    = (*it->second)["needs_network"].as_bool_or(true);
            }
            llm_providers_.push_back(std::move(item));
        }
    } catch (const std::exception& e) {
        appendLog(QStringLiteral("[!] 读取推理服务商清单失败：%1 —— 设置界面里的"
                                 "「服务商」会是空的")
                      .arg(QString::fromUtf8(e.what())));
    }
}

void MainWindow::refreshSourceCombo() {
    if (!source_combo_) return;
    const auto& inf = engine_.info();
    if (inf.sources.empty()) return;   // 拿不到就保留构建时的那份默认清单

    const QString keep = source_combo_->currentText();

    // 屏蔽信号：这里是**程序在改控件**，不是用户在选。不屏蔽的话，
    // clear() 会把 currentText 变成空串，scheduleReanalysis 会认为
    // "参数变了"，于是在启动瞬间就多跑一次分析。
    QSignalBlocker block(source_combo_);
    source_combo_->clear();
    for (const std::string& name : inf.sources) {
        source_combo_->addItem(QString::fromStdString(name));
    }
    const int idx = source_combo_->findText(keep);
    source_combo_->setCurrentIndex(idx >= 0 ? idx : 0);

    // 数据源清单变了，标的清单要跟着变 —— 两者天生是一对。
    refreshSymbolCombo(source_combo_->currentText());
}

// ── 标的清单 ──────────────────────────────────────────────

void MainWindow::fillSymbolItems(const QString& source) {
    if (!symbol_combo_) return;
    QSignalBlocker block(symbol_combo_);
    symbol_combo_->clear();

    if (source == QStringLiteral("csv")) {
        for (const char* s : {"DEMO-A", "DEMO-B", "DEMO-C"}) {
            symbol_combo_->addItem(QString::fromLatin1(s), QString::fromLatin1(s));
        }
        return;
    }
    if (source != QStringLiteral("tushare") && !source.isEmpty()) {
        symbol_combo_->addItem(QStringLiteral("SYNTH"), QStringLiteral("SYNTH"));
        return;
    }

    // tushare（或引擎还没连上）：上证成分股。
    // 清单可被启动配置档的 watchlist 整体替换 —— 名字认不出来的只显示代码，
    // 不显示 "?"，那会让人以为数据出了问题。
    if (!profile_.watchlist.empty()) {
        for (const std::string& code : profile_.watchlist) {
            const std::string name = name_for(code);
            symbol_combo_->addItem(QString::fromStdString(name.empty() ? code : watch_label({code, name})),
                                   QString::fromStdString(code));
        }
        return;
    }
    for (const WatchItem& item : sse_watchlist()) {
        symbol_combo_->addItem(QString::fromStdString(watch_label(item)),
                               QString::fromStdString(item.code));
    }
}

QString MainWindow::currentSymbol() const {
    if (!symbol_combo_) return {};
    // 下拉选中的项：userData 放着规范代码，而显示文本是"代码  中文名"。
    // 手输之后 Qt 会把 currentIndex 置 -1、userData 失效，这时只能解析文本。
    const QVariant data = symbol_combo_->currentData();
    const QString  text = (symbol_combo_->currentIndex() >= 0 && data.isValid())
                              ? data.toString()
                              : symbol_combo_->currentText();
    return QString::fromStdString(symbol_from_text(text.toStdString()));
}

void MainWindow::refreshSymbolCombo(const QString& source, const QString& keep) {
    if (!symbol_combo_) return;
    const QString want = keep.isEmpty() ? currentSymbol() : keep;

    fillSymbolItems(source);

    const int idx = symbol_combo_->findData(want);
    {
        QSignalBlocker block(symbol_combo_);
        if (idx >= 0) {
            symbol_combo_->setCurrentIndex(idx);
        } else if (!want.isEmpty()) {
            // 清单里没有就保留用户手输的代码：可编辑组合框允许文本不在清单里，
            // 而"我输入的代码被系统偷偷改掉"是最让人恼火的一类行为。
            symbol_combo_->setEditText(want);
        } else if (symbol_combo_->count() > 0) {
            symbol_combo_->setCurrentIndex(0);
        }
    }
}

// ── 启动配置档 ────────────────────────────────────────────

void MainWindow::applyStartupProfile() {
    profile_ = load_profile();

    if (!profile_.error.empty()) {
        appendLog(QStringLiteral("[!] 启动配置档有问题：%1")
                      .arg(QString::fromStdString(profile_.error)));
    }

    // 内置默认也好、配置档也好，最终都要落到"打开就是上证某只票的实时数据"。
    // 所以缺省数据源是 tushare，缺省标的是清单里的第一只。
    const QString def_source = QStringLiteral("tushare");
    const QString def_symbol = sse_watchlist().empty()
                                   ? QString()
                                   : QString::fromStdString(sse_watchlist().front().code);

    const QString source = profile_.source.empty() ? def_source
                                                   : QString::fromStdString(profile_.source);
    {
        QSignalBlocker b1(source_combo_);
        int idx = source_combo_->findText(source);
        if (idx < 0) {
            source_combo_->addItem(source);   // 引擎还没连上时清单是占位的
            idx = source_combo_->count() - 1;
        }
        source_combo_->setCurrentIndex(idx);
    }
    refreshSymbolCombo(source, profile_.symbol.empty() ? def_symbol
                                                       : QString::fromStdString(profile_.symbol));
    if (profile_.bars > 0) {
        QSignalBlocker b2(bars_spin_);
        bars_spin_->setValue(static_cast<int>(profile_.bars));
    }

    if (!profile_.loaded) {
        appendLog(QStringLiteral("未找到启动配置档 —— 走内置默认（%1 / %2）。"
                                 "用 `finpulse-cli --tushare-token <token> --save-profile` "
                                 "存一份，之后每次打开都照它来")
                      .arg(source, currentSymbol()));
        return;
    }

    appendLog(QStringLiteral("启动配置档 %1 → 数据源 %2 / 标的 %3 / %4 根%5")
                  .arg(QString::fromStdString(profile_.path), source, currentSymbol())
                  .arg(bars_spin_->value())
                  .arg(profile_.has_token() ? QStringLiteral(" / token 已配置")
                                            : QStringLiteral(" / 未配置 Tushare token")));
    if (source == QStringLiteral("tushare") && !profile_.has_token()) {
        appendLog(QStringLiteral("  提示：配置档里没有 tushare_token，会按"
                                 "「本地缓存 → 合成演示数据」回落。"
                                 "写入方式：finpulse-cli --tushare-token <token> --save-profile"));
    }
}

// ── 数据出处 ──────────────────────────────────────────────

void MainWindow::logProvenance(const Json& raw) {
    const Json& prov = raw["provenance"];
    if (!prov.is_object()) return;

    const std::string mode   = prov["mode"].as_string_or("");
    const std::string detail = prov["detail"].as_string_or("");
    const std::string reason = prov["reason"].as_string_or("");
    const long long   as_of  = prov["as_of"].as_int_or(0);

    if (mode == "live_api") {
        appendLog(QStringLiteral("数据出处：实时接口（%1）%2")
                      .arg(QString::fromStdString(detail),
                           as_of > 0 ? QStringLiteral("，截至 %1")
                                           .arg(QString::fromStdString(format_date(as_of)))
                                     : QString()));
    } else if (mode == "local_cache") {
        appendLog(QStringLiteral("[!] 数据出处：本地缓存 —— 不是实时数据（%1）")
                      .arg(QString::fromStdString(detail)));
        if (!reason.empty()) {
            appendLog(QStringLiteral("    未能取实时数据的原因：%1")
                          .arg(QString::fromStdString(reason)));
        }
        appendLog(QStringLiteral("    把 tushare_token 写进启动配置档后重开即可拉到实时行情"));
    } else if (mode == "demo_synthetic") {
        // 最容易被误认成真行情的一档，所以字最多、最刺眼。
        appendLog(QStringLiteral("[!!] 数据出处：合成演示数据 —— 既不是实时行情，"
                                 "也不是本地缓存（%1）")
                      .arg(QString::fromStdString(detail)));
        if (!reason.empty()) {
            appendLog(QStringLiteral("    原因：%1").arg(QString::fromStdString(reason)));
        }
    } else if (!mode.empty()) {
        appendLog(QStringLiteral("数据出处：%1").arg(QString::fromStdString(mode)));
    }
}

// ── 批量拉取 ──────────────────────────────────────────────

std::vector<std::string> MainWindow::pullSymbols() const {
    if (!profile_.watchlist.empty()) return profile_.watchlist;
    std::vector<std::string> out;
    out.reserve(sse_watchlist().size());
    for (const WatchItem& item : sse_watchlist()) out.push_back(item.code);
    return out;
}

void MainWindow::runPullAsync() {
    if (!engine_.alive()) {
        showError(QStringLiteral("引擎还没就绪"), QStringLiteral("等引擎握手完成后再拉取。"));
        return;
    }
    if (pull_busy_.exchange(true)) return;
    if (busy_.load() || agent_busy_.load() || chat_busy_.load()) {
        // 四件事都走 engine_.rpc()，同时跑就是几个线程往同一把请求-响应
        // 通道上塞请求。不排队、直接拒绝，并把原因说清楚。
        pull_busy_.store(false);
        showError(QStringLiteral("有任务正在跑"),
                  QStringLiteral("分析 / 研判 / 对话与拉取共用同一条请求通道，"
                                 "请等当前任务结束后再拉取。"));
        return;
    }

    const std::vector<std::string> symbols = pullSymbols();
    const std::string source = source_combo_->currentText().toStdString();
    const std::size_t bars   = static_cast<std::size_t>(bars_spin_->value());
    const std::string token  = profile_.tushare_token;

    if (symbols.empty()) {
        pull_busy_.store(false);
        showError(QStringLiteral("清单是空的"),
                  QStringLiteral("启动配置档的 watchlist 里没有标的，"
                                 "也没拿到内置的上证成分股清单。"));
        return;
    }

    appendLog(QStringLiteral("开始批量拉取：%1 只标的（%2，每只 %3 根）")
                  .arg(symbols.size())
                  .arg(QString::fromStdString(source))
                  .arg(bars));
    updateActionState();

    if (pull_thread_.joinable()) pull_thread_.join();
    pull_thread_ = std::thread([this, symbols, source, bars, token]() {
        try {
            Json params = Json::object();
            Json arr    = Json::array();
            for (const std::string& s : symbols) arr.push(Json(s));
            params.set("symbols", std::move(arr));
            params.set("source", source);
            params.set("bars", static_cast<long long>(bars));
            if (!token.empty()) params.set("token", token);

            const auto t0 = std::chrono::steady_clock::now();
            // 每只一次网络往返，超时给得比单次取数宽：整批共用一个 deadline。
            Json res = engine_.rpc().call("source.pull", std::move(params), 300000);
            const double ms = std::chrono::duration<double, std::milli>(
                                  std::chrono::steady_clock::now() - t0).count();

            QMetaObject::invokeMethod(
                this, [this, res, ms]() { applyPullResult(res, ms); }, Qt::QueuedConnection);
        } catch (const std::exception& e) {
            const QString msg = QString::fromUtf8(e.what());
            QMetaObject::invokeMethod(
                this,
                [this, msg]() {
                    pull_busy_.store(false);
                    updateActionState();
                    appendLog(QStringLiteral("[错误] 批量拉取失败：%1").arg(msg));
                    showError(QStringLiteral("批量拉取失败"), msg);
                },
                Qt::QueuedConnection);
        }
    });
}

void MainWindow::applyPullResult(const Json& result, double elapsed_ms) {
    pull_busy_.store(false);

    const Json& items = result["items"];
    int ok = 0;
    QStringList failures;

    for (const Json& it : items.items()) {
        const std::string sym  = it["symbol"].as_string_or("");
        const std::string name = name_for(sym);
        const QString label = name.empty() ? QString::fromStdString(sym)
                                           : QStringLiteral("%1 %2")
                                                 .arg(QString::fromStdString(sym),
                                                      QString::fromStdString(name));
        if (it["ok"].as_bool_or(false)) {
            ++ok;
            const long long as_of = it["as_of"].as_int_or(0);
            appendLog(QStringLiteral("  [OK] %1   %2 根   截至 %3")
                          .arg(label)
                          .arg(it["rows"].as_int_or(0))
                          .arg(as_of > 0 ? QString::fromStdString(format_date(as_of))
                                         : QStringLiteral("—")));
        } else {
            // 失败项单独排队最后一起打：夹在成功项中间会让人漏看。
            failures << QStringLiteral("  [!!] %1   %2")
                            .arg(label, QString::fromStdString(it["error"].as_string_or("失败")));
        }
    }
    for (const QString& line : failures) appendLog(line);

    appendLog(QStringLiteral("拉取完成：成功 %1 只 / 失败 %2 只，共 %3 ms")
                  .arg(ok)
                  .arg(failures.size())
                  .arg(elapsed_ms, 0, 'f', 0));
    const std::string dir = result["dir"].as_string_or("");
    if (!dir.empty()) {
        appendLog(QStringLiteral("落盘目录：%1").arg(QString::fromStdString(dir)));
    }
    if (!failures.isEmpty()) {
        appendLog(QStringLiteral("  失败项多为「未取到实时数据」时，先确认 token："
                                 "~/.finpulse/profile.json 的 tushare_token"));
    }
    updateActionState();
}

MainWindow::RequestParams MainWindow::collectParams() const {
    RequestParams p;
    p.source     = source_combo_->currentText().toStdString();
    p.symbol     = currentSymbol().toStdString();
    p.bars       = static_cast<std::size_t>(bars_spin_->value());
    p.seed       = 42;
    p.method     = method_combo_->currentText().toStdString();
    p.horizon    = static_cast<std::size_t>(horizon_spin_->value());
    p.folds      = static_cast<std::size_t>(folds_spin_->value());
    p.min_train  = 60;
    return p;
}

void MainWindow::scheduleReanalysis() {
    if (!reanalyze_timer_) return;

    // 参数和上一次**真正发起**的分析完全一样 → 什么都不做。
    // `editingFinished` 之类分不清"改过"与"只是失焦"的信号全靠这一句过滤，
    // 否则每次点开别处都要白跑一次分析（一次分析 = 4 个 RPC）。
    if (last_request_ && *last_request_ == collectParams()) return;

    // 引擎还没起来时别发请求。启动阶段本来也没有信号会被触发（初值是在
    // 接线之前设的），这里是给"引擎启动失败后用户还在改参数"这种情况兜底。
    if (!engine_.alive()) return;

    reanalyze_timer_->start();   // 重启计时：连点几下只会触发一次重算
}

void MainWindow::runAnalysisAsync() {
    if (busy_.exchange(true)) {
        // 已经有一次在跑。**不能**直接丢弃：用户"改根数→再改预测方法"时，
        // 第二次会被静默吞掉，界面停在前一次结果上 —— 又变回"改了没用"。
        // 所以记一笔，并让定时器稍后重试，等这次跑完自动补上最新参数。
        pending_reanalysis_.store(true);
        if (reanalyze_timer_) reanalyze_timer_->start();
        return;
    }

    // 研判/对话/拉取和分析共用 engine_.rpc()，不能同时跑。按钮那时本来
    // 就是灰的，这里是第二道防线 —— 将来多一个触发入口时，这道防线仍然管用。
    if (agent_busy_.load() || chat_busy_.load() || pull_busy_.load()) {
        busy_.store(false);
        pending_reanalysis_.store(true);   // 它们结束后自动补跑
        if (reanalyze_timer_) reanalyze_timer_->start();
        return;
    }

    pending_reanalysis_.store(false);

    if (worker_.joinable()) worker_.join();

    // 回放和重新分析都要往同一张图表上写，同时进行必然互相踩：
    // 回放会往"分析结果的 500 根"后面继续追加，画出一张拼接出来的怪图。
    // 重新分析优先，回放直接停掉（图表随后会被新结果整体替换）。
    if (replaying_.load()) stopReplay();

    const RequestParams params = collectParams();
    // 记下"这次真的用了什么参数"：scheduleReanalysis 靠它判断要不要再跑一次。
    last_request_ = params;
    setBusy(true);

    worker_ = std::thread([this, params]() {
        auto bundle = std::make_shared<AnalysisBundle>();
        Json raw;

        try {
            const auto t0 = std::chrono::steady_clock::now();

            // tushare 的 token 走"额外参数"透传给数据源：接口上不硬编码
            // 任何数据源的参数名，加一个源不必改这里。
            Json extra = Json::object();
            if (params.source == "tushare" && !profile_.tushare_token.empty()) {
                extra.set("token", profile_.tushare_token);
            }

            CandleSeries series = engine_.load_series(params.source, params.symbol,
                                                      params.bars, params.seed,
                                                      extra, &raw);
            bundle->series = series;
            bundle->load_raw = raw;

            const auto bars_json = [&series] {
                Json arr = Json::array();
                for (const auto& b : series.bars()) arr.push(b.to_json());
                return arr;
            };

            // 指标：只用得上均线和布林带，其余的交给「风险」页
            Json ind_params = Json::object();
            ind_params.set("bars", bars_json());
            Json specs = Json::array();
            specs.push(Json("ma:5,20"));
            specs.push(Json("boll:20,2"));
            ind_params.set("specs", std::move(specs));

            const Json ind = engine_.rpc().call("analysis.indicators", std::move(ind_params), 30000);
            bundle->ma_short   = extract_line(ind, "ma5");
            bundle->ma_long    = extract_line(ind, "ma20");
            bundle->boll_mid   = extract_line(ind, "mid");
            bundle->boll_upper = extract_line(ind, "upper");
            bundle->boll_lower = extract_line(ind, "lower");

            Json stats_params = Json::object();
            stats_params.set("bars", bars_json());
            bundle->stats = engine_.rpc().call("analysis.stats", std::move(stats_params), 30000);

            ForecastResult fc = engine_.forecast(series.bars(), params.method, params.horizon);
            bundle->forecast        = fc.points;
            bundle->forecast_method = params.method;

            Json bt_params = Json::object();
            bt_params.set("bars", bars_json());
            bt_params.set("method", params.method);
            bt_params.set("horizon", static_cast<long long>(params.horizon));
            bt_params.set("folds", static_cast<long long>(params.folds));
            bt_params.set("min_train", static_cast<long long>(params.min_train));
            bundle->backtest = engine_.rpc().call("forecast.backtest", std::move(bt_params), 60000);

            bundle->elapsed_ms =
                std::chrono::duration<double, std::milli>(
                    std::chrono::steady_clock::now() - t0).count();
        } catch (const std::exception& e) {
            const QString msg = QString::fromUtf8(e.what());
            QMetaObject::invokeMethod(
                this,
                [this, msg]() {
                    busy_.store(false);
                    setBusy(false);
                    showError(QStringLiteral("分析失败"), msg);
                },
                Qt::QueuedConnection);
            return;
        }

        QMetaObject::invokeMethod(
            this,
            [this, bundle]() {
                busy_.store(false);
                applyAnalysis(std::move(*bundle));
                setBusy(false);
            },
            Qt::QueuedConnection);
    });
}

void MainWindow::applyChartView(const AnalysisBundle& bundle) {
    const QString symbol = QString::fromStdString(bundle.series.symbol());

    chart_->setSeries(bundle.series);
    chart_->clearOverlays();
    chart_->clearForecast();

    if (has_finite(bundle.ma_short)) {
        chart_->setOverlay(QStringLiteral("MA5"), to_qt(bundle.ma_short), QColor(0xE6, 0x8A, 0x1E));
    }
    if (has_finite(bundle.ma_long)) {
        chart_->setOverlay(QStringLiteral("MA20"), to_qt(bundle.ma_long), QColor(0x2E, 0x75, 0xB6));
    }
    if (boll_check_->isChecked() && has_finite(bundle.boll_upper)) {
        chart_->setOverlay(QStringLiteral("BOLL上"), to_qt(bundle.boll_upper), QColor(0x9A, 0x9A, 0xA2), true);
        chart_->setOverlay(QStringLiteral("BOLL下"), to_qt(bundle.boll_lower), QColor(0x9A, 0x9A, 0xA2), true);
    }

    QVector<ForecastPoint> fc;
    fc.reserve(static_cast<qsizetype>(bundle.forecast.size()));
    for (const auto& p : bundle.forecast) fc.append(p);
    chart_->setForecast(fc);
    chart_->setTitle(QStringLiteral("%1   %2 根日线   预测方法 %3")
                         .arg(symbol)
                         .arg(bundle.series.size())
                         .arg(QString::fromStdString(bundle.forecast_method)));
}

void MainWindow::applyAnalysis(AnalysisBundle bundle) {
    current_series_ = bundle.series;
    last_bundle_    = bundle;  // 回放会把图表切走，结束后靠这一份贴回来

    // 数据出处先写进日志页：这是"我看到的是不是真行情"的唯一答案，
    // 放在别的页签里没人会翻。
    logProvenance(bundle.load_raw);

    // 工具桥线程要读的那一份快照。**这里是它唯一的写入点** ——
    // 两份数据源各写各的迟早会漂移，而漂移的表现是"智能体报告里的价格
    // 和界面上的对不上"，那种 bug 能查一整天。
    {
        std::lock_guard<std::mutex> lk(agent_state_mu_);
        agent_state_ = bundle.series;
    }

    // ── 图表 ──
    applyChartView(bundle);

    // ── 行情表格先放一个"最新"快照，回放会逐条更新它 ──
    if (!bundle.series.empty()) {
        const auto& last = bundle.series.back();
        const auto& prev = bundle.series.size() > 1 ? bundle.series.at(bundle.series.size() - 2) : last;
        Quote q;
        q.symbol     = bundle.series.symbol();
        q.ts_ms      = last.ts_ms;
        q.last       = last.close;
        q.prev_close = prev.close;
        q.open       = last.open;
        q.high       = last.high;
        q.low        = last.low;
        q.volume     = last.volume;
        quote_model_->upsertQuote(q);
    }

    // ── 风险页 ──
    {
        const Json& s = bundle.stats;
        std::vector<std::pair<QString, QString>> rows;
        const auto add = [&rows](const QString& k, const QString& v) { rows.emplace_back(k, v); };

        add(QStringLiteral("样本区间"),
            QStringLiteral("%1 ~ %2   (%3 根)")
                .arg(QString::fromStdString(s["first_ts"].is_number()
                                                ? format_date(s["first_ts"].as_int())
                                                : ""),
                     QString::fromStdString(s["last_ts"].is_number()
                                                ? format_date(s["last_ts"].as_int())
                                                : ""))
                .arg(bundle.series.size()));
        add(QStringLiteral("区间涨跌"), pct(s["total_return_pct"].as_double_or(std::nan(""))));
        add(QStringLiteral("年化收益 (CAGR)"), pct(s["cagr_pct"].as_double_or(std::nan(""))));
        add(QStringLiteral("年化波动率"), pct(s["ann_vol_pct"].as_double_or(std::nan("")), false));
        add(QStringLiteral("夏普比率"), json_num(s, "sharpe"));
        add(QStringLiteral("索提诺比率"), json_num(s, "sortino"));
        add(QStringLiteral("最大回撤"), pct(s["max_drawdown_pct"].as_double_or(std::nan(""))));
        add(QStringLiteral("  回撤区间"),
            QStringLiteral("%1 → %2 %3")
                .arg(QString::fromStdString(s["max_drawdown_peak_date"].as_string_or("—")),
                     QString::fromStdString(s["max_drawdown_trough_date"].as_string_or("—")),
                     s["max_drawdown_recovery_date"].is_string() ? QStringLiteral("(已收复)")
                                                                 : QStringLiteral("(未收复)")));
        add(QStringLiteral("VaR 95% / 日"), pct(s["var95_pct"].as_double_or(std::nan(""))));
        add(QStringLiteral("CVaR 95% / 日"), pct(s["cvar95_pct"].as_double_or(std::nan(""))));
        add(QStringLiteral("偏度"), json_num(s, "skew"));
        add(QStringLiteral("超额峰度"), json_num(s, "excess_kurtosis"));
        add(QStringLiteral("自相关 lag-1"), json_num(s, "autocorr_lag1"));
        add(QStringLiteral("自相关 lag-5"), json_num(s, "autocorr_lag5"));
        add(QStringLiteral("上涨日占比"), pct(s["positive_days_pct"].as_double_or(std::nan("")), false));
        add(QStringLiteral("单日最大涨 / 跌"),
            pct(s["best_day_pct"].as_double_or(std::nan(""))) + QStringLiteral("  /  ") +
                pct(s["worst_day_pct"].as_double_or(std::nan(""))));

        write_pairs(stats_view_, rows);
    }

    // ── 回测页 ──
    {
        const Json&  b = bundle.backtest;
        const double skill = b["skill"].as_double_or(0.0);

        std::vector<std::pair<QString, QString>> rows;
        const auto add = [&rows](const QString& k, const QString& v) { rows.emplace_back(k, v); };

        add(QStringLiteral("方法"), QString::fromStdString(b["method"].as_string_or("—")));
        add(QStringLiteral("折数 × 步长"),
            QStringLiteral("%1 × %2   （扩张窗口，无前视）")
                .arg(b["folds"].as_int_or(0))
                .arg(b["horizon"].as_int_or(0)));
        add(QStringLiteral("模型 MAE / RMSE"),
            json_num(b, "mae") + QStringLiteral("  /  ") + json_num(b, "rmse"));
        add(QStringLiteral("随机游走 MAE / RMSE"),
            json_num(b, "base_mae") + QStringLiteral("  /  ") + json_num(b, "base_rmse"));
        add(QStringLiteral("MAPE"), json_num(b, "mape") + QStringLiteral("%"));
        add(QStringLiteral("技能分 1-MSE/MSE_rw"),
            num(skill, 4, true) +
                (skill > 0 ? QStringLiteral("   [跑赢随机游走]") : QStringLiteral("   [不如随机游走]")));
        add(QStringLiteral("方向命中率"),
            num(b["dir_acc"].as_double_or(std::nan("")), 2) + QStringLiteral("%"));
        if (b["interval_coverage"].is_number()) {
            add(QStringLiteral("区间覆盖率"),
                pct(b["interval_coverage"].as_double_or(0.0) * 100.0, false) +
                    QStringLiteral("   （名义 95%）"));
        }

        write_pairs(backtest_view_, rows);

        QString tail = QStringLiteral(
            "\n说明\n  · 「技能分」= 1 - MSE_model / MSE_randomwalk。\n"
            "    日频价格的一阶自相关接近 0，随机游走极难被打败；\n"
            "    技能分在 0 附近徘徊是正常结果，显著为正才值得留意。\n"
            "  · 方向命中率没有把随机游走算成 0%（它不携带方向信息），\n"
            "    而是按 50% 处理 —— 否则会人为抬高所有模型的成绩。\n"
            "  · 区间覆盖率若明显低于 95%：区间按同方差假设计算，\n"
            "    价格水平大幅变动时绝对误差随之放大，这是 AR 的已知局限。\n");

        if (b["per_fold"].is_array()) {
            tail += QStringLiteral("\n分折明细\n  折   训练样本    本折RMSE    基线RMSE\n");
            for (const auto& f : b["per_fold"].items()) {
                tail += QStringLiteral("  %1    %2    %3    %4\n")
                            .arg(f["index"].as_int_or(0) + 1, 2)
                            .arg(f["train_size"].as_int_or(0), 8)
                            .arg(json_num(f, "rmse").rightJustified(9))
                            .arg(json_num(f, "base_rmse").rightJustified(9));
            }
        }
        backtest_view_->appendPlainText(tail);
    }

    const QString sym = QString::fromStdString(bundle.series.symbol());
    appendLog(QStringLiteral("分析完成：%1，%2 根，耗时 %3 ms")
                  .arg(sym)
                  .arg(bundle.series.size())
                  .arg(bundle.elapsed_ms, 0, 'f', 1));
    status_label_->setText(QStringLiteral("就绪 —— %1").arg(sym));
}

// ── 智能体研判 ────────────────────────────────────────────
//
// 这里做的事和 CLI 的 [7/7] 是同一套，只是结果渲染到部件而不是终端。
// 两处共用的一份逻辑是事件文案（app/EventText.h）—— 复制一份中文说明到
// 界面里迟早会跟 CLI 的说法对不上。

bool MainWindow::ensureAgent() {
    if (agent_svc_) return true;

    DataHub& hub = DataHub::instance();

    // ── 反向工具通道 ──
    //
    // 惰性启动：没人跑研判就不该白占一个监听端口。启动之后一直留着，
    // 后续每次研判复用（令牌、工具目录都不必重建）。
    bridge_ = std::make_unique<ToolBridge>(&hub);
    bridge_->register_default_tools();

    // 注入的是**本窗口正在展示的**那份行情。这就是"终端状态只存在于
    // C++ 侧"的具体内容：Python 分析进程看不见 agent_state_。
    // 加锁的理由见 MainWindow.h 里 agent_state_ 的说明。
    bridge_->set_quote_provider([this](const std::string& symbol) -> Json {
        std::lock_guard<std::mutex> lk(agent_state_mu_);
        if (agent_state_.empty()) return Json::object();

        const Candle& last = agent_state_.back();
        const double  prev = agent_state_.size() > 1
                                 ? agent_state_.at(agent_state_.size() - 2).close
                                 : last.close;
        Json q = Json::object();
        q.set("symbol", symbol.empty() ? agent_state_.symbol() : symbol);
        q.set("ts_ms", static_cast<long long>(last.ts_ms));
        q.set("last", last.close);
        q.set("open", last.open);
        q.set("high", last.high);
        q.set("low", last.low);
        q.set("volume", static_cast<long long>(last.volume));
        q.set("change_pct", prev != 0.0 ? (last.close / prev - 1.0) * 100.0 : 0.0);
        q.set("source", "C++ GUI 内存");
        return q;
    });

    bridge_->set_series_provider([this](const std::string& symbol) -> Json {
        std::lock_guard<std::mutex> lk(agent_state_mu_);
        Json s = Json::object();
        s.set("symbol", symbol.empty() ? agent_state_.symbol() : symbol);
        s.set("bars", static_cast<long long>(agent_state_.size()));
        s.set("loaded", !agent_state_.empty());
        return s;
    });

    try {
        bridge_->start();
        appendAgent(QStringLiteral("工具桥已启动：%1  （只绑回环，Python 反向回调 C++）")
                        .arg(qs(bridge_->endpoint())));
        QStringList names;
        for (const std::string& n : bridge_->tool_names()) names << qs(n);
        appendAgent(QStringLiteral("已注册工具：%1").arg(names.join(QStringLiteral(", "))));
    } catch (const std::exception& e) {
        // 桥起不来不阻断研判：角色会如实写"该工具不可用"，决议照样出。
        // 这里把原因写出来，免得用户对着报告里的"工具不可用"猜。
        appendAgent(QStringLiteral("[警告] 工具桥启动失败：%1 —— 研判继续，"
                                   "终端工具将报不可用。")
                        .arg(QString::fromUtf8(e.what())));
    }

    agent_svc_ = std::make_unique<AgentService>(engine_, &hub);

    // ── 事件 → AI 研判页 ──
    //
    // 必须现在就订：订阅晚于调用就只能收到空白。
    agent_sub_ = std::make_unique<Subscription>(
        &hub,
        hub.subscribe(
            "agent.stream.**",
            [this](const Topic&, const Json& payload) {
                // 跑在引擎的读线程上，绝不能在这里碰部件。
                const std::string wire = payload["event"].as_string_or("?");
                const std::string what =
                    event_text::describe(event_text::strip_prefix(wire), payload["data"]);
                QMetaObject::invokeMethod(
                    this,
                    [this, wire, what]() {
                        const QString tail = what.empty()
                                                 ? QString()
                                                 : QStringLiteral("  · ") + qs(what);
                        appendAgent(QStringLiteral("  %1%2").arg(qs(wire), tail));
                    },
                    Qt::QueuedConnection);
            },
            "AI 研判页"));

    // ── 投委会 / 角色清单 ──
    // 候选项取自引擎，不是界面自己编的 —— 界面写死一份就会在配置改了之后
    // 提供一个引擎不认识的 id，而报错要等跑完才知道。
    //
    // 清单存进成员供**设置对话框**使用（原先页内那个投委会下拉框已经搬走），
    // 这里只负责把它填对，并且在没有清单时如实报错而不是让用户跑到一半失败。
    try {
        agent_panels_.clear();
        for (const AgentService::PanelInfo& p : agent_svc_->panels()) {
            agent_panels_.push_back({p.id, p.name});
        }
        if (agent_panels_.empty()) {
            appendAgent(QStringLiteral("[错误] 引擎没有返回任何投委会配置。"));
            return false;
        }
    } catch (const std::exception& e) {
        appendAgent(QStringLiteral("[错误] 读取投委会配置失败：%1")
                        .arg(QString::fromUtf8(e.what())));
        return false;
    }

    try {
        agent_roles_.clear();
        for (const AgentService::RoleInfo& r : agent_svc_->roles()) {
            agent_roles_.push_back({r.id, r.name});
        }
    } catch (const std::exception& e) {
        // 角色清单取不到不影响组会：它只服务于"只跑单个角色"这个可选项。
        appendAgent(QStringLiteral("[警告] 读取角色清单失败：%1 —— 「角色」下拉只会有"
                                   "「整场投委会」")
                        .arg(QString::fromUtf8(e.what())));
    }

    appendAgent(QStringLiteral("可用角色 %1 个 / 投委会 %2 个（在「设置…」里选）")
                    .arg(agent_roles_.size())
                    .arg(agent_panels_.size()));
    return true;
}

void MainWindow::runDebateAsync() {
    if (agent_busy_.exchange(true)) return;      // 上一次还在跑
    if (agent_thread_.joinable()) agent_thread_.join();

    if (current_series_.empty()) {
        agent_busy_.store(false);
        showError(QStringLiteral("无法研判"),
                  QStringLiteral("还没有数据。请先执行一次分析。"));
        return;
    }

    // 刻意**不清空**记录区：对话是累积的，跑一次研判只是往里面插一段
    // 结论。清掉的话，用户刚问过的问题和它的回答会一起消失 —— 而那段
    // 上下文常常正是他为什么要跑这次研判的原因。
    if (!ensureAgent()) {
        agent_busy_.store(false);
        updateActionState();
        return;
    }

    // 参数必须在**主线程**收好：工作线程里读 combo / spin 是未定义行为，
    // 而且 Qt 不报错，只是偶尔拿到垃圾值。
    const AgentSettings settings = agent_settings_;

    AgentService::Options opt;
    opt.panel        = settings.panel;      // 空 = 用默认投委会
    opt.rounds       = settings.rounds;     // 0 = 用投委会配置里的轮数
    opt.method       = method_combo_->currentText().toStdString();
    opt.horizon      = horizon_spin_->value();
    opt.folds        = folds_spin_->value();
    opt.min_train    = 60;
    opt.include_text = true;

    // 推理后端：设置里填了就用填的，没填就照角色配置来（引擎侧会当"没覆盖"）。
    opt.provider = settings.provider;
    opt.model    = settings.model;
    opt.base_url = settings.base_url;
    opt.api_key  = settings.api_key;   // 不做任何加工：密钥里可能有非常见字符

    if (bridge_ && bridge_->running()) {
        opt.bridge_endpoint = bridge_->endpoint();
        opt.bridge_token    = bridge_->token();
    }

    // 序列也快照一份：研判要跑几百毫秒，期间用户完全可能点"重新分析"，
    // 那样 current_series_ 会被整体换掉（它还是 worker 线程在写）。
    const CandleSeries series = current_series_;
    const std::string role    = settings.role;

    appendChatTurn(QStringLiteral("FinPulse"),
                   role.empty()
                       ? QStringLiteral("=== 投委会研判开始：%1，%2 根 ===")
                             .arg(qs(series.symbol()))
                             .arg(series.size())
                       : QStringLiteral("=== 单角色研判开始：%1 × %2，%3 根 ===")
                             .arg(qs(role), qs(series.symbol()))
                             .arg(series.size()),
                   QStringLiteral("system"));
    updateActionState();

    agent_thread_ = std::thread([this, series, opt, role]() {
        AgentService::Outcome out;
        auto fail = [this](const QString& msg) {
            QMetaObject::invokeMethod(
                this,
                [this, msg]() {
                    appendChatTurn(QStringLiteral("FinPulse"),
                                   QStringLiteral("研判失败：%1").arg(msg),
                                   QStringLiteral("error"));
                    agent_busy_.store(false);
                    updateActionState();
                    updateStatus();
                },
                Qt::QueuedConnection);
        };

        try {
            const auto t0 = std::chrono::steady_clock::now();
            // 选了单个角色就不组会：只有这一个角色发言，快很多，
            // 适合"只想问一个视角"。这也是设置界面里那一项的承诺。
            out = role.empty() ? agent_svc_->debate(series, opt)
                               : agent_svc_->run_role(role, series, opt);
            const double ms = std::chrono::duration<double, std::milli>(
                                  std::chrono::steady_clock::now() - t0).count();

            QMetaObject::invokeMethod(
                this,
                [this, out, ms]() {
                    // 投票表靠空格对齐，走等宽气泡 —— 经 Markdown 渲染会散架。
                    appendAgentBlock(render_outcome(out, ms));
                    // 反向通道的实测数字：0 次就说明 Python 没真的回调过
                    // C++，而这件事在报告里只表现为"少了几个数字"。
                    if (bridge_) {
                        const ToolBridge::Stats bs = bridge_->stats();
                        appendAgent(QStringLiteral(
                                        "反向工具通道：调用 %1 次 / HTTP 请求 %2 次 / "
                                        "拒绝 %3 / 未知工具 %4")
                                        .arg(bs.tool_calls)
                                        .arg(bs.requests)
                                        .arg(bs.rejected_auth + bs.rejected_host +
                                             bs.rejected_bad_request)
                                        .arg(bs.unknown_tools));
                    }
                    agent_busy_.store(false);
                    updateActionState();
                    // updateActionState 刻意不在空闲时碰状态栏（那样会把
                    // 分析结果显示的标的与根数冲掉），所以收尾要显式把
                    // "研判中…" 换回正常信息 —— 不然它会一直挂着。
                    updateStatus();
                    appendLog(QStringLiteral("研判完成：%1（%2），工具调用 %3 次")
                                  .arg(qs(out.direction.empty() ? "无方向" : out.direction),
                                       qs(out.run_id))
                                  .arg(bridge_ ? bridge_->stats().tool_calls : 0));
                },
                Qt::QueuedConnection);
        } catch (const std::exception& e) {
            fail(QString::fromUtf8(e.what()));
        }
    });
}

// ── 对话 ──────────────────────────────────────────────────
//
// 这一页和第 7 节那个"跑一次研判出一份报告"是两种东西：
//   * 研判有**契约**（段落要齐、方向要从指定段里抽、护栏会检查）；
//   * 对话没有契约，用户问什么就答什么。
// 所以它们走的是两个 RPC（agent.debate / agent.chat），而不是同一套参数。

void MainWindow::appendChatHtml(const std::string& html) {
    if (!chat_view_) return;
    chat_html_ += html;
    chat_view_->setHtml(QString::fromStdString(chat::page_head() + chat_html_));
    // 新消息在最下面，不滚的话用户看到的永远是开头那几句。
    chat_view_->verticalScrollBar()->setValue(chat_view_->verticalScrollBar()->maximum());
}

void MainWindow::appendChatTurn(const QString& who, const QString& text,
                                const QString& tone) {
    appendChatHtml(chat::bubble(who.toStdString(), chat::render_text(text.toStdString()),
                                tone.toStdString()));
}

void MainWindow::clearChat(const QString& why) {
    chat_html_.clear();
    chat_history_ = Json::array();
    if (chat_view_) chat_view_->clear();   // clear() 会把 placeholder 重新显示出来
    if (!why.isEmpty()) {
        appendChatTurn(QStringLiteral("FinPulse"), why, QStringLiteral("system"));
    }
}

void MainWindow::appendAgent(const QString& line) {
    // 事件流和状态行是**运维信息**：它要能一眼扫过，不该和模型回答抢注意力。
    // 所以走等宽、灰度的一条通道，而不是跟回答一样的绿色气泡。
    appendChatHtml(chat::pre_bubble(QStringLiteral("事件").toStdString(),
                                    line.toStdString(), "system"));
}

void MainWindow::appendAgentBlock(const QString& text) {
    appendChatHtml(chat::pre_bubble(QStringLiteral("FinPulse · 研判结果").toStdString(),
                                    text.toStdString(), "assistant"));
}

QString MainWindow::provenanceText(const Json& load_raw) const {
    const Json& prov = load_raw["provenance"];
    if (!prov.is_object()) return {};

    const std::string mode   = prov["mode"].as_string_or("");
    const std::string detail = prov["detail"].as_string_or("");
    if (mode == "live_api")      return QStringLiteral("实时接口（%1）").arg(qs(detail));
    if (mode == "local_cache")   return QStringLiteral("本地缓存 —— 不是实时数据（%1）").arg(qs(detail));
    if (mode == "demo_synthetic") return QStringLiteral("合成演示数据 —— 既不是实时行情也不是缓存");
    return qs(mode);
}

Json MainWindow::chatContext() const {
    Json ctx = Json::object();
    if (current_series_.empty()) return ctx;

    ctx.set("symbol", current_series_.symbol());
    ctx.set("source", source_combo_ ? source_combo_->currentText().toStdString()
                                    : std::string("?"));
    ctx.set("rows", static_cast<long long>(current_series_.size()));

    const Candle& last = current_series_.back();
    ctx.set("as_of", format_date(last.ts_ms));
    ctx.set("last_close", last.close);

    // 风险指标直接取上一次分析的产物。**没有就不塞** —— 提示词里那条
    // "没有的就说没有"要靠这里不给它留编造的余地。
    if (last_bundle_) {
        const Json& s = last_bundle_->stats;
        const auto put = [&ctx, &s](const char* key, const char* stat_key) {
            const Json& v = s[stat_key];
            if (v.is_number()) {
                ctx.set(key, QStringLiteral("%1%").arg(v.as_double(), 0, 'f', 2).toStdString());
            }
        };
        put("range_pct", "total_return_pct");
        put("ann_vol_pct", "ann_vol_pct");
        put("max_drawdown_pct", "max_drawdown_pct");
        ctx.set("provenance", provenanceText(last_bundle_->load_raw).toStdString());
    }
    return ctx;
}

void MainWindow::sendChat() {
    if (!chat_input_) return;

    const QString text = chat_input_->text().trimmed();
    if (text.isEmpty()) return;
    chat_input_->clear();

    // ── 命令 ──
    // 用显式前缀而不是关键词匹配："帮我看看投委会会怎么想" 这种正常提问
    // 不该被当成要跑一场几百毫秒的辩论。
    if (text == QStringLiteral("/clear")) {
        clearChat(QStringLiteral("对话已清空。"));
        return;
    }
    if (text == QStringLiteral("/help")) {
        appendChatTurn(QStringLiteral("FinPulse"),
                       QStringLiteral(
                           "**能做什么**\n"
                           "- 直接提问：基于当前荷载的行情回答，数字全部来自终端\n"
                           "- /debate 跑一场投委会研判，出一份结构化报告\n"
                           "- /clear 清空记录与多轮历史\n"
                           "- 「设置…」配后端 / 模型 / 密钥 / 投委会 / Tushare token\n"
                           "\n"
                           "**它不会做什么**\n"
                           "- 不联网查新闻、不查财报、不给买卖建议\n"
                           "- 数据出处不是实时接口时会主动提醒你"),
                       QStringLiteral("assistant"));
        return;
    }
    if (text == QStringLiteral("/debate")) {
        appendChatTurn(QStringLiteral("你"), text, QStringLiteral("user"));
        runDebateAsync();
        return;
    }

    appendChatTurn(QStringLiteral("你"), text, QStringLiteral("user"));

    // 多轮历史在这里就记上：即使这次请求失败，用户看到的那句话也已经在
    // 记录里了，下一次提问带着它才连贯。
    {
        Json turn = Json::object();
        turn.set("role", std::string("user"));
        turn.set("content", text.toStdString());
        chat_history_.push(std::move(turn));
    }

    if (!engine_.alive()) {
        appendChatTurn(QStringLiteral("FinPulse"),
                       QStringLiteral("引擎还没就绪，等握手完成后再问。"),
                       QStringLiteral("error"));
        return;
    }
    if (chat_busy_.exchange(true)) return;

    // 对话也走 engine_.rpc()，和其它三件事共用同一条请求-响应通道。
    if (busy_.load() || agent_busy_.load() || pull_busy_.load()) {
        chat_busy_.store(false);
        appendChatTurn(QStringLiteral("FinPulse"),
                       QStringLiteral("有任务正在跑（分析 / 研判 / 拉取），"
                                      "它们和对话共用同一条请求通道，等它结束再问。"),
                       QStringLiteral("error"));
        return;
    }

    // 参数与上下文都必须在**主线程**收好：工作线程里读部件是未定义行为。
    const Json messages = chat_history_;   // 含刚追加的这条 user
    const Json context  = chatContext();
    const std::string provider    = agent_settings_.provider;
    const std::string model       = agent_settings_.model;
    const std::string base_url    = agent_settings_.base_url;
    const std::string api_key     = agent_settings_.api_key;
    const std::string role        = agent_settings_.role;
    const double      temperature = agent_settings_.temperature;
    const int         max_tokens  = agent_settings_.max_tokens;

    updateActionState();

    if (chat_thread_.joinable()) chat_thread_.join();

    chat_thread_ = std::thread([this, messages, context, provider, model, base_url,
                                api_key, role, temperature, max_tokens]() {
        // 所有收尾都走这一个入口：把气泡、历史、忙碌标志三件事放在同一次
        // 投递里。分成两次 invokeMethod 的话，两者的执行顺序要靠"谁先
        // post"来保证 —— 那种约定迟早会被后来的改动无声地破坏。
        auto finish = [this](const QString& body, const QString& tone) {
            QMetaObject::invokeMethod(
                this,
                [this, body, tone]() {
                    appendChatTurn(QStringLiteral("FinPulse"), body, tone);
                    // 回答也要进多轮历史 —— 少了这一半，模型看到的是
                    // "用户连问几句而自己从没回答过"，上下文就断了。
                    // 失败的回答同样记上：它也是对话的一部分。
                    Json turn = Json::object();
                    turn.set("role", std::string("assistant"));
                    turn.set("content", body.toStdString());
                    chat_history_.push(std::move(turn));

                    chat_busy_.store(false);
                    updateActionState();
                    updateStatus();
                },
                Qt::QueuedConnection);
        };

        try {
            Json params = Json::object();
            params.set("messages", messages);
            params.set("context", context);
            if (!role.empty())     params.set("role", role);
            if (!provider.empty()) params.set("provider", provider);
            if (!model.empty())    params.set("model", model);
            if (!base_url.empty()) params.set("base_url", base_url);
            if (!api_key.empty())  params.set("api_key", api_key);
            params.set("temperature", temperature);
            params.set("max_tokens", static_cast<long long>(max_tokens));

            // 一次对话要等模型生成，超时给得比本地 RPC 宽得多。
            const Json res = engine_.rpc().call("agent.chat", std::move(params), 180000);

            QString text = QString::fromStdString(res["reply"].as_string_or(""));
            const bool  degraded = res["degraded"].as_bool_or(false);

            // 这几条尾巴都是"回答之外必须让用户知道的事"：降级了没有、
            // 被截断了没有、上下文是不是被砍过。少了它们，用户只会觉得
            // "这模型怎么突然变笨了"。
            const std::string reason = res["fallback_reason"].as_string_or("");
            if (degraded && !reason.empty()) {
                text += QStringLiteral("\n\n（未连接大模型：%1）")
                            .arg(QString::fromStdString(reason));
            }
            if (res["finish_reason"].as_string_or("") == "length") {
                text += QStringLiteral("\n\n（回答被 max_tokens 截断，"
                                       "可在「设置…」里调大「最大输出」）");
            }
            if (res["truncated"].as_bool_or(false)) {
                text += QStringLiteral("\n\n（历史过长，只带了最近 24 条消息给模型）");
            }
            const long long tin  = res["usage"]["prompt_tokens"].as_int_or(0);
            const long long tout = res["usage"]["completion_tokens"].as_int_or(0);
            if (tin + tout > 0) {
                text += QStringLiteral("\n\n— %1 · token %2 → %3")
                            .arg(QString::fromStdString(res["provider"].as_string_or("?")))
                            .arg(tin)
                            .arg(tout);
            }

            finish(text, degraded ? QStringLiteral("error") : QStringLiteral("assistant"));
        } catch (const std::exception& e) {
            finish(QStringLiteral("对话失败：%1").arg(QString::fromUtf8(e.what())),
                   QStringLiteral("error"));
        }
    });
}

// ── 设置 ──────────────────────────────────────────────────

void MainWindow::openSettings() {
    if (busy_.load() || agent_busy_.load() || pull_busy_.load() || chat_busy_.load()) {
        showError(QStringLiteral("有任务正在跑"),
                  QStringLiteral("等当前任务结束后再改参数 —— 改到一半生效会让"
                                 "正在跑的那次用上两套参数。"));
        return;
    }

    // 投委会 / 角色清单来自引擎，第一次要先把 AgentService 拉起来。
    // 顺带把工具桥也起了：这本来就是"打算用这一页"的信号。
    if (!ensureAgent()) {
        updateActionState();
        return;
    }
    refreshLlmProviders();

    SettingsDialog dlg(agent_settings_, llm_providers_, agent_panels_, agent_roles_, this);

    // ── 探测走后台线程 ────────────────────────────────────
    //
    // 「测试并获取模型」要发一次真实的 HTTP 请求（通常一两秒，慢的时候十几秒）。
    // 在主线程里发就是这十几秒界面完全不响应 —— 用户点「取消」都没反应，
    // 只能强杀进程。所以：信号出去 → 后台线程做 RPC → 结果回主线程画出来。
    //
    // guard 是 QPointer：用户完全可能在探测还没回来时就点了「取消」，
    // 那时对话框已经析构，直接写它就是野指针。
    QPointer<SettingsDialog> guard(&dlg);
    connect(&dlg, &SettingsDialog::probeRequested, this,
            [this, guard](const QString& provider, const QString& key, const QString& base_url) {
                // 上一次还没有跑完（用户连点两次）就先收干净，避免两个线程
                // 同时往同一把 RPC 通道上塞请求。
                if (settings_thread_.joinable()) settings_thread_.join();

                settings_thread_ = std::thread([this, guard, provider, key, base_url]() {
                    fp::gui::ProbeResult out;
                    try {
                        Json params = Json::object();
                        params.set("api_key", key.toStdString());
                        if (!provider.isEmpty()) params.set("provider", provider.toStdString());
                        if (!base_url.isEmpty()) params.set("base_url", base_url.toStdString());

                        const Json res = engine_.rpc().call("agent.llm.probe",
                                                            std::move(params), 30000);

                        const auto strings = [](const Json& arr) {
                            std::vector<std::string> list;
                            for (const Json& v : arr.items()) {
                                const std::string s = v.as_string_or("");
                                if (!s.empty()) list.push_back(s);
                            }
                            return list;
                        };

                        out.ok           = res["ok"].as_bool_or(false);
                        out.needs_choice = res["needs_choice"].as_bool_or(false);
                        out.confident    = res["confident"].as_bool_or(false);
                        out.provider     = res["provider"].as_string_or("");
                        out.provider_name = res["provider_name"].as_string_or("");
                        out.note         = res["note"].as_string_or("");
                        out.error        = res["error"].as_string_or("");
                        out.hint         = res["hint"].as_string_or("");
                        out.total        = static_cast<int>(res["total"].as_int_or(0));
                        out.filtered     = static_cast<int>(res["filtered"].as_int_or(0));
                        out.candidates   = strings(res["candidates"]);
                        for (const Json& m : res["models"].items()) {
                            const std::string id = m["id"].as_string_or("");
                            if (!id.empty()) out.models.push_back(id);
                        }
                    } catch (const std::exception& e) {
                        out.ok = false;
                        out.error = QStringLiteral("探测失败：%1")
                                        .arg(QString::fromUtf8(e.what())).toStdString();
                    }

                    // 最后一道防线：错误消息里如果回显了请求内容，不能让它
                    // 把密钥带上界面。引擎那边已经保证不回传，这里是第二道。
                    const std::string secret = key.toStdString();
                    if (secret.size() >= 8) {
                        const auto scrub = [&secret](std::string& text) {
                            for (std::size_t pos = text.find(secret); pos != std::string::npos;
                                 pos = text.find(secret, pos)) {
                                text.replace(pos, secret.size(), "******");
                            }
                        };
                        scrub(out.error); scrub(out.hint); scrub(out.note);
                    }

                    QMetaObject::invokeMethod(this, [guard, out]() {
                        if (guard) guard->applyProbeResult(out);
                    }, Qt::QueuedConnection);
                });
            });

    const int rc = dlg.exec();

    // exec() 返回说明对话框已经关掉。若探测还在跑，先等它结束 ——
    // worker 手里握着 engine_.rpc()，让它跑完再往下走，否则它会在
    // 设置已经生效之后才把结果写进一个已经消失的对话框。
    if (settings_thread_.joinable()) settings_thread_.join();
    if (rc != QDialog::Accepted) return;

    applyAgentSettings(dlg.settings());
    updateActionState();
}

void MainWindow::applyAgentSettings(const AgentSettings& s) {
    agent_settings_ = s;

    // Tushare token 立刻生效：下一次分析（extra 参数）和下一次批量拉取
    // 都会读 profile_.tushare_token，不必重启。
    profile_.tushare_token = s.tushare_token;

    const QString err = persistRemembered(s);
    if (!err.isEmpty()) {
        appendChatTurn(QStringLiteral("FinPulse"),
                       QStringLiteral("写入启动配置档失败：%1").arg(err),
                       QStringLiteral("error"));
        return;
    }

    QStringList lines;
    if (s.provider.empty()) {
        // 没选服务商 = 这一项不覆盖，于是沿用角色自己的配置（默认 rule_based）。
        // 不静默接受：那样"填了密钥"和"用上了密钥"会被看成同一件事，
        // 而实际上引擎根本不知道要把密钥发给谁。
        lines << QStringLiteral("⚠ 没有选择「服务商」—— 本次会沿用角色自己的配置"
                                "（默认是规则后端：不联网、不做推理），"
                                "填的密钥不会生效。");
    } else {
        lines << QStringLiteral("模型服务：%1 · %2")
                     .arg(QString::fromStdString(s.provider),
                          s.model.empty() ? QStringLiteral("未指定模型（用该服务商的默认）")
                                          : QString::fromStdString(s.model));
    }
    lines << QStringLiteral("投委会 %1 · 角色 %2 · 轮数 %3")
                 .arg(s.panel.empty() ? QStringLiteral("默认") : QString::fromStdString(s.panel),
                      s.role.empty() ? QStringLiteral("整场") : QString::fromStdString(s.role))
                 .arg(s.rounds == 0 ? QStringLiteral("用配置") : QString::number(s.rounds));
    lines << (s.remember_key ? QStringLiteral("密钥：已写入 %1")
                                  .arg(QString::fromStdString(profile_.path))
                            : QStringLiteral("密钥：只在本次会话的内存里，重开需重填"));

    appendChatTurn(QStringLiteral("FinPulse"), lines.join(QStringLiteral("\n")),
                   s.provider.empty() ? QStringLiteral("error") : QStringLiteral("system"));
}

QString MainWindow::persistRemembered(const AgentSettings& s) {
    const bool want_token = s.remember_tushare && !s.tushare_token.empty();
    const bool want_key   = s.remember_key && !s.api_key.empty();
    if (!want_token && !want_key) return {};

    // 先读回磁盘上的内容再合并：直接写会把用户手写的 watchlist 抹掉。
    // 这也是 CLI 的 --save-profile 用的同一套做法。
    Profile merged = load_profile();
    if (!merged.error.empty()) {
        // 文件坏了不能装作没看见，也不能悄悄把它覆盖掉 —— 那是用户的数据。
        // 唯一正确的做法是拒绝写入并说清原因，让人自己去看那个文件。
        return QStringLiteral("%1 读取失败：%2")
            .arg(QString::fromStdString(merged.path), QString::fromStdString(merged.error));
    }

    if (want_token) merged.tushare_token = s.tushare_token;
    if (want_key) {
        merged.llm_provider = s.provider;
        merged.llm_model    = s.model;
        merged.llm_base_url = s.base_url;
        merged.llm_api_key  = s.api_key;
    }

    const std::string err = save_profile(merged);
    if (!err.empty()) return QString::fromStdString(err);

    profile_ = merged;
    profile_.loaded = true;
    appendLog(QStringLiteral("启动配置档已更新：%1")
                  .arg(QString::fromStdString(profile_.path)));
    return {};
}

// ── 回放 ──────────────────────────────────────────────────

void MainWindow::startReplay() {
    if (replaying_.load()) return;

    if (current_series_.empty()) {
        showError(QStringLiteral("无法回放"), QStringLiteral("还没有数据。请先执行一次分析。"));
        return;
    }

    if (replay_thread_.joinable()) replay_thread_.join();

    const std::string symbol = current_series_.symbol();

    // 节拍完全由"每根之间停多久"决定。Options::speed 那套"历史时间压缩比"
    // 留给 CLI 用（那里能把一年的日线压成几秒），界面上用不上：
    // 日线相邻两根差 86400000ms，speed=1 意味着一根要等一天。
    const int interval_ms = speed_combo_->currentData().toInt();

    ReplaySource::Options opt;
    opt.interval_ms = interval_ms;
    opt.speed       = 0.0;
    opt.emit_kline  = true;

    replay_ = std::make_shared<ReplaySource>(DataHub::instance(), symbol, opt);
    replay_->load(current_series_);  // 内部拷贝一份，current_series_ 仍然可用

    replaying_.store(true);
    updateActionState();
    quote_model_->clearAll();
    quotes_seen_.store(0);
    bars_seen_.store(0);

    // 图表切到流式模式：从空开始，跟着 K 线一根根长出来。
    // 走的订阅路径和接真实行情完全一样，回放不是另一套代码。
    chart_->beginStreaming();
    chart_->setTitle(QStringLiteral("%1   回放中…").arg(QString::fromStdString(symbol)));

    appendLog(QStringLiteral("开始回放 %1：%2 根，每根间隔 %3")
                  .arg(QString::fromStdString(symbol))
                  .arg(current_series_.size())
                  .arg(interval_ms > 0 ? QStringLiteral("%1 ms").arg(interval_ms)
                                       : QStringLiteral("全速（不等待）")));

    auto source = replay_;
    replay_thread_ = std::thread([source]() {
        // run_to_end() 结束前一定会发 market.replay.done.*，
        // 界面收尾统一放在那个订阅回调里做 —— 两处各改一半必然会漏掉一处。
        source->run_to_end();
    });
}

void MainWindow::stopReplay() {
    if (replay_) replay_->request_stop();
    if (replay_thread_.joinable()) replay_thread_.join();
    replay_.reset();
    replaying_.store(false);
    updateActionState();
}

// ── 杂项 ──────────────────────────────────────────────────

void MainWindow::updateActionState() {
    if (!reload_button_ || !debate_button_) return;   // 部件还没建好

    const bool analysis = busy_.load();
    const bool debate   = agent_busy_.load();
    const bool replay   = replaying_.load();
    const bool pull     = pull_busy_.load();
    const bool chatting = chat_busy_.load();

    // 分析、研判、对话、拉取**四者不能同时跑**：它们都走 engine_.rpc()，
    // 同时进行就是几个线程往同一把请求-响应通道上塞请求，应答配错对象
    // 这种问题事后极难查。收在一处，而不是散在五个地方各禁一次按钮。
    const bool rpc_free = !analysis && !debate && !pull && !chatting;

    reload_button_->setEnabled(rpc_free);
    debate_button_->setEnabled(rpc_free);

    // 回放只往总线上发数据、不占 RPC，所以可以和研判并行 —— 没必要为它
    // 加一条本不存在的互斥。
    replay_button_->setEnabled(!analysis && !replay);
    stop_button_->setEnabled(replay);
    speed_combo_->setEnabled(!replay);

    if (pull_button_) {
        pull_button_->setEnabled(rpc_free);
        pull_button_->setText(pull ? QStringLiteral("拉取中…")
                                   : QStringLiteral("拉取数据"));
    }

    if (chat_send_button_) chat_send_button_->setEnabled(!chatting && !analysis && !debate && !pull);
    if (chat_input_)       chat_input_->setEnabled(!chatting);
    if (settings_button_)  settings_button_->setEnabled(rpc_free);
    if (clear_button_)     clear_button_->setEnabled(!chatting);

    reload_button_->setText(analysis ? QStringLiteral("分析中…")
                                     : QStringLiteral("重新分析"));
    debate_button_->setText(debate ? QStringLiteral("研判中…")
                                   : QStringLiteral("跑投委会研判"));
    if (chat_send_button_) {
        chat_send_button_->setText(chatting ? QStringLiteral("等待…")
                                            : QStringLiteral("发送"));
    }

    // 状态栏只在"忙"的时候抢占；空闲时不碰，让它保留分析结果显示的
    // 标的与根数（那个信息比一句"就绪"有用得多）。
    if (analysis)     status_label_->setText(QStringLiteral("分析中…"));
    else if (debate)  status_label_->setText(QStringLiteral("智能体研判中…"));
    else if (chatting) status_label_->setText(QStringLiteral("等待模型回答…"));
    else if (pull)    status_label_->setText(QStringLiteral("批量拉取数据中…"));
}

void MainWindow::setBusy(bool busy) {
    updateActionState();
    if (busy) status_label_->setText(QStringLiteral("分析中…"));
}

void MainWindow::showError(const QString& title, const QString& detail) {
    appendLog(QStringLiteral("[错误] %1: %2").arg(title, detail));
    QMessageBox::warning(this, title, detail);
}

void MainWindow::appendLog(const QString& line) {
    if (!log_view_) return;
    const QString stamp = QDateTime::currentDateTime().toString(QStringLiteral("HH:mm:ss.zzz"));
    log_view_->appendPlainText(QStringLiteral("%1  %2").arg(stamp, line));
}

void MainWindow::updateStatus() {
    if (!status_label_ || current_series_.empty()) return;

    QString text = QStringLiteral("%1   共 %2 根   总线已投递 %3 条行情")
                       .arg(QString::fromStdString(current_series_.symbol()))
                       .arg(current_series_.size())
                       .arg(quotes_seen_.load());

    if (replaying_.load()) {
        // 回放中把两条主题各自的计数都摆出来。K 线和行情是两个独立的订阅、
        // 两个独立的计数器 —— 数字对不上就说明有一条路径掉帧了，
        // 摆在状态栏上比埋在日志里容易发现得多。
        text += QStringLiteral("   [回放中] K 线 %1 / 行情 %2")
                    .arg(bars_seen_.load())
                    .arg(quotes_seen_.load());
    }

    // 把"当前处于拖动模式"摆在状态栏上。这个提示不是可有可无的装饰：
    // 拖动模式是一个**不随鼠标离开而消失**的持久状态，没有任何提示的话，
    // 用户会一直以为图表"自己粘着鼠标"。
    if (chart_ && chart_->dragMode()) {
        text += QStringLiteral("   [拖动模式：再点一下图表退出]");
    }
    status_label_->setText(text);
}

}  // namespace fp::gui
