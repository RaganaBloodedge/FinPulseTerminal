# FinPulse Terminal

**轻量级跨平台金融终端：C++20 桌面壳 + 嵌入式 Python 分析引擎。**

一个进程做不了所有事——C++ 负责界面与低延迟管道，Python 负责统计与建模。
FinPulse 把两者放进同一个终端：壳通过双向管道驱动一个常驻的分析子进程，
进程之间用一套自研的长度前缀帧协议通信，进程之内用一条线程安全的发布订阅
总线把行情、指标、预测分发给任意多个消费者。

```
┌────────────────────────── C++ 壳 (Qt6 / CLI) ──────────────────────────┐
│   MainWindow / CliApp                                                 │
│        │ 语义化调用                    ▲ agent.stream.<run_id> 事件    │
│   PyEngine  ── RpcClient  ── FrameCodec  ── Subprocess                │
│        │         (请求-响应关联+超时)  (4字节前缀帧)   (pipe/进程)      │
│        │                                                              │
│   AgentService ──── ToolBridge（localhost HTTP，Python 反向回调 C++）  │
└────────┼───────────────────────────────────────────────┼──────────────┘
         │            stdin / stdout 双向管道             │
┌────────▼────────────────── Python 引擎 ────────────────▼──────────────┐
│   protocol（对偶编解码）→ rpc（方法表+错误翻译）→ service（28 方法）    │
│   │                        11 个引擎方法 + 17 个 agent.* 方法          │
│   ├─ datasource 注册表（synthetic / csv / tushare 插件）               │
│   ├─ analysis（指标 / 统计） forecast（AR / 随机游走 / 回测）           │
│   └─ agent（角色配置 → LLM 抽象 → 工具 → 护栏/记忆 → 投委会编排）       │
└───────────────────────────────────────────────────────────────────────┘
```

**当前状态（全部为实测数字，非目标值）**

| 验证项 | 结果 |
|---|---|
| C++ 单元测试 | **157 通过 / 0 失败**（tests/，自研 80 行测试框架） |
| Python 单元测试 | **547 通过 / 0 失败**（python/tests/，WSL 与 Windows 双平台） |
| C++ 构建 | 零 error / 零 warning（GCC 13.3.0，-Wall -Wextra -Wpedantic） |
| GUI 构建 | 零 error / 零 warning（Qt 6.8.3，含 AUTOMOC） |
| CLI 全链路 | 6 次 RPC 全成功 |
| CLI 智能体研判 | 一场投委会 2 轮 + 主席，Python 反向回调 C++ 终端工具 6 次，0 拒绝 |
| GUI 运行 | 引擎握手成功，主窗口、对话式 AI 研判页与设置界面渲染正常（见 docs/screenshots/） |
| 端到端冒烟 | `bash scripts/smoke_test.sh` 7 步全通过（构建 → 测试 → CLI → 研判 → CSV → GUI） |

上表不是愿景，是一次 `bash scripts/smoke_test.sh` 的真实产出：任一步失败，
脚本立即非零退出。仓库里没有"待实现"的占位数字。

<p align="center">
  <img src="docs/screenshots/gui-main.png" width="49%" alt="主界面：自绘 K 线、布林带与自选行情表" />
  <img src="docs/screenshots/gui-agent-tab.png" width="49%" alt="AI 研判页：投委会研判（决议 / 采纳与反对 / 分歧记录 / 置信度）" />
</p>

---

## 快速开始

### 依赖

- C++20 编译器（GCC 11+ / Clang 14+ / MSVC 2022）
- CMake 3.20+，Ninja 或 Make
- Python 3.10+（**仅标准库，无需 pip 安装任何东西**）
- Qt 6.4+（可选，找不到会自动跳过 GUI，CLI 不受影响）

### 构建与运行（Linux / WSL）

```bash
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build build -j"$(nproc)"

# 命令行：6 节完整报告（引擎 → 行情 → 指标 → 风险 → 预测 → 回测）
./build/finpulse-cli --bars 250 --seed 42
./build/finpulse-cli --source csv --symbol 600519 --bars 500   # 从 data/ 读 CSV
export TUSHARE_TOKEN=你的token                                  # 实时 A 股日 K
./build/finpulse-cli --source tushare --symbol 000001.SZ --bars 250

# 命令行：智能体投委会研判（第 7 节，事件流 + 投票表 + 主席报告）
./build/finpulse-cli --agent --bars 300          # 三个分析师 → 交叉质证 → 主席
./build/finpulse-cli --agent --list              # 看有哪些角色与投委会
./build/finpulse-cli --agent --role risk_officer --brief
./build/finpulse-cli --agent --no-bridge         # 验证"终端工具不可用"的降级路径

# 显式接入大模型（不填则显式报告"未连接大模型"，绝不静默假装在用 LLM）
./build/finpulse-cli --agent --provider deepseek --model deepseek-chat --api-key <密钥>

# 图形界面（右侧「AI 研判」页 = 对话式问答 + 投委会研判 + 参数设置）
./build/src/gui/finpulse-gui
./build/src/gui/finpulse-gui --screenshot /tmp/x.png --ask "茅台现在风险在哪"

# Windows 上直接双击 scripts/run-gui.cmd：里面挡了 WSLg 的 copy mode 故障
# （窗口建得出来但画不出来，看起来就像"双击了没反应"）。见 docs/manual.md 10.1

# 单元测试
ctest --test-dir build --output-on-failure
python3 python/tests/run_tests.py
```

### 常用参数

```
finpulse-cli [--source synthetic|csv|tushare] [--symbol SYNTH] [--bars N]
             [--csv 路径] [--seed N] [--method ar|randomwalk]
             [--horizon N] [--folds N] [--min-train N] [--bus-demo] [-v]
finpulse-cli --agent [--role <id>] [--panel <id>] [--rounds N]
             [--provider <name>] [--model <id>] [--base-url <url>]
             [--api-key <key>] [--no-bridge] [--brief] [--list]
```

`-v` 打开引擎与桥接层的调试日志；`--bus-demo` 演示总线上多订阅者并行消费。

`--agent` 复用同一个引擎进程：分析方法与 agent 方法装在**同一把** RPC 通道上，
所以研判用的行情就是前面几节刚装载、刚算完指标和回测的那一份。它会顺带起一个
**只绑回环**的 HTTP 工具桥，让 Python 在推理过程中反向调用 C++ 侧的终端状态
（最新行情 / 总线统计 / 数据质量）—— 调用了几次在报告末尾有实测数字。

---

## 项目结构

```
FinPulseTerminal/
├── src/core/        Json（手写受限解析器）/ Log / Topic（通配主题）/ DataHub（发布订阅总线）
├── src/bridge/      FrameCodec / Subprocess（POSIX+Windows 双实现）/ RpcClient / PyEngine
├── src/model/       Timestamp / Quote / Candle / CandleSeries / Forecast
├── src/data/        ReplaySource（历史 K 线按时间轴重放成实时行情）
├── src/agent/       AgentService（C++ 门面）/ ToolBridge（反向工具通道，只绑回环）
├── src/app/         CliApp（6+1 节报告式 CLI）/ EventText（事件→文案，CLI 与 GUI 共用）
├── src/gui/         MainWindow / QuoteTableModel / CandleChartWidget（自绘 K 线）
│                    ChartInteraction（点击切换的拖动模式）+ AI 研判页
├── python/finpulse_engine/
│   ├── protocol.py  与 FrameCodec.cpp 严格对偶的帧编解码
│   ├── rpc.py       方法表 + 错误翻译（任何异常不杀进程）
│   ├── service.py   11 个引擎方法 + 17 个 agent.* 方法
│   ├── stream.py    事件通道（与响应帧共用一把写锁守住帧边界）
│   ├── analysis/    indicators / stats（纯标准库）
│   ├── datasource/  base / registry（@register 插件）/ synthetic / csvfile / tushare
│   ├── forecast/    randomwalk / ar（OLS + AIC 选阶）/ backtest（walk-forward）
│   └── agent/       config（声明式角色/投委会 + 配置体检） llm（rule_based / openai_compat
│                    + override 运行时覆盖：provider/model/base_url/api_key 穿四层）
│                    tools（工具注册表 + HttpToolClient 反向回调） schemas / guardrails
│                    memory（决议记忆） orchestrator（独立 → 交叉质证 → 主席） api（RPC 安装）
│                    configs/*.json（4 角色 + 投委会，改配置不用重编）
├── tests/           C++ 测试（自研框架，157 例）
├── python/tests/    Python 测试（unittest，547 例）
├── tools/           probe.py（手工驱动引擎，含通用 rpc 子命令）/ bench_bridge.cpp（桥接基准）
├── scripts/         smoke_test.sh（一键端到端，7 步）
└── docs/            使用手册 / 架构 / 协议规格 / 设计取舍 / 迭代记录 / 截图
```

---

## 值得展开讲的三处设计

### 1. 帧协议：`[4 字节大端长度][UTF-8 JSON]`

跨语言边界上唯一"两侧必须逐字节一致"的约定。增量解帧器处理半包/粘包/
跨帧切片，零长度帧与超长帧（>16 MiB）立即判定流损坏并退出——因为长度头
一旦错了，继续解只会产出一串垃圾，宁可让壳重建子进程。
规格见 [docs/bridge-protocol.md](docs/bridge-protocol.md)。

### 2. DataHub：锁内抄快照，锁外派发

`publish()` 在锁内只做"匹配 + 抄 shared_ptr 句柄"，handler 一律在锁外调用。
换来三个性质：回调里再订阅/退订/发布不死锁；慢订阅者不阻塞其它线程；
遍历中退订不使迭代器失效。代价是刚退订者可能收到一次在途投递——这是
写进头文件注释的**显式取舍**，不是疏忽。

### 3. 预测：随机游走是唯一严肃的对照

在日频价格上，随机游走极难被打败（Meese–Rogoff, 1983）。所以本项目把
`skill = 1 - MSE_model / MSE_randomwalk` 作为回测的首要指标，并把随机
游走的方向命中率固定记为 50%（它不携带方向信息，记 0% 会人为抬高技能分）。
技能分为负时界面如实显示"**不如随机游走**"。

---

## 文档

| 文档 | 内容 |
|---|---|
| [docs/manual.md](docs/manual.md) | **使用说明书**：安装、命令行/界面操作、报告解读、智能体详解、配置与排错 |
| [docs/architecture.md](docs/architecture.md) | 三支柱架构、线程模型、DataHub 并发语义详解 |
| [docs/bridge-protocol.md](docs/bridge-protocol.md) | 帧格式与 RPC 信封的完整规格（含错误码表） |
| [docs/design-decisions.md](docs/design-decisions.md) | 每一个"不用现成库"的取舍与代价 |
| [docs/iteration-log.md](docs/iteration-log.md) | 开发中发现并修复的真实缺陷记录 |

## License

MIT，见 [LICENSE](LICENSE)。
