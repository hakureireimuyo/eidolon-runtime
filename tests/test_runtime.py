"""eidolon-runtime 最小测试（零网络）。

覆盖：
- 加载角色卡（复用 PersonaSeed + eidolon-character）
- system prompt 由角色设定正确编译
- 未配置 LLM Key 时对话优雅报错（不崩溃）
"""
from __future__ import annotations

import os
import sys
import tempfile
import unittest

# 开发期注入同级兄弟仓库（示例 / 测试运行于 eidolon-runtime 内）。
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
for name in ("PersonaSeed", "eidolon-character"):
    p = os.path.join(ROOT, name)
    if os.path.isdir(p) and p not in sys.path:
        sys.path.insert(0, p)

from eidolon_character.builder import build_seed
from eidolon_character.model import Character, Dialogue, Identity

from runtime.engine import RuntimeEngine, build_system_prompt
from runtime.llm import (
    LLMError,
    LLMUnconfigured,
    UnsupportedCapability,
    DeepSeekService,
    get_service,
    list_providers,
)


def _sample_character() -> Character:
    return Character(
        identity=Identity(name="TestBot", gender="无", species="AI"),
        dialogue=Dialogue(greeting="你好，我是 TestBot。"),
    )


class TestRuntime(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
