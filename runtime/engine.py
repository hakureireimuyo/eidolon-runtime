"""对话引擎:运行时组合层的会话核心。

架构(三层抽象):
- 生成器层(runtime.generators)把 LLM 调度封装为独立可输入输出的对象,
  每个生成器自管理自己的 ContextManager;
- ContextManager 管理上下文分层 + 编译 messages(不再手拼);
- LLMGateway 封装底层 LLM provider 差异(不再直接调 llm_chat)。

engine 只负责:
1. 加载工程包(经数据解析容器 runtime.resources 打开一次、全量解析)
   → 取角色资源(容器中按类型标签取到的一个数据对象)
   → 构造会话及其生成器(当前:角色对话生成器)
2. 会话管理:select() 切换当前会话,chat/reset 委托当前会话的生成器

多会话(QQ 式多角色):每个已加载角色一份 Session(独立生成器集合 +
独立对话历史),按角色名 key 索引,同名重载即替换。角色身份 = 模板
(永久定义),对话历史 = 运行时状态,二者严格分离。
包中没有角色数据块时加载照常成功(character=None),仅对话时要求角色。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Optional

from eidolon_character_service import (
    CharacterLoadError,
    CharacterBundle,
    build_system_prompt,
    character_info as build_character_info,
)
from .generators import Generator, DialogueGenerator
from .resources import (
    CHARACTER_TYPE,
    ResourceRecord,
    ResourceSpace,
    load_package,
)
from .llm_gateway import LLMGateway
from .context import ContextManager

# 重新导出,保持原有 API 兼容
__all__ = ["RuntimeEngine", "ChatMessage", "CharacterLoadError"]


@dataclass
class ChatMessage:
    """对话消息(向后兼容的数据结构)。

    content 为解析后纯文本(值已替换、样式剥离);segments 为解析后
    带样式片段(前端渲染用),user 消息恒为 None。
    """

    role: str  # "user" | "assistant"
    content: str
    segments: Optional[list] = None
    ts: float = field(default_factory=time.time)


@dataclass
class Session:
    """一个已加载角色的会话:包视图 + 生成器集合 + 独立对话历史。

    会话是高于 LLM 会话的一层概念:一个角色对应一个会话,会话下挂
    多个生成器(当前仅角色对话生成器),每个生成器自管理自己的上下文。
    角色身份 = 模板(永久定义),对话历史 = 运行时状态,二者严格分离;
    每个角色一份 Session 互不干扰,key 为角色名(同名重载即替换该会话)。
    """

    key: str
    space: ResourceSpace
    bundle: Optional[CharacterBundle]
    character: Any
    assets: dict[str, bytes]
    asset_types: dict[str, Optional[str]]
    manifest: dict
    generators: dict[str, Generator] = field(default_factory=dict)
    history: list[ChatMessage] = field(default_factory=list)


class RuntimeEngine:
    """一个运行时会话集合:持有多个已加载角色 + 各自的生成器与历史。

    每个角色一份 Session(独立生成器集合 + 独立历史),按角色名 key
    索引,同名重载即替换;select() 切换当前会话,对话/重置只作用于
    当前会话。LLM 调度(上下文管理 + 网关调用)全部封装在生成器层,
    engine 只按生成器接口收发数据。
    """

    def __init__(self, *, gateway: LLMGateway | None = None) -> None:
        # 多会话:key → Session(QQ 式多角色列表)
        self._sessions: dict[str, Session] = {}
        self._active_key: Optional[str] = None
        self.space: Optional[ResourceSpace] = None
        self.bundle: Optional[CharacterBundle] = None
        self.character = None
        # 以下均为当前会话 space/bundle 的派生兼容视图(backend 与历史调用方继续可用)
        self.assets: dict[str, bytes] = {}
        self.asset_types: dict[str, Optional[str]] = {}
        self.manifest: dict = {}

        # LLM 网关(全引擎共享、无状态);生成器按会话各自持有上下文
        self._gateway = gateway or LLMGateway()
        # 当前会话的对话生成器(兼容视图同步目标;未加载任何会话时为 None)
        self._current_generator: Optional[Generator] = None
        # 兼容视图:当前生成器的上下文(devtools 旧入口/历史调用方继续可用)
        self._context: ContextManager = ContextManager()

        # 对话历史(向后兼容 history 属性)——当前会话历史的引用视图,
        # 实际数据存储在会话自己的对话生成器上下文中。
        self.history: list[ChatMessage] = []

    # ---- 加载 ----

    def load(self, path: str) -> dict:
        # 数据解析容器:打开一次、全量解析,角色只是其中一个数据对象
        try:
            space = load_package(path)
        except Exception as exc:  # noqa: BLE001 - 非 Cartridge 包 / 文件不可读
            raise CharacterLoadError(f"无法打开包:{exc}") from exc

        record = space.first(CHARACTER_TYPE, typed_only=True)
        bundle = (
            space.context.extras.get("character_bundle")
            if record is not None
            else None
        )
        character = record.value if record is not None else None
        # 派生兼容视图:媒体字节已在容器内存中,零拷贝
        assets = dict(space.media)
        asset_types = dict(space.media_types)
        manifest = space.manifest

        # 会话 key:角色名优先,无角色数据块回退包名 / 占位(同名重载即替换)
        key = (
            (character.identity.name if character is not None else None)
            or space.name
            or "未命名"
        )

        # 新会话:生成器集合 + 空历史(每次加载都是新会话)。
        # 当前仅角色对话生成器;未来剧情/环境生成器在此登记。
        generators: dict[str, Generator] = {}
        if character is not None:
            gen = DialogueGenerator(
                character,
                gateway=self._gateway,
                system_prompt=build_system_prompt(character),
            )
            generators[gen.id] = gen

        session = Session(
            key=key,
            space=space,
            bundle=bundle,
            character=character,
            assets=assets,
            asset_types=asset_types,
            manifest=manifest,
            generators=generators,
        )
        self._sessions[key] = session
        self._activate(session)
        return self.character_info()

    # ---- 会话切换(多角色) ----

    def _activate(self, session: Session) -> None:
        """把会话视图同步到引擎的兼容属性(引用共享,零拷贝)。"""
        self._active_key = session.key
        self.space = session.space
        self.bundle = session.bundle
        self.character = session.character
        self.assets = session.assets
        self.asset_types = session.asset_types
        self.manifest = session.manifest
        self._current_generator = session.generators.get(DialogueGenerator.id)
        self._context = (
            self._current_generator.context
            if self._current_generator is not None
            else ContextManager()
        )
        self.history = session.history

    def select(self, key: str) -> dict:
        """切换到指定会话(按角色名 key),返回该角色的信息与历史。"""
        session = self._sessions.get(key)
        if session is None:
            raise CharacterLoadError(f"未找到会话:{key}")
        self._activate(session)
        return self.character_info()

    def characters(self) -> list[dict]:
        """已加载角色列表(侧栏用):附头像 data URI(内存实例直出)。"""
        out: list[dict] = []
        for key, s in self._sessions.items():
            if s.character is None:
                continue
            c = s.character.identity
            out.append(
                {
                    "key": key,
                    "name": c.name,
                    "nickname": c.nickname,
                    "avatar": self._avatar_uri(s.bundle),
                    "active": key == self._active_key,
                }
            )
        return out

    @staticmethod
    def _avatar_uri(bundle: Optional[CharacterBundle]) -> Optional[str]:
        """侧栏头像:purpose 依次 avatar → portrait → cover,再任意图片。"""
        if bundle is None:
            return None
        for getter in (bundle.get_avatar, bundle.get_portrait, bundle.get_cover):
            ad = getter()
            if ad is not None and ad.data is not None:
                return ad.data_uri()
        for ad in bundle.assets.values():
            if ad.data is not None and (ad.type or "").startswith("image/"):
                return ad.data_uri()
        return None

    # ---- 角色卡信息(供前端展示) ----

    def character_info(self, *, include_data: bool = True) -> dict:
        if self.character is None:
            info = {"loaded": False}
        else:
            # 优先传自包含 bundle:assets 附带真实字节(base64 data URI 可控开关)
            info = build_character_info(
                self.bundle if self.bundle is not None else self.character,
                include_data=include_data,
            )
        info["package"] = self._package_summary()
        if self._active_key is not None:
            info["key"] = self._active_key
            info["history"] = [
                {
                    "role": m.role,
                    "content": m.content,
                    **(
                        {"segments": m.segments}
                        if m.segments is not None
                        else {}
                    ),
                }
                for m in self.history
            ]
        return info

    def _package_summary(self) -> Optional[dict]:
        if self.space is None:
            return None
        return {
            "id": self.space.id,
            "name": self.space.name,
            "resources": len(self.space.records),
            "media": len(self.space.media),
        }

    # ---- 容器视图(资源空间:工程包的全部数据对象) ----

    def resource_report(self) -> dict:
        """当前工程包的资源空间报告(未加载任何包时返回零值报告)。"""
        if self.space is None:
            return {
                "package": {
                    "id": "",
                    "name": "",
                    "container_version": None,
                    "source": "",
                },
                "counts": {
                    "total": 0,
                    "usable": 0,
                    "typed": 0,
                    "media": 0,
                    "by_status": {},
                    "by_kind": {},
                },
                "types": {},
                "resources": [],
                "media": [],
            }
        return self.space.report()

    def create_resource(
        self,
        type_value: str,
        data: Any = None,
        *,
        id: Optional[str] = None,
        version: Optional[str] = None,
        path: Optional[str] = None,
        required: bool = False,
    ) -> ResourceRecord:
        """在已加载的工程包中动态创建一份资源(容器能力)。"""
        if self.space is None:
            raise CharacterLoadError("尚未加载任何包,无法创建资源。")
        return self.space.create(
            type_value,
            data,
            id=id,
            version=version,
            path=path,
            required=required,
        )

    # ---- 对话历史 ----

    def reset(self) -> None:
        """清空当前会话的对话历史(保留已加载角色和静态上下文)。"""
        if self._current_generator is not None:
            self._current_generator.reset()
        self.history.clear()  # 引用视图:清空会话自己的历史列表

    def chat(self, user_message: str) -> dict:
        """处理一条用户消息,返回模型回复与对话历史(委托角色对话生成器)。"""
        if self.character is None:
            raise CharacterLoadError("尚未加载角色卡,请先加载 .cart / .png。")
        assert self._current_generator is not None  # character 非空 ⇒ 对话生成器存在
        result = self._current_generator.generate({"message": user_message})
        self._sync_history(result["history"])
        return result

    def chat_stream(self, user_message: str):
        """流式对话:产出协议事件 dict(SSE 适配层直接序列化)。

        事件:chat.start → loop.turn / text.delta / tool.* / chat.done|error。
        流结束后历史镜像与生成器上下文同步(无论成功与否)。
        """
        if self.character is None:
            raise CharacterLoadError("尚未加载角色卡,请先加载 .cart / .png。")
        assert self._current_generator is not None  # character 非空 ⇒ 对话生成器存在
        yield {"type": "chat.start", "session_key": self._active_key}
        yield from self._current_generator.generate_stream(
            {"message": user_message}
        )
        # 流结束(成功 / 失败 / 取消):history 镜像就地重填,保持引用一致
        self._sync_history(self._current_generator.history)

    def _sync_history(self, history: list[dict]) -> None:
        """把生成器历史镜像同步到会话历史列表(segments 一并携带)。"""
        self.history[:] = [
            ChatMessage(
                role=m["role"],
                content=m["content"],
                segments=m.get("segments"),
            )
            for m in history
        ]

    # ---- 诊断信息 ----

    def context_cache_info(self) -> dict:
        """返回上下文缓存状态(供调试 / 性能优化)。"""
        return self._context.cache_info()

    def llm_provider(self) -> str:
        """当前使用的 LLM provider 名称。"""
        return self._gateway.provider

    @property
    def context_manager(self) -> ContextManager:
        """当前会话对话生成器的上下文(兼容入口;devtools 请用 resolve_generator)。"""
        return self._context

    # ---- 生成器视图(开发者工具只读入口) ----

    @property
    def active_key(self) -> Optional[str]:
        """当前激活会话 key(未加载任何会话时为 None)。"""
        return self._active_key

    def session_keys(self) -> list[str]:
        """全部会话 key(加载顺序)。"""
        return list(self._sessions)

    def session_generators(self, session_key: str) -> list[dict]:
        """指定会话的生成器清单(开发者工具选择器数据源)。"""
        session = self._sessions.get(session_key)
        if session is None:
            raise CharacterLoadError(f"未找到会话:{session_key}")
        out: list[dict] = []
        for gen in session.generators.values():
            ctx = gen.context
            out.append(
                {
                    "id": gen.id,
                    "label": gen.label,
                    "turns": len(ctx.conversation_turns),
                    "segment_count": len(ctx.ir),
                    "segment_chars": ctx.ir.total_text_length,
                }
            )
        return out

    def resolve_generator(self, session_key: str, generator_id: str) -> Generator:
        """按 会话 key + 生成器 id 解析生成器(开发者工具用)。"""
        session = self._sessions.get(session_key)
        if session is None:
            raise CharacterLoadError(f"未找到会话:{session_key}")
        gen = session.generators.get(generator_id)
        if gen is None:
            raise CharacterLoadError(
                f"会话 {session_key} 无生成器:{generator_id}"
            )
        return gen
