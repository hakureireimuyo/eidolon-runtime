"""LLM Gateway 抽象层测试(零网络)。

使用 mock service 验证:
- LLMRequest / LLMResponse 类型正确性
- Gateway 封装 provider 差异,engine 只需输入→输出
- 注入式 service 可用于测试
- per-request 参数传递(温度覆盖)
"""
from __future__ import annotations

import os
import tempfile
import unittest

from runtime.llm.base import AIService
from runtime.llm.errors import LLMError, LLMUnconfigured
from runtime.llm_gateway import LLMGateway, LLMRequest, LLMResponse


class _MockService(AIService):
    """用于测试的 mock AI 服务。"""

    name = "mock"

    def __init__(self, *, api_key="sk-mock", **kwargs):
        self.api_key = api_key
        self.kwargs = kwargs
        self.call_count = 0
        self.last_messages: list[dict] | None = None

    def chat(self, messages: list[dict], *, stream: bool = False) -> str:
        if not self.api_key:
            raise LLMUnconfigured("mock 未配置")
        self.call_count += 1
        self.last_messages = list(messages)
        # 简单回显最后一条 user 消息
        for m in reversed(messages):
            if m["role"] == "user":
                return f"[mock] {m['content']}"
        return "[mock] empty"


class _FailingService(AIService):
    """总是抛 LLMError 的服务。"""

    name = "failing"

    def __init__(self, **kwargs):
        pass

    def chat(self, messages: list[dict], *, stream: bool = False) -> str:
        raise LLMError("simulated failure")


class TestLLMRequest(unittest.TestCase):
    def test_default_fields(self):
        req = LLMRequest(messages=[{"role": "user", "content": "hi"}])
        self.assertIsNone(req.temperature)
        self.assertIsNone(req.max_tokens)
        self.assertFalse(req.stream)

    def test_with_messages_immutable(self):
        req = LLMRequest(
            messages=[{"role": "user", "content": "old"}],
            temperature=0.5,
        )
        new = req.with_messages([{"role": "user", "content": "new"}])
        self.assertEqual(new.messages[0]["content"], "new")
        # 原对象不变
        self.assertEqual(req.messages[0]["content"], "old")
        # 参数保持
        self.assertEqual(new.temperature, 0.5)


class TestLLMResponse(unittest.TestCase):
    def test_str_returns_content(self):
        resp = LLMResponse(content="hello", provider="mock")
        self.assertEqual(str(resp), "hello")

    def test_defaults(self):
        resp = LLMResponse(content="text")
        self.assertEqual(resp.provider, "")
        self.assertIsNone(resp.finish_reason)
        self.assertEqual(resp.usage, {})


class TestLLMGateway(unittest.TestCase):
    def setUp(self):
        self._cfg = tempfile.NamedTemporaryFile(suffix=".toml", delete=False)
        self._cfg.close()
        os.environ["EIDOLON_RUNTIME_CONFIG"] = self._cfg.name
        for k in (
            "EIDOLON_LLM_PROVIDER",
            "EIDOLON_LLM_API_KEY",
            "EIDOLON_DEEPSEEK_API_KEY",
            "EIDOLON_LLM_TEMPERATURE",
            "EIDOLON_LLM_MAX_TOKENS",
        ):
            os.environ.pop(k, None)

    def tearDown(self):
        os.environ.pop("EIDOLON_RUNTIME_CONFIG", None)
        try:
            os.unlink(self._cfg.name)
        except OSError:
            pass

    def test_complete_with_injected_service(self):
        mock = _MockService()
        gw = LLMGateway(service=mock)
        req = LLMRequest(
            messages=[
                {"role": "system", "content": "you are a bot"},
                {"role": "user", "content": "hello"},
            ]
        )
        resp = gw.complete(req)
        self.assertEqual(resp.content, "[mock] hello")
        self.assertEqual(resp.provider, "mock")
        self.assertEqual(mock.call_count, 1)
        self.assertEqual(mock.last_messages[0]["role"], "system")
        self.assertEqual(mock.last_messages[1]["content"], "hello")

    def test_complete_propagates_llm_error(self):
        gw = LLMGateway(service=_FailingService())
        req = LLMRequest(messages=[{"role": "user", "content": "hi"}])
        with self.assertRaises(LLMError):
            gw.complete(req)

    def test_complete_propagates_unconfigured(self):
        mock = _MockService(api_key="")
        gw = LLMGateway(service=mock)
        req = LLMRequest(messages=[{"role": "user", "content": "hi"}])
        with self.assertRaises(LLMUnconfigured):
            gw.complete(req)

    def test_provider_property_injected(self):
        gw = LLMGateway(service=_MockService())
        self.assertEqual(gw.provider, "mock")

    def test_complete_stream_returns_chunks(self):
        mock = _MockService()
        gw = LLMGateway(service=mock)
        req = LLMRequest(
            messages=[{"role": "user", "content": "stream test"}],
            stream=True,
        )
        chunks = list(gw.complete_stream(req))
        self.assertEqual(len(chunks), 1)
        self.assertIn("stream test", chunks[0].delta)

    def test_per_request_params_not_required(self):
        """不带 per-request 参数时应正常工作。"""
        mock = _MockService()
        gw = LLMGateway(service=mock)
        req = LLMRequest(messages=[{"role": "user", "content": "simple"}])
        resp = gw.complete(req)
        self.assertTrue(resp.content.startswith("[mock]"))


if __name__ == "__main__":
    unittest.main()
