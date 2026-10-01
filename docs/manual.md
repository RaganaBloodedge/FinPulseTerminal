# FinPulse Terminal 使用说明书

> 适用版本：**v0.4.0**（协议版本 v1）
> 本手册覆盖命令行（`finpulse-cli`）与图形界面（`finpulse-gui`）两个前端，
> 以及底层的 Python 分析引擎与智能体投委会。

---

## 目录

1. [这是什么](#1-这是什么)
2. [安装与构建](#2-安装与构建)
3. [第一次运行](#3-第一次运行)
4. [命令行参考](#4-命令行参考)
5. [报告逐节解读](#5-报告逐节解读)
6. [智能体研判详解](#6-智能体研判详解)
7. [图形界面](#7-图形界面)
8. [配置](#8-配置)
9. [环境变量](#9-环境变量)
10. [故障排查](#10-故障排查)
11. [进阶：手工驱动引擎](#11-进阶手工驱动引擎)
12. [目录与文档索引](#12-目录与文档索引)

---

## 1. 这是什么

**一个由 C++ 桌面壳驱动常驻 Python 分析子进程的金融分析终端。**

- **C++ 侧**负责界面、行情分发与低延迟管道；
- **Python 侧**负责统计、建模与智能体推理；
- 两侧用一套自研的**长度前缀帧协议**双向通信（`[4 字节大端长度][UTF-8 JSON]`）；
- 智能体推理时，Python 还能**反向回调 C++** 拿终端的实时内存状态（最新行情、总线统计、数据质量）——这是本项目"C++/Python 联合能力"的核心。

两种使用方式，共用同一套引擎：

| 前端 | 可执行文件 | 适合 |
|---|---|---|
| 命令行 | `finpulse-cli` | 脚本化、可回归、结果可复现 |
| 图形界面 | `finpulse-gui` | 交互式看 K 线、回放行情、跑研判 |

### 系统要求

| 项目 | 要求 |
|---|---|
| C++ 编译器 | GCC 11+ / Clang 14+ / MSVC 2022（需 C++20） |
| CMake | 3.20+ |
| 构建工具 | Ninja 或 Make |
| Python | 3.10+，**仅标准库** —— 无需 `pip install` 任何东西 |
| Qt | 6.4+（**可选**；找不到会自动跳过 GUI，CLI 不受影响） |
| 操作系统 | Linux / macOS / Windows；Windows 下推荐走 WSL |

---

## 2. 安装与构建

### 2.1 Linux / WSL 构建

```bash
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release
cmake --build build -j"$(nproc)"
```

产物：

```
build/finpulse-cli              命令行
build/src/gui/finpulse-gui      图形界面（未找到 Qt6 时不生成）
build/tests/finpulse-tests      C++ 单元测试
build/finpulse-bench            桥接性能基准（FINPULSE_BUILD_TOOLS=ON 时）
```

### 2.2 构建选项

| 选项 | 默认 | 说明 |
|---|---|---|
| `FINPULSE_BUILD_GUI` | `ON` | 构建 Qt6 界面；找不到 Qt6 自动跳过 |
| `FINPULSE_BUILD_TESTS` | `ON` | 构建 C++ 测试 |
| `FINPULSE_BUILD_TOOLS` | `ON` | 构建 `tools/` 下的探针与基准 |

只要 CLI 的话：

```bash
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release -DFINPULSE_BUILD_GUI=OFF
```

### 2.3 Windows 便捷脚本

仓库带两个 `.cmd`（假定 WSL 里已构建到 `~/finpulse-gui-build`）：

```
scripts/run-cli.cmd         双击运行 CLI 报告（可传 K 线根数：run-cli.cmd 500）
scripts/run-gui.cmd         启动 GUI（需 WSLg 或已配置 X server）
```

`run-gui.cmd` 比一行 `wsl` 长得多，是因为它要挡一个 WSLg 的已知故障（窗口建得出来
但画不出来，表现出来就是"双击了却看不到界面"）：启动前先读 `/mnt/wslg/weston.log`
预检会话，坏了就问一句要不要重开 WSL 再试。细节见 10.1 节 —— **那不是本项目的 bug，
但几乎每次双击都会踩到**。

### 2.4 验证安装

```bash
# C++ 侧：157 个用例
FINPULSE_REQUIRE_ENGINE=1 ./build/tests/finpulse-tests

# Python 侧：547 个用例
python3 python/tests/run_tests.py

# 一键端到端（构建 → 测试 → CLI → 智能体 → CSV → GUI，共 7 步）
bash scripts/smoke_test.sh
QT_PREFIX=/path/to/Qt/6.x/gcc_64 bash scripts/smoke_test.sh   # 一并构建并自检 GUI
```

> `FINPULSE_REQUIRE_ENGINE=1` 表示"如果 Python 引擎不可用就直接判失败"。
> 不设这个变量时，需要引擎的用例会在引擎缺失时**跳过**而不是报红——
> 这是为了让没装 Python 的机器也能跑通纯 C++ 部分。

---

## 3. 第一次运行

### 3.1 命令行最小示例

```bash
./build/finpulse-cli --bars 250 --seed 42
```

输出是一份**固定分節的报告**（人看是报告，脚本看是可 `grep` 的结构）：

```
  FinPulse Terminal   v0.4.0
  C++20 / Qt6 桌面壳  +  嵌入式 Python 分析引擎

[1/6] 启动分析引擎
  ------------------------------------------------------------------------
  解释器:                 python3
  引擎:                   finpulse-engine v0.4.0  (Python 3.12.3)
  冷启动耗时:             1076.9 ms
...
```

### 3.2 加一节智能体研判

```bash
./build/finpulse-cli --agent --bars 300
```

第 `[7/7]` 节会跑一场完整的投委会：**三个分析师独立研判 → 交叉质证一轮 → 主席综合出决议**。

### 3.3 图形界面

```bash
./build/src/gui/finpulse-gui
```

启动后自动做一次分析，主区域显示 K 线，右侧页签可切到「AI 研判」——
那是一个**对话页**：直接提问，回答基于当前荷载的行情；要一份结构化报告就点
「跑投委会研判」。模型、密钥、投委会、Tushare token 都在「设置…」里（见 7.4）。

---

## 4. 命令行参考

### 4.1 参数总表

#### 数据

| 参数 | 默认 | 说明 |
|---|---|---|
| `--source <name>` | `synthetic` | 数据源，可选 `synthetic` / `csv` / `tushare` |
| `--symbol <SYM>` | `SYNTH` | 标的代码（CSV 源用它去找文件名） |
| `--bars <n>` | `250` | 取多少根 K 线（智能体研判建议 ≥ 300） |
| `--csv <path>` | — | 显式指定 CSV 路径（`--source csv` 时用） |
| `--seed <n>` | `42` | 合成数据随机种子，`-1` 表示每次随机 |

#### 分析

| 参数 | 默认 | 说明 |
|---|---|---|
| `--indicator <spec>` | 见下 | 指标规格，可重复多次；**一旦显式给出，默认集被丢弃** |
| `--method <name>` | `ar` | 预测方法，可选 `ar` / `randomwalk` |
| `--horizon <n>` | `5` | 预测步长 |
| `--folds <n>` | `5` | 走步回测折数 |
| `--min-train <n>` | `60` | 回测最小训练窗口 |

默认指标集：`ma:5,20`、`rsi:14`、`macd:12,26,9`、`boll:20,2`。

支持的指标种类：`ma`、`ema`、`rsi`、`macd`、`boll`、`atr`、`stoch`。
规格写法为 `种类:参数1,参数2,...`，例如：

```bash
--indicator ma:5,20,60 --indicator rsi:14 --indicator atr:14
```

#### 智能体研判

| 参数 | 默认 | 说明 |
|---|---|---|
| `--agent` | 关 | 跑一场投委会研判（增加第 `[7/7]` 节） |
| `--role <id>` | — | 只跑这一个角色，**不组会**（隐含 `--agent`） |
| `--panel <id>` | `default_committee` | 指定投委会（隐含 `--agent`） |
| `--rounds <n>` | 用配置 | 覆盖投委会配置里的轮数（隐含 `--agent`） |
| `--provider <name>` | 用配置 | 覆盖 LLM 后端（隐含 `--agent`） |
| `--model <id>` | 用配置 | 覆盖模型名（如 `deepseek-chat`，隐含 `--agent`） |
| `--base-url <url>` | 用配置 | 覆盖 API 端点（自建网关 / 本地推理服务用，隐含 `--agent`） |
| `--api-key <key>` | 用配置 | 运行时密钥，**优先于环境变量**（隐含 `--agent`） |
| `--no-bridge` | 关 | **不起**终端工具桥，用于验证"工具不可用"的降级路径 |
| `--brief` | 关 | 只打印结论与投票表，不打印主席报告正文 |
| `--list` | 关 | 列出可用角色与投委会后退出 |

#### 运行

| 参数 | 默认 | 说明 |
|---|---|---|
| `--python <path>` | 自动探测 | 指定 Python 解释器 |
| `--python-root <dir>` | 自动推导 | 引擎包所在目录 |
| `--replay` | 关 | 回放模式：把行情按时间轴投递到 DataHub |
| `--replay-speed <x>` | `0` | 回放倍速，`0` = 全速 |
| `--bus-demo` | 关 | 展示 DataHub 的订阅与投递统计（与 `--replay` 等价） |
| `--json` | 关 | 结束时额外输出**一行**汇总 JSON |
| `-v, --verbose` | 关 | 打开引擎与总线的调试日志 |
| `-h, --help` | — | 显示内置帮助 |

### 4.2 数据源

#### `synthetic`（默认）

本地合成的几何布朗运动行情，不联网、可复现。同一个 `--seed` 永远产出同一条序列——
这让"同一个 bug 能不能重放"变成确定的事。

```bash
./build/finpulse-cli --source synthetic --symbol SYNTH --bars 500 --seed 42
./build/finpulse-cli --bars 500 --seed -1      # -1 = 每次不同
```

#### `csv`

读本地 CSV 文件。**列名大小写不敏感**，并兼容中英文常见别名：

| 目标字段 | 可用列名 |
|---|---|
| 时间 | `date` / `time` / `timestamp` / `datetime` / `trade_date` / `日期` / `时间` |
| 开盘 | `open` / `o` / `开盘` / `开盘价` |
| 最高 | `high` / `h` / `最高` / `最高价` |
| 最低 | `low` / `l` / `最低` / `最低价` |
| 收盘 | `close` / `c` / `adj_close` / `adj close` / `收盘` |
| 成交量 | `volume` / `vol` / `v` / `成交量` |

时间支持 `YYYY-MM-DD`、`YYYY-MM-DD HH:MM:SS`，以及 `20240315` 这种紧凑格式
（通达信 / 同花顺的导出就是长这样）。

**文件定位顺序**：

1. `--csv` 显式给出的路径；
2. 环境变量 `FINPULSE_DATA_DIR` 下的 `<SYMBOL>.csv`；
3. 项目 `data/` 目录下的 `<SYMBOL>.csv`。

```bash
./build/finpulse-cli --source csv --symbol DEMO-A --bars 500
./build/finpulse-cli --source csv --csv /path/to/600519.csv --symbol 600519
```

#### `tushare`（实时行情，需联网）

调用 [Tushare Pro](https://tushare.pro) 的 `daily` 接口拉取真实 A 股日 K。
**没配 token 也能用**——此时自动回落到本地 CSV 缓存，并在报告里**明确写出**
"本地缓存 —— 不是实时数据"，绝不把缓存冒充实时行情。

```bash
# 实时行情：先配置 token（密钥走环境变量，不进命令行历史）
export TUSHARE_TOKEN=你的token
./build/finpulse-cli --source tushare --symbol 000001.SZ --bars 250

# 演示模式：不配 token，自动回落到 data/ 下的本地 CSV
./build/finpulse-cli --source tushare --symbol DEMO-A --bars 250
```

行为细节：

| 项 | 说明 |
|---|---|
| 标的格式 | Tushare 规范代码，如 `000001.SZ`、`600519.SH` |
| 时间窗口 | 按"交易日历密度 ≈ 1.6 根/自然日"自动推算起始日期，多留 30 天余量 |
| 成交量单位 | 接口返回"手"，引擎统一换算成"股"（×100） |
| 停牌日 | 空成交量记 0，**不丢整根 K 线** |
| 去重 | 按 `trade_date` 保留最后一条，输出严格升序 |
| 缓存回落 | 拉取失败 / 0 行 / 未配 token 时，回落 `<SYMBOL>.csv`（定位规则同 `csv` 源） |

**数据出处（provenance）**：`[2/6] 装载行情` 一节会打印这批数据到底从哪来——

```
数据出处:               [!] 本地缓存 —— 不是实时数据
                          缓存文件: data/DEMO-A.csv（截至 2026-09-30）
补救:                   配置 TUSHARE_TOKEN 环境变量后重跑，即可拉取实时行情
```

看到 `[!] 本地缓存` 就说明这次跑的不是实时数据；`实时接口` 则表示真调了 API，
并附数据截至日期。**回落必须可见**——这是刻意设计，静默回落会让人把缓存当成实时行情。

### 4.3 脚本集成：JSON 汇总与退出码

`--json` 会在报告末尾多打**一行** JSON，便于 `jq` 之类的工具消费：

```json
{
  "symbol": "SYNTH",
  "source": "synthetic",
  "bars": 300,
  "last_close": 146.24,
  "skill": -0.1011,
  "rpc_calls": 11,
  "elapsed_ms": 1178.6,
  "agent_valid": true,
  "agent_direction": "BEARISH",
  "agent_run_id": "fa5375d53289",
  "agent_tool_calls": 6
}
```

| 字段 | 含义 |
|---|---|
| `skill` | 回测技能分 `1 - MSE_模型 / MSE_随机游走`，**为负说明不如随机游走** |
| `rpc_calls` | 本次运行发出的 RPC 请求总数 |
| `agent_valid` | 是否形成了**有效决议**（quorum 够、护栏无错） |
| `agent_direction` | 决议方向；**空字符串表示弃权或无方向**，不是 `NEUTRAL` |
| `agent_tool_calls` | Python **反向回调** C++ 终端工具的次数 |

退出码：

| 码 | 含义 |
|---|---|
| `0` | 成功 |
| `2` | 失败（参数错误，或引擎/研判未能完成） |

---

## 5. 报告逐节解读

> **关于分节编号**：总节数取决于是否开启智能体研判 —— 默认 **6 节**，
> 加 `--agent` 后 **7 节**；编号顺序不变，智能体研判永远是最后一节。
> 下面按**默认报告（6 节）**逐节讲，第 7 节见下一章。

### `[1/6] 启动分析引擎`

握手信息：解释器路径、引擎版本、协议版本、可用数据源、可用预测方法、**冷启动耗时**。

冷启动通常在 1 秒上下（Python 进程 + 插件发现）。后续所有分析都复用这个进程，
不再有启动开销——这也是为什么 `[3/6]` 之后各节耗时都在毫秒级。

### `[2/6] 装载行情`

K 线根数、时间区间、最新一根的 OHLC、成交量、较前收盘、区间涨跌，以及**数据质量**检查
（缺口、异常跳空等）。数据质量有问题时这里会以 `[!]` 起头列出，读后续结论时要带着这个前提看。

### `[3/6] 技术指标（引擎侧计算）`

按你给的 `--indicator` 规格逐个输出。默认四件套：

- `ma:5,20` —— 5 日与 20 日均线；
- `rsi:14` —— 14 日相对强弱；
- `macd:12,26,9` —— 快慢线、信号线、柱；
- `boll:20,2` —— 20 日中轨与 ±2σ 上下轨。

### `[4/6] 风险概览`

| 字段 | 含义 |
|---|---|
| 年化收益 (CAGR) | 按日频复利折算的年化收益率 |
| 年化波动率 | 日收益标准差 × √252 |
| 夏普比率 | 超额收益 / 波动率 |
| 索提诺比率 | 只惩罚下行波动的夏普 |
| 最大回撤 | 峰值到谷底的最大跌幅，附**发生区间**与是否已收复 |
| VaR / CVaR (95%) | 95% 置信下的单日最大损失估计 / 尾部平均损失 |
| 偏度 / 超额峰度 | 分布对称性与尾部厚度 |
| 自相关 lag-1 | 一阶自相关，**日频上通常接近 0**，明显非零才值得注意 |
| 上涨日占比 | 上涨交易日比例 |
| 单日最大涨 / 跌 | 极端单日波动 |

### `[5/6] 预测（含 95% 置信区间）`

- **AR 阶数**由 AIC 自动选阶，随后列出各阶系数 φ 与残差标准差 σ；
- 逐日给出预测值与 95% 上下界。

> **关于区间的诚实说明**：区间按**同方差假设**（σ 恒定）计算。价格水平显著变动时，
> 绝对误差会随水平放大，固定宽度的区间会包不住。这是 AR 的已知局限，不是计算错误——
> 报告里也原样印出来了。

### `[6/6] 滚动回测（walk-forward）`

扩张窗口、无前视地逐折滚动，同时用**随机游走**作对照。这是判断"模型到底有没有用"的地方。

| 字段 | 含义 |
|---|---|
| 模型 / 随机游走 MAE、RMSE | 两个模型的绝对误差 |
| MAPE | 平均绝对百分比误差 |
| **技能分** | `1 - MSE_模型 / MSE_随机游走`；**负值 = 不如随机游走** |
| 方向命中率 | 涨跌方向判断的正确率；随机游走固定记 50% |
| 预测区间覆盖率 | 实际落在 95% 区间内的比例；偏离 95% 说明区间标定有问题 |

> 在日频价格上随机游走极难被打败（Meese–Rogoff, 1983）。所以本项目把它作为**首要对照**，
> 并且技能分为负时如实显示"**[!!] 不如随机游走**"，不做美化。
> 随机游走的方向命中率固定记 50%（它不携带方向信息，记 0% 会人为抬高技能分）。

末尾的逐折表可以定位"是哪一折崩了"。

### `[7/7]` 智能体研判（加 `--agent` 后的最后一节）

见下一章。

---

## 6. 智能体研判详解

### 6.1 先看有哪些角色和投委会

```bash
./build/finpulse-cli --agent --list
```

内置 4 个角色 + 1 个投委会：

| 角色 id | 名称 | 职责 |
|---|---|---|
| `technical_analyst` | 技术面分析师 | 只看价格与指标形态，判断趋势状态与关键位，不预测价位、不掺杂基本面 |
| `flow_analyst` | 量价分析师 | 从成交量与波动聚集特征判断参与度与拥挤程度，提供与技术面互补的视角 |
| `risk_officer` | 风险官 | 只关心亏损侧。量化下行风险、回撤深度与尾部损失，并质疑其它角色的乐观结论 |
| `committee_chair` | 投委会主席 | 综合各角色意见形成决议、**正面回答值不值得投**，并**强制记录分歧** |

投委会 `default_committee`（标准投委会）：3 位委员，`quorum = 2`，`rounds = 2`。

### 6.2 一场研判是怎么跑完的

```
独立研判（三位委员并行、互不可见）
        ↓
交叉质证（第 2 轮，委员能看到同伴的结论并改口）
        ↓
主席综合（形成决议 + 投入判断 + 记录分歧 + 定置信度）
```

过程中引擎会把进度**事件**推给 C++ 壳，命令行实时打印：

```
── 进度事件流（Python 边跑边推，经总线转发）──
· 9ms     agent.run.start      开始运行（debate）
· 9ms     agent.debate.start   标准投委会 开会（quorum=2，轮数 2）
· 9ms     agent.role.start     技术面分析师 开始研判
· 33ms    agent.round.done     第 1 轮结束（无人改口）
· 56ms    agent.round.done     第 2 轮结束（无人改口）
· 56ms    agent.chair.start    主席开始综合各位委员的结论
· 57ms    agent.debate.done    会议结束：方向 BEARISH
```

事件名带 `agent.` 前缀是**传输层约定**（用来和未来的其它事件域区分），
文案由前端渲染——GUI 需要的是结构化数据（画轮次表、算耗时），不是中文句子。

### 6.3 投票表怎么读

```
── 第 1 轮 ──
委员                       方向     置信度   权重
技术面分析师            BEARISH       HIGH    1.0
量价分析师              NEUTRAL       HIGH    1.0
风险官                     弃权         —    1.0
```

两条关键规则：

**① 弃权 ≠ NEUTRAL。** 风险官按职责不产出方向标签，它显示的是**弃权**。
弃权既不计入看多也不计入看空，**从计票分母里剔除**。把它记成"看中性"是错的——
那会让"支持面不足半数"的结论读不出来。

**② `weight` 参与计票。** 权重写在投委会配置里，会真正影响结果。
默认三位委员都是 `1.0`；想体现话语权差异（例如给量价分析师 `0.5`），改配置即可。

### 6.4 主席报告的结构

| 段落 | 内容 |
|---|---|
| **决议** | 方向 + 时间视野，并说明有几个分析师给了方向、几个没给、支持面是否过半 |
| **投入判断** | 正面回答「这次值不值得投」：`值得投入` / `不值得投入` / `证据不足，暂不构成投入理由`，再列方向、证据强度、分歧代价三条依据，最后写"什么条件下这个判断会被推翻" |
| **采纳的意见** | 与决议方向一致、且判据可复核的结论 |
| **否决的意见** | 方向不一致的结论。**明确写出"否决只表示方向不一致，不表示该结论错误"**，未表态者的风险项也原样保留 |
| **分歧记录** | 保留实质分歧，**不做平滑处理** |
| **置信度** | `HIGH` / `MEDIUM` / `LOW` + 理由 |

**置信度降档规则**：主席取"支持该方向的分析师中自报置信度的最高值"，
但如果支持面**不足全体半数**，就在这个最高值上**下调一档**。
所以你会看到"技术面给了 HIGH，但决议置信度是 MEDIUM"——因为支持者只有 1/3。

#### 投入判断：把"所以呢"写出来

前五段回答了「这是什么行情」「证据有多强」「分歧在哪」，但读完仍然不知道
**所以呢**。投入判断这一段就是那个"所以呢"，也是整份报告里唯一可以被直接
拿去决策的一句话。

三档的判据是**可复核的**，不是让模型自己把握：

| 档位 | 触发条件 |
|---|---|
| 不值得投入 | ① 多空票数打平、② 全体结论都是 NEUTRAL、③ 支持者不足全体半数、④ 支持方置信度上限只有 `LOW` —— 任一成立 |
| 值得投入 | 以上四条都不成立：方向明确、支持面过半、置信度上限 ≥ `MEDIUM` |
| 证据不足 | 压根没有下级结论可综合 |

> **「值得投入」不是「建议买入」。** 它说的是"本次证据足以支撑一次
> **与决议方向一致**的投入决策"——决议为 `BEARISH` 时，它支持的是看空方向的
> 决策。所以这一段的第一句必须把方向写出来，方向永远以「决议」段为准。
> 仓位数量与具体价位仍然不给：那需要资金约束与风险预算，本系统没有这两样输入。
> 见 `guardrails.ForbiddenPhrases`。

> **为什么档位写死在配置里？** 留给模型自由发挥，它就会写成「建议关注」
> 「值得留意」这类没有操作含义的词——那正是这一段要消灭的东西。
> 三个档位的措辞写在 `committee_chair.json` 的 instructions 里，规则后端
> （`rule_based._fill_investment`）用的是同一套词。

### 6.5 反向工具通道：Python 回调 C++

研判过程中，Python 侧的委员可以调用**声明过的终端工具**，这些工具的实现全在 C++ 进程里：

| 工具 | 返回 |
|---|---|
| `terminal.live_quote` | 终端此刻正在接收的最新一笔行情（实时快照） |
| `terminal.bus_stats` | DataHub 的订阅数、投递数、丢包统计 |
| `terminal.data_quality` | 终端侧的数据质量判定 |
| `terminal.subscriptions` | 当前有哪些订阅在消费行情 |

> **名字里的点号只在配置里存在。** 发给模型时它会被换成下划线
> （`terminal.live_quote` → `terminal_live_quote`）——function-calling 协议
> 只允许 `[A-Za-z0-9_-]`，DeepSeek 之类的服务商会对不合规的名字
> **直接拒掉整次请求**。翻译发生在 HTTP 请求那一层，配置、轨迹、报告里
> 看到的仍是带点号的原名。

机制：C++ 起一个**只绑回环地址**的 HTTP 服务端（`ToolBridge`），
签发一次性的作用域令牌给 Python，Python 用 `urllib` 回调 `POST /tool`。

安全边界（都写在代码注释里）：

- **只监听 `127.0.0.1`/`::1`**，不绑 `0.0.0.0`；
- **校验 Host 头**，防 DNS rebinding；
- **令牌常量时间比较**，防止计时侧信道；
- 请求头 / 请求体**双重限额**（16 KB / 4 MB）。

报告末尾会给出实测数字：

```
── 反向工具通道（Python 回调 C++ 终端）──
工具调用:               6 次
收到 HTTP 请求:         8 次
拒绝 令牌/主机/格式:    0 / 0 / 0
未知工具:               0 次
```

> **注意**：工具桥提供的是**实时快照**，与手上的历史 K 线是两回事。
> 若两者价格明显不一致，说明回放进度与序列末端不同步——角色的提示词里明确要求
> **指出这一点，而不是二选一**。

### 6.6 降级路径

```bash
./build/finpulse-cli --agent --no-bridge --brief
```

不起工具桥时，委员们会**如实报告"终端工具不可用"**，研判照常完成——
而不是编造数据。这是刻意设计的降级路径，也建议定期验证：

```
工具调用:               0 次
  调用为 0：本轮委员可能都没声明终端工具（不是故障）
```

### 6.7 单独跑一个角色 / 换后端 / 改轮数

```bash
# 只跑风险官，不看报告正文
./build/finpulse-cli --agent --role risk_officer --bars 300 --brief

# 换投委会、改轮数
./build/finpulse-cli --agent --panel default_committee --rounds 3

# 换 LLM 后端（默认是内置的规则后端 rule_based）
./build/finpulse-cli --agent --provider deepseek
```

### 6.8 显式接入大模型：provider / model / base-url / api-key

默认跑在内置规则后端上（不联网、不做推理）。想真调用大模型，除了改配置（见 8.3），
还可以**运行时临时覆盖**——四个参数打包生效，优先级高于配置与环境变量：

```bash
./build/finpulse-cli --agent --provider deepseek \
    --model deepseek-chat --api-key <你的密钥>

# 自建网关 / 本地推理服务（如 Ollama、vLLM）：
./build/finpulse-cli --agent --provider openai_compat \
    --base-url http://127.0.0.1:11434/v1 --model llama3
```

**怎么确认真的连上了？** 报告末尾有一段「推理后端」，列出每个角色**实际**用的模型：

```
── 推理后端（每个角色实际用的模型）──
  rule_based ×4    [未连接大模型，跑在内置规则后端上]
  外部模型: [--] 全部跑在内置规则后端上 —— 未连接任何大模型
    1) 命令行: --provider deepseek --api-key <你的密钥> --model deepseek-chat
    2) 配置:   改 python/finpulse_engine/agent/configs/<角色>.json 的 config.model
    3) 环境变量: 导出 DEEPSEEK_API_KEY / OPENAI_API_KEY / MOONSHOT_API_KEY 等
  降级: 缺少 API 密钥（DEEPSEEK_API_KEY 未配置）
```

连上之后，这一段会变成后端分布（如 `deepseek ×4`）+ **token 用量**
（提示 → 补全），降级列表消失。**降级永远显式**：缺密钥、provider 拼错、
`openai_compat` 缺 `base_url`，都会退回规则后端并写明原因——绝不静默假装在用 LLM。

也可以在跑之前先查状态（不花一次研判）：

```bash
python3 tools/probe.py rpc agent.llm.status
```

返回每个角色的 `configured_provider`（配置写的）/ `effective_provider`（实际生效）/
`degraded` / `reason`，以及全部内置后端清单与接入方式。**密钥只进内存**：
不回显、不落盘，状态接口只返回 `has_api_key` 布尔值。

---

## 7. 图形界面

### 7.1 布局

```
┌──────────────────────────────────────────────────────────────────────────┐
│ 数据源[▾] 标的[▾] 根数[spin] 预测[▾] 步长[spin] 折数[spin] ☑布林带        │
│ 回放速度[▾] [重新分析] [回放行情] [停止回放]                              │  工具栏
├──────────────────────────────────┬───────────────────────────────────────┤
│                                  │  行情 │ 风险 │ 回测 │ AI 研判 │ 日志   │
│         K 线图（自绘）            │                                       │
│  单点进入拖动，再点一下退出        │      页签内容区                       │
│  悬停显示十字线，状态栏出价格      │                                       │
├──────────────────────────────────┴───────────────────────────────────────┤
│  状态栏：进度 / 共 N 根 / 总线已投递 M 条 [拖动模式提示]                   │
└──────────────────────────────────────────────────────────────────────────┘
```

### 7.2 工具栏

**参数控件即改即生效**：数据源、标的、根数、预测方法、步长、回测折数任何一项变化后，
250ms 防抖内自动重跑——**不需要**再点「重新分析」。参数没变（比如切走又切回来）
不会触发空跑；上一次还在跑时来了新参数，跑完会自动补跑最新的一份，不丢弃。

| 控件 | 作用 |
|---|---|
| **数据源** | 下拉项**取自引擎**（`synthetic` / `csv` / `tushare`），不是界面写死 |
| **标的 / 根数** | 标的可手输（回车生效）；根数用步进器 |
| **预测 / 步长 / 回测折数** | 预测方法（AR / 随机游走）、预测步长、走步回测折数 |
| **布林带** | 开关 K 线图上的布林带叠加（勾选即时生效，不触发重跑） |
| **回放速度** | 倍速下拉（含全速；改速度即时生效，不触发重跑） |
| **重新分析** | 手动强制重跑（一般不需要，参数变了会自动跑） |
| **回放行情 / 停止回放** | 把历史 K 线按时间轴重放成实时行情 / 中断 |

**为什么要回放**：DataHub 是实时投递语义，直接灌一整段历史是一次性"倾泻"，
测不出订阅者的节流、丢包、脏读行为。回放把时间轴摊开，才是真实场景。

**K 线图的拖动是"模式切换"而不是"按住拖"**：

- **单击一下图表** → 进入拖动模式（光标变抓手，状态栏提示"再点一下图表退出"），
  之后移动鼠标即平移 K 线，不需要按住任何键；
- **再单击一下** → 退出拖动模式，回到悬停十字线联动。

为什么不用"按住左键拖"：按住拖与悬停十字线共用同一套鼠标事件，
二者必然互相打架；切换成持久模式后，"看十字线"和"拖动"两种意图都不用
中途松手，也避免了"鼠标滑出图表后拖动状态丢失"这类不一致。

回放期间图表切到流式模式（逐根生长），结束后自动贴回分析视图。

### 7.3 右侧页签

| 页签 | 内容 |
|---|---|
| **行情** | 逐根 K 线的表格（`QuoteTableModel`） |
| **风险** | 与 CLI 第 `[4/6]` 节相同的统计量 |
| **回测** | 与 CLI 第 `[6/6]` 节相同的回测结果与逐折表 |
| **日志** | 桥接层与引擎的运行日志 |
| **AI 研判** | 对话式问答 + 投委会研判（见 7.4） |

### 7.4 「AI 研判」页（对话式）

这一页把两种**形态不同**的东西放在一起，但走的是两条独立的链路：

| | 对话 | 投委会研判 |
|---|---|---|
| 入口 | 输入框回车 / 「发送」 | 「跑投委会研判」按钮或 `/debate` |
| RPC | `agent.chat` | `agent.debate`（选了单角色则是 `agent.run`） |
| 形态 | 自由问答，没有契约 | 有契约：段落齐、方向从指定段抽取、护栏检查 |
| 速度 | 一次模型调用 | 多角色 × 多轮 + 主席综合 |

**布局**：顶部三个按钮（`跑投委会研判` / `设置…` / `清空对话`），
中间是对话记录区（气泡按时间累积），底部是输入框 + 「发送」。

**命令**（以 `/` 开头，避免把"帮我看看投委会怎么想"这种正常提问误判成要跑辩论）：

| 命令 | 作用 |
|---|---|
| `/debate` | 跑一场投委会研判 |
| `/clear` | 清空记录区与多轮历史 |
| `/help` | 列出这一页能做什么、不做什么 |

**回答基于真实数字**。每次提问都会把当前荷载的行情写进系统提示词：
标的、数据源、根数、数据截止、最新收盘、区间涨跌、年化波动率、最大回撤、
**数据出处**。提示词同时硬性要求"只使用上面给出的数字，没有的就说没有"——
所以"它现在贵不贵"这种问题会基于终端的真实数据回答，而不是模型的记忆。
多轮历史（最近 24 条）随每次提问一起带上，被截断时会明确标注。

**未接大模型时它直说**。对话不会降级到规则后端硬凑一段模板——那比承认
没连上糟糕得多。此时返回一条红色气泡，写明原因和两种接法。

**「设置…」对话框**把全部参数收进一个独立界面，按三组分开：

| 分组 | 字段 |
|---|---|
| 推理后端 | provider（清单取自引擎）/ 模型 / 端点 / **API 密钥** / 温度 / 最大输出 |
| 投委会 | 投委会 / 角色（选了单个角色就**不组会**，只有这一个角色发言）/ 轮数 |
| 数据源 | Tushare token |

关于密钥的两条规则：

- **默认不落盘**。密钥框是密码框、永不回显；只有主动勾选「记住到启动配置档」
  才会以明文写进 `~/.finpulse/profile.json`（权限 0600，不进版本控制）。
  勾了是知情选择，没勾就一个字都不写。
- **「可用」只反映环境变量**。后端下拉项后面标注的（环境可用 / 环境未配：…）
  说的是进程环境变量有没有配；在这个界面填了密钥就以这里的为准。

研判结果按等宽气泡追加进同一条记录：决议与置信度 → **议题**（本次运行参数）
→ 运行告警 → **推理后端**一行（每个角色实际用的模型分布 + token 用量）
→ 降级原因 → 逐轮投票表 → 最终立场 → 主席报告 → 反向工具通道统计。
研判**不清空**记录——你刚问过的问题和它的回答常常正是为什么要跑这次研判的原因。

设计上的四点：

- **惰性启动**：`ToolBridge` 与 `AgentService` 只在你第一次用到这一页时才起
  （没人用就不占端口）；
- **共用一把 RPC 通道**：分析、研判、对话、批量拉取都走同一个引擎进程，
  **不能同时跑**。界面会统一禁用按钮并显示"分析中…"/"研判中…"/"等待模型回答…"；
- **参数只有一个来源**：研判与对话都读「设置」对话框保存的那份参数，
  不各读各的控件——两处各读各的迟早会不一致；
- **实际后端必须可见**：界面显示的"配置写的后端"和报告里"实际生效的后端"是
  两回事——缺密钥会降级到规则后端，不摆出来用户永远以为自己在用 LLM。

### 7.5 无头截图（CI / 文档用）

```bash
QT_QPA_PLATFORM=offscreen ./build/src/gui/finpulse-gui \
    --screenshot /tmp/shot.png --debate 6000
```

| 参数 | 说明 |
|---|---|
| `--screenshot <路径>` | 渲染一帧存成 PNG 后自动退出 |
| `--replay <毫秒>` | 截图前先点一次「回放行情」，等指定毫秒再截 |
| `--debate <毫秒>` | 截图前先切到「AI 研判」页并点一次研判，等指定毫秒再截 |
| `--ask <文本>` | 截图前先在「AI 研判」页把这段话打进输入框并点「发送」 |
| `--ask-wait <毫秒>` | `--ask` 之后等多久再截（默认 3000） |

> 这些钩子都是**真的去找控件并触发**（点按钮 / 往输入框打字），走的是和用户
> 操作完全相同的代码路径。测试专用后门测不出真问题，所以不设后门。
> `--ask` 尤其重要：单测只能证明"回复的 HTML 字符串拼对了"，证明不了
> "屏幕上真的出现了气泡"—— 这两者的差距要用一张截图来填。

---

## 8. 配置

角色与投委会全部是**声明式 JSON**，改配置不用重新编译，也不用改一行代码。

```
python/finpulse_engine/agent/configs/
├── technical_analyst.json      角色
├── flow_analyst.json
├── risk_officer.json
├── committee_chair.json
└── panels/
    └── default_committee.json  投委会
```

### 8.1 角色配置字段

```jsonc
{
  "id": "technical_analyst",
  "name": "技术面分析师",
  "description": "只看价格与指标形态……",
  "category": "technical",
  "version": "1.0.0",
  "capabilities": ["trend_state", "momentum", "level_identification"],
  "config": {
    "model": {
      "provider": "rule_based",   // 后端：rule_based / openai / deepseek / ...
      "temperature": 0.2,
      "max_tokens": 3000          // 单轮输出的硬上限，见 8.4
    },
    "instructions": "……完整提示词……",
    "tools": ["indicators", "stats", "terminal.live_quote"],
    "output_schema": "market_view",
    "output_sections": ["趋势状态", "关键位", "动量", "终端证据", "置信度"],
    "direction_sections": ["趋势状态"],   // 只从这一段抽方向
    "memory": false,
    "reasoning": true,
    "max_tool_calls": 6
  }
}
```

两个字段值得单独说明：

**`output_sections`** —— 输出契约。角色的回答必须逐段命中这些段名，
护栏会检查；缺段会被记录下来。这也是 `--brief` 之外你能一眼看出"角色有没有按规矩答"的地方。

**`direction_sections`** —— **方向抽取的范围**。只在这一段里找 `BULLISH` / `BEARISH` / `NEUTRAL`。

> 这一条是踩过坑加的：风险官的提示词要求它写「反对意见」段，而它**引述别人的观点**时
> 会写出 `BULLISH` 一词。如果全篇扫关键词，就会把"引述别人的看多"当成"你自己的看多"。
> 所以方向**只在声明的段落里、按声明顺序**抽取。

改配置后可以用 `agent.reload`（或重启 CLI/GUI）让引擎重新加载。

### 8.2 投委会配置

```jsonc
{
  "id": "default_committee",
  "name": "标准投委会",
  "chair": "committee_chair",
  "members": [
    { "role": "technical_analyst", "weight": 1.0, "cross_examine": true },
    { "role": "flow_analyst",      "weight": 1.0, "cross_examine": true },
    { "role": "risk_officer",      "weight": 1.0, "cross_examine": true }
  ],
  "quorum": 2,
  "rounds": 2
}
```

| 字段 | 说明 |
|---|---|
| `chair` | 主席角色 id |
| `members[].weight` | **方向票的权重**，真正参与计票 |
| `members[].cross_examine` | 是否参加第二轮交叉质证 |
| `quorum` | **有效委员**数量下限；不够则形不成有效决议 |
| `rounds` | 轮数；`2` 表示"独立研判 + 交叉质证各一轮" |

> `quorum` 建在**有效委员**上，而不是"人数对得上"上。
> 一个委员算有效需要同时满足：调用成功、真的拿到了工具结果、护栏没有报错。
> 只数人头会让"三个委员里有俩其实啥也没查"这种情况看起来像一场合格的会议。

### 8.3 切换 LLM 后端

默认后端是 `rule_based`：**不联网、不调用任何模型**，用终端真实数据把每个段落填充成
合乎格式的回答。它保证了整条链路可离线复现、可回归测试。

要接真实模型，二选一：

**方式一：改角色配置**

```jsonc
"model": {
  "provider": "deepseek",
  "model_id": "deepseek-chat",
  "base_url": "",            // 留空用官方地址
  "temperature": 0.2
}
```

**方式二：命令行临时覆盖（本次运行生效，不改配置）**

```bash
export DEEPSEEK_API_KEY=sk-xxxx
./build/finpulse-cli --agent --provider deepseek --model deepseek-chat
```

**方式三：GUI 设置界面（推荐）**

「AI 研判」页 →「设置…」→ 推理后端：选 provider、填模型名与 API 密钥。
密钥默认只在内存里，勾选「记住到启动配置档」才会写进 `~/.finpulse/profile.json`。
全部留空 = 照角色配置来。详见 7.4 节。

内置后端：

| `provider` | 说明 | 密钥环境变量 |
|---|---|---|
| `rule_based` | 内置规则后端（默认，离线） | — |
| `openai` | OpenAI 官方 `/chat/completions` | `OPENAI_API_KEY` |
| `deepseek` | DeepSeek 开放平台 | `DEEPSEEK_API_KEY` |
| `moonshot`（别名 `kimi`） | 月之暗面 Kimi | `MOONSHOT_API_KEY` |
| `dashscope`（别名 `qwen` / `aliyun`） | 阿里云百炼（OpenAI 兼容） | `DASHSCOPE_API_KEY` |
| `openrouter` | OpenRouter 聚合网关 | `OPENROUTER_API_KEY` |
| `groq` | Groq 推理服务 | `GROQ_API_KEY` |
| `ollama` | 本地 Ollama（默认 `127.0.0.1:11434`） | 不需要 |
| `openai_compat` | 任意 OpenAI 兼容端点，**必须给 `base_url`** | `FINPULSE_LLM_API_KEY` |

**降级是显式的**。以下情况会自动退回 `rule_based`，并**在报告里写明原因**：

- provider 名拼错（不认识的名字不会静默接受）；
- 需要密钥但环境变量为空（运行时 `--api-key` / GUI 密钥框可以补上）；
- `openai_compat` 没给 `base_url`。

> 这一条是刻意的：静默退回规则后端会让人一直以为自己在用 LLM。
> 降级原因会原样出现在角色的执行轨迹里，CLI 报告末尾与 GUI 结果区的
> 「推理后端」一段会汇总每个角色实际用的模型。

### 8.4 输出预算：轮数、截断、以及"沿用上一轮"

接上真实 LLM 之后，最贵的失败不是"报错"，而是**一句话没写完**。三件事
互相咬合，调其中任何一个之前先看完这一段。

**① 轮数预算跟着工具预算走。** 一个角色最多"问后端"几轮 = `max_tool_calls + 2`
（上限 10）。不是常量 —— 真实 LLM 常常**一轮只调一个工具**，写死轮数会让它
在取完数之前就把轮数花光，报告根本没机会写。工具预算用完之后，引擎会
**撤下工具目录**并明确告诉它"现在写报告"，而不是继续递工具让它继续要。

**② 每段 200 字，写进提示词。** 运行时会把 `段落数 × 200 字` 的输出预算
写进任务提示词。同时，**交叉质证**注入的对等结论被截到 900 字（主席那份
仍拿全文 1600 字）：委员要做的是"回应结论"，给它全文只会换来一段同样长的
回话 —— 那正是输出撑爆 `max_tokens` 的由来。

**③ `max_tokens` 是硬上限，撞上就截断。** 截断**不等于**报告不可用：
- 五个段落都写全了、只是末句被切掉 → 记一条 `output_truncated` **警告**，
  报告照常计票；
- 真的缺段 → 由 `section_completeness` 判 **error**，那份报告不进计票。

> 这两件事分开判，是踩过坑才改的。上一版把截断直接写进角色的 `error`，
> 于是一份"写满了五段、只在末尾被切掉"的报告被当成"未产出结论"丢掉，
> 三个委员同时出局、整场投委会作废 —— 而它离成功只差几十个 token。

**④ 某一轮没跑出来，不会追溯地否定上一轮。** 委员在当前轮没产出**可用**
结论时，如果它上一轮的结论仍然成立，就沿用上一轮，并在报告顶部的
「运行告警」里写明谁沿用了、为什么：

```
运行告警  [warning] 第 2 轮以下委员未产出可用结论，已沿用其第 1 轮立场：
          flow_analyst（输出被 max_tokens 截断，段落没写完，
          缺：「波动率状态、背离、终端证据、置信度」）；…
```

沿用**必须可见**：不然一份"第二轮没跑出来、拿第一轮顶上"的会议记录，
读起来就像三位委员真的质证过了。另外，上一轮全员没产出结论时，第二轮
会被整个跳过 —— 没有别人的结论可看，重跑一遍问的是同一个问题。

> **报告头部两块牌子别混**：「议题」（见 8.6）说的是这场会在**议什么**，
> 「运行告警」说的是过程中**出了什么问题**（配置体检、quorum 不足、沿用
> 上一轮、各角色护栏）。此前两者共用「会议问题」一个标签，而中文里那个词
> 同时能读成"会议要议的问题" —— 一次真跑之后，用户问的正是"这里是不是
> 该显示议题"。名字含糊的代价是实打实的。

### 8.5 数字溯源：池子按"模型读过什么"建

`number_provenance` 是这套系统里最有价值的一条护栏：报告里的每个数字，
都要能在**模型读过的东西**里找到出处。它拦的不是格式错误，而是"编一个
看起来合理的收益率"—— 这种失败从文本上完全看不出来。

关键在**池子怎么划**。池子 = 该角色自己的工具输出 **∪ 它那一轮收到的
任务提示词原文**（运行参数 + 其它角色的结论）。

> 这一条也是踩过坑才改的：主席的配置里只声明了 `stats` 一个工具，可它的
> 提示词写着"你手上只有各人的结论与他们的工具输出"，于是真实模型引用技术面
> 的均线、量价的量比 —— **完全正确**，却被判「26 个数字无法溯源」。
> 池子按"它调了哪些工具"建，就会把一整份正确的主席报告判成可疑。
> 一条每次都误报的告警，等于把这条护栏废掉。

容差与放行分三层，都是"模型合法地这么写"驱动的：

| 情况 | 例子 | 判罚 |
|---|---|---|
| 按精度吻合 | 工具 `143.5725`，报告写 `143.57` | 放行 |
| **负值的幅度写法** | 工具 `-24.8469`，报告写 `24.85%` | 放行 |
| 比率的两种写法 | 工具 `0.2246`，报告写 `22.46%`（仅带 `%` 的字面量享受 ÷100） | 放行 |
| 由池中两数相除算出的比值 | `4319967 / 3805282 = 1.135`；商限定在 0.001~100 | 放行 |
| 其余 | 池子与提示词里都没有 | **warning**，并在消息里列出具体是哪个数 |

> **只认除法，不认加减乘。** 池子里任意两数相乘或相加会铺开极大一片数值
> 区间，随便编一个价位都能撞上其中一个 —— 那样这条护栏就等于删掉了。
> 商限定在 0.001~100 则挡掉"成交量 ÷ 价格 ≈ 3194"这类量纲不同的除法，
> 那种商不是分析里会引用的数。

**负值这一行是第 4 次真跑补上的，同一处其实有两类误报：**

1. **非 ASCII 负号。** 模型在中文语境里经常吐 U+2212（数学减号）而不是
   ASCII 的 `-`，而抽取数字的正则 `[-+]?` 只认 ASCII —— 负号在成为 token
   之前就被丢掉了，`−14.2606` 变成正数 `14.2606` 去和池子里的 `-14.2606`
   比，必然对不上。修法是在抽取入口把 `U+2212 / U+FF0D / U+2013 / U+2014`
   一律归一成 `-`。
2. **幅度 vs 符号。** 工具返回 `max_drawdown_pct = -24.8469`，报告写
   「最大回撤 24.85%」—— 回撤是负的、幅度是正的，人话里说的就是后者。
   要求符号一致会把这类**正确**引用整批判成编造。所以判据改成：
   **模型没写符号就按幅度比，显式写了符号就按符号比** —— 后者是模型的
   断言，翻了号（工具给 `+14.2606`、报告写 `-14.2606`）仍然要报。

> 两条都配了"退回旧行为就会红"的回归用例。注意第一条的用例**特意分成两条**：
> "接受 U+2212"那条同时被幅度匹配兜住，拿它验证归一化是在**空转** ——
> 真正能咬住归一化的是"U+2212 必须被当成符号断言"那条（池子里放正数，
> 报告写负号，必须报）。

报告里看到这条告警时，消息会直接给出数字（"2 个数字无法溯源：1.86%、3194.70"），
逐个核对即可。`tools/mock_llm.py` 支持手动复现这条路：
`FINPULSE_MOCK_CITE=params` 会让假主席去引用运行参数。

### 8.6 议题：报告头部要说清"这场会在议什么"

报告头部的「议题」一行由引擎的 `describe_intent()` 生成，内容就是**本次运行
参数**：

```
议题:  标的 600519.SH；样本 260 根 K 线，截至 2026-09-30；
       预测方法 ar、步长 5、回测 5 折、最小训练 60
```

**为什么要有这一行。** 此前头部只有「决议 / 置信度 / 告警」，读者看得到结论、
看不到结论是在什么口径下得出的 —— 而同一组均线，套在 260 根日线上和 60 根
30 分钟线上是两个结论。少了口径，一份**正确**的报告也没法被复核。

**为什么是运行参数而不是一句自然语言问题。** 投委会没有"用户提问"这个输入，
每个角色的职责范围写在各自的 `instructions` 里；真正把这场会与其他场次区分
开来的，就是标的 + 样本区间 + 预测设置。与其另造一句人工摘要，不如把**已经
喂给模型的那份参数**原样摆出来。

> 措辞只写一份：`describe_intent()` 同时供任务提示词和报告头部使用。
> 两处各写一遍，迟早会漂移成两件事 —— 而"模型被告知的"和"用户以为它被问的"
> 不一致，是这套系统里最难查的一类 bug。

单角色路径（`--agent-role`）走的是 `agent.run`，返回的是一个 `RoleRun`、没有
`DebateResult` 可挂，议题由门面层（`api.AgentService.run_role`）补上，并同样
在报告头部显示。

---

## 9. 环境变量

| 变量 | 作用 | 默认 |
|---|---|---|
| `FINPULSE_PYTHON_ROOT` | 引擎包所在目录。把二进制拷到别处去跑时用它指路 | 构建时注入的源码树路径 |
| `FINPULSE_DATA_DIR` | CSV 数据源的查找目录 | 项目 `data/` |
| `FINPULSE_LOG` | 引擎侧日志级别（`DEBUG` / `INFO` / `WARNING` / `ERROR`） | `warn` |
| `FINPULSE_REQUIRE_ENGINE` | 跑测试时设 `1`，表示"引擎不可用就直接判失败" | 未设（不可用时跳过） |
| `TUSHARE_TOKEN` | Tushare Pro 的 API token；不配则 `tushare` 源回落本地 CSV 缓存 | — |
| `<各后端>_API_KEY` | 见上一节的密钥表 | — |

> 日志走 **stderr**，永远不写 stdout——stdout 是协议通道，混进去会破坏帧边界。

---

## 10. 故障排查

| 症状 | 可能原因 | 处理 |
|---|---|---|
| `无法启动引擎` / 握手失败 | Python 找不到，或 `python_root` 不对 | `--python <解释器>`、`--python-root <目录>`，或设 `FINPULSE_PYTHON_ROOT` |
| `bars 只有 N 根，智能体研判至少需要 60 根` | 行情太短 | 加大 `--bars`；研判建议 ≥ 300 |
| 未知角色 / 未知投委会 | id 拼错 | `--agent --list` 看真实可用的 id（报错信息里也会列出） |
| 报告里出现"已降级到规则后端" | provider 名错 / 缺密钥 / 缺 `base_url` | 按提示补环境变量，或用 `--api-key` / GUI 密钥框运行时补 |
| 报告末尾显示"未连接大模型" | 默认就是规则后端，或密钥没被读到 | 看「推理后端」段的接入提示；先 `probe.py agent.llm.status` 查每个角色的实际生效后端 |
| 报错里出现 `Invalid 'tools[N].function.name'` | 工具名含协议不允许的字符（点号等） | 已在发送前翻译成下划线形态；若**仍然**出现，说明有人往注册表里加了**撞名或超长**的工具名 —— 跑 `test_agent.TestToolWireNames` 一行就能定位 |
| `有效委员 0 位，未达 quorum` | 委员集体没产出**可用**结论 | 看「运行告警」里每位的具体原因（截断 / 缺段 / 没有工具数据）。若是 `max_tokens 截断`，调大该角色的 `max_tokens`，或收紧 8.4 节的输出预算 |
| `运行告警` 出现 `[warning] … 已沿用其第 1 轮立场` | 该委员本轮没跑出可用结论，编排层沿用了它上一轮的立场 | **不是故障**，决议仍然有效。要根治就按括号里写的原因处理（截断 → 调 `max_tokens`） |
| 报告头部没有「议题」这一行 | 引擎没把本次运行参数带回来（旧版引擎，或结果解析漏了字段） | 议题由引擎的 `describe_intent` 生成、随结果回传；查 `agent.debate` 返回值里有没有 `intent` 键 |
| `[warning] N 个数字无法溯源：xxx` | 报告里有数字既不在该角色的工具输出里，也不在它收到的提示词里 | 先看消息里列出的具体数字：若是**比值**（量比 / 占比 / 倍数）说明落在 8.5 节的放行范围外；若是某个负值的**幅度**写法、或原文用了 U+2212 这类非 ASCII 负号，见 8.5 节 —— 那两类已放行，仍报出来就是真编造，该重跑 |
| 某角色报"达到最大轮数仍未产出结论" | 模型反复调工具不收敛（例如一直点一个不存在的工具名） | 看轨迹里的 `tool_calls` 序列；把该角色的 `max_tool_calls` 调小，或修提示词 |
| `--source tushare` 报告显示"本地缓存" | 没配 `TUSHARE_TOKEN`，或拉取失败 / 0 行回落 | 配好 token 重跑；回落是显式的，报告里会写原因 |
| 工具调用为 0 次 | 委员没声明终端工具，或用了 `--no-bridge` | 不一定是故障；先看角色配置的 `tools` |
| 技能分为负 | 模型没打过随机游走 | **不是 bug**。日频预测里这是常态 |
| 预测区间覆盖率远低于 95% | σ 恒定假设在价格大幅变动时不成立 | AR 的已知局限，见 `[5/6]` 节的说明 |
| GUI 起不来 | 没装 Qt6，或没有显示环境 | CLI 不受影响；WSL 需 WSLg 或 X server |
| **双击 `run-gui.cmd` 后窗口是空白的 / 看不到界面** | WSLg 掉进了 **copy mode**（已知故障），窗口建得出来但内容画不出来 | 见 10.1 节。`run-gui.cmd` 已内置检测与重开 WSL |
| 图表一直跟着鼠标平移，十字线不动了 | 处于拖动模式（之前单击过图表） | **再单击一下图表**即可退出拖动模式，状态栏也有提示 |
| 事件流只显示事件名、没有说明文字 | 事件名与文案表不匹配 | 运行测试 `finpulse-tests`；有专门的元性质用例守着"每种事件都必须有文案" |
| 图表 K 线与实时行情价格对不上 | 回放进度与序列末端不同步 | 角色提示词要求指出这一点；检查回放速度 |

日志不够用时加 `-v`：

```bash
./build/finpulse-cli --agent --bars 300 -v 2>&1 | head -80
```

### 10.1 GUI 打开了但什么都没有：WSLg 的 copy mode

**症状。** 双击 `run-gui.cmd`，控制台一闪，**看不到界面**。而实际上窗口是存在的
—— 去任务管理器看 `msrdc.exe`（WSLg 在 Windows 侧的窗口宿主），它的窗口标题是：

```
[WARN:COPY MODE] FinPulse Terminal  v0.4.0 — C++20 / Qt6 桌面壳 + …
```

`(Ubuntu)` 后缀是 WSLg 加的，`[WARN:COPY MODE]` 前缀才是关键：它表示**这个 WSLg
会话已经降级成 copy mode** —— 窗口照建、标题照给，但内容**永远画不出来**。

**根因在 WSLg，不在本项目。** WSLg 把 Linux 窗口送上 Windows 桌面时，要先在
`/mnt/shared_memory` 下分配一块共享内存做帧传输；分配失败就关掉图形重定向
（`use_gfxredir = 0`）退回逐帧拷贝。日志里的原始报错：

```
$ grep -n 'rdp_allocate_shared_memory\|use_gfxredir' /mnt/wslg/weston.log
67: rdp_allocate_shared_memory: Failed to open "/mnt/shared_memory/{…}" with error: Input/output error
68: RDP backend: use_gfxredir = 0        ← 坏会话；正常是 use_gfxredir = 1
```

这是微软已知问题（[microsoft/wslg#972](https://github.com/microsoft/wslg/issues/972)），
任何 WSL GUI 程序都会中，**跟 FinPulse 的代码、跟今天的构建都没有关系**。

**这条判据还有个好处：它在任何应用启动之前就已经写进日志了。** `use_gfxredir`
出现在 weston 启动阶段，因此可以在弹出窗口之前就先判断会话好坏 ——
`run-gui.cmd` 的预检就是靠它。

**处理。**

```cmd
wsl --shutdown
```

然后重新双击 `run-gui.cmd`。**状态是每个 WSLg 会话重新掷一次的**：同一个会话里
要么一直好、要么一直坏；换一个会话就是重新掷骰子，所以官方 issue 里有人说要试
三四次。而本机的 WSL 虚拟机空闲一会儿就会关停，于是几乎**每次双击都是新掷一次**。

`scripts/run-gui.cmd` 已经把这件事自动化了：启动前读日志预检 → 坏会话就问一句
要不要重开 WSL（最多 3 次）→ 启动后再看一次窗口标题兜底。之所以要"问一句"而不是
直接重开：`wsl --shutdown` 会一并关掉 Docker Desktop 等其它发行版，可能正在跑
容器或长任务，不该由启动器替用户决定。

> 顺带排除两条看似可行、其实不行的绕法：
> **① 换 X11（xcb）平台。** 本机缺 `libxcb-cursor0`，Qt 加载 xcb 插件会直接 abort
> （`qt.qpa.plugin: From 6.5.0, xcb-cursor0 or libxcb-cursor0 is needed`）；而且
> Xwayland 同样走 WSLg 那条 RDP 通道，照样是 copy mode。
> **② 等一会儿再启动。** 分配失败发生在 weston **启动**阶段，不是启动应用那一刻，
> 等再久也不会自愈；只有换会话。

**怎么快速自查"是不是这个故障"。** 程序、构建、路径都没问题的话，按顺序看三件事：

```bash
wsl -e bash -lc 'ls -l ~/finpulse-gui-build/src/gui/finpulse-gui'   # 二进制在不在
wsl -e bash -lc 'grep -n use_gfxredir /mnt/wslg/weston.log'          # = 1 好，= 0 坏
tasklist /v /fi "IMAGENAME eq msrdc.exe"                              # 标题带 COPY MODE 就是坏
```

前两条都对、窗口却还是空白，才轮到怀疑 FinPulse 自己。

---

## 11. 进阶：手工驱动引擎

桥接出问题时，第一件要判断的是"**是壳的锅还是引擎的锅**"。这个探针就是干这个的：
它和 C++ 壳做的事完全一样（拉起子进程、握手、发请求、收响应），只是用 Python 写的。

```bash
python3 tools/probe.py handshake
python3 tools/probe.py source.load --source synthetic --symbol SYNTH --bars 120
python3 tools/probe.py analysis.indicators --bars 120 --specs ma:5,20 rsi:14
python3 tools/probe.py analysis.stats --bars 250
python3 tools/probe.py forecast.run --method ar --horizon 5
python3 tools/probe.py forecast.backtest --method ar --folds 5 --horizon 5
python3 tools/probe.py raw            # 打印引擎自省信息
python3 tools/probe.py rpc agent.llm.status   # 任意 RPC 方法，JSON 原样打印
                                              # 参数用 --param key=value（可重复）
```

**通了 → 问题在壳；不通 → 问题在引擎。** 比对着两边日志猜要快一个数量级。

桥接性能基准：

```bash
./build/finpulse-bench
```

### RPC 方法总览

引擎对外暴露 **27 个方法**（11 个分析 + 16 个 `agent.*`）：

| 命名空间 | 方法 |
|---|---|
| 基础 | `handshake` `ping` `engine.info` `source.list` `source.load` `source.pull` `analysis.indicators` `analysis.stats` `forecast.list` `forecast.run` `forecast.backtest` |
| agent | `agent.roles` `agent.role.get` `agent.panels` `agent.tools` `agent.bridge.status` `agent.llm.status` `agent.run` `agent.team` `agent.debate` `agent.chat` `agent.trace` `agent.runs` `agent.memory` `agent.consistency` `agent.reload` `agent.stream.stats` |

其中 `agent.llm.status` 报告每个角色配置的与实际生效的推理后端、降级状态与接入方式；
`agent.stream.stats` 报告事件通道的投递统计，`agent.consistency` 用于回查
"同一个标的历次研判的立场是否自相矛盾"——决议记忆（`agent.memory`）就是为它服务的。

两个容易混淆的智能体方法：

- **`agent.debate`**（及 `agent.run` / `agent.team`）：按**契约**出报告。
  段落必须齐、方向要从指定段抽取、护栏会检查。产出的是结构化研判。
- **`agent.chat`**：自由**问答**。不设契约，用户问什么答什么。
  未连接大模型时返回 `degraded=true` 与可读原因，**不假装回答**；
  历史最多带 24 条，超出会被截断并在返回值里标记 `truncated=true`。

`source.pull` 是批量拉取：逐标的取数并落成 `data/<代码>.csv`，
进度以 `data.pull.start` / `data.pull.symbol` / `data.pull.done` 事件推出；
**单只失败不中断整批**，回落数据（缓存/演示）不算拉取成功。

---

## 12. 目录与文档索引

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
│   ├── analysis/    indicators / stats
│   ├── datasource/  base / registry（@register 插件）/ synthetic / csvfile / tushare
│   ├── forecast/    randomwalk / ar（OLS + AIC 选阶）/ backtest（walk-forward）
│   └── agent/       config / llm（含 override 运行时覆盖）/ tools / schemas / guardrails / memory
│                    orchestrator（独立 → 交叉质证 → 主席）/ api（RPC 安装）
│                    configs/*.json（4 角色 + 投委会）
├── tests/           C++ 测试（自研框架，157 例）
├── python/tests/    Python 测试（unittest，547 例）
├── tools/           probe.py（手工驱动引擎）/ bench_bridge.cpp（桥接基准）
├── scripts/         smoke_test.sh（一键端到端，7 步）/ run-cli.cmd / run-gui.cmd
└── docs/            本文档及其它设计文档
```

| 文档 | 内容 |
|---|---|
| **本手册** | 怎么装、怎么用、输出怎么读 |
| [architecture.md](architecture.md) | 三支柱架构、线程模型、DataHub 并发语义 |
| [bridge-protocol.md](bridge-protocol.md) | 帧格式与 RPC 信封的完整规格（含错误码表） |
| [design-decisions.md](design-decisions.md) | 每一个"不用现成库"的取舍与代价 |
| [iteration-log.md](iteration-log.md) | 开发中发现并修复的真实缺陷记录 |

---

## 附：一页速查

```bash
# —— 构建 ——
cmake -S . -B build -G Ninja -DCMAKE_BUILD_TYPE=Release && cmake --build build -j

# —— 日常 ——
./build/finpulse-cli --bars 500 --seed 42            # 完整报告
./build/finpulse-cli --agent --bars 300              # 加一节智能体研判
./build/finpulse-cli --agent --list                  # 有哪些角色 / 投委会
./build/finpulse-cli --agent --role risk_officer --brief
./build/finpulse-cli --agent --no-bridge             # 验证降级路径
./build/finpulse-cli --source csv --symbol DEMO-A --bars 500
export TUSHARE_TOKEN=...                             # 配好后:
./build/finpulse-cli --source tushare --symbol 000001.SZ --bars 250
./build/finpulse-cli --agent --provider deepseek --model deepseek-chat --api-key <密钥>
./build/finpulse-cli --replay --replay-speed 200 --bus-demo
./build/finpulse-cli --bars 300 --json | tail -1     # 一行 JSON
./build/src/gui/finpulse-gui                         # 图形界面

# —— 验证 ——
FINPULSE_REQUIRE_ENGINE=1 ./build/tests/finpulse-tests
python3 python/tests/run_tests.py
bash scripts/smoke_test.sh
```
