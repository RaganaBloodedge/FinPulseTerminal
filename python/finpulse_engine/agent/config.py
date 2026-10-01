"""角色配置 —— 声明式定义分析角色。

设计取自 Fincept 的 ``finagent_core/config_loader.py``：**加一个分析角色 =
加一个 JSON 文件，不改一行代码**。

为什么这件事重要：

* 如果角色写死在 Python 类里，每加一个角色都要动代码、加测试、重新发布；
  写成配置之后，角色是**数据**，可以被校验、被列出、被热替换。
* 配置容易写错。所以这里的校验比"读个 JSON"严格得多：缺字段要报出
  是哪个文件缺了哪个字段、在 config 的哪一层；类型不对要报出期望什么、
  拿到了什么。**静默用默认值把错误盖过去，是配置驱动设计最容易踩的坑。**
* 未知字段一律报错而不是忽略。写错 ``instruction``（少个 s）时，
  如果静默忽略，你会得到一个用默认提示词的分析师，而且完全不报错。

这是本项目里唯一"配置即接口"的地方，所以校验写得比别处啰嗦，是刻意的。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


class ConfigError(ValueError):
    """配置不合法。消息里一定带上文件与字段路径，便于直接定位。"""


# ── 各字段的合法取值 ──────────────────────────────────────────────

#: 输出契约名。角色必须声明用哪一种结构化输出，编排层才能统一解析与校验。
KNOWN_SCHEMAS = ("market_view", "risk_assessment", "trade_signal")

#: 角色的分析域。用于编排时决定谁先发言、谁能反驳谁。
KNOWN_CATEGORIES = ("technical", "risk", "sentiment", "aggregate")


def _require(raw: Dict[str, Any], key: str, where: str) -> Any:
    if key not in raw:
        raise ConfigError(f"{where}: 缺少必填字段 '{key}'")
    return raw[key]


def _as_str(raw: Dict[str, Any], key: str, where: str, default: Optional[str] = None) -> str:
    if key not in raw:
        if default is None:
            raise ConfigError(f"{where}: 缺少必填字段 '{key}'")
        return default
    value = raw[key]
    if not isinstance(value, str):
        raise ConfigError(f"{where}.{key}: 期望字符串，拿到 {type(value).__name__}")
    if not value.strip():
        raise ConfigError(f"{where}.{key}: 不能为空字符串")
    return value


def _as_float(raw: Dict[str, Any], key: str, where: str, default: float) -> float:
    if key not in raw:
        return default
    value = raw[key]
    # bool 是 int 的子类，这里必须显式排掉，否则 True 会被当成 1.0 悄悄通过。
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{where}.{key}: 期望数字，拿到 {type(value).__name__}")
    return float(value)


def _as_int(raw: Dict[str, Any], key: str, where: str, default: int) -> int:
    if key not in raw:
        return default
    value = raw[key]
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{where}.{key}: 期望整数，拿到 {type(value).__name__}")
    return value


def _as_str_list(raw: Dict[str, Any], key: str, where: str) -> List[str]:
    if key not in raw:
        return []
    value = raw[key]
    if not isinstance(value, list):
        raise ConfigError(f"{where}.{key}: 期望字符串数组，拿到 {type(value).__name__}")
    out: List[str] = []
    for i, item in enumerate(value):
        if not isinstance(item, str):
            raise ConfigError(f"{where}.{key}[{i}]: 期望字符串，拿到 {type(item).__name__}")
        out.append(item)
    return out


# ── 配置对象 ──────────────────────────────────────────────────────


@dataclass(frozen=True)
class ModelConfig:
    """一个角色用哪个模型、怎么采样。

    ``provider`` 默认 ``rule_based``：没配 API key 时也能跑。
    这不是"假 LLM"，而是一个**基于真实指标的规则后端**——它读同样的
    工具输出、产出同样结构的研判，只是不做自然语言推理。CI 和离线演示
    靠它；配上 key 换成真 LLM，输出契约完全一致。
    """

    provider: str = "rule_based"
    model_id: str = ""
    temperature: float = 0.3
    max_tokens: int = 2048
    base_url: str = ""

    def to_json(self) -> Dict[str, Any]:
        d: Dict[str, Any] = {"provider": self.provider, "temperature": self.temperature,
                             "max_tokens": self.max_tokens}
        if self.model_id:
            d["model_id"] = self.model_id
        if self.base_url:
            d["base_url"] = self.base_url
        return d


@dataclass(frozen=True)
class RoleConfig:
    """一个分析角色的完整定义。对应一个 JSON 文件。"""

    id: str
    name: str
    description: str
    category: str
    version: str
    capabilities: List[str]
    instructions: str
    tools: List[str]
    output_schema: str
    output_sections: List[str]
    model: ModelConfig
    memory: bool = False
    reasoning: bool = False
    max_tool_calls: int = 6
    #: 允许承载**本角色自己**方向标签的段落。留空表示"只有第一段（结论段）"。
    #: 不能简单等于 output_sections：像风险官那样第一段讲下行风险、后面
    #: 「反对意见」段会引述别人的方向，全量扫描会把引述当成它自己的立场。
    direction_sections: List[str] = field(default_factory=list)
    source_path: Optional[str] = None

    @property
    def direction_scope(self) -> List[str]:
        """真正传给 :func:`extract_direction` 的段落列表。"""
        return list(self.direction_sections) or list(self.output_sections[:1])

    def to_json(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "category": self.category,
            "version": self.version,
            "capabilities": list(self.capabilities),
            "tools": list(self.tools),
            "output_schema": self.output_schema,
            "output_sections": list(self.output_sections),
            "direction_sections": self.direction_scope,
            "model": self.model.to_json(),
            "memory": self.memory,
            "reasoning": self.reasoning,
            "max_tool_calls": self.max_tool_calls,
            # instructions 在列表接口里默认不回传（可能很长），单独用 get 取。
            "instructions_chars": len(self.instructions),
        }


# ── 解析 ──────────────────────────────────────────────────────────

#: 允许出现在顶层与 config 段的字段名。多出来的直接报错。
_TOP_KEYS = frozenset({
    "id", "name", "description", "category", "version", "capabilities", "config",
})
_CONFIG_KEYS = frozenset({
    "model", "instructions", "tools", "output_schema", "output_sections",
    "direction_sections", "memory", "reasoning", "max_tool_calls",
})
_MODEL_KEYS = frozenset({"provider", "model_id", "temperature", "max_tokens", "base_url"})


def parse_config(raw: Any, source: str = "<dict>") -> RoleConfig:
    """把一份已解析的 JSON 校验并转成 :class:`RoleConfig`。

    ``source`` 只用于错误消息，通常是文件路径。
    """
    if not isinstance(raw, dict):
        raise ConfigError(f"{source}: 顶层必须是对象，拿到 {type(raw).__name__}")

    unknown = set(raw) - _TOP_KEYS
    if unknown:
        raise ConfigError(f"{source}: 未知字段 {sorted(unknown)}；允许的字段是 {sorted(_TOP_KEYS)}")

    role_id = _as_str(raw, "id", source)
    if not role_id.replace("-", "").replace("_", "").isalnum():
        raise ConfigError(f"{source}.id: '{role_id}' 只能含字母数字与 - _")

    category = _as_str(raw, "category", source)
    if category not in KNOWN_CATEGORIES:
        raise ConfigError(
            f"{source}.category: '{category}' 不是合法分析域；"
            f"可选 {list(KNOWN_CATEGORIES)}"
        )

    cfg_raw = _require(raw, "config", source)
    if not isinstance(cfg_raw, dict):
        raise ConfigError(f"{source}.config: 期望对象，拿到 {type(cfg_raw).__name__}")

    unknown_cfg = set(cfg_raw) - _CONFIG_KEYS
    if unknown_cfg:
        raise ConfigError(
            f"{source}.config: 未知字段 {sorted(unknown_cfg)}；"
            f"允许的字段是 {sorted(_CONFIG_KEYS)}"
        )

    where = f"{source}.config"

    instructions = _as_str(cfg_raw, "instructions", where)
    if len(instructions) < 80:
        raise ConfigError(
            f"{where}.instructions: 只有 {len(instructions)} 字符，太短了。"
            "一份能用的人格至少要说清 职责 / 工作流 / 输出格式 / 禁止事项。"
        )

    output_schema = _as_str(cfg_raw, "output_schema", where, default="market_view")
    if output_schema not in KNOWN_SCHEMAS:
        raise ConfigError(
            f"{where}.output_schema: '{output_schema}' 未知；可选 {list(KNOWN_SCHEMAS)}"
        )

    # 输出段落得声明出来，而不是让下游去提示词里正则匹配 '## xxx'。
    # 提示词是给人看的，段落名是给程序用的，两者都改成本很低、同步却很容易漏。
    output_sections = _as_str_list(cfg_raw, "output_sections", where)
    if not output_sections:
        raise ConfigError(
            f"{where}.output_sections: 不能为空。规则后端要按这个列表渲染，"
            "护栏也要按它校验输出是否完整。"
        )
    dup = {s for s in output_sections if output_sections.count(s) > 1}
    if dup:
        raise ConfigError(f"{where}.output_sections: 段名重复 {sorted(dup)}")
    if "置信度" not in output_sections and output_schema != "risk_assessment":
        # 置信度是决策链上最关键的一个字段：没有它，下游无法区分
        # "看多但只有 30% 把握" 和 "看多有 90% 把握"。
        raise ConfigError(f"{where}.output_sections: 必须包含 '置信度' 段")

    # 方向标签的合法来源段落。留空 = 只有第一段。声明了就必须是本角色自己的段落，
    # 否则等于允许从别人的话里抄一个方向。
    direction_sections = _as_str_list(cfg_raw, "direction_sections", where)
    stray = [s for s in direction_sections if s not in output_sections]
    if stray:
        raise ConfigError(
            f"{where}.direction_sections: {stray} 不在 output_sections 里。"
            "方向标签只能从本角色自己声明的段落里抽——抽到别处就成了引述别人的结论。"
        )

    model_raw = _require(cfg_raw, "model", where)
    if not isinstance(model_raw, dict):
        raise ConfigError(f"{where}.model: 期望对象，拿到 {type(model_raw).__name__}")
    unknown_model = set(model_raw) - _MODEL_KEYS
    if unknown_model:
        raise ConfigError(
            f"{where}.model: 未知字段 {sorted(unknown_model)}；"
            f"允许的字段是 {sorted(_MODEL_KEYS)}"
        )

    mwhere = f"{where}.model"
    model = ModelConfig(
        provider=_as_str(model_raw, "provider", mwhere, default="rule_based"),
        model_id=_as_str(model_raw, "model_id", mwhere, default=""),
        temperature=_as_float(model_raw, "temperature", mwhere, 0.3),
        max_tokens=_as_int(model_raw, "max_tokens", mwhere, 2048),
        base_url=_as_str(model_raw, "base_url", mwhere, default=""),
    )
    if not 0.0 <= model.temperature <= 2.0:
        raise ConfigError(f"{mwhere}.temperature: {model.temperature} 超出 [0, 2]")
    if model.max_tokens < 128:
        raise ConfigError(f"{mwhere}.max_tokens: {model.max_tokens} 太小，至少 128")
    # 非默认 provider 必须给 model_id —— 否则请求发出去只会拿到 400。
    if model.provider != "rule_based" and not model.model_id:
        raise ConfigError(f"{mwhere}.model_id: provider='{model.provider}' 时必须指定 model_id")

    max_tool_calls = _as_int(cfg_raw, "max_tool_calls", where, 6)
    if not 0 <= max_tool_calls <= 32:
        raise ConfigError(f"{where}.max_tool_calls: {max_tool_calls} 超出 [0, 32]")

    memory = bool(cfg_raw.get("memory", False))
    if "memory" in cfg_raw and not isinstance(cfg_raw["memory"], bool):
        raise ConfigError(f"{where}.memory: 期望布尔，拿到 {type(cfg_raw['memory']).__name__}")

    reasoning = bool(cfg_raw.get("reasoning", False))
    if "reasoning" in cfg_raw and not isinstance(cfg_raw["reasoning"], bool):
        raise ConfigError(f"{where}.reasoning: 期望布尔，拿到 {type(cfg_raw['reasoning']).__name__}")

    return RoleConfig(
        id=role_id,
        name=_as_str(raw, "name", source),
        description=_as_str(raw, "description", source),
        category=category,
        version=_as_str(raw, "version", source, default="1.0.0"),
        capabilities=_as_str_list(raw, "capabilities", source),
        instructions=instructions,
        tools=_as_str_list(cfg_raw, "tools", where),
        output_schema=output_schema,
        output_sections=output_sections,
        direction_sections=direction_sections,
        model=model,
        memory=memory,
        reasoning=reasoning,
        max_tool_calls=max_tool_calls,
        source_path=source if source != "<dict>" else None,
    )


def load_config(path: Path) -> RoleConfig:
    """读并校验单个角色配置文件。"""
    path = Path(path)
    if not path.is_file():
        raise ConfigError(f"配置文件不存在: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        # json 的报错本身带行列号，原样带上，别丢掉这个信息。
        raise ConfigError(f"{path}: JSON 解析失败 —— {exc}") from exc
    return parse_config(raw, source=str(path))


def load_all(config_dir: Optional[Path] = None) -> Dict[str, RoleConfig]:
    """加载目录下所有角色，按 id 建索引。

    重复 id 会报错而不是后者覆盖前者 —— 覆盖意味着"加了个文件但角色没变"，
    这种问题不报出来会很难查。
    """
    if config_dir is None:
        config_dir = Path(__file__).parent / "configs"
    config_dir = Path(config_dir)
    if not config_dir.is_dir():
        raise ConfigError(f"角色配置目录不存在: {config_dir}")

    out: Dict[str, RoleConfig] = {}
    for path in sorted(config_dir.glob("*.json")):
        cfg = load_config(path)
        if cfg.id in out:
            raise ConfigError(
                f"角色 id 重复: '{cfg.id}' 同时出现在 "
                f"{out[cfg.id].source_path} 和 {path}"
            )
        out[cfg.id] = cfg
    if not out:
        raise ConfigError(f"{config_dir} 下没有任何角色配置（*.json）")
    return out


def select_roles(configs: Dict[str, RoleConfig], ids: Iterable[str]) -> List[RoleConfig]:
    """按 id 取角色，缺哪个就报哪个。"""
    out: List[RoleConfig] = []
    for role_id in ids:
        if role_id not in configs:
            raise ConfigError(
                f"没有名为 '{role_id}' 的角色；可选: {sorted(configs)}"
            )
        out.append(configs[role_id])
    return out


# ── 投委会（panel）配置 ───────────────────────────────────────────
#
# 角色是数据，那么"哪几个角色开会、各占多少票、谁主持"也应当是数据。
# 这几项如果写死在编排代码里，换一套委员就得改代码——那前面把角色做成
# JSON 的努力就白费了一半。


@dataclass(frozen=True)
class PanelMember:
    """一个参会委员。"""

    role_id: str
    #: 计票权重。Fincept 的 IC 成员也配了这个字段，但它的计票代码收了
    #: 参数却从不读取（等权计数）。这里是真的用它。
    weight: float = 1.0
    #: 该委员是否参与第二轮交叉质证。风控类角色通常不需要——它的职责
    #: 是评估风险，不是对方向表态。
    cross_examine: bool = True

    def to_json(self) -> Dict[str, Any]:
        # 键名统一用 ``role`` —— 与配置 JSON 里人写的那份**同名**。
        #
        # 之前这里发的是 ``role_id``（Python 侧的属性名），而配置文件里写的是
        # ``role``。同一个东西两个名字跨越语言边界，C++ 侧照配置文件的名字解析，
        # 于是 panel 解析出来 0 个委员、界面上一片空白。C++ 的集成测试当场抓到了，
        # 但这类问题本不该有机会出现：**一份契约只有一个名字**。
        return {"role": self.role_id, "weight": self.weight,
                "cross_examine": self.cross_examine}


@dataclass(frozen=True)
class PanelConfig:
    """一套投委会编排。"""

    id: str
    name: str
    description: str
    chair: str
    members: List[PanelMember]
    #: 最少要有几个委员成功产出结论，会议才有效。达不到就不出决议——
    #: 「所有人都答不出来」时仍然给一个 REJECT，是把系统性故障伪装成
    #: 一个决策意见。Fincept 的 IC 没有这个校验。
    quorum: int = 2
    #: 交叉质证的轮数。1 = 各委员独立作答（Fincept 的做法）；2 = 加一轮
    #: 互相质证。默认 2，因为「辩论」这个词应当有实际内容。
    rounds: int = 2
    source_path: Optional[str] = None

    @property
    def role_ids(self) -> List[str]:
        return [m.role_id for m in self.members]

    def weight_of(self, role_id: str) -> float:
        for m in self.members:
            if m.role_id == role_id:
                return m.weight
        return 1.0

    def to_json(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "chair": self.chair,
            "members": [m.to_json() for m in self.members],
            "quorum": self.quorum,
            "rounds": self.rounds,
        }


_PANEL_KEYS = frozenset({
    "id", "name", "description", "chair", "members", "quorum", "rounds",
})
_MEMBER_KEYS = frozenset({"role", "weight", "cross_examine"})


def parse_panel(raw: Any, source: str = "<dict>") -> PanelConfig:
    """校验并转换一份投委会配置。"""
    if not isinstance(raw, dict):
        raise ConfigError(f"{source}: 顶层必须是对象，拿到 {type(raw).__name__}")
    unknown = set(raw) - _PANEL_KEYS
    if unknown:
        raise ConfigError(
            f"{source}: 未知字段 {sorted(unknown)}；允许 {sorted(_PANEL_KEYS)}"
        )

    panel_id = _as_str(raw, "id", source)
    chair = _as_str(raw, "chair", source)

    members_raw = _require(raw, "members", source)
    if not isinstance(members_raw, list):
        raise ConfigError(f"{source}.members: 期望数组，拿到 {type(members_raw).__name__}")
    if not members_raw:
        raise ConfigError(f"{source}.members: 投委会至少要有一个委员")

    members: List[PanelMember] = []
    seen: List[str] = []
    for i, item in enumerate(members_raw):
        where = f"{source}.members[{i}]"
        if not isinstance(item, dict):
            raise ConfigError(f"{where}: 期望对象，拿到 {type(item).__name__}")
        bad = set(item) - _MEMBER_KEYS
        if bad:
            raise ConfigError(f"{where}: 未知字段 {sorted(bad)}；允许 {sorted(_MEMBER_KEYS)}")
        role_id = _as_str(item, "role", where)
        if role_id in seen:
            # 同一个角色重复参会会让计票变得没法解释（是投两票还是写重了？）。
            raise ConfigError(f"{where}: 角色 '{role_id}' 重复出现")
        seen.append(role_id)
        weight = _as_float(item, "weight", where, 1.0)
        if weight <= 0:
            raise ConfigError(f"{where}.weight: 必须为正，拿到 {weight}")
        cross = item.get("cross_examine", True)
        if not isinstance(cross, bool):
            raise ConfigError(f"{where}.cross_examine: 期望布尔，拿到 {type(cross).__name__}")
        members.append(PanelMember(role_id, weight, cross))

    if chair in seen:
        raise ConfigError(
            f"{source}: 主席 '{chair}' 不能同时是委员——"
            f"它是综合各委员意见的角色，参与计票会让'自己统计自己的票'成立"
        )

    quorum = _as_int(raw, "quorum", source, 2)
    if not 1 <= quorum <= len(members):
        raise ConfigError(
            f"{source}.quorum: {quorum} 应在 1..{len(members)} 之间（委员共 {len(members)} 名）"
        )

    rounds = _as_int(raw, "rounds", source, 2)
    if not 1 <= rounds <= 4:
        raise ConfigError(f"{source}.rounds: {rounds} 应在 1..4 之间")

    return PanelConfig(
        id=panel_id,
        name=_as_str(raw, "name", source),
        description=_as_str(raw, "description", source, default=""),
        chair=chair,
        members=members,
        quorum=quorum,
        rounds=rounds,
        source_path=source if source != "<dict>" else None,
    )


def load_panels(panel_dir: Optional[Path] = None) -> Dict[str, PanelConfig]:
    """加载 ``configs/panels/*.json``。目录不存在时返回空字典（不是错误）。

    投委会是可选编排——只用单角色分析的人不该被一个缺失的目录挡住。
    """
    if panel_dir is None:
        panel_dir = Path(__file__).parent / "configs" / "panels"
    panel_dir = Path(panel_dir)
    if not panel_dir.is_dir():
        return {}

    out: Dict[str, PanelConfig] = {}
    for path in sorted(panel_dir.glob("*.json")):
        raw = json.loads(path.read_text(encoding="utf-8"))
        panel = parse_panel(raw, source=str(path))
        if panel.id in out:
            raise ConfigError(f"投委会 id 重复: '{panel.id}' （{path}）")
        out[panel.id] = panel
    return out


#: 校验问题的两个级别。
#: ``error`` 表示这套投委会**跑不起来**（引用了不存在的角色）；
#: ``warning`` 表示能跑但搭配可疑（职责错位、委员人数过少）。
PANEL_ERROR = "error"
PANEL_WARNING = "warning"


def validate_panel(
    panel: PanelConfig, roles: Dict[str, RoleConfig]
) -> List[tuple]:
    """检查一套投委会引用的角色是否都存在、职责是否搭配得当。

    返回 ``[(级别, 说明)]``，级别取 :data:`PANEL_ERROR` / :data:`PANEL_WARNING`。
    **不抛异常**——由调用方决定是拒绝还是仅告警：引用了还没写的角色在开发期
    是正常状态，把这种情况直接变成异常会让"先把配置写好再补角色"没法做。
    """
    problems: List[tuple] = []

    if panel.chair not in roles:
        problems.append((PANEL_ERROR,
                         f"主席角色 '{panel.chair}' 不存在；可选 {sorted(roles)}"))
    elif roles[panel.chair].category != "aggregate":
        problems.append((PANEL_WARNING,
                         f"主席角色 '{panel.chair}' 的 category 是 "
                         f"'{roles[panel.chair].category}'，期望 'aggregate'——"
                         f"非综合类角色当主席时，它的置信度算法与主席职责不匹配"))

    for member in panel.members:
        if member.role_id not in roles:
            problems.append((PANEL_ERROR, f"委员角色 '{member.role_id}' 不存在"))
        elif roles[member.role_id].category == "aggregate":
            problems.append((PANEL_WARNING,
                             f"委员 '{member.role_id}' 的 category 是 'aggregate'——"
                             f"综合类角色应当做主席，参与计票会自己统计自己"))

    # 委员全部不产出方向的话，这场会开出来只会得到一个 NEUTRAL。
    # 这是合法配置（纯风险评审会），但值得提醒。
    directional = [
        m for m in panel.members
        if m.role_id in roles and roles[m.role_id].category != "risk"
    ]
    if not directional:
        problems.append((PANEL_WARNING,
                         "没有任何委员是方向性角色（category 均为 risk）——"
                         "这场会将只会得到 NEUTRAL，方向由谁给？"))

    return problems
