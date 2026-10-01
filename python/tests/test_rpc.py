"""RPC 调度层测试。

核心断言：**任何异常都不能让引擎进程死掉** —— 全部转成 error 响应。
这是这条链路上最硬的一条要求，测试用例按"错误类别 → 错误码"逐个验证。
"""

from __future__ import annotations

import unittest

from finpulse_engine.rpc import BadData, BadParams, Dispatcher, NotFound, RpcError


def make_dispatcher():
    d = Dispatcher()

    @d.method("math.div")
    def div(a: float, b: float) -> float:
        """a / b"""
        return a / b

    @d.method("biz.fail")
    def fail():
        """故意抛业务错误"""
        raise NotFound("标的不存在", detail="SYMBOL=X")

    @d.method("biz.valueerror")
    def bad_value():
        """内部抛 ValueError"""
        raise ValueError("序列太短")

    @d.method("biz.crash")
    def crash():
        """内部抛未预期异常"""
        raise RuntimeError("boom")

    return d


class RegistrationTests(unittest.TestCase):
    def test_方法名与摘要被登记(self):
        d = make_dispatcher()
        self.assertIn("math.div", d.methods)
        self.assertEqual(d.methods["math.div"], "a / b")

    def test_未命名方法用函数名(self):
        d = Dispatcher()

        @d.method()
        def ping():
            """心跳"""

        self.assertIn("ping", d.methods)

    def test_重复注册立刻报错(self):
        d = Dispatcher()
        d.method("x")(lambda: None)
        with self.assertRaises(RuntimeError):
            d.method("x")(lambda: None)


class DispatchTests(unittest.TestCase):
    def setUp(self):
        self.d = make_dispatcher()

    def test_成功响应带_id与_result(self):
        resp = self.d.handle({"id": 1, "method": "math.div", "params": {"a": 6, "b": 3}})
        self.assertEqual(resp, {"id": 1, "ok": True, "result": 2.0})

    def test_非对象请求返回_Protocol_错误(self):
        resp = self.d.handle([1, 2, 3])
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"]["code"], "Protocol")

    def test_缺少_id的请求被静默丢弃(self):
        # C++ 侧把"无 id"当事件，所以引擎对无 id 请求不回包
        self.assertIsNone(self.d.handle({"method": "math.div", "params": {}}))

    def test_缺少_method字段(self):
        resp = self.d.handle({"id": 2})
        self.assertEqual(resp["error"]["code"], "Protocol")

    def test_params不是对象(self):
        resp = self.d.handle({"id": 3, "method": "math.div", "params": [1, 2]})
        self.assertEqual(resp["error"]["code"], "BadParams")

    def test_未知方法(self):
        resp = self.d.handle({"id": 4, "method": "nope"})
        self.assertEqual(resp["error"]["code"], "UnknownMethod")

    def test_参数名拼错归为_BadParams_而不是内部错误(self):
        # 这是 Dispatcher 最有价值的区分：让壳侧 bug 在壳侧被看见
        resp = self.d.handle({"id": 5, "method": "math.div", "params": {"a": 1, "c": 2}})
        self.assertEqual(resp["error"]["code"], "BadParams")
        self.assertIn("参数不匹配", resp["error"]["message"])

    def test_缺参数也归为_BadParams(self):
        resp = self.d.handle({"id": 6, "method": "math.div", "params": {"a": 1}})
        self.assertEqual(resp["error"]["code"], "BadParams")

    def test_业务错误码原样透传(self):
        resp = self.d.handle({"id": 7, "method": "biz.fail"})
        self.assertEqual(resp["error"]["code"], "NotFound")
        self.assertEqual(resp["error"]["detail"], "SYMBOL=X")

    def test_ValueError归为_BadData(self):
        resp = self.d.handle({"id": 8, "method": "biz.valueerror"})
        self.assertEqual(resp["error"]["code"], "BadData")

    def test_除零归为_MathError(self):
        resp = self.d.handle({"id": 9, "method": "math.div", "params": {"a": 1, "b": 0}})
        self.assertEqual(resp["error"]["code"], "MathError")

    def test_未预期异常不让调度器死掉(self):
        resp = self.d.handle({"id": 10, "method": "biz.crash"})
        self.assertFalse(resp["ok"])
        self.assertEqual(resp["error"]["code"], "RuntimeError")
        # 调度器还能继续服务下一个请求 —— 这才是这条断言的真正意义
        ok = self.d.handle({"id": 11, "method": "math.div", "params": {"a": 4, "b": 2}})
        self.assertTrue(ok["ok"])

    def test_错误响应的形状稳定(self):
        # C++ 侧按这三个字段解析，形状一变就是兼容性事故
        resp = self.d.handle({"id": 12, "method": "nope"})
        self.assertEqual(set(resp), {"id", "ok", "error"})
        self.assertEqual(set(resp["error"]), {"code", "message", "detail"})

    def test_省略_params等价于空对象(self):
        d = Dispatcher()
        d.method("hello")(lambda: "hi")
        resp = d.handle({"id": 13, "method": "hello"})
        self.assertEqual(resp["result"], "hi")


class ErrorHierarchyTests(unittest.TestCase):
    def test_错误子类都是_RpcError(self):
        for exc in (BadParams("x"), NotFound("x"), BadData("x")):
            self.assertIsInstance(exc, RpcError)
            self.assertTrue(exc.code)

    def test_可以被按类型捕获(self):
        with self.assertRaises(BadData):
            raise BadData("价格非正")


if __name__ == "__main__":
    unittest.main()
