"""预测器注册表。与 datasource/registry.py 同构。"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from typing import Any, Dict, List, Type

from .base import Forecaster

log = logging.getLogger("finpulse.forecast")

_REGISTRY: Dict[str, Type[Forecaster]] = {}


def register(cls: Type[Forecaster]) -> Type[Forecaster]:
    name = getattr(cls, "name", "")
    if not name:
        raise ValueError(f"{cls.__name__} 没有设置类属性 name")
    if name in _REGISTRY:
        raise ValueError(f"预测器名冲突: {name}（已由 {_REGISTRY[name].__name__} 占用）")
    _REGISTRY[name] = cls
    log.debug("注册预测器 %s → %s", name, cls.__name__)
    return cls


def discover() -> None:
    import finpulse_engine.forecast as pkg

    for mod in pkgutil.iter_modules(pkg.__path__):
        if mod.name.startswith("_") or mod.name in ("base", "registry", "backtest"):
            continue
        importlib.import_module(f"{pkg.__name__}.{mod.name}")


#: 是否已经因为"查不到"而自动补过一次 discover()。
#: 只补一次，避免一个写错的预测器名在热路径上反复触发全包 import。
_AUTO_DISCOVERED = False


def create(name: str, **kwargs: Any) -> Forecaster:
    """按名字造一个预测器。

    **查不到时会自动补一次 :func:`discover`**，因为最常见的误用不是"名字写错"，
    而是"忘了在启动时调用 discover()"——那种情况下注册表是空的，裸 ``KeyError``
    完全看不出原因（实测踩过：agent 层的 backtest 工具因此静默变成"不可用"，
    再被护栏报成"未取到技能分"，根因被埋了两层）。
    """
    global _AUTO_DISCOVERED
    cls = _REGISTRY.get(name)
    if cls is None and not _AUTO_DISCOVERED:
        _AUTO_DISCOVERED = True
        discover()
        cls = _REGISTRY.get(name)
    if cls is None:
        raise KeyError(
            f"没有名为 '{name}' 的预测器。已注册：{names()}。"
            f"若你新增了一个模块，确认它打了 @register 装饰器；"
            f"若在测试里用，记得先调用 forecast.registry.discover()。"
        )
    return cls(**kwargs)


def names() -> List[str]:
    return sorted(_REGISTRY)


def all_classes() -> Dict[str, Type[Forecaster]]:
    return dict(_REGISTRY)
