// FinPulse Terminal — 主窗口
//
// 线程模型（这部分是 GUI 里最容易出错的地方，单独说明）：
//
//   * 所有 RPC 调用都在一个后台线程里做。分析一次动辄几十到几百毫秒，
//     放在主线程会让界面卡住甚至被系统判定为"无响应"。
//   * 后台线程算完后，用 QMetaObject::invokeMethod(..., Qt::QueuedConnection)
//     把结果投回主线程再更新部件。Qt 的部件只能在主线程碰，这是硬规则。
//   * DataHub 的派发发生在发布它的那个线程（回放线程），所以订阅回调里
//     第一件事同样是"把数据丢回主线程"，绝不能直接去改表格和图表。
//
// 用 invokeMethod + lambda 而不是自定义信号，是因为后者要给每种数据类型
// 注册 metatype，而 lambda 捕获不需要。少一层样板就少一处出错的地方。
#pragma once

#include <QMainWindow>
#include <QString>

#include <atomic>
#include <memory>
#include <mutex>
#include <optional>
#include <thread>
#include <vector>

#include "bridge/PyEngine.h"
#include "core/Json.h"
#include "core/Profile.h"
#include "model/Types.h"
#include "SettingsDialog.h"   // AgentSettings：研判参数是跨界面传递的纯数据

class QCheckBox;
class QComboBox;
class QLabel;
class QLineEdit;
class QPlainTextEdit;
class QPushButton;
class QSpinBox;
class QSplitter;
class QTabWidget;
class QTableView;
class QTextBrowser;
class QTimer;

namespace fp {
class AgentService;
class ReplaySource;
class Subscription;
class ToolBridge;
}  // namespace fp

namespace fp::gui {

class CandleChartWidget;
class QuoteTableModel;

class MainWindow : public QMainWindow {
    Q_OBJECT

public:
    explicit MainWindow(QWidget* parent = nullptr);
    ~MainWindow() override;

private:
    /// 一次完整分析的全部产物。跨线程按值传递，所以必须是可拷贝的纯数据。
    struct AnalysisBundle {
        CandleSeries               series;
        std::vector<double>        ma_short;
        std::vector<double>        ma_long;
        std::vector<double>        boll_upper;
        std::vector<double>        boll_lower;
        std::vector<double>        boll_mid;
        std::vector<ForecastPoint> forecast;
        Json                       stats;
        Json                       backtest;
        /// 装载行情那一步的原始响应。数据出处（实时接口 / 本地缓存 /
        /// 合成演示）只在这里 —— 序列本身带不了这个信息。
        Json                       load_raw;
        std::string                forecast_method;
        double                     elapsed_ms{0.0};
    };

    struct RequestParams {
        std::string source;
        std::string symbol;
        std::size_t bars{250};
        long long   seed{42};
        std::string method;
        std::size_t horizon{5};
        std::size_t folds{5};
        std::size_t min_train{60};

        /// 用来判断"参数真的变了吗"。
        ///
        /// 有它才敢把 `editingFinished` 这类**无法区分"改了"和"只是失焦"**的
        /// 信号也接上：失焦时算一遍发现参数没动，直接不重跑。否则用户点一下
        /// 别处就会白白重算一次（一次完整的分析要跑 4 个 RPC）。
        bool operator==(const RequestParams&) const = default;
    };

    // ── 构建 ──
    void buildUi();
    void buildToolBar();
    void buildAgentTab();
    void wireDataHub();

    /// 按引擎自述的数据源清单重建下拉框。
    ///
    /// 不在界面里写死 ["synthetic", "csv"]：引擎侧新增一个数据源插件
    /// （tushare 就是这么加进来的）时，界面不该需要跟着改代码 ——
    /// 否则新数据源在 CLI 里能用、在界面上却看不见。
    void refreshSourceCombo();

    /// 按当前数据源重建**标的**下拉框：不同数据源的标的根本不是一回事
    /// （上证成分股 / 本地演示 CSV / 合成序列），混在一张清单里只会让人
    /// 选到一个当前源拉不出来的代码。
    ///
    /// ``keep`` 非空时尽量保留它（用户手输的代码不该因为切了数据源就丢）。
    void refreshSymbolCombo(const QString& source, const QString& keep = {});

    /// 当前选中的标的代码。可编辑组合框里可能显示的是
    /// ``600519.SH  贵州茅台``，用户也可能手输 ``600519`` 或 ``sh600519``
    /// —— 归一化交给 data/Watchlist.h 里的纯函数，这里只管取值。
    QString currentSymbol() const;

    /// GUI 的启动默认值来自启动配置档（``~/.finpulse/profile.json``）：
    /// 打开就是你要的那只票的实时数据，不必每次重选。
    void applyStartupProfile();
    /// 把配置档里的清单装进标的下拉框。
    void fillSymbolItems(const QString& source);

    // ── 批量拉取 ──
    /// 「拉取数据」：把清单里的标的逐个取数、落成 data/<代码>.csv。
    /// 跑在后台线程上（十几只股票就是十几次网络往返，占着主线程会卡界面）。
    void runPullAsync();
    /// 拉取结束后在主线程里汇报结果。失败项逐条列出来 ——
    /// 批量任务最没用的反馈就是"部分成功"。
    void applyPullResult(const Json& result, double elapsed_ms);
    /// 「拉取数据」要拉的清单：配置档的 watchlist，否则内置的上证成分股。
    std::vector<std::string> pullSymbols() const;

    // ── 动作 ──
    void startEngine();
    void runAnalysisAsync();
    void applyAnalysis(AnalysisBundle bundle);

    // ── 智能体研判 ──
    /// 首次调用时建工具桥、建 AgentService、订事件、填投委会/角色清单。
    /// 之后是空操作。失败返回 false（错误已写进 AI 研判页）。
    bool ensureAgent();
    void runDebateAsync();
    /// 把一段文字作为一条消息追加到对话记录里。
    ///
    /// ``mono`` 为真时走等宽气泡（事件流、投票表这类靠空格对齐的内容）。
    void appendAgent(const QString& line);
    void appendAgentBlock(const QString& text);

    // ── 对话 ──
    /// 点「发送」或回车：把输入框里这句话发出去。
    ///
    /// 以 `/` 开头的是命令（``/debate`` ``/clear`` ``/help``），其余走
    /// ``agent.chat``。命令用显式前缀而不是关键词匹配："帮我看看投委会怎么想"
    /// 这种正常提问不该被当成要跑一场几百毫秒的辩论。
    void sendChat();
    /// 把一轮对话追加进记录区并同步进多轮历史。
    void appendChatTurn(const QString& who, const QString& text, const QString& tone);
    /// 直接追加一段已经拼好的 HTML（气泡、分隔线……）。
    void appendChatHtml(const std::string& html);
    /// 清空对话记录与多轮历史。
    void clearChat(const QString& why = {});
    /// 拼给模型的行情上下文。**只放终端真的有的数字** —— 提示词里那条
    /// "不许凭印象估算"靠的就是这里不给它留编造的余地。
    Json chatContext() const;
    /// 数据出处的短句（"实时接口（tushare）" / "本地缓存" / "合成演示数据"）。
    QString provenanceText(const Json& load_raw) const;

    // ── 设置 ──
    /// 打开「AI 研判 · 参数设置」。确定后参数立刻生效；
    /// 只有勾了「记住」的那几项才会写进启动配置档。
    void openSettings();
    /// 从引擎拉一次后端清单，供设置界面的下拉框使用。
    void refreshLlmProviders();
    /// 设置改动落地：立刻生效 + （按需）写配置档。
    void applyAgentSettings(const AgentSettings& s);
    /// 把「记住」的那几项并进磁盘上的配置档。返回空串表示成功。
    QString persistRemembered(const AgentSettings& s);

    /// 按当前状态（分析中 / 研判中 / 对话中 / 拉取中 / 回放中）统一刷新
    /// 按钮可用性与文案。
    ///
    /// **为什么要有这么一个函数**：分析走 `engine_.rpc()`，研判也走
    /// `engine_.rpc()`，对话、拉取同样走它 —— 同时跑就是几个线程往同一把
    /// 请求-响应通道上塞请求，应答配错对象这种问题事后极难查。所以这里把
    /// "同一时刻只允许一件事在跑"收在一处，而不是散在五个地方各禁一次按钮。
    void updateActionState();
    /// 只重画图表部分（K 线 + 叠加线 + 预测区间）。回放结束后用它把
    /// 分析视图原样贴回来，而不必重跑一次引擎。
    void applyChartView(const AnalysisBundle& bundle);
    void startReplay();
    void stopReplay();

    /// 参数控件动了之后**自动**重跑分析（带防抖）。
    ///
    /// 在这之前，工具栏上的数据源/标的/根数/预测方法/步长/折数改了都**不会**
    /// 有任何反应 —— 参数只是被躺在控件里，要用户再点一次「重新分析」才被读走。
    /// 那不是"参数没生效"，是"参数压根没被读"。所以这里把控件变更直接接到
    /// 重跑上：改完即见效。
    ///
    /// 防抖 250ms：连点几下数字框的上下箭头只会触发一次重算，而不是每格一次。
    void scheduleReanalysis();

    void setBusy(bool busy);
    void showError(const QString& title, const QString& detail);
    void appendLog(const QString& line);
    /// 把装载行情那一步的出处写进日志页：实时接口 / 本地缓存 / 合成演示。
    /// 三种情况用三种措辞，最坏的一档（演示数据）说得最重 —— 它最容易被
    /// 当成真实行情看。
    void logProvenance(const Json& raw);
    void updateStatus();
    RequestParams collectParams() const;

    // ── 部件 ──
    QComboBox*   source_combo_{nullptr};
    QComboBox*   symbol_combo_{nullptr};
    QSpinBox*    bars_spin_{nullptr};
    QComboBox*   method_combo_{nullptr};
    QSpinBox*    horizon_spin_{nullptr};
    QSpinBox*    folds_spin_{nullptr};
    QCheckBox*   boll_check_{nullptr};
    QComboBox*   speed_combo_{nullptr};
    QTimer*      reanalyze_timer_{nullptr};
    QPushButton* reload_button_{nullptr};
    QPushButton* replay_button_{nullptr};
    QPushButton* stop_button_{nullptr};
    QPushButton* pull_button_{nullptr};
    QPushButton* debate_button_{nullptr};

    /// 「AI 研判」页：记录区 + 输入框 + 两个动作按钮。
    ///
    /// 参数（后端/模型/端点/密钥/温度/投委会/角色/轮数）**不在这里**，
    /// 全部收进 SettingsDialog。理由是它们属于「配置」而按钮属于「操作」：
    /// 十几个配置项挤在工具栏里，会把每轮都要点的按钮挤到看不见。
    QTextBrowser* chat_view_{nullptr};
    QLineEdit*    chat_input_{nullptr};
    QPushButton*  chat_send_button_{nullptr};
    QPushButton*  settings_button_{nullptr};
    QPushButton*  clear_button_{nullptr};

    /// 记录区的累计 HTML。每次追加后整体 setHtml() 一次 ——
    /// 比逐条 append 慢一点，但样式表和气泡结构只有一个来源，不会漂移。
    std::string chat_html_;

    /// 多轮对话历史（``[{role, content}, ...]``）。直接存成 Json 数组，
    /// 因为它原样就是要发给 ``agent.chat`` 的 ``messages``。
    Json chat_history_{Json::array()};

    CandleChartWidget* chart_{nullptr};
    QuoteTableModel*   quote_model_{nullptr};
    QTableView*        quote_view_{nullptr};
    QPlainTextEdit*    stats_view_{nullptr};
    QPlainTextEdit*    backtest_view_{nullptr};
    QPlainTextEdit*    log_view_{nullptr};
    QLabel*            status_label_{nullptr};
    QTabWidget*        side_tabs_{nullptr};

    // ── 状态 ──
    PyEngine      engine_;
    std::thread   worker_;
    std::atomic<bool> busy_{false};

    /// 启动默认值。**只读**使用：界面上的改动不会写回配置档 ——
    /// "我随手切了一下数据源，从此它每次打开都是这个"不是用户能预期的行为。
    /// 要改默认值请用 `finpulse-cli --save-profile`（或直接编辑那个文件）。
    Profile profile_;

    /// 批量拉取的后台线程与忙碌标志。
    std::thread       pull_thread_;
    std::atomic<bool> pull_busy_{false};

    /// 上一次**真正发起**分析时用的参数。用来挡住"参数没变"的空重跑。
    std::optional<RequestParams> last_request_;

    /// 分析进行中又来了新参数时置位：等当前这次跑完，自动用最新参数再跑一次。
    ///
    /// 没有它的话，用户连改两次参数时第二次会被 `busy_` 直接挡掉 ——
    /// 界面停在前一次的结果上，看起来又变成"改了没用"。
    std::atomic<bool> pending_reanalysis_{false};

    // ── 智能体 ──
    /// 反向工具通道。惰性启动：没人跑研判就不该占着一个监听端口。
    std::unique_ptr<ToolBridge>   bridge_;
    std::unique_ptr<AgentService> agent_svc_;
    std::unique_ptr<Subscription> agent_sub_;   ///< agent.stream.** → AI 研判页
    std::thread                   agent_thread_;
    std::atomic<bool>             agent_busy_{false};

    /// 对话的后台线程与忙碌标志。和研判分开，是因为它们的收尾方式完全不同：
    /// 研判要渲染投票表，对话只贴一段回复。
    std::thread       chat_thread_;
    std::atomic<bool> chat_busy_{false};

    /// 设置界面里「测试并获取模型」的后台线程。
    ///
    /// 探测是一次真实的 HTTP 请求（一两秒到十几秒），在主线程里发就是界面
    /// 整个卡住。独立一个线程而不是复用 chat_thread_：用户完全可能在对话
    /// 跑着的时候去改设置。
    std::thread settings_thread_;

    /// 研判参数。**唯一来源** —— 设置对话框是它唯一的编辑入口，
    /// 研判和对话都从这里读，不各读各的控件（那样两处迟早会不一致）。
    AgentSettings agent_settings_;

    /// 引擎自述的清单，供设置对话框使用。界面里不写死这几份清单，
    /// 引擎新增一个后端/角色而界面不知道，就会出现"命令行能用、界面选不到"。
    std::vector<SettingsDialog::ProviderItem> llm_providers_;
    std::vector<SettingsDialog::NamedItem>    agent_panels_;
    std::vector<SettingsDialog::NamedItem>    agent_roles_;

    /// 工具桥线程要读的那份行情快照。
    ///
    /// 单独存一份、并用锁保护，是因为提供者跑在**桥的 accept 线程**上，
    /// 而 current_series_ 会被主线程在每次分析完成时整体替换 —— 直接读它
    /// 就是一次数据竞争（表现为偶发的乱码价格或崩溃，且难复现）。
    /// 两份的写入点只有 applyAnalysis 一处，不会漂移。
    mutable std::mutex agent_state_mu_;
    CandleSeries       agent_state_;

    /// 当前展示的序列。回放要用它，所以必须留着（ReplaySource 拿到的是副本）。
    CandleSeries current_series_;

    /// 最近一次分析的完整产物。回放会把图表切到流式模式，
    /// 结束后靠这个把分析视图贴回来。
    std::optional<AnalysisBundle> last_bundle_;

    /// 回放源用 shared_ptr 持有：后台线程里也存了一份，
    /// 主线程 reset 的时候不会把正在跑的那份打掉。
    std::shared_ptr<ReplaySource> replay_;
    std::thread                   replay_thread_;
    std::atomic<bool>             replaying_{false};

    std::unique_ptr<Subscription> quote_sub_;
    std::unique_ptr<Subscription> kline_sub_;
    std::unique_ptr<Subscription> done_sub_;
    std::atomic<long long>        quotes_seen_{0};
    std::atomic<long long>        bars_seen_{0};
};

}  // namespace fp::gui
