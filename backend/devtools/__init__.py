"""DevTools —— 开发者工具路由包。

只在开发者模式(EIDOLON_RUNTIME_DEVTOOLS=1)下由 backend/main.py 挂载;
开关关闭时路由完全不注册,devtools 代码零执行。

约定(所有工具共同遵守):
- 只读诊断:绝不修改引擎 / 上下文 / 任何运行时状态;
- 状态只存内存:previous / baseline 快照等只保存在进程内闭包变量,
  不写任何文件,进程重启即清空;
- 以「会话 × 生成器」为维度:会话是高于 LLM 会话的一层概念,一个会话
  下挂多个生成器(当前仅角色对话生成器),previous / baseline 按维度隔离;
- 新增工具:在 frontend/devtools/index.html 的卡片数组登记,
  路由在本包内扩展(按工具拆模块,与 context_inspector 同构)。
"""
from __future__ import annotations

import threading
from typing import Any, Optional

from fastapi import APIRouter, HTTPException

from runtime.engine import CharacterLoadError, RuntimeEngine

from .context_inspector import (
    CHANGED,
    SAME,
    capture,
    diff,
    sha1,
    snapshot_public,
    snapshot_segment_text,
)


def build_router(engine: RuntimeEngine) -> APIRouter:
    """构造 devtools 路由(闭包持有内存态 previous / baseline)。"""
    router = APIRouter(prefix="/api/devtools", tags=["devtools"])
    # 内存态:{session_key: {generator_id: {"previous", "baseline"}}}
    # previous 每次 GET /context 后自动推进;baseline 手动钉住。
    state: dict[str, dict[str, dict[str, Any]]] = {}
    lock = threading.Lock()

    def _resolve(engine: RuntimeEngine, session: Optional[str], generator: Optional[str]):
        """按 query 维度解析生成器(缺省:当前激活会话 / dialogue 生成器)。"""
        key = session or engine.active_key
        gid = generator or "dialogue"
        if key is None:
            raise HTTPException(status_code=404, detail="尚未加载任何会话")
        try:
            return key, gid, engine.resolve_generator(key, gid)
        except CharacterLoadError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc

    def _cell(session_key: str, generator_id: str) -> dict:
        """惰性取 会话×生成器 的状态格(引用稳定,内容读写仍需锁)。"""
        with lock:
            return state.setdefault(session_key, {}).setdefault(
                generator_id, {"previous": None, "baseline": None}
            )

    @router.get("/sessions")
    def list_sessions():
        """会话 × 生成器概览(前端选择器数据源)。"""
        sessions = []
        for key in engine.session_keys():
            sessions.append(
                {
                    "key": key,
                    "active": key == engine.active_key,
                    "generators": engine.session_generators(key),
                }
            )
        return {"sessions": sessions}

    @router.get("/context")
    def get_context(session: Optional[str] = None, generator: Optional[str] = None):
        """指定 会话×生成器 的上下文快照 + 与上次查看(previous)/ 基线(baseline)的 hash 对比。

        唯一推进该格 previous 的端点:响应后 previous 更新为当前状态,
        因此每次轮询都得到「自上次查看以来的变化」。多标签页同时轮询会
        互相覆盖 previous(单用户 devtool,接受)。
        """
        key, gid, gen = _resolve(engine, session, generator)
        current = capture(gen.context)
        cell = _cell(key, gid)
        with lock:
            prev, base = cell["previous"], cell["baseline"]
            cell["previous"] = current
        return {
            "dimensions": {"session": key, "generator": gid},
            "baseline_set": base is not None,
            "captured_at": current["captured_at"],
            "current": snapshot_public(current),
            "vs_previous": diff(prev, current, "previous")
            if prev is not None
            else {"reason": "no_previous"},
            "vs_baseline": diff(base, current, "baseline")
            if base is not None
            else {"reason": "no_baseline"},
            "previous_at": prev["captured_at"] if prev else None,
            "baseline_at": base["captured_at"] if base else None,
        }

    @router.post("/context/baseline")
    def set_baseline(session: Optional[str] = None, generator: Optional[str] = None):
        """把指定 会话×生成器 的当前上下文钉住为基线(含全文,仅存内存)。"""
        key, gid, gen = _resolve(engine, session, generator)
        current = capture(gen.context)
        cell = _cell(key, gid)
        with lock:
            cell["baseline"] = current
        return {
            "dimensions": {"session": key, "generator": gid},
            "ok": True,
            "captured_at": current["captured_at"],
        }

    @router.delete("/context/baseline")
    def clear_baseline(session: Optional[str] = None, generator: Optional[str] = None):
        key, gid, _gen = _resolve(engine, session, generator)
        cell = _cell(key, gid)
        with lock:
            if cell["baseline"] is None:
                raise HTTPException(status_code=404, detail="baseline 未设置")
            cell["baseline"] = None
        return {"ok": True}

    @router.get("/context/segment/{tag}")
    def get_segment(
        tag: str,
        against: str = "previous",
        session: Optional[str] = None,
        generator: Optional[str] = None,
    ):
        """单个 segment 的全文(可选带上 previous / baseline 的旧文本对照)。"""
        if against not in ("previous", "baseline"):
            raise HTTPException(
                status_code=400,
                detail=f"against 只支持 previous/baseline,得到 {against!r}",
            )
        key, gid, gen = _resolve(engine, session, generator)
        seg = gen.context.get_segment(tag)
        if seg is None:
            raise HTTPException(status_code=404, detail=f"segment 不存在:{tag}")
        cell = _cell(key, gid)
        with lock:
            basis = cell[against]
        previous_text = None
        status = None
        if basis is not None:
            for layer in basis["layers"]:
                for bseg in layer["segments"]:
                    if bseg["tag"] == tag:
                        previous_text = bseg.get("_text")
                        status = (
                            SAME if bseg["hash"] == sha1(seg.text) else CHANGED
                        )
                        break
        return {
            "tag": tag,
            "layer": seg.layer.name,
            "label": seg.layer.label,
            "role": seg.role,
            "cacheable": seg.cacheable,
            "length": len(seg.text),
            "status": status,
            "current_text": seg.text,
            "previous_text": previous_text,
        }

    @router.get("/context/turn/{index}")
    def get_turn(
        index: int,
        session: Optional[str] = None,
        generator: Optional[str] = None,
    ):
        """单轮对话全文。index 为负时距尾部(-1 = 最新),为正时从最旧起算。"""
        _key, _gid, gen = _resolve(engine, session, generator)
        turns = gen.context.conversation_turns
        if index >= len(turns) or index < -len(turns):
            raise HTTPException(
                status_code=404, detail=f"turn 越界:{index}(共 {len(turns)} 条)"
            )
        t = turns[index]
        return {"index": index, "role": t.role, "length": len(t.content), "text": t.content}

    @router.get("/context/messages")
    def get_messages(session: Optional[str] = None, generator: Optional[str] = None):
        """编译后的最终模型输入(messages 全文,与真实请求同源)。"""
        _key, _gid, gen = _resolve(engine, session, generator)
        manager = gen.context
        messages = manager.compile()
        n_system_segments = sum(1 for s in manager.ir.segments if s.role == "system")
        notes = []
        if messages and messages[0]["role"] == "system":
            notes.append(
                f"message 0 = 合并 system({n_system_segments} 段);"
                "缓存前缀口径见 summary.cache_info(仅 layer<=MID 计入)"
            )
        return {
            "count": len(messages),
            "total_chars": sum(len(m["content"]) for m in messages),
            "messages": [
                {
                    "index": i,
                    "role": m["role"],
                    "chars": len(m["content"]),
                    "content": m["content"],
                }
                for i, m in enumerate(messages)
            ],
            "notes": notes,
        }

    return router
