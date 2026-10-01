"""决策记忆。

这一层不记"聊天历史"——那对分析没有价值。它记的是**结论**：
哪个角色、在什么时候、对哪个标的、给出了什么方向与置信度、当时模型的技能分是多少。

有了这些之后，可以做一件普通对话记忆做不到的事：
**检查结论是不是在反复横跳，以及横跳有没有新证据支撑。**

    同一个角色对同一标的，昨天说 BULLISH、今天说 BEARISH，
    而技能分和波动率都没变——这不是"重新研判"，这是随机输出。
    这种问题在不记录结论的系统里完全无法发现。

结论可以落盘（JSONL），也可以只在内存里活一轮，取决于调用方是否给了路径。
"""

from __future__ import annotations

import json
import threading
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

#: 方向改变但技能分变化小于这个阈值时，视为"没有新证据"。
SKILL_EPSILON = 0.02


@dataclass
class Decision:
    """一次角色结论的记录。"""

    ts_ms: int
    symbol: str
    role_id: str
    role_name: str
    direction: Optional[str]
    confidence: Optional[str]
    skill: Optional[float] = None
    ann_vol_pct: Optional[float] = None
    provider: str = ""
    #: 结论里最关键的一句话，便于人快速回看（不是全文，全文太长）。
    digest: str = ""

    def to_json(self) -> Dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_json(raw: Dict[str, Any]) -> "Decision":
        known = {f for f in Decision.__dataclass_fields__}  # type: ignore[attr-defined]
        return Decision(**{k: v for k, v in raw.items() if k in known})


@dataclass
class ConsistencyReport:
    """同一角色对同一标的的方向一致性。"""

    symbol: str
    role_id: str
    samples: int
    directions: List[str]
    flips: int
    evidence_changed: bool
    message: str

    def to_json(self) -> Dict[str, Any]:
        d = asdict(self)
        return d


class DecisionMemory:
    """结论的滚动记录 + 一致性检查。

    ``limit`` 是内存里的上限，超出后丢最旧的。落盘文件**不做截断**——
    历史结论本身就是有价值的数据，删掉就没法做长期一致性分析了。
    """

    def __init__(self, path: Optional[Path] = None, limit: int = 500) -> None:
        self.path = Path(path) if path else None
        self.limit = max(1, limit)
        self._items: List[Decision] = []
        # 编排层会**并行**跑委员，它们共享一个 DecisionMemory。
        # 没有这把锁的话，两个角色的记录会交错写进同一个 JSONL 文件，
        # 单行损坏会被 `_load` 跳过——也就是说失败是**静默丢数据**。
        self._lock = threading.Lock()
        if self.path and self.path.is_file():
            self._load()

    # ── 读写 ─────────────────────────────────────────────────
    def _load(self) -> None:
        try:
            for line in self.path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    self._items.append(Decision.from_json(json.loads(line)))
                except (json.JSONDecodeError, TypeError):
                    # 单行损坏不该让整份记忆作废——只跳过这一行。
                    continue
        except OSError:
            self._items = []

    def record(self, decision: Decision) -> None:
        with self._lock:
            self._items.append(decision)
            if len(self._items) > self.limit:
                self._items = self._items[-self.limit:]
            if self.path:
                try:
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    with self.path.open("a", encoding="utf-8") as fh:
                        fh.write(json.dumps(decision.to_json(), ensure_ascii=False) + "\n")
                except OSError:
                    # 落盘失败不影响本次分析：记忆是增强项，不是必需品。
                    pass

    # ── 查询 ─────────────────────────────────────────────────
    def all(self) -> List[Decision]:
        return list(self._items)

    def recent(self, n: int = 10, symbol: Optional[str] = None) -> List[Decision]:
        items = [d for d in self._items if symbol is None or d.symbol == symbol]
        return items[-max(0, n):]

    def symbols(self) -> List[str]:
        seen: List[str] = []
        for d in self._items:
            if d.symbol not in seen:
                seen.append(d.symbol)
        return seen

    # ── 一致性 ───────────────────────────────────────────────
    def consistency(self, symbol: str, role_id: str, window: int = 5) -> ConsistencyReport:
        """看某个角色对某个标的最近 ``window`` 次结论是否来回改方向。"""
        items = [d for d in self._items if d.symbol == symbol and d.role_id == role_id][-window:]
        dirs = [d.direction for d in items if d.direction]
        if len(dirs) < 2:
            return ConsistencyReport(
                symbol=symbol, role_id=role_id, samples=len(items), directions=dirs,
                flips=0, evidence_changed=False,
                message=f"样本仅 {len(items)} 次，不足以判断一致性。",
            )

        flips = sum(1 for i in range(1, len(dirs)) if dirs[i] != dirs[i - 1])

        # 证据是否变过：比较首尾两次的技能分与波动率。
        skills = [d.skill for d in items if d.skill is not None]
        vols = [d.ann_vol_pct for d in items if d.ann_vol_pct is not None]
        evidence_changed = False
        if len(skills) >= 2 and abs(skills[-1] - skills[0]) > SKILL_EPSILON:
            evidence_changed = True
        if len(vols) >= 2 and vols[0] not in (0, None) and abs(vols[-1] - vols[0]) / abs(vols[0]) > 0.25:
            evidence_changed = True

        if flips == 0:
            message = f"最近 {len(dirs)} 次结论方向一致（均为 {dirs[-1]}），未观察到横跳。"
        elif evidence_changed:
            message = (f"最近 {len(dirs)} 次中方向改变 {flips} 次，"
                       f"但技能分或波动率同期发生了显著变化，改变有证据支撑。")
        else:
            message = (f"最近 {len(dirs)} 次中方向改变 {flips} 次，"
                       f"而技能分与波动率基本未变——**方向改变缺乏新的证据支撑，"
                       f"应视为输出不稳定而非重新研判**。")

        return ConsistencyReport(
            symbol=symbol, role_id=role_id, samples=len(items),
            directions=dirs, flips=flips, evidence_changed=evidence_changed,
            message=message,
        )

    def consistency_report(self, symbol: str, roles: Sequence[str]) -> List[ConsistencyReport]:
        return [self.consistency(symbol, r) for r in roles]

    def to_json(self) -> Dict[str, Any]:
        return {
            "count": len(self._items),
            "symbols": self.symbols(),
            "recent": [d.to_json() for d in self.recent(10)],
            "persisted": bool(self.path),
            "path": str(self.path) if self.path else None,
        }
