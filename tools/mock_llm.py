"""本地假 LLM 服务 —— 离线验证"接上大模型"的完整链路。

## 它解决什么问题

接真 LLM 之前，"多 agent 到底跑没跑通"这件事只能靠读代码推断；接上之后
每验一次要花真金白银的 token，还受网络和服务商状态影响。这个脚本起一个
OpenAI 兼容的假端点，把**从提示词到决议**的整条链路跑一遍而不联网。

## 与 python/tests/test_agent_llm_path.py 的分工

那份测试是**自动回归**，覆盖 Python 层：工具循环、交叉质证、主席收到
全部委员结论、token 记账。它不进 CLI/GUI。

本脚本用来**手动观察真实端到端** —— 起它，再跑真的 ``finpulse-cli`` 或
``finpulse-gui``，就能看到 C++ 壳 + Python 引擎 + 假模型串起来的样子，
包括每个角色实际发的提示词。改提示词、调编排时用它比烧真 token 划算。

## 用法

    python3 tools/mock_llm.py 18812 &          # 起在 18812

    # CLI：跑一场完整的投委会
    build/finpulse-cli --agent --panel default_committee \\
        --provider openai_compat --base-url http://127.0.0.1:18812 \\
        --model mock-chat --api-key sk-test \\
        --source tushare --symbol 600519.SH --bars 250

    # 想知道每个角色到底收到了什么提示词
    FINPULSE_MOCK_DUMP=1 python3 tools/mock_llm.py 18812
    # 每个请求的 user 消息会追加到 /tmp/mock_reqs.jsonl

    # GUI：在「设置…」里选 openai_compat、填端点 http://127.0.0.1:18812、
    #      密钥随便、点「测试并获取模型」→ 下拉里会出现 mock-chat

    # 让假模型去点带点号的远程工具（压测工具名翻译那条路）
    FINPULSE_MOCK_TOOL=last python3 tools/mock_llm.py 18812

    # 让委员在交叉质证轮"写不完"（复现 max_tokens 截断那条路）
    FINPULSE_MOCK_TRUNCATE=peers python3 tools/mock_llm.py 18812

    # 让主席在报告里引用运行参数（复现"数字溯源误报"那条路）
    FINPULSE_MOCK_CITE=params python3 tools/mock_llm.py 18812

模型行为（刻意做得可预测，便于断言）：第一次请求要一个工具调用，
拿到工具结果之后再给出按角色 ``output_sections`` 拼成的报告正文。

## 它会像真服务商一样校验工具名

DeepSeek 对 ``function.name`` 的字符集是硬校验（``[a-zA-Z0-9_-]``），
不合规直接 400、整次研判作废。假端点如果对此睁一只眼，离线就永远发现
不了"名字发不出去"这类故障 —— 而它真的发生过一次。所以这里**照抄**：
名字不合规就回 400，错误体与线上同形。见 :func:`illegal_tool_name`。
"""

from __future__ import annotations

import json
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

#: 模拟的非对话模型 —— 用来验证「过滤掉 embedding / 语音」那条路径。
MODELS = [
    {"id": "mock-chat"},
    {"id": "text-embedding-3-small"},
]

DUMP = bool(os.environ.get("FINPULSE_MOCK_DUMP"))
DUMP_PATH = "/tmp/mock_reqs.jsonl"

#: 第一次请求去点哪个工具：``first``（默认，本地工具）或 ``last``（远程工具，
#: 名字里带点号）。后者用来手动复现"工具名发不出去"那条路。
PICK = (os.environ.get("FINPULSE_MOCK_TOOL") or "first").strip().lower()

#: 让委员的**交叉质证轮**写不完：只交出第一段，并把 finish_reason 置成
#: ``length``。``FINPULSE_MOCK_TRUNCATE=peers`` 开启。
#:
#: 手动复现"第二轮被 max_tokens 截断"用的 —— 那条路的正确行为**不是**
#: 整场作废，而是沿用委员第一轮的立场。主席不受影响（用「决议」段区分）。
TRUNCATE = (os.environ.get("FINPULSE_MOCK_TRUNCATE") or "").strip().lower()

#: 让**主席**在报告里引用本次运行的参数（"本次最小训练样本 60"）。
#: ``FINPULSE_MOCK_CITE=params`` 开启。
#:
#: 真实模型几乎总会写这么一句，而这些数字**不在主席自己的工具输出里**
#: （它的配置只有 stats 一个工具），只在任务提示词里。手动复现"数字溯源
#: 误报"用的 —— 那条路的正确行为是"提示词里的数字也算出处"，不是报警。
CITE = (os.environ.get("FINPULSE_MOCK_CITE") or "").strip().lower()

#: 服务端对 function.name 的字符集要求，与 DeepSeek 报错里的正则一致。
SERVER_NAME_OK = re.compile(r"^[a-zA-Z0-9_-]+$")

#: ``role='tool'`` 的消息允许出现的字段（OpenAI 类型定义 = DeepSeek 示例）。
TOOL_MESSAGE_FIELDS = frozenset({"role", "content", "tool_call_id"})


def illegal_tool_name(body: dict):
    """挑出第一个非法工具名，返回 ``(位置, 名字)``；全都合法则 None。

    查两处：发出去的工具目录、以及 assistant 消息里回填的历史 tool_calls。
    只查第一处会让"发得出去、第二轮又带上原名"这种半修复蒙混过关。
    """
    for i, tool in enumerate(body.get("tools") or []):
        name = ((tool or {}).get("function") or {}).get("name") or ""
        if not SERVER_NAME_OK.match(name):
            return (f"tools[{i}].function.name", name)
    for i, msg in enumerate(body.get("messages") or []):
        for j, call in enumerate((msg or {}).get("tool_calls") or []):
            name = ((call or {}).get("function") or {}).get("name") or ""
            if not SERVER_NAME_OK.match(name):
                return (f"messages[{i}].tool_calls[{j}].function.name", name)
    return None


def unexpected_tool_field(body: dict):
    """tool 消息里出现了协议没定义的字段，返回 ``(位置, 字段名)``。

    多送字段是赌服务端的宽容度：赢了没收益，输了整次研判作废。
    """
    for i, msg in enumerate(body.get("messages") or []):
        if (msg or {}).get("role") != "tool":
            continue
        extra = sorted(set(msg) - TOOL_MESSAGE_FIELDS)
        if extra:
            return (f"messages[{i}].{extra[0]}", ", ".join(extra))
    return None


def _sections_of(system_text: str) -> list[str]:
    """段落名直接从系统提示词里读 —— 角色配置的 instructions 写了 "## 段落名"。

    这样角色配置一改，假模型跟着变，不需要在这里再抄一份段落清单。
    """
    return re.findall(r"^##\s*(.+?)\s*$", system_text or "", re.M)


def _build_report(system_text: str, user_text: str = "") -> str:
    secs = _sections_of(system_text) or ["结论", "置信度"]
    out = []
    for name in secs:
        if "置信度" in name:
            out.append(f"## {name}\nMEDIUM —— 依据上述工具输出。")
        elif "决议" in name:
            line = "**BULLISH** / 短期。多数委员给出方向，据此形成决议。"
            if CITE == "params":
                # 主席引用刚在提示词里读到的运行参数 —— 真实模型常这么写。
                # 正则容忍"最小训练 60"和"最小训练样本 60"两种措辞：锚在
                # 引擎的提示词文案上，文案一改这个开关就静默失效，而"没引用"
                # 和"引用被漏掉"在断言里长得一模一样。
                m = re.search(r"最小训练(?:样本)?\s*(\d+)", user_text or "")
                if m:
                    line += f" 本次最小训练样本 {m.group(1)}，与各委员口径一致。"
            out.append(f"## {name}\n{line}")
        elif "投入判断" in name:
            # 段名与三档措辞照抄主席配置：假模型的意义就是把配置要求的那套
            # 形状如实演一遍，这样"配置改了、假模型没跟上"会立刻暴露成断言失败。
            #
            # 这一段里刻意不写 BULLISH / BEARISH —— 方向标签按约只出现在「决议」
            # 段（direction_sections）。别段复述方向会让"方向从哪来"变得可疑。
            out.append(f"## {name}\n**值得投入** —— 方向明确，支持面过半。\n"
                       f"- 方向：多数分析师同向。\n"
                       f"- 证据强度：支持方置信度上限 MEDIUM。\n"
                       f"- 分歧的代价：一位给出了不一致的结论，其风险项已原样保留。\n"
                       f"判断会变的条件：支持方置信度下调即应推翻本判断。")
        elif name in ("趋势状态", "量能状态"):
            # 方向标签只出现在"该给方向"的段落里：风控官的
            # risk_assessment 没有这两段，所以它自然不投票 —— 与
            # panels.json 里"风控官按职责不产出方向标签"的说明一致。
            out.append(f"## {name}\n趋势向上，**BULLISH**。")
        else:
            out.append(f"## {name}\n依据工具输出，本段结论如上。")
    return "\n\n".join(out)


class Handler(BaseHTTPRequestHandler):
    def _send(self, payload: dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        if self.path.endswith("/models"):
            self._send({"object": "list", "data": MODELS})
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):  # noqa: N802
        size = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(size) or b"{}")

        # 先按真实服务商的规则把关，再谈别的。错误体形状也照抄。
        bad = illegal_tool_name(body)
        if bad is not None:
            where, name = bad
            print(f"[mock] 400 拒绝：{where} = {name!r}", file=sys.stderr, flush=True)
            self._send({"error": {
                "message": (f"Invalid '{where}': string does not match pattern. "
                            f"Expected a string that matches the pattern "
                            f"'^[a-zA-Z0-9_-]+$'."),
                "type": "invalid_request_error", "code": "invalid_request_error",
            }}, status=400)
            return

        extra = unexpected_tool_field(body)
        if extra is not None:
            where, fields = extra
            print(f"[mock] 400 拒绝：{where} 为未定义字段（{fields}）",
                  file=sys.stderr, flush=True)
            self._send({"error": {
                "message": f"Unrecognized field at '{where}': {fields}",
                "type": "invalid_request_error", "code": "invalid_request_error",
            }}, status=400)
            return

        msgs = body.get("messages") or []
        tools = body.get("tools") or []
        system = "\n".join(m.get("content") or "" for m in msgs
                           if m.get("role") == "system")
        user = "\n".join(m.get("content") or "" for m in msgs
                         if m.get("role") == "user")
        took_tool = any(m.get("role") == "tool" for m in msgs)

        if DUMP:
            with open(DUMP_PATH, "a", encoding="utf-8") as fh:
                fh.write(json.dumps({"system": system, "user": user,
                                     "tool_done": took_tool},
                                    ensure_ascii=False) + "\n")

        if not took_tool and tools:
            # 想让假模型去点带点号的远程工具（压测名字翻译那条路）：
            #   FINPULSE_MOCK_TOOL=last python3 tools/mock_llm.py 18812
            name = tools[-1 if PICK == "last" else 0]["function"]["name"]
            self._send({
                "id": "mock-1", "object": "chat.completion", "choices": [{
                    "index": 0, "finish_reason": "tool_calls",
                    "message": {"role": "assistant", "content": None,
                                "tool_calls": [{
                                    "id": "call_1", "type": "function",
                                    "function": {"name": name,
                                                 "arguments": "{}"}}]}}],
                "usage": {"prompt_tokens": 120, "completion_tokens": 15}})
            return

        report = _build_report(system, user)
        # 委员的质证轮"写不完"：判据是提示词里带着别人的结论、而这不是
        # 主席（主席的段落里有「决议」，且它拿到全部三家的结论）。
        cut = TRUNCATE == "peers" and "### " in user and "## 决议" not in system
        if cut:
            report = report.split("\n\n")[0]
            print("[mock] 截断：委员的质证轮只交出第一段", file=sys.stderr, flush=True)

        self._send({
            "id": "mock-2", "object": "chat.completion", "choices": [{
                "index": 0, "finish_reason": "length" if cut else "stop",
                "message": {"role": "assistant", "content": report}}],
            "usage": {"prompt_tokens": 300, "completion_tokens": 200}})

    def log_message(self, *args):
        pass    # 编排是并发的，默认日志会把终端刷成一片噪声


def serve(port: int) -> ThreadingHTTPServer:
    """起一个假端点并返回它（已开始 serve_forever 的线程）。

    **多线程**：投委会的三个委员是并发跑的，真服务商当然也并发处理。
    单线程假端点会把并发请求排成队，一旦某个连接卡住，表现是客户端
    收到连接重置 —— 排查半天最后发现是测试替身自己的瓶颈。
    """
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def main() -> int:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 18812
    serve(port)
    print(f"假 LLM 已启动：http://127.0.0.1:{port}/v1", flush=True)
    if DUMP:
        print(f"提示词将记录到 {DUMP_PATH}", flush=True)
    try:
        threading.Event().wait()      # 主线程交给 Ctrl-C
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
