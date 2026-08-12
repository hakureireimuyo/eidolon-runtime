"""类型标签寻址:MIME 风格 type 的解析与模式匹配打分。

资源路由的**寻址基础**。协议层(cartridge)只把 `entry.type` 当作一个不透明
的路由标签原样传递；运行时在此定义"标签如何被匹配",从而做到:

- 消费方不需要预先知道包里有哪些 type,只需注册"我能处理哪一类标签"；
- 具体标签(`application/x-eidolon-character`)优先于宽泛标签
  (`application/x-eidolon-*` > `application/*` > `*/*`),
  于是永远存在一个兜底 handler,未知数据也不会加载失败。

打分规则(分数越高越具体,None 表示不匹配):

    application/x-eidolon-character  ->  5000   精确
    application/x-eidolon-*          ->  1464   子类型前缀通配
    application/*+json               ->  1416   结构化后缀通配
    application/*                    ->  1400   主类型限定
    */*                              ->     0   全局兜底
"""

from __future__ import annotations

import re
from typing import Optional

WILDCARD = "*/*"

# 扩展名 -> MIME,用于推断 manifest 未声明文件的类型("孤儿文件"自动适配)。
EXTENSION_TYPES: dict[str, str] = {
    ".json": "application/json",
    ".jsonl": "application/x-ndjson",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
    ".toml": "application/toml",
    ".txt": "text/plain",
    ".md": "text/markdown",
    ".csv": "text/csv",
    ".py": "text/x-python",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".svg": "image/svg+xml",
    ".wav": "audio/wav",
    ".mp3": "audio/mpeg",
    ".ogg": "audio/ogg",
    ".mp4": "video/mp4",
    ".bin": "application/octet-stream",
}

DEFAULT_TYPE = "application/octet-stream"


def normalize(value: Optional[str]) -> str:
    """去掉参数与大小写差异:`Application/JSON; charset=utf-8` -> `application/json`。"""
    if not value:
        return ""
    return str(value).split(";", 1)[0].strip().lower()


def split(value: Optional[str]) -> tuple[str, str, str]:
    """拆成 (主类型, 子类型, 结构化后缀)。

    `application/x-eidolon-world+json` -> `("application", "x-eidolon-world", "json")`
    """
    mime = normalize(value)
    if "/" not in mime:
        return (mime, "", "")
    main, _, rest = mime.partition("/")
    sub, plus, suffix = rest.partition("+")
    return (main, sub, suffix if plus else "")


def guess_type(path: str) -> str:
    """按文件扩展名推断类型标签(用于 manifest 未声明的包内文件)。"""
    lowered = (path or "").lower()
    for ext, mime in EXTENSION_TYPES.items():
        if lowered.endswith(ext):
            return mime
    return DEFAULT_TYPE


def _segment_score(pattern_seg: str, value_seg: str) -> Optional[int]:
    """单段(主类型或子类型)匹配打分。"""
    if pattern_seg == value_seg:
        return 1000
    if "*" not in pattern_seg:
        return None
    regex = "^" + ".*".join(re.escape(part) for part in pattern_seg.split("*")) + "$"
    if re.match(regex, value_seg):
        # 通配符之外的字面字符越多,模式越具体
        return 100 + len(pattern_seg.replace("*", ""))
    return None


def match_score(pattern: str, value: str) -> Optional[int]:
    """`pattern` 匹配类型 `value` 的特异度分数；不匹配返回 None。

    子类型的权重高于主类型(乘 4),保证 `application/x-eidolon-*`
    比 `application/*` 更具体。
    """
    pat = normalize(pattern)
    val = normalize(value)
    if pat in ("", "*", WILDCARD):
        return 0
    if not val:
        val = DEFAULT_TYPE
    if "/" not in pat:
        pat = "*/" + pat
    if "/" not in val:
        val = val + "/"
    p_main, p_sub = pat.split("/", 1)
    v_main, v_sub = val.split("/", 1)
    main = _segment_score(p_main, v_main)
    sub = _segment_score(p_sub, v_sub)
    if main is None or sub is None:
        return None
    return main + sub * 4


def matches(pattern: str, value: str) -> bool:
    """是否匹配(忽略特异度)。"""
    return match_score(pattern, value) is not None
