"""时间工具 —— 与 C++ 侧 ``src/model/Types.cpp`` 的约定保持一致。

约定（两边必须一致，否则时间轴会整体错位）：
    * 时间点一律用 **毫秒级 epoch（UTC）** 表示，不用秒、不用 time_t；
    * 日期字符串一律 ``YYYY-MM-DD``；
    * 日期时间字符串一律 ``YYYY-MM-DD HH:MM:SS``。

**输出**严格按上面的格式；**解析**则宽容一些，额外接受 ``YYYYMMDD``
紧凑格式（外部数据平台的导出长这样，见 :func:`parse_datetime_ms`）。
"""

from __future__ import annotations

import datetime as _dt
import re
from typing import List, Optional

_DATE_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
_DATETIME_RE = re.compile(
    r"^(\d{4})-(\d{1,2})-(\d{1,2})[ T](\d{1,2}):(\d{2})(?::(\d{2}))?$"
)
#: ``YYYYMMDD`` 紧凑格式。通达信 / 同花顺 的导出、Tushare 的 trade_date
#: 字段都是这个形状 —— 所以它不是"顺手支持一下"，而是接真实数据的必需项。
_COMPACT_DATE_RE = re.compile(r"^(\d{4})(\d{2})(\d{2})$")
_EPOCH = _dt.datetime(1970, 1, 1, tzinfo=_dt.timezone.utc)


def to_ms(dt: _dt.datetime) -> int:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=_dt.timezone.utc)
    return int(dt.timestamp() * 1000)


def from_ms(ms: int) -> _dt.datetime:
    return _EPOCH + _dt.timedelta(milliseconds=int(ms))


def format_date(ms: int) -> str:
    return from_ms(ms).strftime("%Y-%m-%d")


def format_datetime(ms: int) -> str:
    return from_ms(ms).strftime("%Y-%m-%d %H:%M:%S")


def parse_datetime_ms(text: str) -> int:
    """解析日期串，返回毫秒级 epoch。

    支持三种写法：

    * ``YYYY-MM-DD``
    * ``YYYY-MM-DD HH:MM[:SS]``
    * ``YYYYMMDD``（紧凑格式）

    紧凑格式**在这里**支持，而不是让每个数据源各自预处理一遍：
    它曾经只在 CSV 数据源里用一个正则转掉，结果新加的 Tushare 数据源
    拿到 ``trade_date="20240102"`` 就整批解析失败 —— 而且失败是**静默**的
    （坏行被跳过 → 空结果 → 悄悄回落到缓存），最难查的那一类。
    格式解析属于时间工具的职责，就该收在这一处。
    """
    s = text.strip()
    m = _DATETIME_RE.match(s)
    if m:
        y, mo, d, h, mi, sec = m.groups()
        return to_ms(_dt.datetime(int(y), int(mo), int(d), int(h), int(mi), int(sec or 0)))
    m = _DATE_RE.match(s)
    if m:
        y, mo, d = m.groups()
        return to_ms(_dt.datetime(int(y), int(mo), int(d)))
    m = _COMPACT_DATE_RE.match(s)
    if m:
        y, mo, d = m.groups()
        return to_ms(_dt.datetime(int(y), int(mo), int(d)))
    raise ValueError(f"无法解析日期: {text!r}")


def recent_trading_days(count: int, end_ms: Optional[int] = None) -> List[int]:
    """最近 ``count`` 个交易日的 00:00 UTC 毫秒时间戳（升序）。

    只跳过周六周日，**不处理节假日**：完整交易日历是一份要持续维护的
    交易所元数据，不属于本项目范围。合成数据源用它只是为了让时间轴
    看起来正常 —— 真实回测应当从 CSV 源读真实日期。
    """
    if count <= 0:
        return []

    cursor = from_ms(end_ms) if end_ms is not None else _dt.datetime.now(_dt.timezone.utc)
    cursor = cursor.replace(hour=0, minute=0, second=0, microsecond=0)

    out: List[int] = []
    while len(out) < count:
        if cursor.weekday() < 5:  # 0=周一 … 4=周五
            out.append(to_ms(cursor))
        cursor -= _dt.timedelta(days=1)
    out.reverse()
    return out
