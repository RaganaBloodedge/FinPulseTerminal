"""Tushare Pro 数据源 —— 真实行情接口，带本地缓存回落。

**这个数据源是"能真的连出去"的，不是桩。**

它直连 Tushare Pro 的 HTTP 接口（``POST https://api.tushare.pro``），
请求体是 ``{api_name, token, params, fields}``，响应是
``{code, msg, data:{fields, items}}`` —— 全用标准库 ``urllib`` 发，不引第三方包。

拿到 token 就这么用::

    export TUSHARE_TOKEN=你的token
    ./build/finpulse-cli --source tushare --symbol 000001.SZ --bars 250

没有 token、或者接口调不通时，**不报错退出**，而是按下面的顺序回落，
并在 ``provenance`` 里如实写明"这一份不是实时接口取的"：

    实时接口（live_api）
        ↓ 取不到
    本地 CSV 缓存（local_cache）
        ↓ 也没有
    合成演示数据（demo_synthetic）

理由很简单：一个演示终端不该因为网络断了、或者你还没配 token 就整个用不了；
但也不能悄悄拿缓存冒充实时数据 —— 所以回落是**显式的**，CLI 会把实际走了
哪条路打在报告里，GUI 会打在日志页里。

**token 从哪来**（按优先级）：

1. 调用参数（CLI ``--tushare-token`` / GUI 与配置档传下来的 ``token``）；
2. 环境变量 ``TUSHARE_TOKEN``；
3. 启动配置档 ``~/.finpulse/profile.json`` 的 ``tushare_token`` 字段。

**关于"实时"二字要说清楚**：``daily`` 接口给的是**日线**，当日数据要等收盘后
才更新，所以它严格来说是"最新的已收盘日线"，不是盘中 tick。真正的盘中实时
需要 Tushare 的实时行情接口（对积分有要求）。不要把它当成分时数据用。
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional

from ..rpc import BadData
from ..timeutil import parse_datetime_ms
from .base import Bar, DataSource
from .csvfile import CsvSource
from .registry import create as create_source
from .registry import register

log = logging.getLogger("finpulse.datasource.tushare")

#: Tushare Pro 的 HTTP 端点。官方没有 SDK 也能用，就是一次 POST。
API_URL = "https://api.tushare.pro"

#: 读取 token 的环境变量名。**token 不写进配置文件**：配置文件会被提交、
#: 会被截图、会被拷来拷去，密钥不该待在那里。
TOKEN_ENV = "TUSHARE_TOKEN"

#: 单次请求超时。行情接口很快，给 20 秒已经很宽裕。
DEFAULT_TIMEOUT = 20.0

#: 请求的字段。少要几个字段能省带宽，也让响应更好读。
_FIELDS = "ts_code,trade_date,open,high,low,close,vol,amount"

#: 自然日 / 交易日 的换算系数。A 股一年约 245 个交易日、365 个自然日，
#: 比例约 1.49；取 1.6 留出长假（春节、国庆）的余量。
_CALENDAR_PER_TRADING = 1.6

#: 换算系数之外再加的缓冲天数，防止恰好跨长假时取的窗口不够。
_WINDOW_PAD_DAYS = 30


def _http_post(url: str, payload: Dict[str, Any], timeout: float) -> Dict[str, Any]:
    """默认传输实现：一次 POST，返回解析后的 JSON。

    单独拎成模块级函数是为了**可注入**：测试里把它换成一个假的，
    于是请求体构造、响应解析、错误处理三条路径都能离线验证，
    不用真的去连 Tushare（那会让测试依赖网络和 token，必然变成"有时候红"）。
    """
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        headers={
            "Content-Type": "application/json",
            "User-Agent": "FinPulseTerminal/0.5 (+datasource)",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw = resp.read()
    return json.loads(raw.decode("utf-8"))


@register
class TushareSource(DataSource):
    """Tushare Pro 日线。取不到时回落到本地 CSV 缓存。"""

    name = "tushare"
    description = "Tushare Pro 日线行情（直连 HTTP 接口；无 token 时回落本地缓存）"
    requires_network = True

    def __init__(
        self,
        transport: Optional[Callable[[str, Dict[str, Any], float], Dict[str, Any]]] = None,
        token: str = "",
        cache: Optional[DataSource] = None,
        demo: Optional[DataSource] = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._transport = transport or _http_post
        self._token = token
        self._cache = cache or CsvSource()
        #: 两级回落各自的来源都可注入：测试里既不想联网，也不想让
        #: "回落链的走向"跟合成源的生成逻辑绑在一起。
        self._demo_source = demo
        self._timeout = timeout
        #: 最近一次 load 的来源说明。service 会把它放进 RPC 返回值。
        self._provenance: Optional[Dict[str, Any]] = None

    # ── 自述 ────────────────────────────────────────────────
    def available(self) -> bool:
        """**永远可用。**

        没有 token 时它退回本地缓存，而不是变成"不可用" —— 让一个数据源
        整个消失，用户只会看到"数据源 tushare 不可用"，却不知道其实还能用
        缓存跑。可用性的真实差别写在 :meth:`info` 的 detail 里。
        """
        return True

    def info(self) -> Dict[str, Any]:
        d = super().info()
        token = self._token or os.environ.get(TOKEN_ENV, "")
        d["token_configured"] = bool(token)
        d["detail"] = (
            f"已配置 {TOKEN_ENV}，直连 {API_URL}"
            if token
            else f"未设置 {TOKEN_ENV}：按 本地缓存 → 合成演示数据 的顺序回落"
        )
        return d

    def provenance(self) -> Optional[Dict[str, Any]]:
        """本次数据的出处。没取过数据时返回 None。"""
        return dict(self._provenance) if self._provenance else None

    # ── 加载 ────────────────────────────────────────────────
    def load(  # type: ignore[override]
        self,
        symbol: str = "000001.SZ",
        bars: int = 250,
        token: str = "",
        allow_cache: bool = True,
        allow_demo: bool = True,
        end_date: str = "",
        **kwargs: Any,
    ) -> List[Bar]:
        bars = int(bars)
        token = token or self._token or os.environ.get(TOKEN_ENV, "").strip()

        if token:
            try:
                rows = self._fetch(token, symbol, bars, end_date)
                if rows:
                    self._provenance = {
                        "mode": "live_api",
                        "detail": f"Tushare Pro daily（{API_URL}）",
                        "symbol": symbol,
                        "rows": len(rows),
                        "as_of": rows[-1].to_dict()["ts"],
                    }
                    return rows[-bars:]
                reason = "接口返回了 0 行数据"
            except Exception as exc:  # noqa: BLE001 —— 任何失败都要能回落
                reason = f"{type(exc).__name__}: {exc}"
                log.warning("Tushare 取数失败，准备回落本地缓存：%s", reason)
        else:
            reason = f"未设置 {TOKEN_ENV}，也没在启动配置档里填 tushare_token"

        # ── 回落一：本地缓存 ────────────────────────────────
        if allow_cache:
            try:
                cached = self._cache.load(symbol=symbol, bars=bars)
            except Exception as exc:  # noqa: BLE001
                cache_err: Optional[Exception] = exc
            else:
                self._provenance = {
                    "mode": "local_cache",
                    "detail": "本地缓存 CSV（未使用实时接口）",
                    "symbol": symbol,
                    "rows": len(cached),
                    "reason": reason,
                }
                log.info("tushare 回落本地缓存：%s（%s 根）", symbol, len(cached))
                return cached
        else:
            cache_err = BadData("调用方要求禁用本地缓存回落")

        if not allow_demo:
            raise BadData(
                f"Tushare 取数不可用（{reason}），本地缓存也读不到 "
                f"符号 {symbol} 的数据（{cache_err}）。"
                f"请设置 {TOKEN_ENV}，或把 {symbol}.csv 放进 data/ 目录。"
            )

        return self._demo(symbol, bars, reason, cache_err)

    # ── 回落二：合成演示数据 ────────────────────────────────
    def _demo(self, symbol: str, bars: int, reason: str,
              cache_err: Exception) -> List[Bar]:
        """既没有实时数据、也没有缓存时，给一份**明确标注过的**演示数据。

        为什么不直接报错：这是终端，不是批处理脚本。打开就看见一场报错
        对话框，比"看见一张标着'演示数据'的图"糟糕得多 —— 后者至少还能
        用来演示界面、调指标；前者什么都做不了。

        为什么必须标注：不标注就会变成"看起来像真实行情"的东西。
        这份数据的出处会一路传到 CLI 报告和 GUI 日志里，写的是
        ``demo_synthetic``，任何一处分不清它就是缺陷。
        """
        log.warning("tushare 连缓存都没有（%s），回落到合成演示数据：%s",
                    cache_err, symbol)
        demo_src = self._demo_source or create_source("synthetic")
        demo = demo_src.load(symbol=symbol, bars=bars)
        self._provenance = {
            "mode": "demo_synthetic",
            "detail": "合成演示数据 —— 既不是实时行情，也不是本地缓存",
            "symbol": symbol,
            "rows": len(demo),
            "reason": f"{reason}；本地缓存不可用：{cache_err}",
        }
        return demo

    # ── 内部 ────────────────────────────────────────────────
    def _fetch(self, token: str, symbol: str, bars: int,
               end_date: str = "") -> List[Bar]:
        """真的发一次 HTTP，返回升序 K 线。"""
        params: Dict[str, Any] = {"ts_code": symbol}
        if end_date:
            params["end_date"] = end_date.replace("-", "")
        else:
            end = _dt.date.today()
            span = int(bars * _CALENDAR_PER_TRADING) + _WINDOW_PAD_DAYS
            params["start_date"] = (end - _dt.timedelta(days=span)).strftime("%Y%m%d")
            params["end_date"] = end.strftime("%Y%m%d")

        payload = {
            "api_name": "daily",
            "token": token,
            "params": params,
            "fields": _FIELDS,
        }
        raw = self._transport(API_URL, payload, self._timeout)
        return self._parse(raw, symbol)

    @staticmethod
    def _parse(raw: Dict[str, Any], symbol: str) -> List[Bar]:
        """把 Tushare 的 ``{fields, items}`` 竖表转成 Bar 列表（升序）。"""
        if not isinstance(raw, dict):
            raise BadData("Tushare 返回的顶层不是 JSON 对象")

        # ``code`` 非 0 就是失败。Tushare 用 2002 表示积分不足、-2001 表示
        # token 错 —— 这两种是最常见的，直接把 msg 带出去，别让用户猜。
        code = raw.get("code")
        if code not in (0, "0"):
            raise BadData(f"Tushare 接口报错 code={code}: {raw.get('msg') or '无说明'}")

        data = raw.get("data") or {}
        fields = list(data.get("fields") or [])
        items = list(data.get("items") or [])
        if not fields:
            raise BadData("Tushare 返回缺少 data.fields，无法解析")

        col = {name: i for i, name in enumerate(fields)}
        required = ("trade_date", "open", "high", "low", "close")
        missing = [k for k in required if k not in col]
        if missing:
            raise BadData(
                f"Tushare 返回缺少字段 {missing}（实际字段：{fields}）"
            )

        vol_i = col.get("vol")
        out: Dict[int, Bar] = {}
        for row in items:
            if not isinstance(row, (list, tuple)):
                continue
            try:
                trade_date = str(row[col["trade_date"]])
                bar = Bar(
                    ts=parse_datetime_ms(trade_date),
                    open=float(row[col["open"]]),
                    high=float(row[col["high"]]),
                    low=float(row[col["low"]]),
                    close=float(row[col["close"]]),
                )
            except (TypeError, ValueError, IndexError) as exc:
                log.debug("跳过一行无法解析的 Tushare 数据（%s）: %s", symbol, exc)
                continue

            # 成交量**单独**解析：停牌日 Tushare 会把它给成空值，那是正常现象，
            # 不该因此把整根 K 线丢掉 —— 价格还在，只是当天没成交。
            # 另外 Tushare 的 vol 单位是"手"（1 手 = 100 股），本项目一律
            # 按"股"存；不换算的话，实时接口与历史 CSV 之间会差 100 倍。
            if vol_i is not None:
                try:
                    bar.volume = int(float(row[vol_i]) * 100)
                except (TypeError, ValueError, IndexError):
                    bar.volume = 0

            out[bar.ts] = bar   # 用 dict 去重：同一天出现两次时保留后者

        # Tushare 默认按交易日**降序**返回，本项目一律要求升序。
        return [out[k] for k in sorted(out)]
