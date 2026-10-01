"""执行轨迹。

每次角色调用产生一棵 span 树：工具调用、后端请求、护栏检查、渲染各占一个 span，
带耗时与属性。存在的理由有三个，都很实际：

* **性能**：一次辩论里 90% 的时间花在哪——后端请求还是工具计算？没有轨迹只能猜。
* **追责**：报告里那个数字来自哪次工具调用？轨迹里有完整的调用序列与返回摘要。
* **可解释**：被问"你怎么知道 LLM 那一步没超时"，能直接给出数字。

刻意不引第三方（OpenTelemetry 之类）：这里只需要树 + 耗时，自己写 80 行足够，
而且不引入对外的数据上报路径——**分析一个本地终端不需要把行为数据发出去**。
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional


@dataclass
class Span:
    name: str
    kind: str = "step"
    start_ms: float = 0.0
    duration_ms: float = 0.0
    attrs: Dict[str, Any] = field(default_factory=dict)
    children: List["Span"] = field(default_factory=list)
    error: Optional[str] = None

    def to_json(self, *, depth: int = 0) -> Dict[str, Any]:
        out: Dict[str, Any] = {
            "name": self.name,
            "kind": self.kind,
            "duration_ms": round(self.duration_ms, 3),
        }
        if self.attrs:
            out["attrs"] = self.attrs
        if self.error:
            out["error"] = self.error
        if self.children and depth < 8:
            out["children"] = [c.to_json(depth=depth + 1) for c in self.children]
        return out


class Tracer:
    """一个极简的 span 树构建器。

    线程安全性说明：本项目里每个角色在自己的线程上跑（辩论阶段是并行的），
    因此 :class:`Tracer` **不保证跨线程安全**。正确用法是每条线程一个 Tracer，
    最后由编排层把各自的根 span 挂到同一棵树上。这比加锁简单，也更快。
    """

    def __init__(self, name: str = "trace") -> None:
        self.root = Span(name=name, kind="root")
        self._stack: List[Span] = [self.root]

    @property
    def current(self) -> Span:
        return self._stack[-1]

    @contextmanager
    def span(self, name: str, kind: str = "step", **attrs: Any) -> Iterator[Span]:
        node = Span(name=name, kind=kind, attrs={k: v for k, v in attrs.items() if v is not None})
        self.current.children.append(node)
        self._stack.append(node)
        t0 = time.perf_counter()
        node.start_ms = t0 * 1000.0
        try:
            yield node
        except Exception as exc:
            node.error = f"{type(exc).__name__}: {exc}"
            raise
        finally:
            node.duration_ms = (time.perf_counter() - t0) * 1000.0
            # 即使抛异常也要出栈，否则后续 span 会挂到错误的父节点上。
            self._stack.pop()

    def attach(self, child_root: Span, name: Optional[str] = None) -> None:
        """把另一条线程的根 span 挂进来（辩论并行阶段用）。"""
        if name:
            child_root.name = name
        self.root.children.append(child_root)

    # ── 汇总 ─────────────────────────────────────────────────
    def total_ms(self) -> float:
        return self.root.duration_ms

    def flatten(self) -> List[Span]:
        out: List[Span] = []

        def walk(node: Span) -> None:
            out.append(node)
            for child in node.children:
                walk(child)

        for child in self.root.children:
            walk(child)
        return out

    def slowest(self, n: int = 5) -> List[Span]:
        return sorted(self.flatten(), key=lambda s: s.duration_ms, reverse=True)[:n]

    def to_json(self) -> Dict[str, Any]:
        spans = self.flatten()
        by_kind: Dict[str, float] = {}
        for s in spans:
            by_kind[s.kind] = by_kind.get(s.kind, 0.0) + s.duration_ms
        return {
            "root": self.root.to_json(),
            "span_count": len(spans),
            "total_ms": round(self.total_ms(), 3),
            "by_kind_ms": {k: round(v, 3) for k, v in sorted(by_kind.items())},
            "slowest": [
                {"name": s.name, "kind": s.kind, "duration_ms": round(s.duration_ms, 3)}
                for s in self.slowest(5)
            ],
            "errors": [
                {"name": s.name, "error": s.error} for s in spans if s.error
            ],
        }


@contextmanager
def null_span() -> Iterator[None]:
    """不需要轨迹时的空实现，避免调用方到处写 if tracer。"""
    yield None
