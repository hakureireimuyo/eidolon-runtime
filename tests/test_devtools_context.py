"""DevTools 上下文检查器测试(零网络)。

覆盖:
- capture:快照结构与 hash 计算
- 一致性:布局单元与 ContextCompiler.compile() 输出等价(锁定编译规则)
- diff:对话对齐(追加 / 截断 / 同时)、前缀缓存命中判定、首个变化点
- API:previous 自动推进、baseline 流程、segment/turn/messages 端点
- 开关解析与内存态边界(新 router = 无历史,模拟进程重启)
"""
from __future__ import annotations

import os
import tempfile
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from runtime.config import _parse_devtools_flag
from runtime.context import ContextLayer, ContextManager, ContextSegment
from runtime.engine import RuntimeEngine

from backend.devtools import build_router
from backend.devtools.context_inspector import (
    _align_turns,
    capture,
    diff,
    sha1,
    snapshot_public,
)


def _manager(**kwargs) -> ContextManager:
    return ContextManager(**kwargs)


def _h(texts: list[str]) -> list[dict]:
    """把文本列表转成 _align_turns 所需的 turn dict。"""
    return [{"hash": sha1(t)} for t in texts]


class TestCapture(unittest.TestCase):
    def test_capture_layer_stats_and_hashes(self):
        mgr = _manager()
        mgr.set_static("character_prompt", "人格")
        mgr.set_low("world", "世界观")
        mgr.set_high("emotion", "情绪")
        snap = capture(mgr)

        self.assertEqual(snap["summary"]["total_segments"], 3)
        self.assertEqual(snap["summary"]["segment_chars"], 7)  # 2 + 3 + 2
        layers = {layer["layer"]: layer for layer in snap["layers"]}
        self.assertEqual(list(layers), ["STATIC", "LOW", "MID", "HIGH"])
        self.assertEqual(layers["STATIC"]["count"], 1)
        self.assertEqual(layers["STATIC"]["chars"], 2)
        seg = layers["STATIC"]["segments"][0]
        self.assertEqual(seg["tag"], "character_prompt")
        self.assertEqual(seg["hash"], sha1("人格"))
        self.assertEqual(seg["length"], 2)
        # 前缀口径:static/low 计入,HIGH 不计
        self.assertTrue(seg["in_prefix"])
        self.assertFalse(layers["HIGH"]["segments"][0]["in_prefix"])

    def test_capture_conversation_negative_index(self):
        mgr = _manager()
        mgr.add_message("user", "你好")
        mgr.add_message("assistant", "你好呀")
        snap = capture(mgr)
        turns = snap["conversation"]["turns"]
        self.assertEqual([t["index"] for t in turns], [-2, -1])  # 最新 = -1
        self.assertEqual(turns[-1]["hash"], sha1("你好呀"))

    def test_units_aligns_with_compile(self):
        """一致性测试:布局单元 == compile() 输出的真实布局。

        编译规则变动时此测试即红,防止 inspector 与编译器漂移。
        """
        mgr = _manager()
        mgr.set_static("character_prompt", "静态人格")
        mgr.set_low("world", "世界观")
        mgr.set_high("emotion", "情绪:平静")  # system HIGH,合并进 message 0
        mgr.ir.add(
            ContextSegment(
                text="一段 user 角色片段", layer=ContextLayer.MID, tag="note", role="user"
            )
        )
        mgr.add_message("user", "你好")
        mgr.add_message("assistant", "你好呀")
        snap = capture(mgr)
        messages = mgr.compile()

        units = snap["layout_units"]
        merged = [u["_text"] for u in units if u["kind"] == "segment" and u["merged"]]
        non_system = [u["_text"] for u in units if u["kind"] == "segment" and not u["merged"]]
        turns = [u["_text"] for u in units if u["kind"] == "turn"]

        self.assertEqual("\n\n".join(merged), messages[0]["content"])
        self.assertEqual(non_system, [messages[1]["content"]])
        self.assertEqual(turns, [m["content"] for m in messages[2:]])
        self.assertEqual(snap["summary"]["prefix_hash"], sha1(messages[0]["content"]))
        self.assertEqual(snap["summary"]["message_count"], len(messages))

    def test_public_snapshot_strips_text(self):
        mgr = _manager()
        mgr.set_static("character_prompt", "x")
        mgr.add_message("user", "y")
        public = snapshot_public(capture(mgr))
        flat = []
        for layer in public["layers"]:
            flat.extend(layer["segments"])
        flat.extend(public["conversation"]["turns"])
        flat.extend(public["layout_units"])
        for item in flat:
            self.assertNotIn("_text", item)
        # hash 仍在(一致性判断的依据)
        self.assertIn("hash", public["layout_units"][0])


class TestDiff(unittest.TestCase):
    def test_align_turns(self):
        # 纯追加
        self.assertEqual(_align_turns(_h(["a", "b"]), _h(["a", "b", "c"])), (0, 1))
        # 纯截断
        self.assertEqual(_align_turns(_h(["a", "b", "c", "d"]), _h(["c", "d"])), (2, 0))
        # 追加 + 截断同时
        self.assertEqual(_align_turns(_h(["a", "b", "c", "d"]), _h(["c", "d", "e", "f"])), (2, 2))
        # 完全不变
        self.assertEqual(_align_turns(_h(["a", "b"]), _h(["a", "b"])), (0, 0))

    def test_diff_identical_is_full_hit(self):
        mgr = _manager()
        mgr.set_static("character_prompt", "人格")
        mgr.add_message("user", "你好")
        prev = capture(mgr)
        cur = capture(mgr)
        d = diff(prev, cur, "previous")
        self.assertTrue(d["prefix_cache_hit"])
        self.assertEqual(d["prefix_break_reason"], "ok")
        self.assertIsNone(d["first_change_index"])
        self.assertEqual(d["conversation"]["status"], "unchanged")
        self.assertEqual(d["summary"]["changed"], 0)

    def test_static_change_breaks_prefix(self):
        mgr = _manager()
        mgr.set_static("character_prompt", "v1")
        prev = capture(mgr)
        mgr.set_static("character_prompt", "v2")
        d = diff(prev, capture(mgr), "previous")
        self.assertFalse(d["prefix_cache_hit"])
        self.assertEqual(d["prefix_break_reason"], "segment_changed")
        self.assertEqual(d["first_change_index"], 0)
        self.assertEqual(d["summary"]["changed"], 1)

    def test_high_change_keeps_prefix_hit(self):
        mgr = _manager()
        mgr.set_static("character_prompt", "v1")
        mgr.set_high("emotion", "平静")
        prev = capture(mgr)
        mgr.set_high("emotion", "愤怒")
        d = diff(prev, capture(mgr), "previous")
        self.assertTrue(d["prefix_cache_hit"])  # 高频段不在缓存前缀口径内
        self.assertEqual(d["summary"]["changed"], 1)
        # 首个变化点在 emotion 单元(布局第 2 位)
        self.assertEqual(d["first_change_index"], 1)

    def test_prefix_segment_added_and_removed(self):
        mgr = _manager()
        mgr.set_static("character_prompt", "v1")
        prev = capture(mgr)
        mgr.set_low("world", "新世界观")
        d = diff(prev, capture(mgr), "previous")
        self.assertFalse(d["prefix_cache_hit"])
        self.assertEqual(d["prefix_break_reason"], "segment_added")

        mgr2 = _manager()
        mgr2.set_static("character_prompt", "v1")
        mgr2.set_low("world", "世界观")
        prev2 = capture(mgr2)
        mgr2.remove("world")
        d2 = diff(prev2, capture(mgr2), "previous")
        self.assertFalse(d2["prefix_cache_hit"])
        self.assertEqual(d2["prefix_break_reason"], "segment_removed")
        self.assertEqual(d2["summary"]["removed"], 1)

    def test_conversation_grew(self):
        mgr = _manager()
        mgr.set_static("character_prompt", "v1")
        mgr.add_message("user", "你好")
        prev = capture(mgr)
        mgr.add_message("assistant", "你好呀")
        mgr.add_message("user", "再见")
        d = diff(prev, capture(mgr), "previous")
        self.assertTrue(d["prefix_cache_hit"])  # 对话增长不破坏前缀
        self.assertEqual(d["conversation"], {"status": "grew", "added": 2, "removed": 0})
        self.assertEqual(d["summary"]["added"], 2)
        # 首个变化点 = 第一个新增轮
        first = d["units"][d["first_change_index"]]
        self.assertEqual(first["kind"], "turn")
        self.assertEqual(first["status"], "added")

    def test_conversation_truncated_by_eviction(self):
        mgr = _manager(max_conversation_turns=3)
        mgr.set_static("character_prompt", "v1")
        for m in ("a", "b", "c"):
            mgr.add_message("user", m)
        prev = capture(mgr)
        for m in ("d", "e"):
            mgr.add_message("user", m)
        d = diff(prev, capture(mgr), "previous")
        self.assertEqual(d["conversation"], {"status": "replaced", "added": 2, "removed": 2})
        self.assertEqual(d["summary"]["removed"], 2)


class TestDevtoolsFlag(unittest.TestCase):
    def test_parse(self):
        for value in ("1", "true", "TRUE", "yes", "on", " On "):
            self.assertTrue(_parse_devtools_flag(value), value)
        for value in ("", "0", "false", "no", "off", "anything"):
            self.assertFalse(_parse_devtools_flag(value), value)


class TestDevtoolsApi(unittest.TestCase):
    """API 测试:裸 FastAPI + build_router,不 import backend.main.app(避免污染全局 engine)。"""

    def setUp(self):
        self._cfg = tempfile.NamedTemporaryFile(suffix=".toml", delete=False)
        self._cfg.close()
        os.environ["EIDOLON_RUNTIME_CONFIG"] = self._cfg.name
        self._make_client()

    def _make_client(self):
        self.engine = RuntimeEngine()
        app = FastAPI()
        app.include_router(build_router(self.engine))
        self.client = TestClient(app)

    def tearDown(self):
        os.environ.pop("EIDOLON_RUNTIME_CONFIG", None)
        try:
            os.unlink(self._cfg.name)
        except OSError:
            pass

    def _seed(self):
        mgr = self.engine.context_manager
        mgr.set_static("character_prompt", "人格")
        mgr.set_high("emotion", "平静")

    def test_first_get_writes_previous(self):
        r = self.client.get("/api/devtools/context")
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertEqual(d["vs_previous"], {"reason": "no_previous"})
        self.assertEqual(d["vs_baseline"], {"reason": "no_baseline"})
        self.assertFalse(d["baseline_set"])
        self.assertIsNone(d["previous_at"])
        # 第二次 GET 已有 previous(自动推进语义)
        d2 = self.client.get("/api/devtools/context").json()
        self.assertIn("units", d2["vs_previous"])
        self.assertTrue(d2["vs_previous"]["prefix_cache_hit"])

    def test_diff_after_context_change(self):
        self._seed()
        self.client.get("/api/devtools/context")
        self.engine.context_manager.set_static("character_prompt", "人格 v2")
        d = self.client.get("/api/devtools/context").json()
        self.assertFalse(d["vs_previous"]["prefix_cache_hit"])
        self.assertEqual(d["vs_previous"]["prefix_break_reason"], "segment_changed")

    def test_baseline_flow(self):
        self._seed()
        r = self.client.post("/api/devtools/context/baseline")
        self.assertEqual(r.status_code, 200)
        d = self.client.get("/api/devtools/context").json()
        self.assertTrue(d["baseline_set"])
        self.assertIn("units", d["vs_baseline"])
        # 修改后 vs baseline 显示 miss
        self.engine.context_manager.set_static("character_prompt", "人格 v2")
        d2 = self.client.get("/api/devtools/context").json()
        self.assertFalse(d2["vs_baseline"]["prefix_cache_hit"])
        # 清除
        self.assertEqual(self.client.delete("/api/devtools/context/baseline").status_code, 200)
        self.assertFalse(self.client.get("/api/devtools/context").json()["baseline_set"])
        self.assertEqual(self.client.delete("/api/devtools/context/baseline").status_code, 404)

    def test_segment_endpoint(self):
        self._seed()
        r = self.client.get("/api/devtools/context/segment/character_prompt?against=previous")
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["current_text"], "人格")
        self.assertIsNone(r.json()["previous_text"])
        # 推进 previous 后修改,带出旧文本与状态
        self.client.get("/api/devtools/context")
        self.engine.context_manager.set_static("character_prompt", "人格 v2")
        d = self.client.get("/api/devtools/context/segment/character_prompt?against=previous").json()
        self.assertEqual(d["status"], "changed")
        self.assertEqual(d["previous_text"], "人格")
        self.assertEqual(d["current_text"], "人格 v2")
        # 404 与非法 against
        self.assertEqual(self.client.get("/api/devtools/context/segment/nope").status_code, 404)
        self.assertEqual(
            self.client.get("/api/devtools/context/segment/character_prompt?against=bogus").status_code,
            400,
        )

    def test_turn_endpoint(self):
        mgr = self.engine.context_manager
        mgr.add_message("user", "你好")
        mgr.add_message("assistant", "你好呀")
        d = self.client.get("/api/devtools/context/turn/-1").json()
        self.assertEqual(d["text"], "你好呀")
        self.assertEqual(d["role"], "assistant")
        d0 = self.client.get("/api/devtools/context/turn/0").json()
        self.assertEqual(d0["text"], "你好")
        self.assertEqual(self.client.get("/api/devtools/context/turn/-3").status_code, 404)
        self.assertEqual(self.client.get("/api/devtools/context/turn/2").status_code, 404)

    def test_messages_endpoint(self):
        self._seed()
        mgr = self.engine.context_manager
        mgr.add_message("user", "你好")
        d = self.client.get("/api/devtools/context/messages").json()
        expected = mgr.compile()
        self.assertEqual([m["content"] for m in d["messages"]], [m["content"] for m in expected])
        self.assertEqual(d["count"], len(expected))
        self.assertTrue(d["notes"])

    def test_state_is_memory_only(self):
        """新 router/引擎无任何历史 —— 状态只存内存(模拟进程重启)。"""
        self._seed()
        self.client.get("/api/devtools/context")
        self.client.post("/api/devtools/context/baseline")
        self.assertTrue(self.client.get("/api/devtools/context").json()["baseline_set"])
        # 重建 router/engine(新进程)
        self._make_client()
        d = self.client.get("/api/devtools/context").json()
        self.assertEqual(d["vs_previous"], {"reason": "no_previous"})
        self.assertFalse(d["baseline_set"])


if __name__ == "__main__":
    unittest.main()
