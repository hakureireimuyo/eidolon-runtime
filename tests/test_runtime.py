"""eidolon-runtime 最小测试(零网络)。

覆盖:
- 加载角色卡(复用 eidolon-character-service)
- system prompt 由角色设定正确编译
- 未配置 LLM Key 时对话优雅报错(不崩溃)
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from eidolon_character.builder import build_seed
from eidolon_character.model import Character, CharacterAsset, Dialogue, Identity
from eidolon_character_service import build_system_prompt, CharacterLoadError

from runtime.engine import RuntimeEngine
from runtime.llm import (
    LLMError,
    LLMUnconfigured,
    UnsupportedCapability,
    DeepSeekService,
    get_service,
    list_providers,
)
from runtime.llm.config_file import load_llm_config, save_llm_config
from fastapi.testclient import TestClient
from backend.main import app


def _sample_character() -> Character:
    return Character(
        identity=Identity(name="TestBot", gender="无", species="AI"),
        dialogue=Dialogue(greeting="你好,我是 TestBot。"),
    )


class TestRuntime(unittest.TestCase):
    def setUp(self):
        # 隔离:指向一个空的临时配置,避免受真实 config.toml / 环境变量影响。
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

    def _make_seed(self) -> str:
        c = _sample_character()
        fd, path = tempfile.mkstemp(suffix=".seed")
        os.close(fd)
        build_seed(c, output_path=path)
        return path

    def test_load_character(self):
        path = self._make_seed()
        try:
            eng = RuntimeEngine()
            info = eng.load(path)
            self.assertTrue(info["loaded"])
            self.assertEqual(info["name"], "TestBot")
            # 资源字节随包载入内存
            self.assertIn("greeting", info)
            # 自包含内存对象:bundle 与派生视图一致
            self.assertIsNotNone(eng.bundle)
            self.assertIs(eng.bundle.character, eng.character)
            self.assertEqual(eng.manifest, eng.bundle.manifest)
        finally:
            os.unlink(path)

    def test_load_character_with_image(self):
        """图片字节进入内存对象,序列化默认附 base64 data URI(可开关)。"""
        c = _sample_character()
        c.assets = [
            CharacterAsset(id="portrait", type="image/png", purpose="portrait")
        ]
        png = b"\x89PNG\r\n\x1a\nfake-bytes"
        fd, path = tempfile.mkstemp(suffix=".seed")
        os.close(fd)
        build_seed(c, images={"portrait": png}, output_path=path)
        try:
            eng = RuntimeEngine()
            info = eng.load(path)
            # 派生视图:真实字节
            self.assertEqual(eng.assets["portrait"], png)
            self.assertEqual(eng.asset_types["portrait"], "image/png")
            # 自包含内存对象:语义访问器直接取真实数据
            self.assertEqual(eng.bundle.get_portrait().data, png)
            # 序列化默认附 base64 data URI
            portrait = info["assets"][0]
            self.assertTrue(
                portrait["data"].startswith("data:image/png;base64,")
            )
            self.assertEqual(portrait["size"], len(png))
            # 开关关闭 → 严格元数据四字段
            meta = eng.character_info(include_data=False)
            self.assertEqual(
                set(meta["assets"][0]), {"id", "type", "purpose", "caption"}
            )
        finally:
            os.unlink(path)

    def test_system_prompt_contains_name(self):
        eng = RuntimeEngine()
        eng.character = _sample_character()
        prompt = build_system_prompt(eng.character)
        self.assertIn("TestBot", prompt)
        self.assertIn("不要承认自己是人工智能", prompt)

    def test_chat_requires_llm_key(self):
        path = self._make_seed()
        try:
            eng = RuntimeEngine()
            eng.load(path)
            with self.assertRaises(LLMUnconfigured):
                eng.chat("hi")
        finally:
            os.unlink(path)


class TestFactory(unittest.TestCase):
    def test_default_provider_is_deepseek(self):
        self.assertIn("deepseek", list_providers())
        self.assertIsInstance(get_service("deepseek"), DeepSeekService)

    def test_provider_selected_by_env(self):
        import os

        os.environ["EIDOLON_LLM_PROVIDER"] = "deepseek"
        self.assertIsInstance(get_service(), DeepSeekService)
        del os.environ["EIDOLON_LLM_PROVIDER"]

    def test_unknown_provider_raises(self):
        with self.assertRaises(LLMError):
            get_service("not-registered-provider")

    def test_multimodal_capabilities_not_supported_by_default(self):
        svc = get_service("deepseek")
        self.assertEqual(svc.capabilities, {"chat"})
        with self.assertRaises(UnsupportedCapability):
            svc.describe_image(b"")
        with self.assertRaises(UnsupportedCapability):
            svc.transcribe_audio(b"")
        with self.assertRaises(UnsupportedCapability):
            svc.synthesize_speech("hi")


class TestConfigFile(unittest.TestCase):
    def setUp(self):
        self._cfg = tempfile.NamedTemporaryFile(suffix=".toml", delete=False)
        self._cfg.close()
        os.environ["EIDOLON_RUNTIME_CONFIG"] = self._cfg.name

    def tearDown(self):
        os.environ.pop("EIDOLON_RUNTIME_CONFIG", None)
        try:
            os.unlink(self._cfg.name)
        except OSError:
            pass

    def test_roundtrip(self):
        saved = save_llm_config(
            {
                "provider": "deepseek",
                "api_key": "sk-abc",
                "model": "deepseek-chat",
                "temperature": 0.5,
                "max_tokens": 512,
            }
        )
        self.assertEqual(saved["api_key"], "sk-abc")
        loaded = load_llm_config()
        self.assertEqual(loaded["provider"], "deepseek")
        self.assertEqual(loaded["api_key"], "sk-abc")
        self.assertEqual(loaded["model"], "deepseek-chat")
        self.assertEqual(loaded["temperature"], 0.5)
        self.assertEqual(loaded["max_tokens"], 512)

    def test_empty_string_clears_field(self):
        save_llm_config({"api_key": "sk-abc"})
        save_llm_config({"api_key": ""})  # 空字符串 = 清除
        self.assertNotIn("api_key", load_llm_config())

    def test_preserves_other_sections(self):
        Path(self._cfg.name).write_text('[other]\nkey = "v"\n', encoding="utf-8")
        save_llm_config({"api_key": "sk-x"})
        text = Path(self._cfg.name).read_text(encoding="utf-8")
        self.assertIn("[other]", text)
        self.assertIn('key = "v"', text)
        self.assertIn('api_key = "sk-x"', text)


class TestServiceFromConfig(unittest.TestCase):
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

    def test_get_service_reads_config(self):
        save_llm_config(
            {
                "provider": "deepseek",
                "api_key": "sk-fromcfg",
                "temperature": 0.3,
                "max_tokens": 256,
            }
        )
        svc = get_service()
        self.assertIsInstance(svc, DeepSeekService)
        self.assertEqual(svc.api_key, "sk-fromcfg")
        self.assertEqual(svc.temperature, 0.3)
        self.assertEqual(svc.max_tokens, 256)


class TestSettingsAPI(unittest.TestCase):
    def setUp(self):
        self._cfg = tempfile.NamedTemporaryFile(suffix=".toml", delete=False)
        self._cfg.close()
        os.environ["EIDOLON_RUNTIME_CONFIG"] = self._cfg.name
        self.client = TestClient(app)

    def tearDown(self):
        os.environ.pop("EIDOLON_RUNTIME_CONFIG", None)
        try:
            os.unlink(self._cfg.name)
        except OSError:
            pass

    def test_get_defaults(self):
        r = self.client.get("/api/settings")
        self.assertEqual(r.status_code, 200)
        data = r.json()
        self.assertEqual(data["provider"], "deepseek")
        self.assertFalse(data["has_api_key"])
        self.assertIsNone(data["temperature"])
        self.assertIsNone(data["max_tokens"])

    def test_put_then_get(self):
        r = self.client.put(
            "/api/settings",
            json={
                "api_key": "sk-ui",
                "model": "deepseek-chat",
                "temperature": 0.7,
                "max_tokens": 800,
            },
        )
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.json()["settings"]["has_api_key"])
        g = self.client.get("/api/settings")
        d = g.json()
        self.assertEqual(d["api_key"], "sk-ui")
        self.assertEqual(d["model"], "deepseek-chat")
        self.assertEqual(d["temperature"], 0.7)
        self.assertEqual(d["max_tokens"], 800)


class TestEngineWithGateway(unittest.TestCase):
    """Engine + LLMGateway + ContextManager 集成测试(零网络,注入 mock service)。"""

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

    def _make_seed(self) -> str:
        c = _sample_character()
        fd, path = tempfile.mkstemp(suffix=".seed")
        os.close(fd)
        build_seed(c, output_path=path)
        return path

    def test_chat_with_mock_service(self):
        """注入 mock service,验证 engine 通过 gateway + context 完成对话。"""
        from runtime.llm.base import AIService
        from runtime.llm.errors import LLMUnconfigured

        class MockService(AIService):
            name = "mock"

            def __init__(self, **kw):
                self.api_key = kw.get("api_key", "sk-mock")

            def chat(self, messages, *, stream=False):
                for m in reversed(messages):
                    if m["role"] == "user":
                        return f"[mock]{m['content']}"
                return "[mock]empty"

        from runtime.llm_gateway import LLMGateway

        path = self._make_seed()
        try:
            gw = LLMGateway(service=MockService())
            eng = RuntimeEngine(gateway=gw)
            eng.load(path)
            result = eng.chat("你好")
            self.assertEqual(result["reply"], "[mock]你好")
            self.assertEqual(len(result["history"]), 2)
            self.assertEqual(result["history"][0]["role"], "user")
            self.assertEqual(result["history"][1]["role"], "assistant")
        finally:
            os.unlink(path)

    def test_context_cache_info_after_chat(self):
        """验证 engine 对话后能返回上下文缓存信息。"""
        from runtime.llm.base import AIService

        class MockService(AIService):
            name = "mock"

            def __init__(self, **kw):
                pass

            def chat(self, messages, *, stream=False):
                return "ok"

        from runtime.llm_gateway import LLMGateway

        path = self._make_seed()
        try:
            gw = LLMGateway(service=MockService())
            eng = RuntimeEngine(gateway=gw)
            eng.load(path)
            eng.chat("test message")
            info = eng.context_cache_info()
            self.assertIn("prefix_segments", info)
            self.assertIn("dynamic_segments", info)
            self.assertEqual(info["conversation_turns"], 2)  # user + assistant
        finally:
            os.unlink(path)

    def test_llm_provider_name(self):
        """验证 engine 能返回当前 provider 名称。"""
        from runtime.llm.base import AIService

        class MockService(AIService):
            name = "mock_provider"

            def __init__(self, **kw):
                pass

            def chat(self, messages, *, stream=False):
                return "ok"

        from runtime.llm_gateway import LLMGateway

        gw = LLMGateway(service=MockService())
        eng = RuntimeEngine(gateway=gw)
        self.assertEqual(eng.llm_provider(), "mock_provider")

    def test_chat_rollback_on_error(self):
        """LLM 出错时对话历史应回滚(不留下未回复的 user 消息)。"""
        from runtime.llm.base import AIService
        from runtime.llm.errors import LLMError

        class FailingService(AIService):
            name = "failing"

            def __init__(self, **kw):
                pass

            def chat(self, messages, *, stream=False):
                raise LLMError("simulated")

        from runtime.llm_gateway import LLMGateway

        path = self._make_seed()
        try:
            gw = LLMGateway(service=FailingService())
            eng = RuntimeEngine(gateway=gw)
            eng.load(path)
            # 先正常对话一轮
            with self.assertRaises(LLMError):
                eng.chat("this will fail")
            # history 应为空(回滚)
            self.assertEqual(len(eng.history), 0)
        finally:
            os.unlink(path)


class TestAssetAPI(unittest.TestCase):
    """/api/asset 字节服务链端到端(经全局 engine)。"""

    def setUp(self):
        self._cfg = tempfile.NamedTemporaryFile(suffix=".toml", delete=False)
        self._cfg.close()
        os.environ["EIDOLON_RUNTIME_CONFIG"] = self._cfg.name
        self.client = TestClient(app)

    def tearDown(self):
        os.environ.pop("EIDOLON_RUNTIME_CONFIG", None)
        try:
            os.unlink(self._cfg.name)
        except OSError:
            pass

    def _load_seed_with_portrait(self) -> bytes:
        c = _sample_character()
        c.assets = [
            CharacterAsset(id="portrait", type="image/png", purpose="portrait")
        ]
        png = b"\x89PNG\r\n\x1a\napi-bytes"
        fd, path = tempfile.mkstemp(suffix=".seed")
        os.close(fd)
        build_seed(c, images={"portrait": png}, output_path=path)
        try:
            with open(path, "rb") as f:
                r = self.client.post(
                    "/api/load",
                    files={
                        "file": (
                            "alice.seed",
                            f.read(),
                            "application/octet-stream",
                        )
                    },
                )
            self.assertEqual(r.status_code, 200)
            self.assertTrue(r.json()["loaded"])
        finally:
            os.unlink(path)
        return png

    def test_asset_endpoint_serves_bundle_bytes(self):
        png = self._load_seed_with_portrait()
        # /api/character 默认附 data URI(前端可直接消费真实数据)
        info = self.client.get("/api/character").json()
        self.assertTrue(
            info["assets"][0]["data"].startswith("data:image/png;base64,")
        )
        # /api/asset 仍按 id 出真实字节
        r = self.client.get("/api/asset/portrait")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.content, png)
        self.assertTrue(r.headers["content-type"].startswith("image/png"))

    def test_asset_endpoint_404(self):
        self.assertEqual(self.client.get("/api/asset/nope").status_code, 404)


if __name__ == "__main__":
    unittest.main()
