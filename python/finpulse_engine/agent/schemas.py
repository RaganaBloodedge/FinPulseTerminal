"""结构化输出契约。

智能体的输出是**给程序读的**，不只是给人读的。所以必须能从自由文本里
稳定地抽出三样东西：

1. **段落是否齐**（``output_sections`` 里声明的段，少一段就是没履职）
2. **方向标签**（BULLISH / BEARISH / NEUTRAL）—— 编排层靠它统计票数
3. **置信度**（HIGH / MEDIUM / LOW）—— 主席靠它封顶，风控靠它降级

为什么用"约定段落名 + 正则抽取"而不是要求模型输出 JSON：真实 LLM 在
生成较长分析时，JSON 的引号转义和换行最容易出错，一错就是整份废掉；
而 markdown 段落即使模型写飘了也还能抽出一部分。**容错比严格更重要**。

抽不出来时不编造：``confidence`` 返回 ``None``，由护栏记为问题项。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

#: 方向标签。
DIRECTIONS = ("BULLISH", "BEARISH", "NEUTRAL")

#: 置信度等级，按强弱排序。
CONFIDENCE_LEVELS = ("LOW", "MEDIUM", "HIGH")

_SECTION_RE = re.compile(r"^#{1,4}\s*(.+?)\s*$", re.MULTILINE)
_DIRECTION_RE = re.compile(r"\b(BULLISH|BEARISH|NEUTRAL)\b", re.IGNORECASE)
_CONFIDENCE_RE = re.compile(r"\b(HIGH|MEDIUM|LOW)\b", re.IGNORECASE)

#: 在文本里找数字。刻意把百分号与负号都吃进来，否则 "-2.55%" 会只匹配到 "2.55"，
#: 溯源检查就会把负号丢掉、误判成"这个数字没出现过"。
_NUMBER_RE = re.compile(r"[-+]?\d[\d,]*(?:\.\d+)?%?")


def parse_sections(text: str) -> Dict[str, str]:
    """把 ``## 段名`` 结构的文本切成 ``{段名: 正文}``。

    只认 ``#``~``####`` 开头的行。正文到下一个标题行为止。
    """
    if not text:
        return {}
    out: Dict[str, str] = {}
    matches = list(_SECTION_RE.finditer(text))
    for i, m in enumerate(matches):
        title = m.group(1).strip()
        # 去掉 markdown 的粗体包装：有些模型会写 "## **趋势状态**"。
        title = title.strip("*_ ").strip()
        if not title:
            continue
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        out[title] = text[start:end].strip()
    return out


_BULL_WORDS = ("上升", "上涨", "看多", "买入", "多头", "放量上涨")
_BEAR_WORDS = ("下降", "下跌", "看空", "卖出", "空头", "放量下跌")


def extract_direction(
    text: str,
    sections: Optional[Dict[str, str]] = None,
    order: Optional[Sequence[str]] = None,
) -> Optional[str]:
    """抽方向标签。

    **只在 ``order`` 列出的段落里查找，按顺序取第一次命中。**

    为什么不能全文搜：风险官的「反对意见」段会**引述**别人的方向
    （「技术面分析师：给出 BULLISH」）。全文搜会把这个引述当成风险官
    自己的方向，于是风控角色莫名其妙地"看多了"。

    为什么 ``order`` 也不能等于"全部声明段落"：上一版的 ``order`` 就是
    ``output_sections`` 全量，结果风险官的前三段（下行风险/尾部损失/
    模型可信度）都没有方向标签，第四段「反对意见」里引述的 BULLISH
    就被当成了它自己的方向——第二轮质证之后尤其明显，因为那时它真的
    在引用别人的结论了。**同一个坑换条路又踩一次。**

    所以现在 ``order`` 只传"该角色的结论段"（配置里的 ``direction_sections``，
    默认为第一段）。这也是所有角色提示词都要求"结论放最前面"的原因。
    """
    if not text:
        return None

    if sections and order:
        for name in order:
            body = sections.get(name)
            if not body:
                continue
            m = _DIRECTION_RE.search(body)
            if m:
                return m.group(1).upper()
            # 中文表述兜底：技术面角色习惯写"上升/下降"而不是英文标签。
            bull = sum(body.count(w) for w in _BULL_WORDS)
            bear = sum(body.count(w) for w in _BEAR_WORDS)
            if bull > bear:
                return "BULLISH"
            if bear > bull:
                return "BEARISH"
        return None

    m = _DIRECTION_RE.search(text)
    return m.group(1).upper() if m else None


def extract_confidence(text: str) -> Optional[str]:
    """抽置信度。

    注意 ``LOW`` 是 ``HIGH`` 的子串吗？不是；但 ``MEDIUM`` 与 ``HIGH`` 都可能
    出现在解释文字里（例如「未达 HIGH」）。所以这里优先在「置信度」段落内
    查找，找不到才退回全文查找。
    """
    if not text:
        return None
    sections = parse_sections(text)
    for name, body in sections.items():
        if "置信度" in name or "可信度" in name:
            m = _CONFIDENCE_RE.search(body)
            if m:
                return m.group(1).upper()
    m = _CONFIDENCE_RE.search(text)
    return m.group(1).upper() if m else None


#: 各种"看着像负号"的字符。模型在中文语境里写负数时经常吐 U+2212（数学减号）、
#: 全角减号或连字符 / 短破折号，而正则里的 ``[-+]`` 只认 ASCII。
#:
#: 这个映射必须做在**抽取之前**：``float("−14.2606")`` 直接抛 ValueError，
#: U+2212 也压根不会被当成符号 —— 负号在成为 token 之前就没了，事后用
#: ``lstrip``/``rstrip`` 怎么补都来不及。
_MINUS_LIKE = str.maketrans({
    "\u2212": "-",   # − 数学减号
    "\uff0d": "-",   # － 全角减号
    "\u2013": "-",   # – en dash
    "\u2014": "-",   # — em dash
})


def normalize_signs(text: str) -> str:
    """把非 ASCII 的"减号"归一成 ASCII ``-``。

    不做的代价不是"漏掉一个边角情况"：报告写 "−14.2606"、工具输出里是
    ``-14.2606``，同一个数会被判成"无法溯源"。真跑一次就误报一次，
    等于把这条护栏废掉 —— 这比不检查更糟。
    """
    return (text or "").translate(_MINUS_LIKE)


def extract_numbers(text: str) -> List[str]:
    """抽出文本里出现的所有数字字面量（归一化去掉千分位逗号）。

    用于数字溯源检查：报告里的数字应该能在工具输出里找到出处。
    入口处先做符号归一化，这样 ``number_pool`` / ``numbers_in_text`` /
    护栏三处调用者都自动受益，不需要各自记得处理一次。
    """
    out: List[str] = []
    for raw in _NUMBER_RE.findall(normalize_signs(text)):
        token = raw.replace(",", "").strip()
        if token in ("+", "-", ""):
            continue
        out.append(token)
    return out


def _canonical_number(token: str) -> Optional[float]:
    """把数字字面量归一化成浮点，无法解析的返回 None。

    ``"2.55%"`` 与 ``"2.55"`` 视为同一个数——百分比符号在溯源比较里
    没有意义（工具输出的字段名已经说明了单位是 pct）。
    """
    t = token.rstrip("%").lstrip("+")
    try:
        return float(t)
    except ValueError:
        return None


#: 日期形态。数字溯源必须先把它们挖掉，否则 "2026-09-30" 会被拆成
#: 2026 / 09 / 30 三个"找不到出处的数字"。
_DATE_RE = re.compile(r"\d{4}\s*[-/年]\s*\d{1,2}\s*[-/月]\s*\d{1,2}\s*日?")


def number_pool(obj: Any, *, depth: int = 0) -> set:
    """递归收集一个对象里出现的所有数字（float 集合）。

    逐层取值而不是对 ``str(obj)`` 跑正则，是为了避免把**字段名**里的数字
    也算进池子——``ma20`` 的 20、``autocorr_lag1`` 的 1 都不是观测值，
    把它们收进来会让溯源检查误放行。
    """
    found: set = set()
    if depth > 12:
        return found
    if isinstance(obj, bool):
        return found
    if isinstance(obj, (int, float)):
        found.add(float(obj))
        return found
    if isinstance(obj, str):
        text = _DATE_RE.sub(" ", obj)
        for token in extract_numbers(text):
            value = _canonical_number(token)
            if value is not None:
                found.add(value)
        return found
    if isinstance(obj, dict):
        for value in obj.values():
            found |= number_pool(value, depth=depth + 1)
        return found
    if isinstance(obj, (list, tuple)):
        for value in obj:
            found |= number_pool(value, depth=depth + 1)
        return found
    return found


def numbers_in_text(text: str) -> List[float]:
    """抽正文里的数字，先剥掉日期。"""
    cleaned = _DATE_RE.sub(" ", text or "")
    out: List[float] = []
    for token in extract_numbers(cleaned):
        value = _canonical_number(token)
        if value is not None:
            out.append(value)
    return out


def decimals_of(token: str) -> int:
    """这个字面量写了几位小数。用于"模型把 4 位截成 2 位"的容差判断。"""
    t = token.rstrip("%").lstrip("+-")
    if "." not in t:
        return 0
    return len(t.split(".", 1)[1])


@dataclass
class OutputCheck:
    """一次输出的体检结果。"""

    text: str = ""
    sections: Dict[str, str] = field(default_factory=dict)
    missing_sections: List[str] = field(default_factory=list)
    direction: Optional[str] = None
    confidence: Optional[str] = None
    empty_sections: List[str] = field(default_factory=list)
    #: 这次体检里**允许**承载方向标签的段落。留档是为了事后能复盘
    #: "这个方向是从哪个段抽出来的"——抽错段是这套东西最容易翻的车。
    direction_sections: List[str] = field(default_factory=list)

    @property
    def complete(self) -> bool:
        return not self.missing_sections and not self.empty_sections

    def to_json(self) -> Dict[str, Any]:
        return {
            "complete": self.complete,
            "sections_found": list(self.sections),
            "missing_sections": list(self.missing_sections),
            "empty_sections": list(self.empty_sections),
            "direction": self.direction,
            "direction_from": list(self.direction_sections),
            "confidence": self.confidence,
            "chars": len(self.text),
        }


def check_output(
    text: str,
    expected_sections: Sequence[str],
    direction_sections: Optional[Sequence[str]] = None,
) -> OutputCheck:
    """按声明的段落列表检查输出。

    ``direction_sections`` 是**允许承载本角色方向标签**的段落，默认取
    第一段（结论段）。只有这些段里的标签才算角色自己的方向——其余段落
    可能在引述别人的方向，见 :func:`extract_direction`。
    """
    sections = parse_sections(text or "")
    missing = [s for s in expected_sections if s not in sections]
    empty = [s for s in expected_sections
             if s in sections and len(sections[s].strip()) < 8]
    eligible = list(direction_sections) if direction_sections is not None \
        else list(expected_sections[:1])
    return OutputCheck(
        text=text or "",
        sections=sections,
        missing_sections=missing,
        empty_sections=empty,
        direction=extract_direction(text, sections, eligible),
        confidence=extract_confidence(sections.get("置信度", "") or text),
        direction_sections=eligible,
    )


def direction_from_check(check: OutputCheck, *, role_category: str = "") -> Optional[str]:
    """从体检结果里定方向。

    方向已在 :func:`check_output` 里按"声明顺序只看结论段"抽好，
    这里只是取出来。保留这个函数是为了让调用点读起来明确：
    "我拿到的是方向，不是原始文本"。
    """
    return check.direction if check.direction in DIRECTIONS else None
