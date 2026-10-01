# 设计取舍

C++ 核心库与 Python 引擎均不引入第三方依赖。本文记录各项"不用现成库"的决定与代价。

取舍依据：在当前规模（数百至数千个数据点）下，自研实现应可完整理解、测试与交付。

---

## 1. 手写 JSON 解析器（不用 nlohmann/json）

| | nlohmann/json | 手写 |
|---|---|---|
| 单 TU 编译时间 | ~3 s | 0.4 s |
| 依赖 | 单头文件，2.5 万行 | ~500 行 |
| 错误处理 | 异常 | `JsonError` 带 `offset()`，可定位到字节 |

限制：嵌套深度 64，数字仅支持 double / int64。协议通道两端的 JSON 均由本项目生成，
不接收外部输入，因此不需要完整 JSON 标准。

整数构造重载必须覆盖 `int / unsigned / long / unsigned long / long long /
unsigned long long` 全档宽度，否则 `int64_t`（Linux 上为 `long`）会触发歧义调用。

## 2. 手写 OLS（不用 numpy / statsmodels）

依据为发布体积：

- 带部分主元的高斯消元解 (p+1)×(p+1) 正规方程：p ≤ 10 时最大 11×11，约 40 行
- 引入 numpy：发布包从数百 KB 增至 30 MB 以上，并新增一个运行环境失败点

代价：无向量化。数百个数据点的 AR 拟合约 1 ms。

替换点：forecaster 层。`@register` 一个新类即可接入其它后端，调用方不变。

数值安全：滞后项高度相关，系数矩阵常接近奇异，因此必须使用部分主元。有测试覆盖
奇异矩阵报错路径。

## 3. 手写测试框架（80 行，不用 GoogleTest）

GoogleTest 需通过 `FetchContent` 拉取网络依赖，离线环境构建失败。自研的
`FP_TEST / FP_CHECK / FP_CHECK_EQ / FP_CHECK_NEAR / FP_CHECK_THROWS` 覆盖全部需求，
并处理两个具体问题：

- 用 `__LINE__` 拼标识符、名字作字符串，绕开 `##` 拼不出中文标识符的限制
- `FP_CHECK_EQ` 使用值拷贝而非 `const auto&`，避开 `-Wdangling-reference` 误报

## 4. 手写统计（不用 pandas）

`stats.py` 约 300 行，覆盖收益率 / CAGR / 年化波动 / 夏普 / 索提诺 / 最大回撤 /
VaR / CVaR / 偏度 / 峰度 / 自相关。三条包级约定：

1. 使用简单收益率（非对数收益率），与业界报告口径一致
2. 年化按 252 交易日
3. 样本不足时返回 `None` 而非 0：0 会被下游解释为"波动率为零"

## 5. 预测区间的精确公式（不用 σ√h）

随机游走的 h 步误差方差为 `h·σ²`。AR(p) 的正确公式为 `σ²·Σψ²ⱼ`（MA(∞) 表示的 ψ
权重递推）。φ 显著非零时 `σ√h` 高估不确定性：φ=0.8 的 AR(1) 在 h=10 处，真实标准差
约为 `√h` 形式的 2/3。系数已拟合，使用精确公式无额外代价。

## 6. 技能分与方向命中率

- `skill = 1 - MSE_model / MSE_randomwalk`，随机游走为唯一对照基准（Meese–Rogoff, 1983）
- 随机游走的方向命中率固定记为 50%：它不携带方向信息，记 0% 会抬高所有模型的技能分
- 技能分为负时，界面显示"不如随机游走"

## 7. 合成数据源的波动率锚定

`annual_vol` 参数须给出对应波动率，否则实际波动与参数不符。稳态方差满足：

```text
E[var] = ω / (1 - α·E[z²] - β)        E[z²] = (1-p) + p·scale² ≈ 1.26（跳变混合）
一根 K 线的对数收益 = drift + gap + shock
→ Var = E[var] · (E[z²] + gap_scale²)
两式联立解出 ω 和 var。
```

三版修正：仅用 `(1-α-β)` 作分母时 `annual_vol=0.28` 实测 53.9%；补跳变项后 37.6%；
补跳空项后 29.7% / 31.2% / 30.5%。回归测试：`test_annual_vol锚定_回归防线`。

## 8. Windows 句柄不进头文件

`Subprocess.h` 不引入 `<windows.h>`：它会污染每个包含者的全局命名空间（`min` / `max`
宏及数十个类型名）。句柄用 `std::intptr_t` 承载，在 `.cpp` 中 `reinterpret_cast`。
POSIX 侧对称处理，头文件中同样只有 `intptr_t`，平台差异集中在一个 `.cpp` 内。

## 9. Python 引擎只用标准库

原因：桌面包需连同引擎一并分发，依赖越多安装失败率越高；发行包体积；当前规模
不需要向量化。两个实现约束：

- stdout 连管道时默认 8 KB 块缓冲，需 `python3 -u` 与 `PYTHONUNBUFFERED=1`
- `sys.stdin.buffer.read(n)` 阻塞至读满 n 字节，需改用 `os.read`

## 10. CSV 列名中英文别名

`日期/时间/trade_date` → `ts`，`收盘/收盘价/adj_close` → `close`，并支持 `20240315`
紧凑日期格式（通达信 / 同花顺导出）。`csv.Sniffer` 自动嗅探分隔符，兼容制表符导出。
