"""RPC 方法的具体实现。

与 ``rpc.Dispatcher`` 分开的理由：调度只负责"把请求路由到函数"，
这里负责"函数到底做什么"。分开之后，每个方法都能脱离管道被直接调用，
单元测试不需要伪造一整个进程。

所有方法都遵循同一条规矩：**输入在做任何计算之前先校验**，
并把校验失败翻译成明确的错误码（BadParams / BadData / NotFound）。
让一个脏输入一路流到指标计算里再炸出 ZeroDivisionError，
对调用方来说是完全不可用的信息。
"""

from __future__ import annotations

import inspect
import logging
import platform
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from . import __version__
from .analysis import indicators, stats
from .datasource import registry as ds_registry
from .datasource.base import Bar
from .datasource.csvfile import write_bars
from .forecast import backtest
from .forecast import registry as fc_registry
from .rpc import BadData, BadParams, Dispatcher, NotFound, RpcError
from .stream import NullSink
from .timeutil import format_date

log = logging.getLogger("finpulse.service")

#: 与 C++ 侧 RpcClient 约定的协议版本。不匹配就直接拒绝握手。
PROTOCOL_VERSION = 1

#: 判断时间步长时用的兜底值（一个自然日的毫秒数）
_DEFAULT_STEP_MS = 86_400_000


# ── 输入校验 ──────────────────────────────────────────────

def require_bars(raw: Any, min_count: int = 2, what: str = "bars") -> List[Bar]:
    """把 RPC 传来的原始数组转成 Bar 列表，并做最低限度的健全性检查。"""
    if raw is None:
        raise BadParams(f"缺少必需的 {what} 参数")
    if not isinstance(raw, list):
        raise BadParams(f"{what} 必须是数组，实际是 {type(raw).__name__}")
    if len(raw) < min_count:
        raise BadData(f"{what} 至少需要 {min_count} 根，实际只有 {len(raw)} 根")

    out: List[Bar] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise BadData(f"{what}[{i}] 不是对象")
        try:
            out.append(Bar.from_dict(item))
        except (KeyError, TypeError, ValueError) as exc:
            raise BadData(f"{what}[{i}] 字段不合法: {exc}") from exc
    return out


def validate_bars(bars: Sequence[Bar]) -> List[str]:
    """数据质量检查。返回问题列表，不抛异常 —— 由调用方决定是警告还是拒绝。"""
    issues: List[str] = []
    for i, b in enumerate(bars):
        if b.high < b.low:
            issues.append(f"第 {i} 根 high({b.high}) < low({b.low})")
        if b.open <= 0 or b.close <= 0:
            issues.append(f"第 {i} 根价格非正 (o={b.open}, c={b.close})")
        if not (b.low - 1e-9 <= b.close <= b.high + 1e-9):
            issues.append(f"第 {i} 根 close({b.close}) 落在 [low, high] 之外")
        if i > 0 and b.ts <= bars[i - 1].ts:
            issues.append(f"第 {i} 根时间戳未严格递增")
        if len(issues) >= 10:
            issues.append("… 其余问题已省略")
            break
    return issues


def normalize_bars(bars: List[Bar]) -> List[Bar]:
    """排序 + 同一时间戳去重（保留后出现的）。"""
    ordered = sorted(bars, key=lambda b: b.ts)
    out: List[Bar] = []
    for b in ordered:
        if out and out[-1].ts == b.ts:
            out[-1] = b
        else:
            out.append(b)
    return out


def median_step_ms(ts_list: Sequence[int]) -> int:
    """相邻时间戳间隔的中位数，用来推算"下一根 K 线大概在什么时候"。

    日线序列里因为有周末，相邻差会是 1 天和 3 天的混合值。
    取中位数能稳定得到 1 天；取平均则会被周末拖偏。
    """
    if len(ts_list) < 2:
        return _DEFAULT_STEP_MS
    diffs = sorted(ts_list[i] - ts_list[i - 1] for i in range(1, len(ts_list)))
    mid = diffs[len(diffs) // 2]
    return mid if mid > 0 else _DEFAULT_STEP_MS


def make_forecaster(method: str, options: Dict[str, Any]):
    """按名字造一个预测器，只透传它能接受的选项。

    直接 `cls(**options)` 的问题是：调用方多传一个字段就 TypeError。
    而"多传字段"在前端迭代时几乎必然发生（比如 UI 加了设置项但引擎还没更新），
    那种失败对使用者毫无帮助。这里按签名过滤，未知字段直接忽略并记日志。
    """
    classes = fc_registry.all_classes()
    cls = classes.get(method)
    if cls is None:
        raise NotFound(f"未知预测方法: {method}（可用: {', '.join(fc_registry.names())}）")

    accepted = {
        name
        for name in inspect.signature(cls.__init__).parameters
        if name != "self"
    }
    kwargs = {k: v for k, v in options.items() if k in accepted}
    ignored = set(options) - accepted
    if ignored:
        log.warning("预测方法 %s 忽略了不认识的选项: %s", method, sorted(ignored))
    return cls(**kwargs)


# ── 方法注册 ──────────────────────────────────────────────

def build(dispatcher: Dispatcher, event_out: Any = None,
          write_lock: Any = None) -> None:
    """把所有 RPC 方法注册到调度器上。引擎启动时调用一次。

    ``event_out`` 是事件（单向推送）要写进去的二进制流。为 ``None`` 时
    智能体照常工作，只是不推进度事件 —— 单测与离线调用就是这种情况。

    ``write_lock`` 必须与主循环写响应帧的锁是同一把。
    """

    # 事件落点。没有事件通道时（单测、离线调用）退化成空实现 ——
    # 这样业务代码只管 emit，不必到处分叉成"有人听 / 没人听"两套。
    # 用 hasattr 而不是 isinstance：EventSink 是 Protocol，运行时不可判。
    sink = event_out if hasattr(event_out, "emit") else NullSink()

    # ── 元信息 ────────────────────────────────────────────

    @dispatcher.method("handshake")
    def handshake(client: str = "", protocol: int = PROTOCOL_VERSION) -> Dict[str, Any]:
        """握手：交换协议版本与能力清单。"""
        if int(protocol) != PROTOCOL_VERSION:
            raise RpcError(
                f"协议版本不匹配：引擎={PROTOCOL_VERSION} 客户端={protocol}",
                "ProtocolMismatch",
            )
        log.info("客户端 %s 已连接", client or "(未署名)")
        return {
            "name": "finpulse-engine",
            "version": __version__,
            "protocol": PROTOCOL_VERSION,
            "python_version": platform.python_version(),
            "sources": ds_registry.names(),
            "forecasters": fc_registry.names(),
            "methods": sorted(dispatcher.methods),
            "client": client,
        }

    @dispatcher.method("ping")
    def ping(nonce: int = 0) -> Dict[str, Any]:
        """心跳。壳用它判断引擎是真活着还是只是管道还没断。"""
        return {"nonce": int(nonce), "alive": True}

    @dispatcher.method("engine.info")
    def engine_info() -> Dict[str, Any]:
        """引擎自省：已注册的方法、数据源、预测器。"""
        return {
            "version": __version__,
            "protocol": PROTOCOL_VERSION,
            "methods": dict(dispatcher.methods),
            "sources": [ds_registry.create(n).info() for n in ds_registry.names()],
            "forecasters": fc_registry.names(),
        }

    # ── 数据源 ────────────────────────────────────────────

    @dispatcher.method("source.list")
    def source_list() -> Dict[str, Any]:
        """列出所有数据源及其可用状态。"""
        items = []
        for name in ds_registry.names():
            src = ds_registry.create(name)
            items.append({
                "name": name,
                "description": src.description,
                "requires_network": src.requires_network,
                "available": src.available(),
                "symbols": src.symbols(),
            })
        return {"sources": items}

    @dispatcher.method("source.load")
    def source_load(
        source: str = "synthetic",
        symbol: str = "SYNTH",
        bars: int = 250,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """从数据源加载一段日线，返回规范化后的 K 线数组。"""
        bars = int(bars)
        if bars <= 0:
            raise BadParams("bars 必须为正整数")

        try:
            src = ds_registry.create(source)
        except KeyError as exc:
            raise NotFound(
                f"未知数据源: {source}（可用: {', '.join(ds_registry.names())}）"
            ) from exc

        if not src.available():
            raise RpcError(f"数据源 {source} 在当前环境下不可用", "Unavailable")

        raw = src.load(symbol=symbol, bars=bars, **kwargs)
        if not raw:
            raise BadData(f"数据源 {source} 对 {symbol} 没有返回任何数据")

        normalized = normalize_bars(list(raw))
        issues = validate_bars(normalized)
        if issues:
            log.warning("数据质量提示（%s/%s）: %s", source, symbol, issues[:3])

        result: Dict[str, Any] = {
            "source": source,
            "symbol": symbol,
            "count": len(normalized),
            "bars": [b.to_dict() for b in normalized],
            "warnings": issues,
        }

        # 数据出处。有些数据源（tushare）能在"实时接口"和"本地缓存"之间
        # 回落 —— 那种情况下**必须**把实际走了哪条路告诉调用方，否则
        # 用户会以为自己看的是实时行情。用 getattr 探测而不是要求所有
        # 数据源都实现：只有会回落的数据源才需要回答这个问题。
        prov = getattr(src, "provenance", None)
        if callable(prov):
            try:
                detail = prov()
            except Exception as exc:  # noqa: BLE001 —— 出处查询失败不该影响取数
                log.debug("读取数据源出处失败: %s", exc)
                detail = None
            if detail:
                result["provenance"] = detail

        return result

    @dispatcher.method("source.pull")
    def source_pull(
        symbols: Optional[List[str]] = None,
        source: str = "tushare",
        bars: int = 250,
        out_dir: str = "",
        token: str = "",
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """批量拉取若干标的的日线，落成 CSV 缓存，返回逐个标的的结果。

        **为什么要在引擎里做，而不是让前端循环调 source.load**：
        批量拉取的核心是"落盘"，而落盘要和 CSV 数据源的读取定位严格一致
        （见 :func:`csvfile.write_bars`）。这套逻辑放一份在引擎里，
        CLI 与 GUI 共用；放两份在各自的前端里，迟早会分叉成"GUI 拉的数据
        命令行读不到"。

        进度以事件推出（``data.pull.start`` / ``data.pull.symbol`` /
        ``data.pull.done``），界面上因此能看到"第 3/10 只：招商银行"，
        而不是转圈等一个总结果。

        **单个标的失败不中断整批**：批量任务里最常见的失败是某只股票代码
        写错或已退市，为它放弃另外九只没有道理。失败原因逐条记在 items 里。
        """
        if not symbols:
            raise BadParams("symbols 不能为空")
        if not isinstance(symbols, (list, tuple)):
            raise BadParams("symbols 必须是数组")
        bars = int(bars)
        if bars <= 0:
            raise BadParams("bars 必须为正整数")

        # 去重但保序。重复代码会让同一只股票被拉两次、汇总里出现两行，
        # 而它多半只是粘贴时手抖 —— 这种输入该被就地收拾掉，不该报错。
        wanted: List[str] = []
        for raw in symbols:
            sym = str(raw).strip()
            if sym and sym not in wanted:
                wanted.append(sym)
        if not wanted:
            raise BadParams("symbols 里没有有效的标的代码")

        try:
            src = ds_registry.create(source)
        except KeyError as exc:
            raise NotFound(
                f"未知数据源: {source}（可用: {', '.join(ds_registry.names())}）"
            ) from exc
        if not src.available():
            raise RpcError(f"数据源 {source} 在当前环境下不可用", "Unavailable")
        if not src.requires_network:
            # csv/synthetic 都是本地数据：把缓存读出来再写回缓存，除了
            # 刷新一下 mtime 没有任何作用，只会让人误以为"刚更新过行情"。
            raise BadParams(f"{source} 是本地数据源，没有需要拉取的东西")

        sink.emit("data.pull.start",
                  {"total": len(wanted), "source": source, "bars": bars})

        items: List[Dict[str, Any]] = []
        for index, sym in enumerate(wanted, start=1):
            sink.emit("data.pull.symbol",
                      {"index": index, "total": len(wanted), "symbol": sym})
            rec: Dict[str, Any] = {
                "symbol": sym, "ok": False, "rows": 0, "path": "",
                "mode": "", "as_of": 0, "error": "",
            }
            try:
                rows = src.load(symbol=sym, bars=bars, token=token)
                prov = getattr(src, "provenance", None)
                mode = ""
                if callable(prov):
                    mode = str((prov() or {}).get("mode", ""))
                rec["mode"] = mode
                if mode in ("local_cache", "demo_synthetic"):
                    # 拿到的是回落数据。**这不算拉取成功**：批量拉取的目的
                    # 就是把实时数据落到本地，拿缓存覆盖缓存毫无意义，
                    # 还会让用户以为刚才真的刷新了行情。
                    rec["error"] = f"未取到实时数据（回落 {mode}）"
                    items.append(rec)
                    continue
                path = write_bars(rows, symbol=sym, out_dir=out_dir)
                rec.update(ok=True, rows=len(rows), path=str(path),
                           as_of=int(rows[-1].ts))
            except Exception as exc:  # noqa: BLE001 —— 一只失败不该拖垮整批
                rec["error"] = f"{type(exc).__name__}: {exc}"
                log.warning("拉取 %s 失败: %s", sym, rec["error"])
            items.append(rec)

        ok = sum(1 for it in items if it["ok"])
        first_dir = ""
        for it in items:
            if it["path"]:
                first_dir = str(Path(it["path"]).parent)
                break
        result = {
            "source": source,
            "bars": bars,
            "total": len(wanted),
            "ok": ok,
            "failed": len(wanted) - ok,
            "dir": first_dir,
            "items": items,
        }
        sink.emit("data.pull.done",
                  {"total": result["total"], "ok": ok, "failed": result["failed"]})
        return result

    # ── 分析 ──────────────────────────────────────────────

    @dispatcher.method("analysis.indicators")
    def analysis_indicators(
        bars: Optional[List[Dict[str, Any]]] = None,
        specs: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """计算技术指标。

        每条输出线与输入等长，前导不足的位置是 null —— C++ 侧画图时
        可以和 K 线按下标直接对齐。
        """
        bs = require_bars(bars, min_count=2)
        spec_list = list(specs) if specs else ["ma:5,20", "rsi:14"]
        if len(spec_list) > 16:
            raise BadParams("一次最多计算 16 条指标")

        closes = [b.close for b in bs]
        highs = [b.high for b in bs]
        lows = [b.low for b in bs]

        results = []
        for spec in spec_list:
            try:
                results.append(indicators.compute_spec(spec, closes, highs, lows))
            except ValueError as exc:
                raise BadParams(f"指标 {spec!r} 无法计算: {exc}") from exc

        return {
            "count": len(bs),
            "ts": [b.ts for b in bs],
            "specs": spec_list,
            "results": results,
        }

    @dispatcher.method("analysis.stats")
    def analysis_stats(
        bars: Optional[List[Dict[str, Any]]] = None,
        risk_free: float = 0.0,
    ) -> Dict[str, Any]:
        """描述性统计与风险指标。"""
        bs = require_bars(bars, min_count=5)
        closes = [b.close for b in bs]
        ts = [b.ts for b in bs]

        report = stats.summary(closes, ts, float(risk_free))

        # 把回撤区间的索引翻译成日期。这一步放在引擎里做，
        # 是因为壳拿到的 bars 可能已经被用户滚动过，索引不再对得上。
        for key, out_key in (
            ("max_drawdown_peak_index", "max_drawdown_peak_date"),
            ("max_drawdown_trough_index", "max_drawdown_trough_date"),
            ("max_drawdown_recovery_index", "max_drawdown_recovery_date"),
        ):
            idx = report.get(key)
            if isinstance(idx, int) and 0 <= idx < len(ts):
                report[out_key] = format_date(ts[idx])
            else:
                report[out_key] = None

        return report

    # ── 预测 ──────────────────────────────────────────────

    @dispatcher.method("forecast.list")
    def forecast_list() -> Dict[str, Any]:
        """列出所有可用的预测方法。"""
        items = []
        for name in fc_registry.names():
            cls = fc_registry.all_classes()[name]
            items.append({
                "name": name,
                "description": cls.description,
                "params": {
                    k: (v.default if v.default is not inspect.Parameter.empty else None)
                    for k, v in inspect.signature(cls.__init__).parameters.items()
                    if k != "self"
                },
            })
        return {"forecasters": items}

    @dispatcher.method("forecast.run")
    def forecast_run(
        bars: Optional[List[Dict[str, Any]]] = None,
        method: str = "ar",
        horizon: int = 5,
        options: Optional[Dict[str, Any]] = None,
        symbol: str = "",
    ) -> Dict[str, Any]:
        """用指定方法预测未来 horizon 步。"""
        horizon = int(horizon)
        if not (1 <= horizon <= 60):
            raise BadParams("horizon 应在 1..60 之间")

        bs = require_bars(bars, min_count=30)
        options = dict(options or {})

        model = make_forecaster(method, options)
        closes = [b.close for b in bs]

        try:
            model.fit(closes)
            preds = list(model.predict(horizon))
        except ValueError as exc:
            raise BadData(str(exc)) from exc

        try:
            bands = list(model.interval(horizon))
        except Exception:  # noqa: BLE001 — 区间算不出来不该拖垮点预测
            log.warning("方法 %s 未能给出预测区间，退化为点估计", method)
            bands = [(v, v) for v in preds]

        step = median_step_ms([b.ts for b in bs])
        last_ts = bs[-1].ts

        points = []
        for i, value in enumerate(preds):
            lo, hi = bands[i] if i < len(bands) else (value, value)
            points.append({
                "ts": last_ts + step * (i + 1),
                "value": round(float(value), 4),
                "lower": round(float(lo), 4),
                "upper": round(float(hi), 4),
            })

        return {
            "method": method,
            "symbol": symbol,
            "last_close": round(closes[-1], 4),
            "points": points,
            "meta": {**model.meta(), "horizon": horizon, "options": options},
        }

    @dispatcher.method("forecast.backtest")
    def forecast_backtest(
        bars: Optional[List[Dict[str, Any]]] = None,
        method: str = "ar",
        horizon: int = 5,
        folds: int = 5,
        min_train: int = 60,
        options: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """滚动回测，并把随机游走作为对照基线一起报告。"""
        horizon = int(horizon)
        folds = int(folds)
        min_train = int(min_train)

        if not (1 <= horizon <= 30):
            raise BadParams("horizon 应在 1..30 之间")
        if not (1 <= folds <= 30):
            raise BadParams("folds 应在 1..30 之间")
        if min_train < 10:
            raise BadParams("min_train 至少为 10")

        needed = min_train + folds * horizon
        bs = require_bars(bars, min_count=needed)
        options = dict(options or {})

        closes = [b.close for b in bs]

        def factory():
            return make_forecaster(method, options)

        try:
            report = backtest.run(
                closes, factory, folds=folds, horizon=horizon, min_train=min_train
            )
        except ValueError as exc:
            raise BadData(str(exc)) from exc

        report["method"] = method
        return report

    # ── 智能体 ────────────────────────────────────────────
    # 单独一个模块注册：它有自己的配置体系、生命周期与事件通道。
    # 放在这里只是为了"引擎启动时一次性装配"这件事只有一个入口。
    from .agent import api as agent_api
    agent_api.install(dispatcher, out=event_out, write_lock=write_lock)

    log.debug("已注册 %d 个 RPC 方法", len(dispatcher.methods))
