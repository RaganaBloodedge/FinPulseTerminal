"""discovery（模型发现）的回归测试。

覆盖两条硬性要求和一串失败路径：

1. **密钥只到达用户选定的那一家** —— 起两个假服务商，调用 A，
   断言 B 一次都没被访问。这是 discovery 存在的理由：跨服务商自动探测
   会把用户的密钥发给它不属于的公司，所以这里**永远只问一个端点**；
2. 各类失败（401 / 404 / 非 JSON / 连不上）都要给出「能照着做的下一步」，
   而不是一句"获取失败"；
3. 非对话模型（embedding / whisper / rerank…）要从列表里滤掉，
   并把滤掉的数量带回去 —— 用户不该在下拉里选中一个必然 400 的模型；
4. 密钥不出现在任何返回值里（响应、错误、提示，一个片段都不行）；
5. 前缀推断**只读字符串、不发请求**：sk-proj- → openai、gsk_ → groq、
   通用 sk- → 给候选但 confident=False（猜错了就会把密钥发给错的那家）。

不联网：所有 HTTP 交互都走本进程内起的假服务器（127.0.0.1 随机端口）。
"""
from __future__ import annotations

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

from finpulse_engine.agent.llm import discovery

#: 各测试自建的假服务器。tearDownClass 统一关。
_SERVERS: list[tuple[HTTPServer, threading.Thread]] = []


def _serve(behavior):
    """起一个假 /v1/models。behavior: ok / 401 / 404 / html / slow。"""

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            if not self.path.endswith("/models"):
                self.send_response(404)
                self.end_headers()
                return
            if behavior == "ok":
                body = json.dumps({"object": "list", "data": [
                    {"id": "text-embedding-3-small"},
                    {"id": "whisper-1"},
                    {"id": "demo-chat-pro"},
                    {"id": "demo-reasoner"},
                ]}).encode("utf-8")
                self.send_response(200)
            elif behavior == "only-embed":
                # 全是非对话模型：验证"滤完一个不剩"时宁原样给出、不藏空列表。
                body = json.dumps({"data": [{"id": "text-embedding-3-large"}]}).encode("utf-8")
                self.send_response(200)
            elif behavior == "401":
                body = json.dumps({"error": {
                    "message": "Authentication Fails, Your api key: ****abcd is invalid"
                }}).encode("utf-8")
                self.send_response(401)
            elif behavior == "404":
                body = b'{"detail": "Not Found"}'
                self.send_response(404)
            else:  # html —— 端点存在但不是 API
                self.send_response(200)
                self.send_header("Content-Type", "text/html")
                self.end_headers()
                self.wfile.write(b"<html>not an api</html>")
                return
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _SERVERS.append((server, thread))
    return f"http://127.0.0.1:{server.server_port}"


def tearDownModule():
    for server, _thread in _SERVERS:
        server.shutdown()


class TestKeysOnlyReachTheChosenProvider(unittest.TestCase):
    """第一条硬性要求：密钥只发给选定的一家。"""

    def test_密钥只到达选定的那一家(self):
        # 两个假服务商都记录自己被访问过。探测 A 之后，B 的日志必须是空的 ——
        # 那是"绝不拿密钥挨家试"这条设计决定的直接证据。
        hits: list[str] = []
        url_a = _serve("ok")
        url_b = _serve("ok")

        original = discovery._http_get_json

        def spying_get(url, api_key, timeout):
            hits.append(url)
            return original(url, api_key, timeout)

        discovery._http_get_json = spying_get
        try:
            res = discovery.fetch_models(provider="openai_compat", api_key="sk-secret",
                                         base_url=url_a, timeout=5)
        finally:
            discovery._http_get_json = original

        self.assertTrue(res["ok"])
        self.assertEqual([h for h in hits if h.startswith(url_b)], [],
                         msg="密钥被发给了未选定的服务商 B —— 这是泄露")
        self.assertTrue(any(h.startswith(url_a) for h in hits),
                        msg="选定的服务商 A 一次都没被问到，探测等于没跑")

    def test_密钥不出现在任何返回值里(self):
        res = discovery.fetch_models(provider="openai_compat", api_key="sk-supersecret",
                                     base_url=_serve("401"), timeout=5)
        blob = json.dumps(res, ensure_ascii=False)
        self.assertNotIn("sk-supersecret", blob)


class TestFailurePathsGiveNextSteps(unittest.TestCase):
    """第二条硬性要求：失败要说清楚下一步，而不是一句"获取失败"。"""

    def test_401要指出密钥问题并带上服务端原话(self):
        res = discovery.fetch_models(provider="openai_compat", api_key="bad-key",
                                     base_url=_serve("401"), timeout=5)
        self.assertFalse(res["ok"])
        self.assertIn("密钥被拒绝", res["error"])
        self.assertIn("invalid", res["error"])   # 服务端的原始说明要带回来

    def test_404要指向端点写错并给出手填出路(self):
        res = discovery.fetch_models(provider="openai_compat", api_key="k",
                                     base_url=_serve("404"), timeout=5)
        self.assertFalse(res["ok"])
        self.assertIn("/models", res["error"])
        self.assertIn("手动填模型名", res["error"])

    def test_非JSON响应要明说不是兼容端点(self):
        res = discovery.fetch_models(provider="openai_compat", api_key="k",
                                     base_url=_serve("html"), timeout=5)
        self.assertFalse(res["ok"])
        self.assertIn("不是 OpenAI 兼容", res["error"])

    def test_连不上要给出网络方向(self):
        # 127.0.0.1 上没开的端口 —— 最常见的自建端点故障。
        # 注意别绑死措辞：Linux 上是"连接拒绝"、Windows 上常常变成"超时"，
        # 两条分支都是真实故障、也都带出了端点地址（这才是用户排查要的）。
        res = discovery.fetch_models(provider="openai_compat", api_key="k",
                                     base_url="http://127.0.0.1:1", timeout=2)
        self.assertFalse(res["ok"])
        self.assertIn("127.0.0.1:1/v1/models", res["error"])
        self.assertTrue(("连不上" in res["error"]) or ("超时" in res["error"]),
                        msg=f"两种网络故障的文案都没对上：{res['error']!r}")

    def test_未知服务商要列出可选项(self):
        res = discovery.fetch_models(provider="no-such-vendor", api_key="k")
        self.assertFalse(res["ok"])
        self.assertIn("deepseek", res["hint"])

    def test_规则后端没有模型列表(self):
        res = discovery.fetch_models(provider="rule_based", api_key="whatever")
        self.assertFalse(res["ok"])
        self.assertIn("不联网", res["error"])


class TestChatModelFiltering(unittest.TestCase):
    """非对话模型必须被滤掉，且滤掉几个要说清楚。"""

    def test_embedding和语音被滤掉并计数(self):
        res = discovery.fetch_models(provider="openai_compat", api_key="k",
                                     base_url=_serve("ok"), timeout=5)
        self.assertTrue(res["ok"])
        ids = [m["id"] for m in res["models"]]
        self.assertNotIn("text-embedding-3-small", ids)
        self.assertNotIn("whisper-1", ids)
        self.assertIn("demo-chat-pro", ids)
        self.assertIn("demo-reasoner", ids)
        self.assertEqual(res["filtered"], 2)
        self.assertEqual(res["total"], 2)

    def test_全是非对话模型时原样给出不藏空列表(self):
        # 服务商只回了 embedding 时，宁可原样列出让用户自己判断，
        # 也不能给一个空下拉 —— 空列表看起来就像"这家没模型"。
        res = discovery.fetch_models(provider="openai_compat", api_key="k",
                                     base_url=_serve("only-embed"), timeout=5)
        self.assertTrue(res["ok"])
        self.assertTrue(res["models"])          # 非空
        self.assertEqual(res["total"], len(res["models"]))
        self.assertTrue(res["hint"])            # 并提示用户自行确认


class _EmbedOnlyHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = json.dumps({"data": [{"id": "text-embedding-3-large"}]}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class TestPrefixGuessingStaysOffline(unittest.TestCase):
    """前缀推断只读字符串。它认不出的时候必须把选择权交还用户。"""

    def test_sk_proj_唯一对应openai(self):
        g = discovery.guess_provider("sk-proj-abcdefgh")
        self.assertTrue(g["confident"])
        self.assertEqual(g["provider"], "openai")

    def test_gsk_唯一对应groq(self):
        g = discovery.guess_provider("gsk_abcdefgh")
        self.assertTrue(g["confident"])
        self.assertEqual(g["provider"], "groq")

    def test_通用sk前缀给出候选但不替用户猜(self):
        # OpenAI / DeepSeek / Moonshot 的密钥格式一模一样 —— 猜错一家，
        # 密钥就发给了错的服务商。所以这里 confident 必须是 False。
        g = discovery.guess_provider("sk-abcdefgh")
        self.assertFalse(g["confident"])
        self.assertEqual(g["provider"], "")
        self.assertIn("deepseek", g["candidates"])
        self.assertIn("openai", g["candidates"])

    def test_anthropic密钥要明说接不了(self):
        g = discovery.guess_provider("sk-ant-api03-xyz")
        self.assertFalse(g["confident"])
        self.assertIn("Anthropic", g["note"])

    def test_空密钥返回空候选(self):
        g = discovery.guess_provider("")
        self.assertFalse(g["confident"])
        self.assertEqual(g["candidates"], [])


if __name__ == "__main__":
    unittest.main()
