"""数据源注册表。

新增一个数据源不需要改引擎的任何一行代码：建个模块、写个类、
打上 ``@register``，``discover()`` 会在启动时自动把它捡起来。
这就是本项目"插件化"承诺的具体落点。
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from typing import Dict, List, Type

from .base import DataSource

log = logging.getLogger("finpulse.datasource")

_REGISTRY: Dict[str, Type[DataSource]] = {}


def register(cls: Type[DataSource]) -> Type[DataSource]:
    """类装饰器：登记一个数据源。名字冲突会立刻抛错，不留隐患。"""
    name = getattr(cls, "name", "")
    if not name:
        raise ValueError(f"{cls.__name__} 没有设置类属性 name")
    if name in _REGISTRY:
        raise ValueError(f"数据源名冲突: {name}（已由 {_REGISTRY[name].__name__} 占用）")
    _REGISTRY[name] = cls
    log.debug("注册数据源 %s → %s", name, cls.__name__)
    return cls


def discover() -> None:
    """import 本包下的所有子模块，触发它们模块级的 ``@register``。"""
    import finpulse_engine.datasource as pkg

    for mod in pkgutil.iter_modules(pkg.__path__):
        if mod.name.startswith("_") or mod.name in ("base", "registry"):
            continue
        importlib.import_module(f"{pkg.__name__}.{mod.name}")


#: 是否已经因为"查不到"而自动补过一次 discover()。理由同 forecast/registry。
_AUTO_DISCOVERED = False


def create(name: str) -> DataSource:
    global _AUTO_DISCOVERED
    cls = _REGISTRY.get(name)
    if cls is None and not _AUTO_DISCOVERED:
        _AUTO_DISCOVERED = True
        discover()
        cls = _REGISTRY.get(name)
    if cls is None:
        raise KeyError(
            f"没有名为 '{name}' 的数据源。已注册：{names()}。"
            f"若你新增了一个模块，确认它打了 @register 装饰器；"
            f"若在测试里用，记得先调用 datasource.registry.discover()。"
        )
    return cls()


def names() -> List[str]:
    return sorted(_REGISTRY)


def all_classes() -> Dict[str, Type[DataSource]]:
    return dict(_REGISTRY)
