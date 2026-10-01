"""数据源子包。

具体数据源在本包的子模块里定义，通过 ``registry.discover()`` 自动装载。
"""

from __future__ import annotations

from .base import Bar, DataSource
from . import registry

__all__ = ["Bar", "DataSource", "registry"]
