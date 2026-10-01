#!/usr/bin/env python3
"""手工驱动引擎的探针工具。

它在做的事情和 C++ 壳完全一样：拉起子进程、握手、发请求、收响应。
区别只是它用 Python 写的、且只处理一问一答。

**为什么值得单独维护一个工具**

    桥接出问题时，第一件要判断的是"到底是壳的锅还是引擎的锅"。
    有这个东西之后，只要跑一条 ``python3 tools/probe.py handshake``：
    通了 → 问题在壳；不通 → 问题在引擎。
    比对着两边日志猜要快一个数量级。

用法::

    python3 tools/probe.py handshake
    python3 tools/probe.py source.load --source synthetic --symbol SYNTH --bars 120
    python3 tools/probe.py analysis.indicators --bars 120 --specs ma:5,20 rsi:14
    python3 tools/probe.py analysis.stats --bars 250
    python3 tools/probe.py forecast.run --method ar --horizon 5
    python3 tools/probe.py forecast.backtest --method ar --folds 5 --horizon 5
    python3 tools/probe.py raw           # 打印引擎自省信息
"""

from __future__ import annotations

import argparse
import json
import os
import struct
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
PY_ROOT = REPO_ROOT / "python"

HEADER = struct.Struct(">I")


class ProbeError(RuntimeError):
    pass


class EngineProcess:
    """极简的同步引擎客户端。只够探针用。"""

    def __init__(self, python: Optional[str] = None) -> None:
        env = dict(os.environ)
        env["PYTHONPATH"] = str(PY_ROOT)
        env["PYTHONUNBUFFERED"] = "1"
        env["PYTHONIOENCODING"] = "utf-8"
        env.setdefault("FINPULSE_LOG", "info")

        exe = python or sys.executable
        self.proc = subprocess.Popen(  # noqa: S603 - 探针工具，命令是我们自己拼的
            [exe, "-u", "-m", "finpulse_engine"],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            cwd=str(PY_ROOT),
            env=env,
        )
        self._buf = bytearray()
        self._next_id = 1

    # ── 帧 IO ─────────────────────────────────────────────

    def _write_frame(self, obj: Dict[str, Any]) -> None:
        body = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        assert self.proc.stdin is not None
        self.proc.stdin.write(HEADER.pack(len(body)) + body)
        self.proc.stdin.flush()

    def _read_frame(self) -> Dict[str, Any]:
        assert self.proc.stdout is not None
        while True:
            while len(self._buf) >= HEADER.size:
                (n,) = HEADER.unpack_from(self._buf, 0)
                if len(self._buf) < HEADER.size + n:
                    break
                body = bytes(self._buf[HEADER.size:HEADER.size + n])
                del self._buf[:HEADER.size + n]
                return json.loads(body.decode("utf-8"))

            chunk = self.proc.stdout.read1(65536)
            if not chunk:
                raise ProbeError("引擎在响应之前退出了（看 stderr 的输出）")
            self._buf.extend(chunk)

    # ── 调用 ──────────────────────────────────────────────

    def call(self, method: str, params: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        rid = self._next_id
        self._next_id += 1
        self._write_frame({"id": rid, "method": method, "params": params or {}})
        msg = self._read_frame()
        if msg.get("id") != rid:
            raise ProbeError(f"响应 id 不匹配: 期望 {rid}，收到 {msg.get('id')}")
        if not msg.get("ok"):
            err = msg.get("error") or {}
            raise ProbeError(f"[{err.get('code')}] {err.get('message')}")
        return msg.get("result") or {}

    def close(self) -> None:
        try:
            if self.proc.stdin:
                self.proc.stdin.close()
            self.proc.wait(timeout=5)
        except (subprocess.TimeoutExpired, OSError):
            self.proc.kill()
        finally:
            if self.proc.stdout:
                self.proc.stdout.close()


# ── 带缓存的辅助调用 ──────────────────────────────────────

def load_bars(eng: EngineProcess, count: int, source: str = "synthetic",
              symbol: str = "SYNTH") -> List[Dict[str, Any]]:
    res = eng.call("source.load", {"source": source, "symbol": symbol, "bars": count})
    return res["bars"]


def money(v: Any) -> str:
    return "n/a" if v is None else f"{float(v):,.2f}"


def pct(v: Any) -> str:
    return "n/a" if v is None else f"{float(v):+.2f}%"


# ── 子命令 ────────────────────────────────────────────────

def cmd_handshake(eng: EngineProcess, args: argparse.Namespace) -> int:
    info = eng.call("handshake", {"client": "probe", "protocol": 1})
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0


def cmd_raw(eng: EngineProcess, args: argparse.Namespace) -> int:
    info = eng.call("engine.info")
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0


def cmd_source_load(eng: EngineProcess, args: argparse.Namespace) -> int:
    res = eng.call("source.load", {
        "source": args.source, "symbol": args.symbol, "bars": args.bars,
    })
    bars = res["bars"]
    print(f"{res['source']}/{res['symbol']}: {res['count']} 根")
    if res.get("warnings"):
        print(f"  数据质量提示: {res['warnings'][:3]}")
    for b in bars[:3] + (["..."] if len(bars) > 6 else []) + bars[-3:]:
        if b == "...":
            print("  ...")
            continue
        print(f"  ts={b['ts']}  O={b['open']:>9.2f} H={b['high']:>9.2f} "
              f"L={b['low']:>9.2f} C={b['close']:>9.2f} V={b['volume']:>12,}")
    return 0


def cmd_indicators(eng: EngineProcess, args: argparse.Namespace) -> int:
    bars = load_bars(eng, args.bars, args.source, args.symbol)
    res = eng.call("analysis.indicators", {"bars": bars, "specs": args.specs})
    print(f"输入 {res['count']} 根，指标 {len(res['results'])} 条")
    for item in res["results"]:
        print(f"\n[{item['spec']}]  meta={item['meta']}")
        for name, line in item["lines"].items():
            tail = [v for v in line[-5:] if v is not None]
            print(f"  {name:<12} 末 5 个值: "
                  + ", ".join(f"{v:.4f}" for v in tail))
    return 0


def cmd_stats(eng: EngineProcess, args: argparse.Namespace) -> int:
    bars = load_bars(eng, args.bars, args.source, args.symbol)
    s = eng.call("analysis.stats", {"bars": bars})
    width = max(len(k) for k in s)
    for k, v in s.items():
        print(f"  {k:<{width}} = {v}")
    return 0


def cmd_forecast(eng: EngineProcess, args: argparse.Namespace) -> int:
    bars = load_bars(eng, args.bars, args.source, args.symbol)
    res = eng.call("forecast.run", {
        "bars": bars, "method": args.method, "horizon": args.horizon,
        "options": {"order": args.order} if args.order is not None else {},
    })
    print(f"方法={res['method']}  最后收盘={money(res['last_close'])}")
    print(f"meta: {json.dumps(res['meta'], ensure_ascii=False)}")
    print("  步   预测值      下界      上界")
    for i, p in enumerate(res["points"], 1):
        print(f"  {i:<3} {money(p['value']):>10} {money(p['lower']):>10} {money(p['upper']):>10}")
    return 0


def cmd_backtest(eng: EngineProcess, args: argparse.Namespace) -> int:
    bars = load_bars(eng, args.bars, args.source, args.symbol)
    res = eng.call("forecast.backtest", {
        "bars": bars, "method": args.method, "folds": args.folds,
        "horizon": args.horizon, "min_train": args.min_train,
    })
    print(f"方法={res['method']}  折数={res['folds']}  步长={res['horizon']}  "
          f"预测点={res['n_predictions']}")
    print()
    print(f"  模型       MAE={money(res['mae']):>10}  RMSE={money(res['rmse']):>10}  "
          f"MAPE={res['mape']}%")
    print(f"  随机游走    MAE={money(res['base_mae']):>10}  RMSE={money(res['base_rmse']):>10}")
    print(f"  方向命中率  {res['dir_acc']}%   （随机游走无方向信息，按 50% 计）")
    print(f"  技能分      1 - MSE_model/MSE_rw = {res['skill']:+.4f}"
          f"   {'✓ 跑赢基线' if res['skill'] > 0 else '✗ 不如基线'}")
    return 0


def cmd_rpc(eng: EngineProcess, args: argparse.Namespace) -> int:
    """通用方法调用：探针不需要为每个 RPC 方法都写一个子命令。

    ``python3 tools/probe.py rpc agent.llm.status`` 这样的调用足以覆盖
    低频方法 —— 探针的职责是"能问到引擎"，不是"替每个方法排版输出"。
    """
    params: Dict[str, Any] = {}
    for kv in (args.param or []):
        if "=" not in kv:
            raise ProbeError(f"--param 期望 key=value，得到: {kv}")
        key, _, raw = kv.partition("=")
        try:
            params[key] = json.loads(raw)   # 数字/布尔/JSON 值按类型解析
        except json.JSONDecodeError:
            params[key] = raw               # 解不动就当字符串
    res = eng.call(args.method, params)
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="FinPulse 引擎探针")
    sub = parser.add_subparsers(dest="cmd", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--source", default="synthetic")
        p.add_argument("--symbol", default="SYNTH")

    p = sub.add_parser("handshake", help="握手并打印能力清单")
    p.set_defaults(func=cmd_handshake)

    p = sub.add_parser("raw", help="打印引擎自省信息")
    p.set_defaults(func=cmd_raw)

    p = sub.add_parser("source.load", help="加载一段行情")
    common(p)
    p.add_argument("--bars", type=int, default=120)
    p.set_defaults(func=cmd_source_load)

    p = sub.add_parser("analysis.indicators", help="计算技术指标")
    common(p)
    p.add_argument("--bars", type=int, default=120)
    p.add_argument("--specs", nargs="+", default=["ma:5,20", "rsi:14", "macd"])
    p.set_defaults(func=cmd_indicators)

    p = sub.add_parser("analysis.stats", help="统计与风险指标")
    common(p)
    p.add_argument("--bars", type=int, default=250)
    p.set_defaults(func=cmd_stats)

    p = sub.add_parser("forecast.run", help="执行一次预测")
    common(p)
    p.add_argument("--bars", type=int, default=250)
    p.add_argument("--method", default="ar")
    p.add_argument("--horizon", type=int, default=5)
    p.add_argument("--order", type=int, default=None)
    p.set_defaults(func=cmd_forecast)

    p = sub.add_parser("forecast.backtest", help="滚动回测（含随机游走对照）")
    common(p)
    p.add_argument("--bars", type=int, default=250)
    p.add_argument("--method", default="ar")
    p.add_argument("--folds", type=int, default=5)
    p.add_argument("--horizon", type=int, default=5)
    p.add_argument("--min-train", type=int, default=60, dest="min_train")
    p.set_defaults(func=cmd_backtest)

    p = sub.add_parser("rpc", help="调用任意 RPC 方法，JSON 原样打印")
    p.add_argument("method", help="方法名，如 agent.llm.status")
    p.add_argument("--param", action="append", metavar="KEY=VALUE",
                   help="请求参数，可重复；值能 JSON 解析就带类型，否则当字符串")
    p.set_defaults(func=cmd_rpc)

    args = parser.parse_args(argv)

    eng = EngineProcess()
    try:
        return int(args.func(eng, args))
    except ProbeError as exc:
        print(f"探针失败: {exc}", file=sys.stderr)
        return 1
    finally:
        eng.close()


if __name__ == "__main__":
    sys.exit(main())
