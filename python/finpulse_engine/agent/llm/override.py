"""LLM 运行时覆盖。

用来做到"**不改一行 JSON、不设环境变量**，也能把某一场研判接到真实大模型上"。
命令行是这个::

    finpulse-cli --agent --provider deepseek --api-key sk-xxx \\
                 --model deepseek-chat --base-url https://api.deepseek.com/v1

**为什么打包成一个对象，而不是四个散参数。**

覆盖要从 RPC 层一路穿到后端构造处，中间经过
``api → orchestrator（4 个方法 + 2 个内部方法）→ roles → registry``。
四个散参数意味着每一层都要多四个形参、四次透传；将来加第五个（比如超时、
``top_p``）就要把这条链再改一遍。而漏改一层的后果不是报错，是**参数在某一层
被悄悄丢掉** —— 表现是"我明明填了 key，它却说没有"，且无从查起。
一个对象只穿一个参数，漏不掉。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class LlmOverride:
    """本次运行对 LLM 配置的覆盖。字段为空表示"这一项不覆盖"。"""

    provider: str = ""
    model_id: str = ""
    base_url: str = ""
    api_key: str = ""

    def any(self) -> bool:
        """是否有任何一项真的被指定了。"""
        return bool(self.provider or self.model_id or self.base_url or self.api_key)

    def describe(self) -> Dict[str, Any]:
        """给日志和界面看。**绝不回传密钥本身，连片段都不回传。**

        在界面上回显密钥是很多工具的默认做法，但那意味着密钥会出现在
        截图、录屏和工单里。这里只回一个布尔值：够用来确认"填进去了"，
        不够用来泄露。
        """
        return {
            "provider": self.provider,
            "model_id": self.model_id,
            "base_url": self.base_url,
            "has_api_key": bool(self.api_key),
        }


def from_params(params: Dict[str, Any]) -> Optional[LlmOverride]:
    """从 RPC 参数里取覆盖项。全空时返回 None，让调用方走"照配置来"。"""
    ov = LlmOverride(
        provider=str(params.get("provider") or "").strip(),
        model_id=str(params.get("model") or "").strip(),
        base_url=str(params.get("base_url") or "").strip(),
        api_key=str(params.get("api_key") or "").strip(),
    )
    return ov if ov.any() else None
