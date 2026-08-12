"""生成一个示例角色包(alice.seed),供运行时加载 / 端到端验证。

运行:
    python -m examples.make_sample
"""
from __future__ import annotations

import io
import os
import sys

# 开发期把同级兄弟仓库注入 import 路径(与运行时 loader 一致)。
ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
for name in ("PersonaSeed", "eidolon-character"):
    p = os.path.join(ROOT, name)
    if os.path.isdir(p) and p not in sys.path:
        sys.path.insert(0, p)

from eidolon_character.builder import build_seed
from eidolon_character.model import (
    Appearance,
    Background,
    Character,
    CharacterAsset,
    Dialogue,
    DialogueExample,
    Identity,
    Personality,
)


def _make_portrait() -> bytes:
    """用 Pillow 画一张纯色占位立绘(无外部图片依赖)。失败则返回空。"""
    try:
        from PIL import Image

        img = Image.new("RGB", (256, 256), (110, 168, 254))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue()
    except Exception:  # pragma: no cover
        return b""


def main() -> None:
    character = Character(
        identity=Identity(name="Alice", nickname="小艾", gender="女", age=22, species="人类"),
        background=Background(
            summary="出生在赛博都市 Neo Tokyo 的 AI 工程师,热衷开源。",
            occupation="AI Engineer",
            location="Neo Tokyo",
        ),
        appearance=Appearance(
            description="浅蓝短发,常戴一副细框眼镜,穿白色连帽衫。",
            features=["浅蓝短发", "细框眼镜", "连帽衫"],
        ),
        personality=Personality(
            description="理性、耐心,带着一点毒舌的幽默感。",
            traits=["理性", "耐心", "幽默"],
            values=["自由", "开源", "诚实"],
        ),
        dialogue=Dialogue(
            style="口语化、简洁,偶尔抛技术梗。",
            greeting="嗨,我是 Alice。有什么想聊的？",
            examples=[
                DialogueExample(
                    user="你是做什么的？",
                    assistant="AI 工程师,主要折腾自然语言处理相关的东西。",
                )
            ],
        ),
        assets=[CharacterAsset(id="portrait", type="image/png", purpose="portrait", caption="角色立绘")],
    )

    out = os.path.join(os.path.dirname(__file__), "alice.seed")
    build_seed(
        character,
        images={"portrait": _make_portrait()},
        output_path=out,
        author="eidolon-runtime-example",
    )
    print("已生成示例角色包:", out)


if __name__ == "__main__":
    main()
