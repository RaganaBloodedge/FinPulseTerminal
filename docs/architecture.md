# 架构文档

本文回答三个问题：为什么是两个进程、总线上有哪些并发保证、GUI 的线程
模型为什么这样切。

---

## 1. 三支柱

### 1.1 进程桥接（C++ ↔ Python）

**决策：子进程 + 管道，而不是进程内嵌解释器（CPython C API）或 gRPC。**

| 方案 | 否决理由 |
|---|---|
| 进程内嵌 CPython | GIL 与 Qt 主线程抢锁；解释器崩溃 = 整个终端崩溃；C++ 侧要链接 Python 库，构建复杂度陡增 |
| gRPC / Socket | 引入 protobuf 与代码生成，违背零依赖目标；本地进程间走 TCP 纯属浪费 |
| **子进程 + 双向管道** ✅ | 故障隔离（引擎崩了壳还在，可自动重启）；语言解耦；`CreateProcessW`/`posix_spawn` 都是几十行的事 |

代价是需要自研一套帧协议（见 bridge-protocol.md）。这笔交易是划算的：
帧协议 200 行，而 gRPC 的依赖树是几百个文件。

### 1.2 进程内发布订阅（DataHub）

生产者不关心谁来消费，消费者不关心数据从哪来。行情回放线程发
`market.quote.SYNTH`，表格、图表、策略各自订阅——新增一个消费者
（比如日志面板）不需要动任何生产者的代码。

通配规则（`TopicPattern`）：

- `*` 匹配**一层**：`market.*.AAPL` 命中 `market.quote.AAPL`
- `**` **只允许出现在模式末尾**：`market.**` 合法，`market.**.AAPL` 拒绝

`**` 放中间会把匹配退化成正则回溯，为一个不存在的需求引入复杂度不划算。
`capture()` 可以取回通配命中的那一段，用于"一条订阅按符号分发"的场景。

### 1.3 插件化注册表（datasource / forecast）

```python
@register
class CsvSource(DataSource):
    name = "csv"
```

新增数据源 = 建模块 + 写类 + 打装饰器，引擎一行不改。`discover()` 在启动时
import 所有子模块触发注册。预测器同构。这是"对扩展开放"的最小实现。

---

## 2. DataHub 的并发语义（本项目最值得讲的一段）

### 2.1 实现

`publish()` 两段式：

```text
锁内：   遍历订阅表，匹配 pattern，把命中的 shared_ptr<Entry> 抄进局部 vector
锁外：   逐个调用 handler
```

### 2.2 由此得到的三个性质

1. **回调内再调 subscribe / unsubscribe / publish 不会死锁。**
   handler 执行时锁已经放掉了。
2. **慢订阅者不阻塞其它线程。**
   一个 handler 里 sleep(10s) 只拖住自己的派发，别的线程照常 publish。
3. **遍历中退订不使迭代器失效。**
   `shared_ptr` 保住了 Entry 的命，vector 里的句柄照常可用。

### 2.3 显式付出的代价

一个刚刚 `unsubscribe` 的订阅者，**仍可能收到"已在途"的那一次投递**。
退订语义定义为"不再接收此后发布的主题"，而不是"绝对不会再被调用"。
需要严格保证的订阅者应自己检查 valid 标志。（Qt 的跨线程 disconnect
在直连时也有类似语义。）

### 2.4 递归防护

handler 里再 publish 会形成递归派发。`thread_local` 深度计数 + 上限 8 层，
超限直接丢弃并计数——环形触发的结果是栈溢出，必须在源头拦住。

---

## 3. 线程模型

### 3.1 CLI

单线程驱动全部 RPC（同步 call），ReplaySource 在独立线程里按时间轴重放。
CLI 生命周期短，简单优先。

### 3.2 GUI

三条线程，跨越处只有两个：

```text
主线程（Qt）            后台线程                回放线程
   │                      │                      │
   │ ── runAnalysisAsync ─▶                      │
   │    （收集参数后 detached）                 │
   │                      │ RPC: load/指标/统计   │
   │                      │ /预测/回测           │
   │ ◀─ invokeMethod ─────┘                     │
   │    （QueuedConnection，按值投递）           │
   │ ◀──────── invokeMethod（DataHub 回调）──────┘
```

两条硬规则：

1. **Qt 部件只能在主线程碰。** 后台线程算完，用
   `QMetaObject::invokeMethod(..., Qt::QueuedConnection)` 把结果**按值**
   投回主线程（`AnalysisBundle` 是纯数据结构，可拷贝）。
2. **DataHub 的派发发生在发布线程**（回放线程），所以订阅回调里第一件事
   同样是"把数据丢回主线程"，绝不能直接改表格和图表。

用 `invokeMethod` + lambda 而不是自定义信号，是因为后者要给每种数据类型
注册 metatype，而 lambda 捕获不需要——少一层样板就少一处出错的地方。

### 3.3 引擎生命周期

```text
ensure_alive() ──▶ 存活？──▶ 是：直接用
      │ 否
restart_with_backoff()：200 / 400 / 800 / 1600 ms 指数退避
      │
restarting_ 原子标志 CAS，防止并发请求触发两次重启
```

超时判定只有**读线程的 deadline** 一个权威来源；同步 `call()` 不做超时
等待，避免两个时钟打架。

---

## 4. 数据流（GUI 一次"重新分析"）

```text
工具栏 ─▶ runAnalysisAsync()
            │ 后台线程
            ├─ source.load      → CandleSeries（规范化 + 质量检查）
            ├─ analysis.indicators → MA / BOLL
            ├─ analysis.stats   → 风险面板数据
            ├─ forecast.run     → 预测点 + 区间
            └─ forecast.backtest → 技能分等指标
            ▼
      AnalysisBundle（纯数据，可拷贝）
            │ QueuedConnection
            ▼
      applyAnalysis()（主线程）→ 图表 / 表格 / 面板刷新
```

回放是另一条独立通路：`ReplaySource` 把历史 K 线按时间轴重放成"实时"行情，
发布到 `market.quote.<SYM>`；GUI 订阅后逐条投回主线程更新表格。
两条通路互不阻塞，正是不把"加载"和"实时"耦在一个调用里的原因。
