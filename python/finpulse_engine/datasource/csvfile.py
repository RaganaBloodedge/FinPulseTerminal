"""本地 CSV 数据源。

列名大小写不敏感，兼容中文数据平台导出的常见别名：

    date / time / timestamp / datetime / trade_date  → ts
    open / o / 开盘                                    → open
    high / h / 最高                                    → high
    low  / l / 最低                                    → low
    close / c / adj_close / adj close / 收盘           → close
    volume / vol / v / 成交量                          → volume

日期支持 ``YYYY-MM-DD`` 与 ``YYYY-MM-DD HH:MM:SS``；也接受 ``20240315``
这种紧凑格式 —— 通达信/同花顺的导出就长这样。

文件定位顺序：
    1. 显式传入的 ``path``；
    2. 环境变量 ``FINPULSE_DATA_DIR`` 下的 ``<SYMBOL>.csv``；
    3. 项目 ``data/`` 目录下的 ``<SYMBOL>.csv``。

**读与写共用同一套定位逻辑**（:func:`data_dir` / :func:`csv_path_for`）：
批量拉取把文件写到哪儿、数据源从哪儿读，必须是同一个答案。这两处一旦
分叉，症状是"我刚拉完数据，它却说读不到" —— 排查起来要绕一大圈。
"""

from __future__ import annotations

import csv
import io
import logging
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from ..rpc import BadData, NotFound
from ..timeutil import format_datetime, parse_datetime_ms
from .base import Bar, DataSource
from .registry import register

log = logging.getLogger("finpulse.datasource.csv")

#: 写出的表头。与上面的别名表对偶 —— 读方认得的每一种名字，写方只用其中
#: 最标准的一个，不玩花样。
CSV_HEADER = ("date", "open", "high", "low", "close", "volume")


def _looks_like_int(s: str) -> bool:
    try:
        int(float(s))
        return True
    except (TypeError, ValueError):
        return False


def data_dir(create: bool = False) -> Optional[Path]:
    """数据目录：``$FINPULSE_DATA_DIR`` 优先，否则项目根下的 ``data/``。

    ``create=True`` 时会尝试把目录建出来（批量拉取要往里面写）。
    定位不到且建不出来时返回 None —— 调用方据此给出可读的错误。
    """
    env = os.environ.get("FINPULSE_DATA_DIR")
    if env:
        p = Path(env).expanduser()
        if p.is_dir():
            return p
        if create:
            try:
                p.mkdir(parents=True, exist_ok=True)
                log.info("已创建数据目录 %s（来自 FINPULSE_DATA_DIR）", p)
                return p
            except OSError as exc:
                log.warning("无法创建数据目录 %s: %s", p, exc)

    # finpulse_engine/datasource/csvfile.py → parents[3] 是项目根
    candidate = Path(__file__).resolve().parents[3] / "data"
    if candidate.is_dir():
        return candidate
    if create:
        try:
            candidate.mkdir(parents=True, exist_ok=True)
            return candidate
        except OSError as exc:
            log.warning("无法创建数据目录 %s: %s", candidate, exc)
    return None


def csv_path_for(symbol: str, *, path: str = "", out_dir: str = "") -> Path:
    """某个标的的 CSV 文件该落在哪。显式 ``path`` > ``out_dir`` > 数据目录。"""
    if path:
        return Path(path).expanduser()
    if not symbol:
        raise BadData("既没有给 path，也没有给 symbol，无法定位 CSV 文件")
    d = Path(out_dir).expanduser() if out_dir else data_dir(create=True)
    if d is None:
        raise NotFound("找不到也建不出数据目录，请用 out_dir 参数显式指定")
    return d / f"{symbol}.csv"


def _fmt_price(v: float) -> str:
    """价格写到 4 位小数、去掉尾随零：文件要给人看，也要能进版本控制。"""
    s = f"{float(v):.4f}".rstrip("0").rstrip(".")
    return s or "0"


def write_bars(rows: Iterable[Bar], *, symbol: str = "", path: str = "",
               out_dir: str = "") -> Path:
    """把 K 线写成 CSV，供 :class:`CsvSource` 再读回来。

    先写临时文件、再原子替换：写到一半进程没了的时候，宁可留着上一份
    **完整**的缓存，也不要留下一个"看着有、其实缺一半"的文件 ——
    那种文件会被静静地当成有效数据读进去，然后所有指标都算错。
    """
    bars = list(rows)
    if not bars:
        raise BadData("没有可写入的数据行")

    target = csv_path_for(symbol, path=path, out_dir=out_dir)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BadData(f"建不了目录 {target.parent}: {exc}") from exc

    tmp = target.with_name(target.name + ".tmp")
    try:
        with tmp.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.writer(fh)
            writer.writerow(CSV_HEADER)
            for b in sorted(bars, key=lambda x: x.ts):
                writer.writerow([format_datetime(b.ts), _fmt_price(b.open),
                                 _fmt_price(b.high), _fmt_price(b.low),
                                 _fmt_price(b.close), int(b.volume)])
        os.replace(tmp, target)
    except OSError as exc:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        raise BadData(f"写 CSV 失败 {target}: {exc}") from exc
    return target


@register
class CsvSource(DataSource):
    name = "csv"
    description = "从本地 CSV 文件读取 OHLCV（支持中英文列名）"
    requires_network = False

    _ALIASES: Dict[str, str] = {
        "date": "ts", "time": "ts", "timestamp": "ts", "datetime": "ts",
        "trade_date": "ts", "tradedate": "ts", "日期": "ts", "时间": "ts",
        "open": "open", "o": "open", "开盘": "open", "开盘价": "open",
        "high": "high", "h": "high", "最高": "high", "最高价": "high",
        "low": "low", "l": "low", "最低": "low", "最低价": "low",
        "close": "close", "c": "close", "adj_close": "close", "adj close": "close",
        "adjclose": "close", "收盘": "close", "收盘价": "close",
        "volume": "volume", "vol": "volume", "v": "volume",
        "成交量": "volume", "成交额": "volume",
    }

    def available(self) -> bool:
        # CSV 源永远"可用"：数据目录没了也还能靠显式 path 工作。
        # 真正的失败（文件找不到）会在 load 里以 NotFound 报出来。
        return True

    # ── 文件定位 ──────────────────────────────────────────

    @staticmethod
    def _data_dir() -> Optional[Path]:
        # 与 write_bars 共用同一套定位逻辑 —— 读和写必须给出同一个答案。
        return data_dir()

    def _resolve(self, symbol: str, path: str) -> Path:
        if path:
            p = Path(path).expanduser()
            if not p.is_file():
                raise NotFound(f"CSV 文件不存在: {p}")
            return p

        if not symbol:
            raise BadData("既没有给 path，也没有给 symbol，无法定位 CSV 文件")

        d = self._data_dir()
        if d is None:
            raise NotFound("找不到数据目录，请用 path 参数显式指定 CSV 文件")

        for cand in (d / f"{symbol}.csv", d / f"{symbol.upper()}.csv", d / f"{symbol.lower()}.csv"):
            if cand.is_file():
                return cand
        raise NotFound(f"数据目录 {d} 下没有 {symbol}.csv")

    # ── 主流程 ────────────────────────────────────────────

    def load(  # type: ignore[override]
        self,
        symbol: str = "",
        bars: int = 250,
        path: str = "",
        drop_invalid: bool = True,
        **kwargs: Any,
    ) -> List[Bar]:
        file_path = self._resolve(symbol, path)
        text = file_path.read_text(encoding="utf-8-sig")
        if not text.strip():
            raise BadData(f"文件为空: {file_path.name}")

        # 自动嗅探分隔符：中文数据平台偶尔导出制表符分隔
        sample = text[:4096]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel

        reader = csv.DictReader(io.StringIO(text), dialect=dialect)
        if not reader.fieldnames:
            raise BadData(f"{file_path.name} 缺少表头行")

        colmap: Dict[str, str] = {}
        for raw in reader.fieldnames:
            key = (raw or "").strip().lower().replace("\ufeff", "")
            target = self._ALIASES.get(key)
            if target and target not in colmap:
                colmap[target] = raw

        missing = [c for c in ("ts", "open", "high", "low", "close") if c not in colmap]
        if missing:
            raise BadData(
                f"{file_path.name} 缺少必需列: {', '.join(missing)}",
                detail=f"实际表头: {reader.fieldnames}",
            )

        out: List[Bar] = []
        skipped = 0
        for lineno, row in enumerate(reader, start=2):
            try:
                raw_ts = (row.get(colmap["ts"]) or "").strip()
                if not raw_ts:
                    skipped += 1
                    continue
                # 紧凑格式（20240315）由 parse_datetime_ms 自己认。
                # 这里原先也有一份转换正则 —— 那种"每个数据源各转一遍"的
                # 结果，就是新数据源忘了转然后整批解析失败（见 timeutil 注释）。
                ts = parse_datetime_ms(raw_ts)

                def num(col: str, default: float = 0.0) -> float:
                    v = (row.get(colmap[col]) or "").strip()
                    if not v or v in ("-", "--", "N/A", "null"):
                        return default
                    return float(v.replace(",", ""))

                o, h, l, c = num("open"), num("high"), num("low"), num("close")
                vol_raw = (row.get(colmap["volume"]) or "0").strip() if "volume" in colmap else "0"
                vol = int(float(vol_raw.replace(",", ""))) if vol_raw and vol_raw not in ("-", "--", "N/A") else 0

                # 停牌/缺失行：价格全为 0 或含 0，回测里这类行会污染收益率
                if drop_invalid and (o <= 0 or h <= 0 or l <= 0 or c <= 0):
                    skipped += 1
                    continue

                out.append(Bar(ts=ts, open=o, high=h, low=l, close=c, volume=vol))
            except (ValueError, KeyError, AttributeError) as exc:
                if drop_invalid:
                    skipped += 1
                    log.debug("跳过 %s 第 %d 行: %s", file_path.name, lineno, exc)
                    continue
                raise BadData(f"{file_path.name} 第 {lineno} 行解析失败: {exc}")

        if skipped:
            log.info("%s: 跳过 %d 行（空值或价格非法）", file_path.name, skipped)

        out.sort(key=lambda b: b.ts)

        # 只保留最近 bars 根 —— 文件通常比请求的多
        if bars > 0 and len(out) > bars:
            out = out[-int(bars):]

        if not out:
            raise BadData(f"{file_path.name} 没有解析出任何有效数据行")
        return out
