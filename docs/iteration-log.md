# 迭代记录

开发过程中修复的缺陷。每条对应仓库中的一道回归防线。测试用例数从首轮的 211 演进到 C++ 157 / Python 547。

---

## 1. Subprocess 的 waitpid 竞态

**现象**：引擎正常退出时偶发误报 `引擎未在 1.5s 内退出，强制终止`，日志时间戳显示只过了 10 ms。
**根因**：读线程读到 EOF 后回收了子进程，主线程随后的 `waitpid` 拿到 `ECHILD`（没有这个子进程），被误读成"超时"。两个线程收尸同一个进程。
**修复**：`Subprocess` 内部加 `wait_mu_` + `cached_exit_` + `reaped_`，收尸只发生一次、结果缓存共享；`RpcClient::reader_loop` 读到 EOF 时不再调用 `proc_.wait()`，收尸归 `stop()`，读线程只报告 EOF。

## 2. Python stdout 块缓冲

**现象**：引擎已计算 8 秒，壳一个字节都没收到。
**根因**：stdout 连接到管道时，Python 默认 8 KB 块缓冲，响应不够大就一直攒着。
**修复**：启动参数加 `-u`，环境变量加 `PYTHONUNBUFFERED=1`。两处同改，单独改其一可被更换启动方式绕过。

## 3. `sys.stdin.buffer.read(n)` 的语义陷阱

**现象**：引擎卡住不动，不报错也不退出。
**根因**：`read(n)` 阻塞到读满 n 字节才返回，而请求帧大多比读块短，永远凑不齐。
**修复**：改用 `os.read(fd, chunk)`，有多少返回多少。该问题不崩溃、不报错，只是不返回。

## 4. 合成源波动率锚定

见 design-decisions.md 第 7 条。`annual_vol=0.28` 实测 53.9% → 37.6% → 30.5%。
**根因**：无条件方差漏了跳变混合的二阶矩抬升和隔夜跳空的方差贡献，两处各自只差一点，合起来差了近一倍。靠 stats 输出逐项对账定位。
**修复**：无条件方差中补入这两项贡献。

## 5. DataHub 递归派发

**风险**：handler 里再 `publish` 形成递归派发，环形触发导致栈溢出。
**修复**：`thread_local` 深度计数，上限 8 层，超限丢弃并计入统计。

## 6. GUI 层的六处编译错误

GUI 代码在 `FINPULSE_BUILD_GUI=OFF` 状态下贯穿整个开发期，从未进入构建。首次以 Qt6 真实编译，6 个 TU 的错误同时出现：

| 错误 | 根因 | 修复 |
|---|---|---|
| `Version.h: No such file` | include 路径少了 `core/` 前缀 | 改成 `core/Version.h` |
| `std::string` → `QVariant` | Qt 不做隐式转换 | `QString::fromStdString` |
| `QString::arg(std::string)` | 同上 | 同上 |
| `std::max(long long&, const int64_t&)` | 模板推导冲突（`int64_t` 在 Linux 是 `long`） | 显式 `static_cast<long long>` |
| `incomplete type 'QDateTime'` | 只用了前向声明 | 补 `#include <QDateTime>` |
| `Subscription` 三参构造不存在 | 构造签名是 `(hub, id)` 两参，多传了个 `0` | 去掉多余实参 |

## 7. GUI 的 nullptr connect

**现象**：启动时打印 `QObject::connect(...): invalid nullptr parameter`，十字光标联动失效。
**根因**：`connect(chart_, ...)` 写在 `buildToolBar()` 里，而 `buildToolBar()` 在 `buildUi()` 中先于 `chart_` 创建被调用，此时 `chart_` 为 nullptr。Qt 只打一行警告，不崩溃，功能静默失效。
**修复**：connect 移到 `chart_` 创建之后，并加注释说明顺序约束。

## 8. 测试自身抓出的问题

Python 测试首轮运行，211 例挂 9 个，其中 1 个是实现缺陷，8 个是测试期望值本身出错：

| 问题 | 归属 | 处置 |
|---|---|---|
| `evaluate()` 在折里没有预测时裸抛 `ZeroDivisionError` | 实现缺陷 | 加显式检查，报可读错误（静默返回 0 会违背"样本不足不返回 0"的包级约定） |
| ATR 测试的手算期望值错了 | 测试错误 | 重新手算（TR 首根是 H−L，此处为 0） |
| "数据不足"边界算错（100 根 > 85 根门槛，不会触发报错） | 测试错误 | 改用 80 根 |
| `dict(bar)` 对 dataclass 不适用 | 测试错误 | 直接改属性 |
| 浮点精度（`0.10000000000000009`） | 测试写法 | `assertAlmostEqual` |
| 跨周末取数忘了包含结束日本身 | 测试理解偏差 | 修正期望值 |

## 9. 其他已修复的编译期问题

| 问题 | 修复 |
|---|---|
| `FP_TEST` 宏 `##` 拼中文名不是合法标识符 | 两层宏：`__LINE__` 拼标识符，中文名作字符串 |
| `Json(int64_t)` 构造歧义 | 补 `long` / `unsigned long` 重载 |
| `ReplaySource(..., Options opt = {})` GCC 拒绝默认实参 | 改两个构造函数重载 |
| `-Wdangling-reference`（TestFramework.h） | `FP_CHECK_EQ` 改值拷贝 |
| `-Wformat-truncation`（`format_date` 缓冲区） | `char buf[16]` → `[32]`、`[24]` → `[48]` |
| `parse_date` 失败返回 0 | 0 是合法 epoch，改返回 -1；并拒绝两位数年份 |
| `normalize()` 去重保留第一条 | CSV 追加写覆盖语义要求保留**最后**一条；`std::unique` 保留第一条，弃用 |
| 测试断言写错（`market.*.AAPL` 本就该被匹配） | 修正断言并补真正的字面量精确匹配用例 |

## 10. 回放行情是个摆设

**现象**：日志中 `装载 500 根 SYNTH 的历史数据` 到 `回放结束，共发布 500 根` 相隔 1 毫秒（15:14:51.068 → .069），表格清空又瞬间填满，蜡烛图不动。
**根因**：两层叠加。`MainWindow::startReplay()` 使用 `ReplaySource` 默认 `Options{}`，`speed = 0.0` 的语义是"全速、不 sleep"；GUI 只订阅 `market.quote.**`，未订阅 `market.kline.**`，把速度调慢烛图也不会动。
**修复**：工具栏加「回放速度」下拉（全速/20ms/60ms/200ms，`userData` 存间隔毫秒数，`currentData().toInt()` 取出）；新增 `CandleChartWidget::beginStreaming()` / `appendBar()`，图表从空开始逐根生长，铺满 120 根后转滚动窗口（不改 `offset_`）；`MainWindow` 增订 `market.kline.**`；`applyChartView()` 从 `applyAnalysis()` 拆出，靠缓存的 `last_bundle_` 恢复分析视图；收尾统一到 `market.replay.done.*` 订阅回调（原线程 lambda 与订阅回调各改一半界面状态）；`runAnalysisAsync()` 先停回放；状态栏显示 `[回放中] K 线 N / 行情 N`。
**防回归**：`--screenshot` 加 `--replay <ms>`，定时器到点后找到按钮并 `click()`，走与用户点击相同的路径；两张截图分别抓到回放进行中（68 根 / 3.98s，节拍 ≈ 58ms 对上 60ms 设定）与放完后的恢复画面。

## 11. 事件说明文字整段消失

**现象**：CLI 智能体研判一节中事件流只剩事件名（`agent.round.done`），其后应有的"第 1 轮结束（无人改口）"缺失。无报错，退出码 0，事件已收到。
**根因**：描述表的键用语义名（`role.start`），Python 侧推送的事件名带传输层前缀（`agent.role.start`），每条分支都不命中，`describe()` 返回空串，调用方按约定只打事件名。
**修复**：前缀在传输层边界剥掉（`event_text::strip_prefix()`，只剥一层）；文案抽到 `src/app/EventText.h`（纯函数、无 IO，CLI 与 GUI 共用）。
**防回归**：元性质测试，把引擎推的 11 种事件名列成清单，逐个断言"必须产出非空说明"。

## 12. AgentService 析构后的事件处理器是 use-after-free

**现象**：无。接 GUI 时按析构顺序审查发现，从未真正崩过；事件在服务析构之后到达即为野指针写。
**根因**：`AgentService` 构造时向 `PyEngine` 装入的事件处理器以 lambda 捕获 `this`，处理器读 `seq_`、写 `forwarded_`、用 `hub_`，均经过 `this`。引擎的生命周期由调用方掌握，可能比 `AgentService` 更长。原析构注释称"留一个已失效的 handler 是安全的"，该判断不成立。
**修复**：把处理器访问的状态（序号、转发计数、最近主题、总线指针）抽成 `Sink` 结构，用 `shared_ptr` 持有，处理器只捕获 `weak_ptr`；服务析构后 weak 失效，残留处理器退化为空操作。
**防回归**：测试先建服务再立即销毁，绕过 C++ 门面直接用 RPC 触发一次智能体运行，断言进程不崩且该批事件一条都未到总线（计数不变）。

## 13. 紧凑日期格式让 Tushare 整批静默回落缓存

**现象**：`--source tushare` 每次都打印"本地缓存 —— 不是实时数据"，即使 token 已配置。无报错，回落原因只在 provenance 的 detail 中显示"解析 0 行"。
**根因**：Tushare `daily` 返回的日期是紧凑格式 `20240102`，而 `parse_datetime_ms` 只认 `YYYY-MM-DD` 与 `YYYY-MM-DD HH:MM:SS`。紧凑格式原在 `csvfile` 数据源内预处理（通达信导出兼容逻辑），不是时间解析的通用能力。Tushare 每行日期解析失败被逐条跳过，最终 0 行触发回落。
**修复**：紧凑格式下沉到 `timeutil.parse_datetime_ms`，`csvfile` 删除重复实现。
**防回归**：`test_timeutil` 补格式矩阵用例（三种写法、跨年一致、非法输入），另补 Tushare 侧解析用例。

## 14. 拖动切换的 release 顺序

**现象**：单元测试 `释放点离得远也算拖动` 失败，按下到释放位移超过点击阈值的操作被判定成"点击"，拖动模式被误切换。
**根因**：`DragToggle::release(x)` 先置 `active_ = false` 再调 `move(x)`，而 `move()` 开头有 `if (!active_) return;`，释放点这次的位移未进入判定。距离判定退化为"按下点 → 最后一次移动"。
**修复**：调换顺序，先 `move(x)` 记录释放点位移，再清 `active_`；注释写明 release 的顺序是语义约束，不是风格问题。
**防回归**：`ChartInteraction` 的 9 个用例中的边界用例。

## 15. 对话式研判接线时暴露的三个问题

把 AI 研判页从"一排控件 + 只读文本框"改成"对话气泡 + 独立设置对话框"时，两处缺陷在编译期和首轮测试各被抓到一个，第三个在渲染层。

### 15.1 `Message.assistant()` 不存在

**现象**：`agent.chat` 带多轮历史时抛 `AttributeError: type object 'Message' has no attribute 'assistant'`，仅在历史中出现 assistant 消息时触发。
**根因**：`Message` 只有 `system()` / `user()` / `tool_result()` 三个静态构造器，`agent.chat` 回放历史时写了 `Message.assistant(text)`。此前无代码需构造 assistant 消息，角色运行时中模型回复直接取 `LlmResponse.text`。
**修复**：补 `Message.assistant(text, *, tool_calls=None)`，使 `user()` / `assistant()` / `system()` 三者齐备；补一条"系统提示词只出现一次且在历史之前"的多轮用例。

### 15.2 `class QFormLayout*` 在命名空间里声明出另一个类型

**现象**：`SettingsDialog.cpp` 中十余处 `error: invalid use of incomplete type 'class fp::gui::QFormLayout'`。
**根因**：头文件里把私有方法写成 `void buildBackendGroup(class QFormLayout* form);`。`class X*` 是详细类型说明符，在命名空间 `fp::gui` 中这样写会在该命名空间内声明一个名叫 `QFormLayout` 的新类，与全局 `::QFormLayout` 永远不是同一个类型。`.cpp` 里 `#include <QFormLayout>` 引入全局那个，参数表却是 `fp::gui::QFormLayout`，一调 `addRow` 就报 incomplete type。
**修复**：在全局作用域前置声明 `class QFormLayout;`，参数表写 `QFormLayout*`；错误原因写入头文件注释。

### 15.3 欢迎语里的行内代码被原样显示

**现象**：首条欢迎气泡里 `/debate` 的反引号原样出现在屏幕上。
**根因**：`ChatRender::render_text()` 只支持 `**粗体**`、行首 `#`、行首 `-`/`*` 三种写法，不含行内代码，此为设计决定；写欢迎语时按完整 Markdown 习惯用了反引号。
**修复**：改文案，不用反引号。未给渲染层加行内代码支持，该能力只有一个消费者。

## 16. 报告中的比率表示约定冲突

**现象**：同一份报告第 5 段写「当前年化波动率 0.15%，处于自身历史的 22.09% 分位」，第 4 段（取自 `stats`）写 22.46%，同一个量相差 100 倍；同次运行护栏报 `flow_analyst.number_provenance：1 个数字无法溯源`，detail 指名 `22.09%`（= `0.2209 × 100`）。
**根因**：`rule_based._fill_vol_regime` 一处出错。引擎里比率有两种表示约定且都未写进字段名：`stats` / C++ 的 `change_pct` 带 `_pct` 后缀，已是百分数（`ann_vol_pct = 22.46`）；`flow.volatility_regime` 是小数（`current = 0.1531`）。`_pct()` 的语义是"给已是百分数的值补 `%`"，套在小数上把 15.31% 写成 0.15%；`percentile` 一格手写了 `* 100`，数字对但与工具输出的 `0.2209` 引用对不上，护栏如实报告无可溯源。
**修复**：新增 `_frac_pct()` 专门渲染小数形态的比率，与 `_pct()` 分开，单位由调用点显式声明，不做"自动判断量级"的合并版；`NumberProvenance._matches` 增加一条：字面量带 `%` 时额外用 `值 ÷ 100` 去池子里比一次，只对带 `%` 的字面量换算。不改护栏的话每次跑量价类角色都会误报一条，用户几天内就会学会无视它，等于把整条溯源检查废掉。
**防回归**：新增 `python/tests/test_agent_llm_path.py`（6 例，本进程起假端点，不联网）：七个角色全走外部模型、工具循环真的发生、交叉质证把委员结论交付回去、主席拿得到全部委员结论、第 1 轮看不到别人的结论、token 记账；交叉质证断言第 2 轮请求里带 `### <其他委员>` 注入块、第 1 轮必须没有。
**已知缺口**：报告未加自洽性检查（同一指标的多处引用必须一致），已记入待办。

## 17. `terminal.live_quote` 工具名的点号导致 DeepSeek 400

**现象**：配置 DeepSeek 后跑标准投委会，三个委员全部弃权，报告顶部 `[error] 有效委员 0 位（[]），未达 quorum 2 位`，错误体 `Invalid 'tools[2].function.name': string does not match pattern '^[a-zA-Z0-9_-]+$'`；三个角色报的下标分别是 `tools[2]`、`tools[3]`、`tools[2]`。
**根因**：远程工具名 `terminal.live_quote` 含点号，而 `function.name` 的协议约束是 `^[a-zA-Z0-9_-]+$`。技术面工具清单 `[indicators, stats, terminal.live_quote]` 报 `tools[2]`，量价 `[..., flow, terminal.bus_stats]` 报 `tools[3]`，下标与清单一一对应。点号是刻意的（配置里一眼分辨工具跑在 C++ 终端，`ToolBridge` 令牌作用域按此名授权，测试钉住"远程工具一律带 `terminal.` 前缀"），故修法是在边界翻译而非改名。
**修复**：在传输层把 `terminal.live_quote` 翻译为 `terminal_live_quote`（线上），回程再翻回。翻译放 `openai_compat`，一个 provider 改动覆盖 deepseek / moonshot / dashscope / openrouter / groq / ollama / 自建网关。需改的字段有三处：请求 `tools[].function.name`、assistant 消息的 `tool_calls[].function.name`、`role='tool'` 消息的 `name`；只改第一处则第一轮通过、第二轮 400，报错位置从 `tools[2]` 变成 `messages[3]`。线上名由 `to_wire_name()` 机械生成（非法字符 → 下划线），不保证可逆（`a.b` 与 `a_b` 撞名），回程用对照表；表建立时拒绝撞名与线上名超过 64 字符，`default_registry()` 加自检。线上形态里去掉 `role='tool'` 消息的 `name` 字段（`Message` 内部保留），只发协议定义过的三个键。假端点从单线程 `HTTPServer` 改成 `ThreadingHTTPServer`（委员并发跑，单线程表现为 `ConnectionResetError`）。
**防回归**：假端点改照抄 DeepSeek 规则，`function.name` 不匹配 `^[a-zA-Z0-9_-]+$` 即回 400。新增 `test_agent_llm_path.py::TestRemoteToolWireNames`（4 例）、`TestWireEncoding`（5 例）、`test_agent.py::TestToolWireNames`（9 例）；`tools/mock_llm.py` 加校验与 `FINPULSE_MOCK_TOOL=last`。把 `to_wire_name` 换回恒等函数后 4 例全红，报错 `('tools[2].function.name', 'terminal.live_quote')` 等与原故障逐字一致。

## 18. 第 2 轮被 max_tokens 截断

**现象**：修掉工具名后重跑 DeepSeek，三名委员再次全部弃权，`有效委员 0 位（[]），未达 quorum 2 位`；报错为 `technical_analyst：（第 2 轮输出被 max_tokens=1600 截断）`、`flow_analyst：（max_tokens=1400 截断）`、`risk_officer：（max_tokens=1600 截断）`，token 消耗 23633 → 10146。第 1 轮技术面已给出 `BEARISH`，第 2 轮全被截断导致决议作废。
**根因**：三个原因叠加。
1. 轮数预算小于工具预算：`roles.py` 中 `MAX_TURNS = 4`，角色 `max_tool_calls = 6`。规则后端一轮把工具全部调完，真实 LLM 常一轮只调一个工具，量价分析师 4 个工具即耗尽 4 轮，报告未写。
2. 截断被等同"整份报告不可用"：`response.finish_reason == "length"` 写入 `run.error`，而 `RoleRun.ok = error is None and text`、`is_effective` 要求 `ok`，"没写完"与"没写"同等处理。
3. quorum 建在最终一轮：`debate()` 按最终一轮的 `effective_runs` 判定，第 2 轮三人全被截断，第 1 轮成立的结论一并作废。
**修复**：轮数从工具预算推导，`turn_budget(max_tool_calls) = min(TURN_HARD_CAP, max(2, int(max_tool_calls)) + 2)`；工具预算用完后撤下工具并催促"现在写报告"；截断只记 `run.truncated`，配 warning 级护栏 `output_truncated`，可用性交给 `section_completeness`（error）判定；各角色 `max_tokens` 从 1400/1600/1800 提到 2600/3000/3200；交叉质证轮对未产出可用结论的委员沿用其上一轮结论（判据为 `is_effective`），沿用必须打印到会议问题；没有对等结论可质证的委员第二轮不重跑；任务提示词写明输出预算（段落数 × 200 字），交叉质证注入的对等结论截到 900 字（主席仍 1600 字）；quorum 不足的原因只在 `orchestrator.why_unusable` 一处实现。
**防回归**：新增 11 例（Python 511 → 522）：`test_agent.TestOutputBudget`（7 例）、`test_agent_llm_path.TestRound2Truncation`（4 例）、`tools/mock_llm.py` 加 `FINPULSE_MOCK_TRUNCATE=peers`。退回旧行为均验红：`turn_budget` 换回常量 4、截断改回写 `run.error`、摘掉沿用兜底（`TestRound2Truncation` 4 例全红，报错 `有效委员 0 位（[]），未达 quorum 2 位` 与线上逐字一致）。

## 19. 主席报告的 26 条数字无法溯源

**现象**：修完轮数/截断/沿用后重跑 DeepSeek，决议 `BEARISH / MEDIUM`、改口 2 人，但主席报告报 26 条 `number_provenance` 告警（technical_analyst 8、flow_analyst 8、risk_officer 4、committee_chair 26）。
**根因**：主席的工具清单只有 `["stats"]`，池子按"它调了什么工具"建；而报告引用的均线来自 `indicators`、量比来自 `flow`、技能分来自 `backtest`，这些数字经注入的对等结论进入其视野，不是它自己算的。`stats` 的数字全过，26 个非 `stats` 数字全被判无法溯源，下标一一对应。
**修复**：`GuardrailContext` 增 `prompt_text`（该角色本轮收到的任务提示词原文，含运行参数与其它角色结论），池子改为 `number_pool(ctx.tool_results) | number_pool(ctx.prompt_text)`（`roles.py` 本就拼出这份提示词当 user 消息，留一份引用交给 `_check`）；新增 `_is_derived`，认可池中任意两数相除的商（按精度吻合即视为有出处，带 `%` 的字面量同时比小数形态），只认除法不认加减乘（乘法加法值域太宽会掏空护栏），商限定在 `0.001 ~ 100`（排除量纲不同的两个数相除）；`carry_reason` 只放 `why_unusable()` 的返回值本身，去掉嵌套；告警 message 写入具体数字（如 `2 个数字无法溯源：1.86%、3194.70`）。
**防回归**：新增 9 例（Python 522 → 531）：`test_agent.TestGuardrails`（6 例）、`TestRoleRuntime.test_task_prompt_reaches_the_guardrails`（1 例，断言提示词原文确实送到护栏且这两个数都不在主席自己的工具输出里）、`test_agent_llm_path.TestChairCitesPromptNumbers`（2 例）、`tools/mock_llm.py` 加 `FINPULSE_MOCK_CITE=params`。退回旧行为验红：摘掉提示词、`_is_derived` 恒返回 False。百分数比值用例需写成 `62.5% = 5000000/8000000`，`16.0% = 4/25` 会被"小整数一律放行"（`SMALL_INT_MAX(32)`）白名单兜住而空转。

## 20. 会议问题命名歧义与两类溯源误报

**现象**：报告头部的 `会议问题` 一栏被读成"本场会议要议的问题"，它实际是引擎返回的 `out.problems`（`CliApp.cpp:640` / `MainWindow.cpp:240` 都是 `kv("会议问题", prob)`），本义为"会议层面的问题（含 quorum 不足的原因）"（`AgentService.h:183` 注释）。该栏同时含两类溯源误报。
**根因**：命名歧义：中文里 `问题` 兼有"故障"与"议题"两义。议题通道缺失：`AgentOptions`（`AgentService.h:69-97`）、`DebateResult`（`orchestrator.py:202`）、`build_task_prompt`（`roles.py:181`）均无议题字段，`RoleRuntime.run(instruction=...)` 全仓零调用点，只有 `roles.py:319` 一处形参转发。溯源误报两类：类 A 模型写 `−14.2606`（U+2212），`_NUMBER_RE` 的 `[-+]?` 只认 ASCII，抽出无符号的 `14.2606` 去和池中 `-14.2606` 比；类 B 工具返 `max_drawdown_pct = -24.8469`，报告写「最大回撤 24.85%」，缺幅度匹配。类 C 纯减法衍生（`148 = 250 − 102`）是 `_is_derived` 只认除法的取舍，非 bug。
**修复**：`schemas.normalize_signs()` 把 `U+2212 / U+FF0D / U+2013 / U+2014` 统一成 ASCII `-`，挂在 `extract_numbers()` 入口（一处覆盖 `number_pool` / `numbers_in_text` / 护栏三处调用者）；`NumberProvenance._matches()` 改成没写符号按幅度比、显式写了符号按符号比；`roles.describe_intent(ctx)` 从 `build_task_prompt` 抽出运行参数段供提示词与报告头部共用，`DebateResult.intent` 随结果回传，单角色路径由 `api.AgentService.run_role` 补上；C++ 侧 `Outcome` 加 `intent` 字段并在两条解析路径读出，`会议问题` 改名为 `运行告警`，另起一行 `议题`。
**防回归**：Python 531 → 538（+7：3 条溯源、1 条"议题与提示词同源"、1 条编排 `intent` 序列化、另 2 条为同源拆分出的抽取级用例），C++ 157 / 157。类 A 补两条并存用例：一条守"别误报"（报告写 U+2212、池中为负数），一条守"别把断言一起放过"（池中放正数、报告写 U+2212 负号必须报）；四个回归点均验红（`normalize_signs` 退回恒等、摘掉幅度分支、`to_json` 去掉 `intent`）。

## 21. 双击 run-gui 看不到界面（WSLg 会话故障）

**现象**：双击 `scripts/run-gui.cmd`，控制台一闪，界面消失。
**排查**：全部离线可查：

| 检查 | 结果 |
|---|---|
| `~/finpulse-gui-build/src/gui/finpulse-gui` 在不在 | 在，1.2MB，时间戳是今天的构建 |
| 进程能不能活 | 能。`timeout 20` 跑满 20 秒被掐（rc=124），不是自己退的 |
| 界面内容对不对 | 对。`QT_QPA_PLATFORM=offscreen --screenshot` 出的 PNG 135KB，有内容 |
| 动态库缺不缺 | `ldd` 无 `not found` |
| 窗口有没有到 Windows | 到了。`msrdc.exe` 的标题是 `[WARN:COPY MODE] FinPulse Terminal v0.4.0 …` |

**根因**：非本项目故障。WSLg 把 Linux 窗口送上 Windows 桌面时先在 `/mnt/shared_memory` 分配共享内存，weston.log 中 `rdp_allocate_shared_memory: Failed to open ... Input/output error` 后接 `use_gfxredir = 0`：分配失败关闭图形重定向，退回逐帧拷贝（copy mode），窗口照建、内容画不出来（microsoft/wslg#972）。干净会话日志只有 `use_gfxredir = 1` 且无该报错。每个 WSLg 会话重新抽取该状态，实测四次冷启动两坏两好；本机 WSL 虚拟机空闲即关停，几乎每次双击都是新掷一次。
**修复**：`scripts/run-gui.cmd` 加预检，在弹窗口之前用 `wsl -e bash -lc "...grep -q 'use_gfxredir = 0' ..."` 的退出码判断会话好坏（`use_gfxredir` 在 weston 启动阶段写入，早于任何应用启动），启动器只用 `errorlevel` 判断；加后检查 `tasklist /v /fi "IMAGENAME eq msrdc.exe"` 的标题里有没有 `COPY MODE`；命中则询问是否 `wsl --shutdown` 重来（最多 3 次），不直接重开以免一并关掉 Docker Desktop 等发行版。脚本故意写成纯 ASCII（cmd.exe 按 ANSI 读脚本），中文说明放 `manual.md` 10.1 节。

## 22. 报告缺投入判断段

**现象**：用户反馈"agent 最后的结论没有提到是否值得投资"。上一轮据此添加的 `议题` 行只回显本次运行参数（标的 / 样本 / 预测设置），是元信息，未回答"值不值得投"。
**根因**：报告头部与主席正文都没有正面回答投入问题的段落。
**修复**：`configs/committee_chair.json` 的 `output_sections` 在「决议」之后插入「投入判断」，instructions 输出格式由五段改六段，三档措辞写死；`llm/rule_based.py` 新增 `@filler("投入判断")`，按会议状态填三档 + 三条依据 + 翻转条件；`tools/mock_llm.py` 与 `tests/test_agent_llm_path.py` 的内联假后端各加一个「投入判断」分支；`roles.py` 未改，输出预算按 `len(output_sections)` 自动 +200 字。档位判据先看方向、再看强度：`NEUTRAL` 是"没有方向"而非"高置信度地认为中性"，避免全体 `NEUTRAL` 被判成「值得投入」；`BEARISH` 时不写「值得投入」，改写「支持一次与决议方向（BEARISH）一致的投入决策」。仓位数量与具体价位仍不给出（`ForbiddenPhrases` 未动）。报告头部（CLI 的「决议 / 置信度 / 议题」几行）未加投入判断行。
**防回归**：测试 538 → 547（+9），C++ 157 / 157。落点：配置契约 1、规则后端分档 6（过半 / 少数派 / 打平 / 全体中性 / 无下级结论 / 看空方向要写明投什么）、护栏缺段 1、LLM 路径 1、编排层 1；断言查档位措辞（`**值得投入**`）而非段名。四点回归验红（配置删段 / `all_neutral` 恒 False / `@filler` 摘掉 / 假后端分支删掉）。
