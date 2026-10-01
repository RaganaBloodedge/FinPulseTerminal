"""预测器抽象基类。"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any, Dict, List, Tuple


class Forecaster(ABC):
    """一个"喂进价格序列、吐出未来若干步预测"的对象。

    生命周期固定为 ``fit(series)`` → ``predict(h)`` / ``interval(h)``。
    回测会反复构造新的实例来保证每个折上的模型都是"干净"的——
    复用同一个实例是最容易引入前视偏差的写法。
    """

    #: 唯一标识，出现在 RPC 的 method 参数里
    name: str = ""
    #: 一行说明
    description: str = ""

    @abstractmethod
    def fit(self, series: List[float]) -> None:
        """在历史序列上拟合。序列按时间升序，最后一个元素是"现在"。"""

    @abstractmethod
    def predict(self, horizon: int) -> List[float]:
        """预测未来 ``horizon`` 步的点估计值。"""

    def interval(self, horizon: int, level: float = 0.95) -> List[Tuple[float, float]]:
        """预测区间。默认退化成点估计（上下界都等于预测值）。

        子类如果知道自己的预测方差（AR 是知道的），就该重写它——
        一个没有区间的预测，使用者无法判断该信几分。
        """
        return [(v, v) for v in self.predict(horizon)]

    def meta(self) -> Dict[str, Any]:
        """模型自述：阶数、系数、拟合优度等，用于展示和排查。"""
        return {}

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<{type(self).__name__} name={self.name!r}>"
