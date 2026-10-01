"""数据源抽象基类。

实现者只需要回答一个问题："给我符号和根数，返回按时间升序的 K 线"。
排序、去重、质量校验由引擎统一负责 —— 让每个数据源各写一遍，
结果一定是有的写了有的没写。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Dict, List


@dataclass
class Bar:
    """一根 K 线。字段名与 C++ 侧 ``fp::Candle`` 的 JSON 表示一一对应。"""

    ts: int
    open: float
    high: float
    low: float
    close: float
    volume: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ts": self.ts,
            "open": self.open,
            "high": self.high,
            "low": self.low,
            "close": self.close,
            "volume": self.volume,
        }

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Bar":
        return cls(
            ts=int(d["ts"]),
            open=float(d["open"]),
            high=float(d["high"]),
            low=float(d["low"]),
            close=float(d["close"]),
            volume=int(d.get("volume", 0) or 0),
        )


class DataSource(ABC):
    """行情数据源。"""

    #: 唯一标识，出现在 RPC 的 source 参数里
    name: str = ""
    #: 一行说明，用于 UI 和 handshake 上报
    description: str = ""
    #: 需要网络/外部服务。离线环境下引擎会把它标记为不可用。
    requires_network: bool = False

    def available(self) -> bool:
        """当前环境下是否可用。"""
        return True

    @abstractmethod
    def load(self, symbol: str, bars: int, **kwargs: Any) -> List[Bar]:
        """取数据。返回根数可以少于请求量（例如文件里本来就没那么多）。"""

    def symbols(self) -> List[str]:
        """支持的符号列表。空列表表示"任意符号都接受"。"""
        return []

    def info(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "requires_network": self.requires_network,
            "available": self.available(),
            "symbols": self.symbols(),
        }
