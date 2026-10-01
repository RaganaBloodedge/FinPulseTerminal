"""模型发现 —— 用一把密钥问出"你是谁、你有哪些模型"。

## 为什么要有这个模块

接一个 OpenAI 兼容服务，用户手上通常只有一串 API Key。让他再去填
provider 名、模型名、端点地址，等于把服务商文档里的知识抄进界面里 ——
而其中**模型清单**这件事，服务商自己的 ``GET /v1/models`` 端点就答得出来。

Cherry Studio、Open WebUI、Cline 都是这么做的：填密钥 → 点一下 →
模型列表自己出来，用户从下拉里挑。这里对齐的就是这个交互。

## 一个刻意**不做**的事：跨服务商自动探测

"自动识别你在用哪家"听起来更省事：拿密钥依次去问 OpenAI、DeepSeek、
Moonshot…… 谁答话就是谁。**但这条路会把用户的密钥发给无关的第三方。**
一个 DeepSeek 的密钥被 POST 到 ``api.openai.com``，收到它的公司就看到了
一份本不该给它的凭据 —— 它用不了，但这仍然是一次泄露。

所以这里的规则是硬的：

    密钥只发给**用户自己选定的那一家**。识别靠本地前缀推断，
    推断不出来就让用户在四五个名字里点一下，而不是替他试一遍。

这多花用户一次点击，换掉的是"我的密钥被发给了我不知道的公司"。
:func:`guess_provider` 因此**只读字符串、不联网**；:func:`fetch_models`
一次只问一个端点。

## 失败也要说清楚

拉不到模型列表有五六种原因（密钥错、端点错、这家不实现 /models、
网络不通、返回的不是标准结构），而它们对用户意味着完全不同的下一步。
所以 :func:`fetch_models` 返回的 ``hint`` 必须能直接照做 —— 只说
"获取失败"等于让用户自己猜。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Sequence, Tuple

from finpulse_engine.agent.llm import openai_compat
from finpulse_engine.agent.llm.base import LlmError
from finpulse_engine.agent.llm.registry import SPECS

#: 探测单个端点的超时。比推理短得多 —— 列个模型不该等十几秒，
#: 而且用户正盯着一个转圈的按钮。
DEFAULT_TIMEOUT = 12.0

#: 模型列表里出现这些片段的，几乎都不是能拿来对话的模型。
#: 把它们滤掉，是因为下拉框里混进 ``text-embedding-3-small`` 之后，
#: 用户很可能选中它，然后收到一句"该模型不支持对话"的 400 —— 那次失败
#: 本来可以在列表这一步就避免。
_NON_CHAT_MARKERS: Tuple[str, ...] = (
    "embedding", "embed-", "-embed", "bge-", "rerank", "reranker",
    "whisper", "transcribe", "tts", "speech", "dall-e", "dall·e",
    "stable-diffusion", "flux", "midjourney", "image", "vision-encoder",
    "moderation", "guard", "text-similarity", "clip",
)

#: 排在前面的常见对话模型。排序只影响观感 —— 一屏能看到的就那几条，
#: 把 `deepseek-chat` 排在第 40 位和没排一样。
_CHAT_PRIORITY: Tuple[str, ...] = (
    "deepseek-chat", "deepseek-reasoner", "gpt-4o", "gpt-4o-mini",
    "gpt-4.1", "o3", "o4-mini", "claude-", "moonshot-v1", "kimi-",
    "qwen", "glm-", "llama", "mistral",
)

#: 密钥前缀 → 服务商。**纯本地字符串匹配，不发任何请求。**
#: 值为空串表示"认得出是哪家，但本引擎接不了"。
_PREFIX_RULES: Tuple[Tuple[str, str], ...] = (
    ("sk-proj-", "openai"),
    ("sk-ant-", ""),          # Anthropic，协议不同
    ("sk-or-", "openrouter"),
    ("gsk_", "groq"),
    ("sk-", ""),              # 太通用，认不出具体哪家
)


def guess_provider(api_key: str) -> Dict[str, Any]:
    """按密钥形态**本地**猜服务商。不联网，不发密钥。

    返回 ``{"confident": bool, "provider": str, "candidates": [..], "note": str}``。

    ``confident=False`` 时 ``provider`` 是空串，界面该让用户在
    ``candidates`` 里点一下 —— 这一步是**必须**的：`sk-` 开头的密钥
    在 OpenAI / DeepSeek / Moonshot 之间长得一模一样，猜错了就会把密钥
    发给错的那家。
    """
    key = (api_key or "").strip()
    if not key:
        return {"confident": False, "provider": "", "candidates": [], "note": ""}

    for prefix, provider in _PREFIX_RULES:
        if not key.startswith(prefix):
            continue
        if provider:
            return {
                "confident": True,
                "provider": provider,
                "candidates": [provider],
                "note": f"密钥前缀 {prefix} 对应 {provider}",
            }
        if prefix == "sk-ant-":
            return {
                "confident": False,
                "provider": "",
                "candidates": [],
                "note": "这是 Anthropic 的密钥格式。本引擎走 OpenAI 兼容协议，"
                        "接不了 Anthropic 原生接口；若你用的是中转网关，"
                        "请选「自定义端点」并填网关地址。",
            }
        # 通用 sk- 前缀：常见的那几家都有可能。
        common = ["deepseek", "openai", "moonshot", "dashscope", "openrouter"]
        return {
            "confident": False,
            "provider": "",
            "candidates": common,
            "note": "密钥以 sk- 开头，OpenAI / DeepSeek / Moonshot 等几家的格式"
                    "完全一致，无法从密钥本身分辨。请选一下你用的是哪家 —— "
                    "选错会把密钥发给无关的服务商，所以这一步不做自动猜测。",
        }

    return {
        "confident": False,
        "provider": "",
        "candidates": ["openai", "deepseek", "moonshot", "dashscope", "openrouter"],
        "note": "这个密钥的格式不属于已知的几家，请选择服务商；"
                "若是自建或中转端点，选「自定义端点」并填地址。",
    }


def _is_chat_model(model_id: str) -> bool:
    lowered = model_id.lower()
    return not any(marker in lowered for marker in _NON_CHAT_MARKERS)


def _rank(model_id: str) -> Tuple[int, str]:
    lowered = model_id.lower()
    for index, marker in enumerate(_CHAT_PRIORITY):
        if marker in lowered:
            return (index, lowered)
    return (len(_CHAT_PRIORITY), lowered)


def _http_get_json(url: str, api_key: str, timeout: float) -> Tuple[Optional[Dict], str]:
    """``GET`` 一个 JSON 端点。返回 ``(payload, error)``，两者必有一个为空。

    不用 ``requests``：这里只有一个 GET，多一个第三方依赖换不来什么 ——
    和分析侧的取舍保持一致。
    """
    headers = {"Accept": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")
            parsed = json.loads(body)
            # 各家的错误结构不一样，但都在这几个键里兜圈子。
            err = parsed.get("error") if isinstance(parsed, dict) else None
            if isinstance(err, dict):
                detail = str(err.get("message") or err.get("code") or "")
            elif isinstance(err, str):
                detail = err
            if not detail and isinstance(parsed, dict):
                detail = str(parsed.get("message") or "")
        except Exception:  # noqa: BLE001 - 错误体解析失败不该盖掉原始错误码
            detail = ""
        detail = detail.strip()[:200]
        if exc.code in (401, 403):
            return None, (f"密钥被拒绝（HTTP {exc.code}）"
                          + (f"：{detail}" if detail else "")
                          + "。请确认密钥完整、未过期，且确实属于所选服务商。")
        if exc.code == 404:
            return None, (f"该端点没有 /models 接口（HTTP 404）：{url}\n"
                          "可能是端点地址写错了（多写或少写了一段路径），"
                          "也可能这家服务商不提供模型列表 —— "
                          "那就只能手动填模型名。")
        return None, f"服务端返回 HTTP {exc.code}" + (f"：{detail}" if detail else "")
    except urllib.error.URLError as exc:
        return None, f"连不上 {url}：{exc.reason}。检查网络、代理或端点地址。"
    except TimeoutError:
        return None, f"请求 {url} 超时（{timeout:.0f} 秒）。"

    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        return None, (f"{url} 返回的不是 JSON（前 120 字符：{raw[:120]!r}）。"
                      "这个地址很可能不是 OpenAI 兼容端点。")
    if not isinstance(payload, dict):
        return None, f"{url} 返回的 JSON 顶层不是对象，不符合 OpenAI 兼容格式。"
    return payload, ""


def fetch_models(*, provider: str, api_key: str = "", base_url: str = "",
                 timeout: float = DEFAULT_TIMEOUT) -> Dict[str, Any]:
    """问**一个**端点要模型列表。密钥只发给 ``provider``/``base_url`` 指向的这一处。

    返回的形状是界面直接能用的：

    ``ok`` / ``provider`` / ``provider_name`` / ``base_url`` / ``models``
    （``[{"id", "label"}]``）/ ``total`` / ``filtered``（滤掉几个非对话模型）/
    ``error`` / ``hint``。
    """
    name = (provider or "").strip().lower()
    spec = SPECS.get(name)
    if spec is None:
        return {
            "ok": False, "provider": name, "provider_name": "", "base_url": "",
            "models": [], "total": 0, "filtered": 0,
            "error": f"未知的服务商 '{provider}'",
            "hint": f"可选：{sorted(k for k in SPECS if k != 'rule_based')}",
        }
    if name == "rule_based":
        return {
            "ok": False, "provider": name, "provider_name": spec.name, "base_url": "",
            "models": [], "total": 0, "filtered": 0,
            "error": "规则后端不联网，没有模型列表",
            "hint": "要对话就必须选一个真实的推理服务商。",
        }

    try:
        url = openai_compat.models_url(base_url, name)
    except LlmError as exc:
        return {
            "ok": False, "provider": name, "provider_name": name, "base_url": "",
            "models": [], "total": 0, "filtered": 0,
            "error": str(exc),
            "hint": "这家没有内置端点，请在「自定义端点」里填完整地址，"
                    "例如 http://127.0.0.1:11434/v1",
        }

    payload, error = _http_get_json(url, api_key, timeout)
    if error:
        return {
            "ok": False, "provider": name, "provider_name": name, "base_url": url,
            "models": [], "total": 0, "filtered": 0, "error": error, "hint": "",
        }

    rows = payload.get("data")
    if rows is None:
        rows = payload.get("models")   # 少数网关用这个键
    if not isinstance(rows, list):
        return {
            "ok": False, "provider": name, "provider_name": name, "base_url": url,
            "models": [], "total": 0, "filtered": 0,
            "error": f"{url} 的响应里没有 data[] 数组，不符合 OpenAI 兼容格式",
            "hint": "该端点可能不是标准的 OpenAI 兼容服务。",
        }

    ids: List[str] = []
    for item in rows:
        if isinstance(item, dict):
            value = item.get("id") or item.get("name") or item.get("model")
        elif isinstance(item, str):
            value = item
        else:
            continue
        text = str(value or "").strip()
        if text:
            ids.append(text)
    ids = sorted(set(ids))

    chat_ids = [i for i in ids if _is_chat_model(i)]
    # 过滤后一个不剩时，宁可把原样列表给出去也不要给一个空下拉 ——
    # 用户至少能自己判断哪个能用，而空列表只能得到"这家没模型"的错觉。
    shown = chat_ids or ids
    shown.sort(key=_rank)

    total = len(shown)
    models = [{"id": mid, "label": mid} for mid in shown]

    hint = ""
    if not chat_ids and ids:
        hint = ("这家返回的模型名里没看出对话模型，已原样列出 —— "
                "请自行确认哪个能用于对话。")
    elif not ids:
        hint = "这家返回了空的模型列表。"

    return {
        "ok": True,
        "provider": name,
        "provider_name": spec.name,
        "base_url": url,
        # 上层要拿 URL 去反推用户填的 base_url，这里给的是规整后的完整地址。
        "endpoint": url,
        "models": models,
        "total": total,
        "filtered": len(ids) - len(chat_ids),
        "error": "",
        "hint": hint,
    }


def describe_providers() -> List[Dict[str, Any]]:
    """给界面用的服务商清单：跳过规则后端（它不能对话）。

    界面**不该**自己写一份服务商列表 —— 引擎加了新后端而界面不知道，
    就会出现"命令行能用、界面里选不到"。
    """
    out: List[Dict[str, Any]] = []
    for name, spec in SPECS.items():
        if name == "rule_based":
            continue
        out.append({
            "name": name,
            "description": spec.description,
            "default_base_url": openai_compat.DEFAULT_BASE_URLS.get(name, ""),
            "key_env": spec.default_key_env,
            "needs_key": bool(spec.default_key_env),
        })
    return out
