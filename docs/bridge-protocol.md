# 帧协议与 RPC 信封规格

**版本 1** ｜ 状态：稳定 ｜ 对应实现：`src/bridge/FrameCodec.cpp` ↔ `python/finpulse_engine/protocol.py`

本文件是 C++ 壳与 Python 引擎之间的**唯一权威约定**。任何一侧修改实现，
必须同步修改另一侧并升级本文件的版本号——这条要求同时写在两侧源文件头部。

---

## 1. 帧格式（Frame）

```text
偏移  长度  字段
0     4    N  = 体长度，无符号 32 位，**大端序**（网络字节序）
4     N    体 = UTF-8 编码的 JSON 文本
```

一条物理消息就是一帧。多帧直接背靠背拼接，中间没有分隔符。

### 1.1 长度上限

| 规则 | 值 | 理由 |
|---|---|---|
| 零长度帧 | **非法** | 协议中没有合法用途；出现即说明长度头已错，继续解码只会产出垃圾 |
| 最大体长 | 16 MiB（`16 * 1024 * 1024`） | 正常响应最大几百 KB，上限只用于把"跑飞的流"拦下来 |

触发任一条即判定**流损坏**：双方都清空缓冲、终止会话。
C++ 侧引擎进程退出码 2；壳捕获后重建子进程（带指数退避）。

### 1.2 大端序的理由

无。`struct.Struct(">I")` 与 `uint32_t` 的网络序写法只是**同一个约定的两种拼法**。
选大端是因为抓包工具（Wireshark 等）默认按网络序显示，调试时少一次心算。

### 1.3 增量解码

流可能被任意切开（半包、粘包、一帧跨三次 read），解码器必须是**增量**的：

- C++：`FrameCodec::append(bytes)` + 循环 `next()`
- Python：`FrameReader.feed(chunk)`（生成器，边喂边吐）

两侧都实现了 `compact()` 阈值式缓冲整理（64 KiB），避免每轮 `memmove`。

### 1.4 JSON 约定

- 紧凑分隔符（`,:`，无空格）——省字节，非必须但两侧一致；
- **`NaN` / `Infinity` 非法**。Python 侧 `json.dumps(..., allow_nan=False)`
  在发送端直接抛错：指标算出 NaN 是 bug，应该在源头修掉，而不是发给对面
  一个"看不懂但又不报错"的响应。C++ 侧解析器同样拒绝。

---

## 2. RPC 信封（Envelope）

### 2.1 请求

```json
{ "id": 42, "method": "forecast.run", "params": { "bars": [...], "horizon": 5 } }
```

### 2.2 成功响应

```json
{ "id": 42, "ok": true, "result": { ... } }
```

### 2.3 错误响应

```json
{ "id": 42, "ok": false,
  "error": { "code": "BadData", "message": "bars 至少需要 30 根，实际只有 10 根", "detail": "" } }
```

### 2.4 关键规则

| 规则 | 说明 |
|---|---|
| **有 `id` = 请求/响应，无 `id` = 事件** | 用"有没有 id"区分两种流向，少一个字段就少一处不一致的机会 |
| `id` 由壳生成，引擎原样回填 | 壳靠它把异步响应关联回挂起的 future |
| 引擎收到无 `id` 的请求 → 丢弃不回包 | 壳侧无从关联，回包也是垃圾 |
| 错误对象三个字段固定为 `code / message / detail` | `code` 是稳定枚举给程序看；`message/detail` 给人看 |
| 引擎内任何异常都转成错误响应 | **单条请求失败绝不允许杀死引擎进程**（帧损坏除外，那时流已不可信） |

---

## 3. 错误码表

| code | 来源 | 含义 | 壳侧典型处置 |
|---|---|---|---|
| `Protocol` | 引擎 | 请求不是 JSON 对象 / 缺 method | 属于壳的 bug，记日志 |
| `ProtocolMismatch` | 引擎 | 握手时协议版本不一致 | 中止启动，提示升级 |
| `UnknownMethod` | 引擎 | 方法未注册 | 记日志 |
| `BadParams` | 引擎 | 参数缺失/名字拼错/类型不对 | **壳的 bug**，与"业务失败"区分开 |
| `BadData` | 引擎 | 数据不合法（CSV 缺列、序列过短、价格非正） | 提示用户 |
| `NotFound` | 引擎 | 符号/数据源/方法不存在 | 提示用户 |
| `MathError` | 引擎 | 计算中除零等 | 提示用户 |
| `Unavailable` | 引擎 | 数据源在当前环境不可用（如需网络但离线） | 提示用户 |
| `EngineError` | 引擎 | 其他业务错误兜底 | 记日志 |
| 负数 | **C++ 壳本地** | `-1` 超时 / `-2` 传输断开 / `-3` 协议解析失败 | 本地错误不走管道，用负数与引擎侧正数名错误天然区分 |

---

## 4. 方法清单（v1 共 10 个）

| 方法 | 请求参数 | 返回 |
|---|---|---|
| `handshake` | `client`, `protocol` | 版本、协议号、数据源/预测器清单、方法表 |
| `ping` | `nonce` | `{nonce, alive:true}` |
| `engine.info` | — | 版本、方法说明、数据源详情 |
| `source.list` | — | 数据源及可用状态 |
| `source.load` | `source`, `symbol`, `bars`, `**kwargs` | 规范化 K 线数组 + 质量提示 |
| `analysis.indicators` | `bars`, `specs[]`（如 `"ma:5,20"`） | 与输入**等长**的指标线（前导 null） |
| `analysis.stats` | `bars`, `risk_free` | 描述统计与风险指标全集 |
| `forecast.list` | — | 预测器及其参数默认值 |
| `forecast.run` | `bars`, `method`, `horizon`, `options` | 预测点 + 95% 区间 + 模型自述 |
| `forecast.backtest` | `bars`, `method`, `horizon`, `folds`, `min_train` | walk-forward 指标 + 随机游走对照 |

---

## 5. 启动握手时序

```text
壳                                        引擎
│ 1. 探测解释器（.venv → python3 → py）      │
│ 2. spawn: python -u -m finpulse_engine    │
│    env: PYTHONPATH / PYTHONUNBUFFERED=1   │
│        PYTHONIOENCODING=utf-8             │
│──────── {id:1, method:"handshake"} ──────▶│
│◀─────── {id:1, ok:true, result:{...}} ────│
│ 3. 校验 protocol == 1，否则抛错退出         │
```

**stderr 全程保留给日志**（`FINPULSE_LOG` 控制级别），stdout 是纯协议通道。
这一条在 `Log.h` 与 `__main__.py` 两处都有断言式注释——它是整个桥接里
最容易在后期改坏的不变量。

---

## 6. 版本历史

| 版本 | 变更 |
|---|---|
| 1 | 初版：帧格式、10 个方法、错误码表 |
