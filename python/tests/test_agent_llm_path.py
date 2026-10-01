# -*- coding: utf-8 -*-
"""投委会走**真实 LLM 后端**时的全链路回归。

## 为什么单独一个文件

``rule_based`` 是内置模板引擎：它把工具输出的真实数值渲染成规定格式，
**不需要解析模型回复**。而接上真 LLM 时，多出来一整条路径：

    角色提示词 → HTTP 请求 → 模型返回 tool_calls → 引擎执行工具
              → 工具结果回填 → 再请求 → 模型返回报告正文 → 解析段落/方向

这条路径此前没有任何测试覆盖 —— 而它是用户真正在用的那条。这里的假
服务器把这条链路的每一环都走一遍：

* 模型要求调用工具 → 引擎真的去调了，并把结果回填；
* 报告正文由模型给出 → 段落与方向被正确解析，决议能出来；
* **交叉质证真的把委员的结论交付回去了**。

最后一条是多 agent 与"并行作答再计票"的分水岭。只看结论表分不出来：
两种做法都能产出三行方向。所以这里直接检查**发出去的提示词**——
第 2 轮的请求里必须带着其他委员的第一轮结论。

不联网、不烧 token：所有 HTTP 都走本进程内起的假服务器（随机端口）。

## 假服务器为什么还要校验工具名

因为**真服务商会校验**。DeepSeek 对 ``function.name`` 的字符集是硬校验，
不合规直接 400，整次请求作废——三个委员集体"未产出结论"，一场投委会
废掉。这条规则如果只活在服务商那边，我们的离线测试就永远发现不了：

    真实世界：名字带点号 → 服务商 400 → 报告里全是"后端调用失败"
    假服务器（宽松）：名字带点号 → 一路绿灯 → 测试全绿

所以这里的假端点**照抄服务商的规则**：名字不合规就回 400，与线上同样的
错误体。这样"工具名能不能发出去"这件事在离线就能被测到。
"""

from __future__ import annotations

import json
import re
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from finpulse_engine.agent import api as agent_api
from finpulse_engine.agent.guardrails import NumberProvenance
from finpulse_engine.agent.llm.base import Message, ToolCall
from finpulse_engine.agent.llm.openai_compat import OpenAiCompatProvider
from finpulse_engine.agent.llm.override import LlmOverride
from finpulse_engine.agent.tools import to_wire_catalog
from test_agent_api import make_bars, make_service

#: 本文件起的假服务器，tearDownClass 统一关。
_SERVER: list = []

#: 委员的显示名 —— 交叉质证的注入块以 "### <名字>" 开头。
MEMBER_NAMES = ("技术面分析师", "量价分析师", "风险官")

#: 服务端对 function.name 的字符集要求。与 DeepSeek 报错里给出的正则一致。
SERVER_NAME_OK = re.compile(r"^[a-zA-Z0-9_-]+$")

#: ``role='tool'`` 的消息允许出现的字段。OpenAI 的类型定义是这三个，
#: DeepSeek 的官方示例也只带这三个。
TOOL_MESSAGE_FIELDS = frozenset({"role", "content", "tool_call_id"})


def unexpected_tool_field(body: dict):
    """tool 消息里出现了协议没定义的字段，返回 ``(位置, 字段名)``。

    多送字段属于**赌服务端的宽容度**：赢了没有任何收益，输了整次研判作废。
    所以假端点在这里也严格 —— 与"名字必须合法"是同一条思路。
    """
    for i, msg in enumerate(body.get("messages") or []):
        if (msg or {}).get("role") != "tool":
            continue
        extra = sorted(set(msg) - TOOL_MESSAGE_FIELDS)
        if extra:
            return (f"messages[{i}].{extra[0]}", ", ".join(extra))
    return None


def illegal_tool_name(body: dict):
    """按服务端的规则挑出第一个非法工具名，返回 ``(位置, 名字)``；没有则 None。

    查两处，因为同一个字段会从两个方向发出去：

    * ``tools[].function.name`` —— 我们**发出去**的工具目录；
    * assistant 消息里 ``tool_calls[].function.name`` —— 第二轮把第一轮的
      历史回填给服务端的那份。

    只查第一处的话，"发得出去、第二轮又带上原名"这种半修复能蒙混过关。
    （tool 消息也带工具名，但它现在只带协议定义的三个字段，
    由 :func:`unexpected_tool_field` 管。）
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


class _Recorder:
    """假服务器收到的请求。测试靠它检查提示词里到底装了什么。"""

    def __init__(self) -> None:
        self.requests: list = []
        #: 被服务端按"名字不合规"拒掉的请求。非空即等于线上会整场报废。
        self.rejections: list = []
        self.lock = threading.Lock()

    def add(self, entry: dict) -> None:
        with self.lock:
            self.requests.append(entry)

    def reject(self, where: str, name: str) -> None:
        with self.lock:
            self.rejections.append((where, name))

    def where(self, pred):
        return [r for r in self.requests if pred(r)]


def _start_fake_llm(rec: _Recorder, *, pick: str = "first",
                    truncate_peers: bool = False,
                    cite_prompt_params: bool = False) -> str:
    """起一个 OpenAI 兼容的假服务，返回 base_url。

    两个动作：还没取过工具 → 要一个 tool_call；取过之后 → 给报告正文。
    报告按角色配置里的 ``## 段落名`` 生成，方向标签写成 BULLISH。

    ``pick='first'`` 点目录里的第一个工具，``'last'`` 点最后一个，
    ``'first_last'`` 两个一起点。本项目的远程工具（``terminal.*``）永远
    排在角色工具清单末尾，所以 ``'last'`` 系专门用来压测"名字里有点号"
    那条路；而只点远程工具的话，委员手上一条可用数据都没有，护栏会
    （正确地）判定它无法形成判断 —— 所以要看决议得用 ``'first_last'``。

    ``truncate_peers=True``：**只对委员的交叉质证轮**返回一份被截断的
    报告（只有第一段，``finish_reason='length'``）。用来复现"第 2 轮
    写不完"的现场，见 :class:`TestRound2Truncation`。主席不受影响 ——
    用 ``## 决议`` 这个它独有的段落名区分。

    ``cite_prompt_params=True``：让**主席**的报告里引用运行参数
    （"本次最小训练样本 60"）。真实模型常这么写，而这个数字不在主席
    自己的工具输出里、只在任务提示词里 —— 用来压"数字溯源的池子必须
    并上提示词"这条，见 :class:`TestChairCitesPromptNumbers`。
    """

    class Handler(BaseHTTPRequestHandler):
        def _json(self, payload, status: int = 200):
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def _reject(self, where: str, name: str):
            rec.reject(where, name)
            # 错误体照抄 DeepSeek 的形状：排查时看到的东西必须与线上一致。
            self._json({"error": {
                "message": (f"Invalid '{where}': string does not match pattern. "
                            f"Expected a string that matches the pattern "
                            f"'^[a-zA-Z0-9_-]+$'."),
                "type": "invalid_request_error", "code": "invalid_request_error",
            }}, status=400)

        def do_POST(self):  # noqa: N802
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")

            bad = illegal_tool_name(body)
            if bad is not None:
                self._reject(*bad)
                return

            extra = unexpected_tool_field(body)
            if extra is not None:
                self._reject(*extra)
                return

            msgs = body.get("messages") or []
            tools = body.get("tools") or []
            system = "\n".join(m.get("content") or "" for m in msgs
                               if m.get("role") == "system")
            user = "\n".join(m.get("content") or "" for m in msgs
                             if m.get("role") == "user")
            took_tool = any(m.get("role") == "tool" for m in msgs)

            rec.add({"system": system, "user": user, "tool_done": took_tool,
                     "model": body.get("model"),
                     "tool_names": [t["function"]["name"] for t in tools]})

            if not took_tool and tools:
                if pick == "first_last" and len(tools) > 1:
                    wanted = [tools[0], tools[-1]]
                else:
                    wanted = [tools[-1 if pick == "last" else 0]]
                calls = [{
                    "id": f"call_{i}", "type": "function",
                    "function": {"name": t["function"]["name"], "arguments": "{}"},
                } for i, t in enumerate(wanted, start=1)]
                self._json({
                    "id": "c1", "object": "chat.completion", "choices": [{
                        "index": 0, "finish_reason": "tool_calls",
                        "message": {"role": "assistant", "content": None,
                                    "tool_calls": calls}}],
                    "usage": {"prompt_tokens": 120, "completion_tokens": 15}})
                return

            secs = re.findall(r"^##\s*(.+?)\s*$", system, re.M) or ["结论", "置信度"]
            lines = []
            for name in secs:
                if "置信度" in name:
                    lines.append(f"## {name}\nMEDIUM —— 依据上述工具输出。")
                elif "决议" in name:
                    lines.append(f"## {name}\n**BULLISH** / 短期。据此形成决议。")
                elif "投入判断" in name:
                    # 与 tools/mock_llm.py 保持同一形状：段名与三档措辞都写在
                    # 主席配置里，两个假后端都得照演，否则"换一个假后端跑"
                    # 会得到结构不同的报告，测试之间就不再可比。
                    lines.append(
                        f"## {name}\n**值得投入** —— 方向明确，支持面过半。\n"
                        f"- 方向：多数分析师同向。\n"
                        f"- 证据强度：支持方置信度上限 MEDIUM。\n"
                        f"- 分歧的代价：一位给出了不一致的结论，其风险项已原样保留。\n"
                        f"判断会变的条件：支持方置信度下调即应推翻本判断。")
                elif name in ("趋势状态", "量能状态"):
                    lines.append(f"## {name}\n趋势向上，**BULLISH**。")
                else:
                    lines.append(f"## {name}\n依据工具输出，本段结论如上。")
            # 委员的质证轮"写不完"：只交出第一段就被 max_tokens 切掉。
            # 判据是"提示词里带着别人的结论、而这不是主席" —— 主席拿到
            # 全部三家的结论，且它的段落里有「决议」。
            cut = truncate_peers and "### " in user and "## 决议" not in system
            if cut:
                lines = lines[:1]

            # 主席引用本次运行的参数。真实模型几乎总会写一句"样本 N 根 /
            # 最小训练样本 M"——这些都是它刚在提示词里读到的数字。
            #
            # 正则容忍"最小训练 60"与"最小训练样本 60"：把锚点钉在引擎的
            # 提示词文案上，文案一改这个开关就**静默失效**，而"假模型没引用"
            # 与"引用被漏掉"在断言里长得一模一样。
            if cite_prompt_params and "## 决议" in system and lines:
                m = re.search(r"最小训练(?:样本)?\s*(\d+)", user)
                if m:
                    lines[0] += f"本次最小训练样本 {m.group(1)}。"

            self._json({
                "id": "c2", "object": "chat.completion", "choices": [{
                    "index": 0, "finish_reason": "length" if cut else "stop",
                    "message": {"role": "assistant",
                                "content": "\n\n".join(lines)}}],
                "usage": {"prompt_tokens": 300, "completion_tokens": 200}})

        def log_message(self, *args):
            pass

    # 多线程：三个委员是并发跑的，真服务商当然也并发处理。单线程假端点
    # 会把并发请求排成队，一旦有连接卡住，客户端侧表现为"连接重置" ——
    # 排查半天最后发现是测试替身自己的瓶颈。
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _SERVER.append((server, thread))
    return f"http://127.0.0.1:{server.server_port}"


class TestDebateViaLlm(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.rec = _Recorder()
        cls.base_url = _start_fake_llm(cls.rec)

    @classmethod
    def tearDownClass(cls):
        for server, thread in _SERVER:
            server.shutdown()
            server.server_close()
        _SERVER.clear()

    def _debate(self):
        self.rec.requests.clear()
        self.rec.rejections.clear()
        svc = make_service()
        ctx = agent_api.context_from(make_bars(260), symbol="TEST.SH")
        out = svc.debate(ctx, panel="default_committee",
                         override=LlmOverride(provider="openai_compat",
                                              model_id="mock-chat",
                                              base_url=self.base_url,
                                              api_key="sk-test"))
        return out

    @staticmethod
    def _runs(out):
        """把 7 次角色运行摊平：两轮的各委员 + 主席。"""
        runs = []
        for rnd in out.get("rounds", []):
            runs.extend(rnd.get("members", {}).values())
        if out.get("chair"):
            runs.append(out["chair"])
        return runs

    @staticmethod
    def _peer_blocks(entry):
        return sum(1 for n in MEMBER_NAMES if f"### {n}" in entry["user"])

    def test_接上大模型后仍然出得来决议(self):
        out = self._debate()
        self.assertTrue(out.get("valid"), msg=f"编排判定这一场不成立：{out.get('problems')}")
        self.assertTrue(out.get("direction"), msg=f"没有方向：{out.get('direction')}")

    def test_七个角色运行全部走外部模型(self):
        """3 委员 × 2 轮 + 主席 = 7 次。一次都不该落在 rule_based 上。"""
        out = self._debate()
        used = [r.get("provider") for r in self._runs(out)]
        self.assertEqual(len(used), 7, msg=f"角色运行 {len(used)} 次：{used}")
        self.assertNotIn("rule_based", used,
                         msg=f"有角色降级到规则后端：{used}")

    def test_工具循环真的发生了(self):
        """模型要求调工具 → 引擎执行 → 结果回填 → 再请求出报告。

        没有这一环，就是"模型凭记忆编数字"，护栏会拦下，报告也就废了。
        """
        self._debate()
        took = self.rec.where(lambda r: r["tool_done"])
        self.assertGreater(len(took), 0, msg="没有任何请求带工具结果")

    def test_第2轮把其他委员的结论交付回去(self):
        """**交叉质证** —— 多 agent 与"并行作答再计票"的分水岭。

        只看最后的结论表是分不出来的：两种做法都能给出三行方向。
        真正的区别在**第 2 轮发出去的提示词**里。
        """
        self._debate()

        cross = self.rec.where(lambda r: self._peer_blocks(r) >= 1)
        self.assertGreaterEqual(
            len(cross), len(MEMBER_NAMES),
            msg=f"只有 {len(cross)} 个请求带了他方结论，质证没有发生")

        # 第 1 轮必须**看不到**别人的结论：开局就互相可见会锚定判断，
        # 那样三个"独立视角"会迅速退化成一份意见。
        # 每个委员第 1 轮发两次请求（要工具 + 出报告），共 2×3 个。
        blind = self.rec.where(lambda r: self._peer_blocks(r) == 0)
        self.assertEqual(len(blind), 2 * len(MEMBER_NAMES),
                         msg=f"第 1 轮有 {len(blind)} 个请求，预期 "
                             f"{2 * len(MEMBER_NAMES)} 个；"
                             f"多出来的说明首轮就泄露了他方结论")

    def test_主席拿得到全部委员的结论(self):
        self._debate()
        chairs = self.rec.where(lambda r: self._peer_blocks(r) == len(MEMBER_NAMES))
        self.assertGreaterEqual(len(chairs), 1,
                                msg="主席没有拿到全部 3 位委员的结论")

    def test_token用量被记录下来(self):
        """接上真后端时，用量必须是真数字 —— 它决定用户能不能算成本。"""
        out = self._debate()
        total = 0
        for run in self._runs(out):
            usage = run.get("usage") or {}
            total += int(usage.get("total_tokens") or 0)
        self.assertGreater(total, 0, msg="没有记录 token 用量")

    def test_发出去的工具名能过服务端的字符集校验(self):
        """服务端会因为一个非法工具名**拒掉整次请求**。

        所以这不是"某个工具用不了"，而是那一次研判整场作废 —— 用户看到的
        是三个委员集体"弃权"，加上一句来自服务商的 400。假端点照抄了
        DeepSeek 的校验规则，所以这一条能在离线拦住同样的错误。
        同时确认没有往 tool 消息里塞协议没定义的字段。
        """
        self._debate()
        self.assertEqual(
            [], self.rec.rejections,
            msg=f"服务端按名字不合规拒了 {len(self.rec.rejections)} 个请求："
                f"{self.rec.rejections[:3]}")
        for entry in self.rec.requests:
            for name in entry["tool_names"]:
                self.assertRegex(name, SERVER_NAME_OK, msg=f"非法工具名：{name}")


class TestWireEncoding(unittest.TestCase):
    """传输层换名的单元测试：同一个工具名在协议里会走三个方向。

    只换工具目录是不够的 —— 第一轮之后，assistant 的历史 tool_calls 与
    role='tool' 消息的 name 也会带着原名回填给服务端。三个方向都换到，
    才算修干净。
    """

    def test_工具目录里的名字换成线上名(self):
        wire = to_wire_catalog([
            {"type": "function", "function": {"name": "terminal.live_quote",
                                              "description": "d",
                                              "parameters": {"type": "object",
                                                             "properties": {},
                                                             "required": []}}},
            {"type": "function", "function": {"name": "indicators",
                                              "description": "d",
                                              "parameters": {"type": "object",
                                                             "properties": {},
                                                             "required": []}}},
        ])
        sent = [c["function"]["name"] for c in wire.schemas]
        self.assertEqual(sent, ["terminal_live_quote", "indicators"])
        # 描述与参数结构不能被顺手改掉。
        self.assertEqual(wire.schemas[0]["function"]["description"], "d")
        self.assertIn("parameters", wire.schemas[0]["function"])

    def test_assistant历史里的工具名也要换(self):
        """第二轮请求会把第一轮的 tool_calls 原样回填 —— 那里也有名字。"""
        msg = Message.assistant("", tool_calls=[ToolCall(id="c1",
                                                        name="terminal.live_quote")])
        d = OpenAiCompatProvider._wire_message(msg)
        self.assertEqual(d["tool_calls"][0]["function"]["name"],
                         "terminal_live_quote")
        # 换名不能反过来污染内部对象：轨迹与 tool_results 还按原名查。
        self.assertEqual(msg.tool_calls[0].name, "terminal.live_quote")

    def test_tool消息只带协议定义过的字段(self):
        """``Message.name`` 是我们自己加的可追溯字段，不该出现在线上。

        OpenAI 的 tool 消息类型就三个键，DeepSeek 的官方示例也只带三个。
        多送一个未定义的字段属于赌服务端的宽容度：赢了没收益，输了
        整次研判作废。``tool_call_id`` 已经足够把结果和调用对上。
        """
        d = OpenAiCompatProvider._wire_message(
            Message.tool_result("c1", "terminal.bus_stats", "{}"))
        self.assertEqual(set(d), TOOL_MESSAGE_FIELDS)
        self.assertEqual(d["tool_call_id"], "c1")
        self.assertEqual(d["content"], "{}")

    def test_模型回传的线上名翻回原名(self):
        """回程翻译：模型只会看到线上名，它回传的也是线上名。"""
        wire = to_wire_catalog([
            {"type": "function", "function": {"name": "terminal.bus_stats",
                                              "description": "d",
                                              "parameters": {"type": "object",
                                                             "properties": {},
                                                             "required": []}}},
        ])
        raw = {"choices": [{
            "finish_reason": "tool_calls",
            "message": {"role": "assistant", "content": None, "tool_calls": [{
                "id": "c1", "type": "function",
                "function": {"name": "terminal_bus_stats", "arguments": "{}"}}]}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 2}}
        resp = OpenAiCompatProvider._parse(raw, wire)   # 静态方法，不需要实例
        self.assertEqual(resp.tool_calls[0].name, "terminal.bus_stats")

    def test_模型编的工具名原样带回(self):
        """认不出来的名字不要在这里丢：让工具层给出"未知工具"的明确结果。"""
        wire = to_wire_catalog([
            {"type": "function", "function": {"name": "indicators",
                                              "description": "d",
                                              "parameters": {"type": "object",
                                                             "properties": {},
                                                             "required": []}}},
        ])
        raw = {"choices": [{
            "finish_reason": "tool_calls",
            "message": {"role": "assistant", "tool_calls": [{
                "id": "c9", "type": "function",
                "function": {"name": "make_me_rich", "arguments": "{}"}}]}}]}
        resp = OpenAiCompatProvider._parse(raw, wire)
        self.assertEqual(resp.tool_calls[0].name, "make_me_rich")


class TestRemoteToolWireNames(unittest.TestCase):
    """远程工具（``terminal.*``）的线上名与派发 —— 真实故障的回归测试。

    现场：接上 DeepSeek 跑投委会，三个委员全部"未产出结论"，报告里写着

        HTTP 400 调用 https://api.deepseek.com/v1/chat/completions 失败:
        Invalid 'tools[2].function.name': string does not match pattern
        '^[a-zA-Z0-9_-]+$'

    ``tools[2]`` 就是 ``terminal.live_quote`` —— 点号不在协议允许的字符集里。
    注册表用点号是有意的（人类读配置一眼分辨本地/远程），所以修法是**在
    边界上翻译**而不是改名。这一个类专门让模型去点那个带点号的工具：
    发出去必须是下划线形态，派发时必须翻回注册表的原名。
    """

    @classmethod
    def setUpClass(cls):
        cls.rec = _Recorder()
        # first_last：本地工具（拿得到数据）+ 远程工具（名字里有点号），
        # 一次 assistant 消息里同时点两个 —— 真实模型就是这么干的。
        cls.base_url = _start_fake_llm(cls.rec, pick="first_last")

    @classmethod
    def tearDownClass(cls):
        for server, thread in _SERVER:
            server.shutdown()
            server.server_close()
        _SERVER.clear()

    def _debate(self):
        self.rec.requests.clear()
        self.rec.rejections.clear()
        svc = make_service()
        ctx = agent_api.context_from(make_bars(260), symbol="TEST.SH")
        return svc.debate(ctx, panel="default_committee",
                          override=LlmOverride(provider="openai_compat",
                                               model_id="mock-chat",
                                               base_url=self.base_url,
                                               api_key="sk-test"))

    @staticmethod
    def _invocations(out):
        calls = []
        for rnd in out.get("rounds", []):
            for run in rnd.get("members", {}).values():
                calls.extend(run.get("tool_calls") or [])
        return calls

    def test_点名的工具是带点号那个(self):
        """先确认这一场真的压到了远程工具 —— 否则下面的断言都是空转。"""
        out = self._debate()
        named = {c["name"] for c in self._invocations(out)}
        self.assertTrue(
            any(n.startswith("terminal.") and "." in n for n in named),
            msg=f"没有任何一次调用落在远程工具上：{sorted(named)}")

    def test_发出去的远程工具名已换成下划线(self):
        """模型看到的必须是 ``terminal_live_quote``：点号会被服务端 400 掉。"""
        self._debate()
        seen = {n for e in self.rec.requests for n in e["tool_names"]}
        self.assertIn("terminal_live_quote", seen, msg=f"实际发出：{sorted(seen)}")
        for name in seen:
            self.assertNotIn(".", name, msg=f"线上名里还有点号：{name}")

    def test_模型点名后派发到的是注册表里的原名(self):
        """线上名只在网络上来回；引擎内部与轨迹里记的仍是 ``terminal.*``。

        记反了的症状是：工具明明执行了，``tool_results`` 却查不到 —— 于是
        护栏说"报告里的数字无法溯源"，规则后端的 ``data.get("terminal.x")``
        也永远取不到值。两处都会表现为"工具调了但没用"。
        """
        out = self._debate()
        remote = [c for c in self._invocations(out)
                  if c["name"].startswith("terminal.")]
        self.assertTrue(remote, msg="远程工具的调用没有记进轨迹")
        for call in remote:
            self.assertIn(".", call["name"],
                          msg=f"轨迹里的工具名被写成了线上名：{call['name']}")

    def test_这一场仍然出得来决议(self):
        """把名字问题修掉之后，整条链路要能正常收尾。

        顺带把**故障症状**也钉住：名字非法时的表现是每个角色都
        "后端调用失败：HTTP 400 ..."，然后编排层拒绝出决议。这一条要求
        报告的问题清单与角色错误里都不再出现这句话。
        """
        out = self._debate()
        self.assertEqual([], self.rec.rejections,
                         msg=f"仍有请求被服务端拒掉：{self.rec.rejections[:3]}")

        problems = json.dumps(out.get("problems") or [], ensure_ascii=False)
        self.assertNotIn("后端调用失败", problems, msg=problems)
        for rnd in out.get("rounds", []):
            for run in rnd.get("members", {}).values():
                self.assertNotIn("后端调用失败", str(run.get("error") or ""),
                                 msg=f"{run.get('role_id')} 又调用失败了")

        self.assertTrue(out.get("valid"), msg=f"编排判定不成立：{out.get('problems')}")
        self.assertTrue(out.get("direction"), msg=f"没有方向：{out.get('direction')}")


class TestRound2Truncation(unittest.TestCase):
    """委员在交叉质证轮"写不完"时，整场会**不能**跟着一起报废。

    现场：三个委员第 2 轮全部被 max_tokens 截断，报告顶部写着

        有效委员 0 位（[]），未达 quorum 2 位。未形成有效判断的委员：
        technical_analyst：未产出结论（（第 2 轮输出被 max_tokens=1600 截断））…

    整场作废，三万多 token 打了水漂 —— 而**第一轮明明有成立的结论**。
    两轮之间没有出现任何新事实，第二轮的失败不该追溯地否定第一轮。

    所以规则是：本轮没产出可用结论的委员，沿用其上一轮的立场，并在
    会议问题里写明"谁沿用了、为什么"。这里就压这一条。
    """

    @classmethod
    def setUpClass(cls):
        cls.rec = _Recorder()
        cls.base_url = _start_fake_llm(cls.rec, truncate_peers=True)

    @classmethod
    def tearDownClass(cls):
        for server, thread in _SERVER:
            server.shutdown()
            server.server_close()
        _SERVER.clear()

    def _debate(self):
        self.rec.requests.clear()
        self.rec.rejections.clear()
        svc = make_service()
        ctx = agent_api.context_from(make_bars(260), symbol="TEST.SH")
        return svc.debate(ctx, panel="default_committee",
                          override=LlmOverride(provider="openai_compat",
                                               model_id="mock-chat",
                                               base_url=self.base_url,
                                               api_key="sk-test"))

    def test_这一场仍然出得来决议(self):
        """这是整个改动的意义所在：一次截断不该让一场会议归零。"""
        out = self._debate()
        self.assertTrue(out.get("valid"),
                        msg=f"编排判定不成立：{out.get('problems')}")
        self.assertTrue(out.get("direction"), msg=f"没有方向：{out.get('direction')}")

    def test_沿用上一轮这件事被写进了会议问题(self):
        """沿用必须**可见**。不说清楚，读报告的人会以为三位真的质证过了。"""
        out = self._debate()
        text = json.dumps(out.get("problems") or [], ensure_ascii=False)
        self.assertIn("沿用", text, msg=f"会议问题里没有交代沿用：{text}")
        self.assertIn("截断", text, msg=f"没有说清为什么本轮不可用：{text}")

    def test_第二轮确实被截断过否则这个用例是空转(self):
        """先确认压到了现场：委员的质证轮真的返回了截断输出。"""
        out = self._debate()
        rounds = out.get("rounds") or []
        self.assertEqual(len(rounds), 2)
        self.assertTrue(rounds[1].get("carried"),
                        msg=f"第二轮没有任何委员被沿用：{rounds[1].get('effective')}")
        # 第二轮的每个成员都应该是**上一轮**的那份记录（round=1），
        # 而不是新一轮的残稿 —— 残稿连计票都不该进。
        for rid, run in rounds[1]["members"].items():
            self.assertEqual(run["round"], 1, msg=f"{rid} 的轮次标记被改写了")
        self.assertTrue(rounds[1].get("carry_reason"),
                        msg="沿用没有给出理由")

    def test_主席拿到的是可用的委员结论(self):
        """主席只该看到**成功**的委员；截断的残稿不能进它的输入。"""
        out = self._debate()
        self.assertIsNotNone(out.get("chair"), msg="主席没有跑")
        self.assertTrue(out["chair"].get("ok"), msg=f"{out['chair'].get('error')}")

    def test_主席报告回答了是否值得投入(self):
        """LLM 路径下，主席的结论必须落在一个明确的投入档位上。

        用户读完方向与置信度之后问的是"所以呢"。这条守着那个答案真的出现在
        LLM 路径的报告里；同时它也是**假后端与配置是否同步**的哨兵 ——
        把假后端那段分支删掉，它会退回通用占位句，这里立刻变红。
        """
        out = self._debate()
        text = (out.get("chair") or {}).get("text") or ""
        self.assertIn("## 投入判断", text, msg="主席报告里没有投入判断段")
        # 用带 ** 的字面量："不值得投入"里也含"值得投入"，只查后者会空转。
        self.assertIn("**值得投入**", text, msg="投入判断段没有给出明确档位")


class TestChairCitesPromptNumbers(unittest.TestCase):
    """主席引用提示词里的数字，不该被判成"编造"。

    现场：主席的配置里只声明了 ``stats`` 一个工具，报告里却出现
    「26 个数字无法溯源」—— 均线、量比、成交量、技能分，**全都是各分析师
    自己工具的输出**（技术面有 indicators、量价有 flow、风险官有 backtest）。
    它们跟着注入的结论进入主席的视野，主席引用它们是履职，不是编造。

    池子按"模型调了哪些工具"建，就会把一份正确的主席报告整份判为可疑；
    按"模型读到了什么"建才对。这里让假模型真的引用一个**只存在于提示词里**
    的数字，把这条钉死。
    """

    #: ``context_from`` 的默认最小训练样本。够大、且不是任何工具的输出，
    #: 所以它只会从提示词里被认出来。
    MIN_TRAIN = 60

    @classmethod
    def setUpClass(cls):
        cls.rec = _Recorder()
        cls.base_url = _start_fake_llm(cls.rec, cite_prompt_params=True)

    @classmethod
    def tearDownClass(cls):
        for server, thread in _SERVER:
            server.shutdown()
            server.server_close()
        _SERVER.clear()

    def _debate(self):
        self.rec.requests.clear()
        self.rec.rejections.clear()
        svc = make_service()
        ctx = agent_api.context_from(make_bars(260), symbol="TEST.SH")
        return svc.debate(ctx, panel="default_committee",
                          override=LlmOverride(provider="openai_compat",
                                               model_id="mock-chat",
                                               base_url=self.base_url,
                                               api_key="sk-test"))

    def test_主席确实引用了运行参数(self):
        """先确认压到了现场，否则下面那条断言是空转。"""
        out = self._debate()
        chair = out.get("chair") or {}
        # 断言的是**假后端自己写的那句**，不是引擎提示词里的措辞 ——
        # 锚在提示词文案上的话，文案一改这个用例就会以"假主席没引用"
        # 的形式红掉，而它真正想测的是"引用之后护栏怎么判"。
        self.assertIn(f"本次最小训练样本 {self.MIN_TRAIN}", chair.get("text") or "",
                      msg="假主席没有引用运行参数，这个用例失去意义")
        # 这个数必须大到不被"小整数一律放行"那条白名单兜住 —— 否则池子
        # 怎么建都不会报警，测了也白测。
        self.assertGreater(float(self.MIN_TRAIN), NumberProvenance.SMALL_INT_MAX)

    def test_主席报告没有数字溯源告警(self):
        out = self._debate()
        issues = ((out.get("chair") or {}).get("guardrails") or {}).get("items") or []
        bad = [i for i in issues if i.get("guardrail") == "number_provenance"]
        self.assertEqual(bad, [], msg=f"主席被误报编造数字：{bad}")


if __name__ == "__main__":
    unittest.main()
