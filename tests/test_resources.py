"""资源路由框架测试（零网络）。

覆盖框架的四条承诺：
1. 未知类型自动适配（含 manifest 未声明的孤儿文件）；
2. 版本不匹配时自动迁移 / 前向兼容 / 降级，且整包加载永不中断；
3. 运行时动态定义类型与动态创建资源；
4. 写回时未知数据零丢失。
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest

import cartridge as cart
from eidolon_character.builder import build_cart
from eidolon_character.model import Character, Dialogue, Identity

from runtime.engine import RuntimeEngine
from runtime.loader import CharacterLoadError
from runtime.resources import (
    CHARACTER_TYPE,
    STATUS_DEGRADED,
    STATUS_FORWARD,
    STATUS_GENERIC,
    STATUS_LOADED,
    STATUS_MIGRATED,
    DynamicResource,
    ResourceRegistry,
    Version,
    VersionRange,
    install_builtins,
    load_package,
    match_score,
)

WORLD_TYPE = "application/x-eidolon-world"


def fresh_registry(name: str = "test") -> ResourceRegistry:
    """独立注册表，避免测试之间互相污染。"""
    return install_builtins(ResourceRegistry(name))


def make_package(entries=(), *, raw_files=None, name="测试工程"):
    """构造一个临时 .cart，返回路径。entries: [(id, type, dict, version)]"""
    pkg = cart.create_package(name)
    for entry_id, type_value, payload, version in entries:
        data = (
            payload
            if isinstance(payload, (bytes, bytearray))
            else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        )
        pkg.add_entry(entry_id, type_value, data, version=version)
    for path, data in (raw_files or {}).items():
        pkg.raw_files[path] = data
    fd, path = tempfile.mkstemp(suffix=".cart")
    os.close(fd)
    cart.write_cart(pkg, path)
    return path


class TestTypeRouting(unittest.TestCase):
    def test_specificity_order(self):
        target = "application/x-eidolon-character"
        exact = match_score(target, target)
        prefix = match_score("application/x-eidolon-*", target)
        family = match_score("application/*", target)
        fallback = match_score("*/*", target)
        self.assertGreater(exact, prefix)
        self.assertGreater(prefix, family)
        self.assertGreater(family, fallback)
        self.assertEqual(fallback, 0)

    def test_non_matching_pattern(self):
        self.assertIsNone(match_score("image/*", "application/json"))

    def test_structured_suffix(self):
        self.assertIsNotNone(match_score("*/*+json", "application/x-eidolon-world+json"))

    def test_later_registration_wins_on_tie(self):
        reg = fresh_registry("tie")

        @reg.handler(WORLD_TYPE, name="first")
        def first(data, descriptor, context=None):
            return "first"

        @reg.handler(WORLD_TYPE, name="second")
        def second(data, descriptor, context=None):
            return "second"

        handler, _ = reg.resolve(WORLD_TYPE)
        self.assertEqual(handler.name, "second")

    def test_fallback_always_available(self):
        reg = fresh_registry("fallback")
        handler, score = reg.resolve("application/vnd.made-up-thing")
        self.assertIsNotNone(handler)
        self.assertFalse(reg.supports("video/x-unknown-codec"))


class TestUnknownDataAdaptation(unittest.TestCase):
    """不预定义任何内容，也要能读出数据。"""

    def test_unknown_type_becomes_navigable(self):
        path = make_package(
            [("world", WORLD_TYPE, {"name": "废土", "regions": [{"name": "北境"}]}, "1.0")]
        )
        try:
            space = load_package(path, registry=fresh_registry("unknown"))
            record = space.get("world")
            self.assertEqual(record.status, STATUS_GENERIC)
            self.assertTrue(record.is_usable)
            self.assertFalse(record.is_typed)
            world = record.value
            self.assertIsInstance(world, DynamicResource)
            self.assertEqual(world.name, "废土")
            self.assertEqual(world["regions.0.name"], "北境")
            self.assertIsNone(world.not_declared_at_all)
            self.assertEqual(world.schema()["regions"], [{"name": "string"}])
        finally:
            os.unlink(path)

    def test_undeclared_files_are_discovered(self):
        path = make_package(
            [("world", WORLD_TYPE, {"name": "废土"}, "1.0")],
            raw_files={
                "data/quests.json": json.dumps({"q1": {"title": "寻找灯塔"}}).encode(),
                "notes.md": "# 手写笔记".encode("utf-8"),
            },
        )
        try:
            space = load_package(path, registry=fresh_registry("orphan"))
            self.assertIn("quests", space)
            self.assertIn("notes", space)
            self.assertEqual(space.get("quests").kind, "file")
            self.assertEqual(space.value("quests").get("q1.title"), "寻找灯塔")
            self.assertIn("手写笔记", space.value("notes"))
        finally:
            os.unlink(path)

    def test_binary_and_broken_json_do_not_break_loading(self):
        path = make_package(
            [
                ("good", WORLD_TYPE, {"name": "ok"}, "1.0"),
                ("broken", "application/json", b"{ not json at all", None),
                ("blob", "application/octet-stream", b"\x00\x01\x02", None),
            ]
        )
        try:
            space = load_package(path, registry=fresh_registry("broken"))
            self.assertEqual(len(space), 3)
            self.assertEqual(space.get("good").value.name, "ok")
            self.assertEqual(space.get("broken").status, STATUS_DEGRADED)
            self.assertTrue(space.get("broken").diagnostics)
            self.assertEqual(space.value("blob"), b"\x00\x01\x02")
        finally:
            os.unlink(path)

    def test_missing_entry_goes_to_report(self):
        pkg = cart.create_package("缺块工程")
        pkg.add_entry("ghost", WORLD_TYPE, b"{}", version="1.0")
        del pkg.raw_files["data/ghost.bin"]
        pkg.entries.clear()
        fd, path = tempfile.mkstemp(suffix=".cart")
        os.close(fd)
        cart.write_cart(pkg, path)
        try:
            space = load_package(path, registry=fresh_registry("missing"))
            missing = space.context.extras["missing"]
            self.assertEqual(len(missing), 1)
            self.assertEqual(missing[0]["id"], "ghost")
        finally:
            os.unlink(path)


class TestVersioning(unittest.TestCase):
    def test_version_range_parsing(self):
        self.assertTrue(VersionRange.parse("^1.2").contains("1.9"))
        self.assertFalse(VersionRange.parse("^1.2").contains("2.0"))
        self.assertTrue(VersionRange.parse("~1.2").contains("1.2.9"))
        self.assertFalse(VersionRange.parse("~1.2").contains("1.3"))
        self.assertTrue(VersionRange.parse(">=1.0,<2.0").contains("1.5"))
        self.assertTrue(VersionRange.parse("1.x").contains("1.7"))
        self.assertTrue(VersionRange.parse("*").contains("42.0"))

    def test_unparsable_version_is_tolerated(self):
        v = Version.parse("not-a-version")
        self.assertFalse(v.parsed)
        self.assertEqual(v.raw, "not-a-version")

    def test_migration_chain(self):
        reg = fresh_registry("migrate")
        reg.define(WORLD_TYPE, version="3.0", defaults={"regions": []})

        @reg.migration(WORLD_TYPE, frm="<2.0", to="2.0")
        def to_v2(data, context=None):
            data["regions"] = data.pop("areas", [])
            return data

        @reg.migration(WORLD_TYPE, frm=">=2.0,<3.0", to="3.0")
        def to_v3(data, context=None):
            data["era"] = data.get("era", "第三纪")
            return data

        path = make_package([("world", WORLD_TYPE, {"name": "旧世界", "areas": [1]}, "1.0")])
        try:
            space = load_package(path, registry=reg)
            record = space.get("world")
            self.assertEqual(record.status, STATUS_MIGRATED)
            self.assertEqual(len(record.migrations), 2)  # 1.0 -> 2.0 -> 3.0
            self.assertEqual(record.effective_version, "3.0.0")
            self.assertEqual(record.value.regions, [1])
            self.assertEqual(record.value.era, "第三纪")
        finally:
            os.unlink(path)

    def test_forward_compatibility_keeps_unknown_fields(self):
        reg = fresh_registry("forward")
        reg.define(WORLD_TYPE, version="1.0")
        path = make_package(
            [("world", WORLD_TYPE, {"name": "未来", "quantum_layer": {"depth": 7}}, "9.9")]
        )
        try:
            record = load_package(path, registry=reg).get("world")
            self.assertEqual(record.status, STATUS_FORWARD)
            self.assertTrue(record.is_typed)
            self.assertEqual(record.value.get("quantum_layer.depth"), 7)
            self.assertTrue(record.diagnostics)
        finally:
            os.unlink(path)

    def test_backward_version_without_migration_degrades(self):
        reg = fresh_registry("backward")
        reg.define(WORLD_TYPE, version="5.0")
        path = make_package([("world", WORLD_TYPE, {"name": "远古"}, "1.0")])
        try:
            record = load_package(path, registry=reg).get("world")
            self.assertEqual(record.status, STATUS_DEGRADED)
            self.assertEqual(record.value.name, "远古")  # 数据仍然读得到
        finally:
            os.unlink(path)

    def test_handler_exception_degrades_instead_of_crashing(self):
        reg = fresh_registry("boom")

        @reg.handler(WORLD_TYPE, name="exploding")
        def boom(data, descriptor, context=None):
            raise RuntimeError("处理器内部错误")

        path = make_package(
            [
                ("world", WORLD_TYPE, {"name": "废土"}, "1.0"),
                ("other", "application/json", {"ok": True}, None),
            ]
        )
        try:
            space = load_package(path, registry=reg)
            self.assertEqual(space.get("world").status, STATUS_DEGRADED)
            self.assertEqual(space.get("world").value.name, "废土")
            self.assertTrue(space.get("other").is_usable)  # 其它资源不受影响
        finally:
            os.unlink(path)


class TestDynamicDefinition(unittest.TestCase):
    def test_define_type_without_writing_code(self):
        reg = fresh_registry("define")
        reg.define(
            "application/x-eidolon-quest",
            version="1.0",
            required=("title",),
            defaults={"steps": []},
            description="任务模块",
        )
        path = make_package(
            [("quest", "application/x-eidolon-quest", {"title": "寻找灯塔"}, "1.0")]
        )
        try:
            record = load_package(path, registry=reg).get("quest")
            self.assertEqual(record.status, STATUS_LOADED)
            self.assertTrue(record.is_typed)
            self.assertEqual(record.value.title, "寻找灯塔")
            self.assertEqual(record.value.steps, [])  # 默认值补齐
        finally:
            os.unlink(path)

    def test_strict_schema_missing_field_degrades(self):
        reg = fresh_registry("strict")
        reg.define("application/x-eidolon-quest", required=("title",), strict=True)
        path = make_package([("quest", "application/x-eidolon-quest", {"x": 1}, "1.0")])
        try:
            record = load_package(path, registry=reg).get("quest")
            self.assertEqual(record.status, STATUS_DEGRADED)
            self.assertIn("缺少必填字段", " ".join(record.diagnostics))
        finally:
            os.unlink(path)

    def test_plugin_install_hook(self):
        reg = fresh_registry("plugin")

        def register(target):
            target.define("application/x-eidolon-faction", version="1.0")

        reg.install(register)
        self.assertTrue(reg.supports("application/x-eidolon-faction"))


class TestDynamicCreationAndRoundTrip(unittest.TestCase):
    def test_create_and_save_preserves_unknown_data(self):
        reg = fresh_registry("roundtrip")
        path = make_package(
            [
                ("world", WORLD_TYPE, {"name": "废土", "secret": [1, 2, 3]}, "9.9"),
                ("legacy", "application/x-eidolon-legacy", {"keep": "me"}, "0.1"),
            ]
        )
        out = path.replace(".cart", "_out.cart")
        try:
            space = load_package(path, registry=reg)
            record = space.create(
                "application/x-eidolon-quest", {"title": "新任务"}, id="quest"
            )
            self.assertEqual(record.id, "quest")
            space.save(out)

            reloaded = load_package(out, registry=reg)
            self.assertEqual(
                sorted(reloaded.records), ["legacy", "quest", "world"]
            )
            # 运行时看不懂的字段与模块原样保留
            self.assertEqual(reloaded.value("world").get("secret"), [1, 2, 3])
            self.assertEqual(reloaded.value("legacy").get("keep"), "me")
            self.assertEqual(reloaded.value("quest").get("title"), "新任务")
            self.assertEqual(reloaded.get("world").source_version, "9.9")
        finally:
            os.unlink(path)
            if os.path.exists(out):
                os.unlink(out)

    def test_auto_id_generation(self):
        space = load_package(
            make_package([("world", WORLD_TYPE, {"name": "n"}, "1.0")]),
            registry=fresh_registry("autoid"),
        )
        first = space.create("application/x-eidolon-quest", {"title": "a"})
        second = space.create("application/x-eidolon-quest", {"title": "b"})
        self.assertEqual(first.id, "quest")
        self.assertEqual(second.id, "quest-2")
        os.unlink(space.source)


class TestDynamicResource(unittest.TestCase):
    def test_paths_and_defaults(self):
        res = DynamicResource({"a": {"b": [{"c": 1}]}})
        self.assertEqual(res["a.b.0.c"], 1)
        self.assertEqual(res.get("a.b.5.c", "缺省"), "缺省")
        self.assertIsNone(res.nothing_here)
        self.assertIn("a.b", res)

    def test_set_creates_intermediate_levels(self):
        res = DynamicResource()
        res.set("world.regions.count", 3)
        self.assertEqual(res.to_dict(), {"world": {"regions": {"count": 3}}})

    def test_merge_is_deep_and_lossless(self):
        res = DynamicResource({"a": {"x": 1, "y": 2}, "keep": True})
        res.merge({"a": {"y": 9, "z": 3}})
        self.assertEqual(res.to_dict(), {"a": {"x": 1, "y": 9, "z": 3}, "keep": True})


class TestEngineIntegration(unittest.TestCase):
    def _character_seed(self, extra_entries=()):
        fd, path = tempfile.mkstemp(suffix=".cart")
        os.close(fd)
        build_cart(
            Character(
                identity=Identity(name="TestBot"),
                dialogue=Dialogue(greeting="你好"),
            ),
            output_path=path,
        )
        if extra_entries:
            pkg = cart.open(path)
            for entry_id, type_value, payload, version in extra_entries:
                pkg.add_entry(
                    entry_id,
                    type_value,
                    json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                    version=version,
                )
            cart.write_cart(pkg, path)
        return path

    def test_engine_loads_character_and_sibling_resources(self):
        path = self._character_seed(
            extra_entries=[("world", WORLD_TYPE, {"name": "废土"}, "1.0")]
        )
        try:
            eng = RuntimeEngine()
            info = eng.load(path)
            self.assertTrue(info["loaded"])
            self.assertEqual(info["name"], "TestBot")
            self.assertEqual(info["package"]["resources"], 2)
            report = eng.resource_report()
            self.assertEqual(report["counts"]["total"], 2)
            self.assertEqual(eng.space.value("world").name, "废土")
        finally:
            os.unlink(path)

    def test_engine_can_load_package_without_character(self):
        path = make_package([("world", WORLD_TYPE, {"name": "无人世界"}, "1.0")])
        try:
            eng = RuntimeEngine()
            with self.assertRaises(CharacterLoadError):
                eng.load(path)
            info = eng.load(path, require_character=False)
            self.assertFalse(info["loaded"])
            self.assertEqual(info["package"]["resources"], 1)
            self.assertIsNone(eng.character)
        finally:
            os.unlink(path)

    def test_engine_creates_resource_at_runtime(self):
        path = self._character_seed()
        try:
            eng = RuntimeEngine()
            eng.load(path)
            record = eng.create_resource(
                "application/x-eidolon-note", {"text": "运行时新增"}
            )
            self.assertIn(record.id, eng.space)
            self.assertEqual(eng.space.value(record.id).get("text"), "运行时新增")
        finally:
            os.unlink(path)


class TestResourceAPI(unittest.TestCase):
    def setUp(self):
        from fastapi.testclient import TestClient

        from backend.main import app, engine

        self.client = TestClient(app)
        self.engine = engine
        fd, self.path = tempfile.mkstemp(suffix=".cart")
        os.close(fd)
        build_cart(Character(identity=Identity(name="ApiBot")), output_path=self.path)
        pkg = cart.open(self.path)
        pkg.add_entry(
            "world", WORLD_TYPE, json.dumps({"name": "废土"}).encode(), version="1.0"
        )
        cart.write_cart(pkg, self.path)
        engine.load(self.path)

    def tearDown(self):
        try:
            os.unlink(self.path)
        except OSError:
            pass

    def test_list_resources(self):
        data = self.client.get("/api/resources").json()
        self.assertTrue(data["loaded"])
        self.assertEqual(data["counts"]["total"], 2)
        ids = [r["id"] for r in data["resources"]]
        self.assertIn("world", ids)
        self.assertIn("character", ids)

    def test_get_single_resource_with_value(self):
        data = self.client.get("/api/resources/world").json()
        self.assertEqual(data["type"], WORLD_TYPE)
        self.assertEqual(data["value"]["name"], "废土")
        self.assertEqual(self.client.get("/api/resources/nope").status_code, 404)

    def test_define_type_then_create_resource(self):
        r = self.client.post(
            "/api/registry/types",
            json={
                "type": "application/x-eidolon-api-test",
                "version": "1.0",
                "required": ["title"],
                "defaults": {"done": False},
            },
        )
        self.assertEqual(r.status_code, 200)
        created = self.client.post(
            "/api/resources",
            json={
                "type": "application/x-eidolon-api-test",
                "id": "api-quest",
                "data": {"title": "接口创建"},
            },
        ).json()
        self.assertEqual(created["id"], "api-quest")
        self.assertEqual(created["value"]["title"], "接口创建")
        self.assertFalse(created["value"]["done"])
        self.assertTrue(created["typed"])

    def test_registry_report(self):
        data = self.client.get("/api/registry").json()
        names = [h["name"] for h in data["handlers"]]
        self.assertIn("raw", names)
        self.assertIn("eidolon-character", names)

    def test_character_type_is_routed(self):
        record = self.engine.space.first(CHARACTER_TYPE, typed_only=True)
        self.assertIsNotNone(record)
        self.assertEqual(record.value.identity.name, "ApiBot")


if __name__ == "__main__":
    unittest.main()
