"""资源处理器协议：把字节解释成领域对象的唯一扩展点。

Handler 是运行时**唯一**知道"某类数据是什么意思"的地方；内核（registry /
router / space）始终领域无知（见 docs/runtime-core-design.md §1）。

一个 handler 需要回答四件事：

    type_patterns  我认领哪些类型标签（支持通配）
    versions       我支持哪些 Schema 版本（版本范围表达式）
    decode/load    字节 -> 中间结构 -> 领域对象
    encode         领域对象 -> 字节（供动态创建与写回包）

三个现成基类：`JSONHandler`（JSON 编解码）、`FunctionHandler`（把一个函数变成
handler）、`SchemaHandler`（只给出字段声明就能动态生成 handler，不必写代码）。
"""

from __future__ import annotations

import json
from typing import Any, Callable, Optional, Sequence

from .dynamic import DynamicResource, unwrap, wrap


class ResourceHandler:
    """处理器基类。默认行为 = 原样透传字节。"""

    type_patterns: Sequence[str] = ("*/*",)
    versions: str = "*"
    name: str = ""
    description: str = ""
    # 数据版本高于 versions 上界时，是否允许"前向兼容"加载（忽略未知字段）
    forward_compatible: bool = True
    # 通用处理器（只做格式装载、不赋予领域语义），产出的记录标记为 generic
    generic: bool = False

    def __init__(self, **kwargs: Any) -> None:
        for key, value in kwargs.items():
            setattr(self, key, value)
        if not self.name:
            self.name = type(self).__name__

    # ---- 字节 <-> 中间结构 ----
    def decode(self, raw: bytes, descriptor: Any) -> Any:
        return raw

    def encode(self, value: Any, descriptor: Any) -> bytes:
        if isinstance(value, (bytes, bytearray)):
            return bytes(value)
        return str(value).encode("utf-8")

    # ---- 中间结构 -> 领域对象 ----
    def load(self, data: Any, descriptor: Any, context: Any = None) -> Any:
        return data

    # ---- 元信息 ----
    def default_version(self) -> Optional[str]:
        """动态创建该类型资源时写入的版本号（取支持范围下界）。"""
        from .versioning import VersionRange

        lower = VersionRange.parse(self.versions).lower
        return str(lower) if lower else None

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "types": list(self.type_patterns),
            "versions": self.versions,
            "description": self.description,
            "forward_compatible": self.forward_compatible,
            "generic": self.generic,
        }

    def __repr__(self) -> str:  # pragma: no cover - 调试用
        return f"<{type(self).__name__} {self.name} {list(self.type_patterns)}>"


class RawHandler(ResourceHandler):
    """全局兜底：任何无法解释的字节都能被装载，不丢数据。"""

    type_patterns = ("*/*",)
    name = "raw"
    description = "未识别类型的原始字节兜底处理器"
    generic = True


class TextHandler(ResourceHandler):
    """文本类型：解码为 str。"""

    type_patterns = ("text/*",)
    name = "text"
    description = "文本资源"
    generic = True

    def decode(self, raw: bytes, descriptor: Any) -> Any:
        return bytes(raw).decode("utf-8", errors="replace")

    def encode(self, value: Any, descriptor: Any) -> bytes:
        return str(value).encode("utf-8")


class JSONHandler(ResourceHandler):
    """JSON 编解码基类。子类通常只需要覆写 `load`。"""

    type_patterns = ("application/json", "*/*+json")
    name = "json"
    description = "通用 JSON 资源（自适应结构）"
    generic = True

    def decode(self, raw: bytes, descriptor: Any) -> Any:
        text = bytes(raw).decode("utf-8-sig", errors="strict")
        if not text.strip():
            return {}
        return json.loads(text)

    def encode(self, value: Any, descriptor: Any) -> bytes:
        payload = unwrap(value)
        if isinstance(payload, (bytes, bytearray)):
            return bytes(payload)
        return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")

    def load(self, data: Any, descriptor: Any, context: Any = None) -> Any:
        return wrap(data)


class AutoHandler(JSONHandler):
    """未注册的 `application/*` 类型的自适应装载器。

    尝试按 JSON 解析成可导航的动态资源；不是 JSON 就原样保留字节。
    这让"包里塞进一个运行时从没见过的模块"也能被读出来、改得动、写得回。
    """

    type_patterns = ("application/*",)
    name = "auto"
    description = "未知结构化类型的自适应装载器"
    generic = True

    def decode(self, raw: bytes, descriptor: Any) -> Any:
        try:
            return super().decode(raw, descriptor)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return bytes(raw)

    def load(self, data: Any, descriptor: Any, context: Any = None) -> Any:
        if isinstance(data, (bytes, bytearray)):
            return bytes(data)
        return wrap(data)


class FunctionHandler(JSONHandler):
    """把一个 `fn(data, descriptor, context)` 函数包装成 handler。"""

    generic = False

    def __init__(
        self,
        fn: Callable[..., Any],
        *,
        type_patterns: Sequence[str],
        versions: str = "*",
        name: Optional[str] = None,
        description: str = "",
        decoder: Optional[Callable[[bytes, Any], Any]] = None,
        encoder: Optional[Callable[[Any, Any], bytes]] = None,
    ) -> None:
        super().__init__()
        self._fn = fn
        self._decoder = decoder
        self._encoder = encoder
        self.type_patterns = tuple(type_patterns)
        self.versions = versions
        self.name = name or getattr(fn, "__name__", "handler")
        self.description = description or (fn.__doc__ or "").strip().split("\n")[0]

    def decode(self, raw: bytes, descriptor: Any) -> Any:
        if self._decoder is not None:
            return self._decoder(raw, descriptor)
        return super().decode(raw, descriptor)

    def encode(self, value: Any, descriptor: Any) -> bytes:
        if self._encoder is not None:
            return self._encoder(value, descriptor)
        return super().encode(value, descriptor)

    def load(self, data: Any, descriptor: Any, context: Any = None) -> Any:
        try:
            return self._fn(data, descriptor, context)
        except TypeError as exc:
            if "positional argument" not in str(exc):
                raise
            try:
                return self._fn(data, descriptor)
            except TypeError:
                return self._fn(data)


class SchemaValidationError(Exception):
    """动态定义的类型缺少必填字段。"""


class SchemaHandler(JSONHandler):
    """由字段声明动态生成的 handler——注册新资源类型无需编写 Python 类。

        registry.define(
            "application/x-eidolon-world",
            version="1.0",
            required=("name",),
            defaults={"regions": []},
        )

    产物是 `DynamicResource`：声明过的字段有默认值与校验，未声明的字段照样保留。
    """

    generic = False

    def __init__(
        self,
        type_value: str,
        *,
        version: str = "1.0",
        versions: Optional[str] = None,
        required: Sequence[str] = (),
        defaults: Optional[dict] = None,
        schema: Optional[dict] = None,
        name: Optional[str] = None,
        description: str = "",
        strict: bool = False,
    ) -> None:
        super().__init__()
        self.type_value = type_value
        self.type_patterns = (type_value,)
        self.version = version
        self.versions = versions or f"^{version}"
        self.required = tuple(required)
        self.defaults = dict(defaults or {})
        self.schema = dict(schema or {})
        self.strict = strict
        self.name = name or f"schema:{type_value}"
        self.description = description or f"动态定义的资源类型 {type_value}"

    def default_version(self) -> Optional[str]:
        return self.version

    def load(self, data: Any, descriptor: Any, context: Any = None) -> Any:
        payload = unwrap(data)
        if not isinstance(payload, dict):
            payload = {"value": payload}
        missing = [k for k in self.required if k not in payload]
        if missing and self.strict:
            raise SchemaValidationError(
                f"{self.type_value} 缺少必填字段：{', '.join(missing)}"
            )
        merged = {**self.defaults, **payload}
        resource = DynamicResource(merged)
        if missing:
            # 非严格模式：补空占位，保证下游访问不 KeyError
            for key in missing:
                if key not in merged:
                    resource[key] = None
        return resource

    def to_dict(self) -> dict:
        out = super().to_dict()
        out.update(
            {
                "declared_type": self.type_value,
                "version": self.version,
                "required": list(self.required),
                "defaults": dict(self.defaults),
                "schema": dict(self.schema),
                "strict": self.strict,
                "dynamic": True,
            }
        )
        return out
