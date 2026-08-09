"""对话引擎：运行时层的核心。

- 从 Character 构建 system prompt（角色设定 -> 给模型的「你是谁」）；
- 维护对话历史（运行时可变状态，不属于角色卡模板）；
- 驱动最基础对话：把 [system, 历史..., user] 交给 LLM，取回回复。

角色身份 = 模板（永久定义），对话历史 = 运行时状态，二者严格分离
（见 eidolon-character 约定与 docs/project-responsibilities.md §3.4）。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

# 先导入 loader：它负责把同级 PersonaSeed / eidolon-character 注入 sys.path，
# 之后才能安全 import eidolon_character。
from .loader import load_character_file, CharacterLoadError
from .llm import chat as llm_chat, LLMUnconfigured, LLMError
from eidolon_character.model import Character  # noqa: E402


@dataclass
class ChatMessage:
    role: str  # "user" | "assistant"
    content: str
    ts: float = field(default_factory=time.time)


def build_system_prompt(c: Character) -> str:
    """把角色设定编译成一段 system prompt。"""
    idn = c.identity
    lines: list[str] = []
    lines.append("你正在扮演一个虚构角色，与用户进行对话。请始终严格保持在角色之内：")
    lines.append("- 不要承认自己是人工智能、语言模型或程序；")
    lines.append("- 不要跳出角色，也不要用括号描写动作（除非角色设定本就如此）；")
    lines.append("- 以第一人称、符合角色性格与背景的方式回应。")
    lines.append("")
    lines.append("# 角色设定")
    if idn.name:
        lines.append(f"姓名：{idn.name}")
    if idn.nickname:
        lines.append(f"昵称：{idn.nickname}")
    if idn.gender:
        lines.append(f"性别：{idn.gender}")
    if idn.age is not None:
        lines.append(f"年龄：{idn.age}")
    if idn.species:
        lines.append(f"物种：{idn.species}")

    if c.background.summary:
        lines.append(f"\n# 背景\n{c.background.summary}")
    bg_extra: list[str] = []
    if c.background.occupation:
        bg_extra.append(f"职业：{c.background.occupation}")
    if c.background.location:
        bg_extra.append(f"所在地：{c.background.location}")
    if bg_extra:
        lines.append("\n".join(bg_extra))

    if c.appearance.description:
        lines.append(f"\n# 外貌\n{c.appearance.description}")
    if c.appearance.features:
        lines.append("外貌特征：" + "、".join(c.appearance.features))

    if c.personality.description:
        lines.append(f"\n# 性格\n{c.personality.description}")
    if c.personality.traits:
        lines.append("性格特质：" + "、".join(c.personality.traits))
    if c.personality.values:
        lines.append("价值观：" + "、".join(c.personality.values))

    if c.dialogue.style:
        lines.append(f"\n# 说话风格\n{c.dialogue.style}")

    if c.dialogue.examples:
        lines.append("\n# 对话示例")
        for ex in c.dialogue.examples:
            lines.append(f"用户：{ex.user}")
            lines.append(f"{idn.name}：{ex.assistant}")

    lines.append("\n请基于以上设定回应用户。")
    return "\n".join(lines)


class RuntimeEngine:
    """一个运行时会话：持有一个已加载角色 + 一段对话历史。"""

    def __init__(self) -> None:
        self.character: Optional[Character] = None
        self.assets: dict[str, bytes] = {}
        self.asset_types: dict[str, Optional[str]] = {}
        self.manifest: dict = {}
        self.history: list[ChatMessage] = []

    # ---- 加载 ----
    def load(self, path: str) -> dict:
        character, assets, manifest = load_character_file(path)
        self.character = character
        self.assets = assets
        self.manifest = manifest
        self.asset_types = {a.id: a.type for a in character.assets}
        self.history = []
        return self.character_info()

    # ---- 角色卡信息（供前端展示） ----
    def character_info(self) -> dict:
        if self.character is None:
            return {"loaded": False}
        c = self.character
        return {
            "loaded": True,
            "name": c.identity.name,
            "nickname": c.identity.nickname,
            "gender": c.identity.gender,
            "age": c.identity.age,
            "species": c.identity.species,
            "background": {
                "summary": c.background.summary,
                "occupation": c.background.occupation,
                "location": c.background.location,
            },
            "appearance": {
                "description": c.appearance.description,
                "features": c.appearance.features,
            },
            "personality": {
                "description": c.personality.description,
                "traits": c.personality.traits,
                "values": c.personality.values,
            },
            "dialogue": {
                "style": c.dialogue.style,
                "greeting": c.dialogue.greeting,
                "examples": [
                    {"user": e.user, "assistant": e.assistant}
                    for e in c.dialogue.examples
                ],
            },
            "assets": [
                {"id": a.id, "type": a.type, "purpose": a.purpose, "caption": a.caption}
                for a in c.assets
            ],
            "greeting": c.dialogue.greeting,
        }

    # ---- 对话历史 ----
    def reset(self) -> None:
        self.history = []

    def chat(self, user_message: str) -> dict:
        if self.character is None:
            raise CharacterLoadError("尚未加载角色卡，请先加载 .seed / .png。")

        system_prompt = build_system_prompt(self.character)
        messages: list[dict] = [{"role": "system", "content": system_prompt}]
        for m in self.history:
            messages.append({"role": m.role, "content": m.content})
        messages.append({"role": "user", "content": user_message})

        reply = llm_chat(messages)

        self.history.append(ChatMessage(role="user", content=user_message))
        self.history.append(ChatMessage(role="assistant", content=reply))
        return {
            "reply": reply,
            "history": [{"role": m.role, "content": m.content} for m in self.history],
        }
