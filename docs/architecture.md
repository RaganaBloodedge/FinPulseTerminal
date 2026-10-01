# 架构

本文描述进程结构、总线并发语义与 GUI 线程模型。实现细节见 `src/` 与 `python/finpulse_engine/`。

---

## 1. 三支柱

### 1.1 进程桥接（C++ ↔ Python）

| 方案 | 取舍 |
|---|---|
| 进程内嵌 CPython | GIL 与 Qt 主线程竞争；解释器崩溃导致终端整体崩溃；C++ 侧需链接 Python 库，构建复杂度上升 |
| gRPC / 本地 Socket | 引入 protobuf 与代码生成，与零第三方依赖目标冲突；本地进程间通信走 TCP 无收益 |
| 子进程 + 双向管道（当前实现） | 故障隔离：引擎崩溃不影响壳，可自动重启；语言解耦；`CreateProcessW` / `posix_spawn` 实现量小 |

代价是自研帧协议（见 [bridge-protocol.md](bridge-protocol.md)），规模约 200 行。

### 1.2 进程内发布订阅（DataHub）

生产者不关心消费者，消费者不关心数据来源。行情回放线程发布 `market.quote.SYNTH`，
表格、图表、策略各自订阅；新增消费者不需要修改任何生产者代码。

通配规则（`TopicPattern`）：

- `*` 匹配一层：`market.*.AAPL` 命中 `market.quote.AAPL`
- `**` 只允许出现在模式末尾：`market.**` 合法，`market.**.AAPL` 被拒绝

`**` 置于中间会使匹配退化为正则回溯，`capture()` 用于取回通配命中的那一段，
支持一条订阅按符号分发。

### 1.3 插件化注册表（datasource / forecast）

```python
@register
class CsvSource(DataSource):
    name = "csv"
```

新增数据源只需新增模块并加装饰器，引擎代码不变；`discover()` 在启动时 import
所有子模块以触发注册。预测器同构。

---

## 2. DataHub 并发语义

### 2.1 实现

`publish()` 分两段：

```text
锁内：   遍历订阅表，匹配 pattern，把命中的 shared_ptr<Entry> 抄进局部 vector
锁外：   逐个调用 handler
```

### 2.2 性质

1. handler 内再次调用 `subscribe` / `unsubscribe` / `publish` 不会死锁：handler 执行时锁已释放。
2. 慢 handler 不阻塞其它线程：某个 handler 阻塞 10 s 只影响自身派发，其它线程照常 `publish`。
3. 遍历中退订不使迭代器失效：vector 中的 `shared_ptr` 持有 Entry。

### 2.3 退订语义

刚 `unsubscribe` 的订阅者仍可能收到一次在途投递。退订的含义是"不再接收此后发布的主题"，
不保证"不再被调用"。需要严格保证的订阅者应自行检查 valid 标志。

### 2.4 递归防护

handler 内再次 `publish` 会形成递归派发。使用 `thread_local` 深度计数，上限 8 层，
超限则丢弃并计数。

---

## 3. 线程模型

### 3.1 CLI

单线程驱动全部 RPC（同步 `call`），`ReplaySource` 在独立线程按时间轴重放。

### 3.2 GUI

三条线程，跨线程交互两处：

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

两条规则：

1. Qt 部件只在主线程访问。后台线程计算完成后，用
   `QMetaObject::invokeMethod(..., Qt::QueuedConnection)` 将结果按值投回主线程
   （`AnalysisBundle` 为纯数据结构，可拷贝）。
2. DataHub 的派发在发布线程（回放线程）进行，因此订阅回调需先将数据投回主线程，
   不能直接修改表格与图表。

使用 `invokeMethod` + lambda 而不注册自定义信号：后者需要为每种数据类型注册 metatype。

### 3.3 引擎生命周期

```text
ensure_alive() ──▶ 存活？──▶ 是：直接用
      │ 否
restart_with_backoff()：200 / 400 / 800 / 1600 ms 指数退避
      │
restarting_ 原子标志 CAS，防止并发请求触发两次重启
```

超时判定以读线程的 deadline 为唯一来源；同步 `call()` 不做超时等待。

---

## 4. 数据流（GUI 一次重新分析）

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

回放为独立通路：`ReplaySource` 将历史 K 线按时间轴重放为实时行情，发布到
`market.quote.<SYM>`；GUI 订阅后逐条投回主线程更新表格。两条通路互不阻塞。
