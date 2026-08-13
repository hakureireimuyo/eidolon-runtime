"""DevTools —— 开发者工具路由包。

只在开发者模式(EIDOLON_RUNTIME_DEVTOOLS=1)下由 backend/main.py 挂载;
开关关闭时路由完全不注册,devtools 代码零执行。

约定(所有工具共同遵守):
- 只读诊断:绝不修改引擎 / 上下文 / 任何运行时状态;
- 状态只存内存:previous / baseline 快照等只保存在进程内闭包变量,
  不写任何文件,进程重启即清空;
- 新增工具:在 frontend/devtools/index.html 的卡片数组登记,
  路由在本包内扩展(按工具拆模块,与 context_inspector 同构)。
"""
from __future__ import annotations

import threading

from fastapi import APIRouter, HTTPException

from runtime.engine import RuntimeEngine

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
    # 内存态:previous 每次 GET /context 后自动推进;baseline 手动钉住。
    state = {"previous": None, "baseline": None}
    lock = threading.Lock()

    @router.get("/context")
    def get_context():
        """当前上下文快照 + 与上次查看(previous)/ 基线(baseline)的 hash 对比。

        唯一推进 previous 的端点:响应后 previous 更新为当前状态,
        因此每次轮询都得到「自上次查看以来的变化」。多标签页同时轮询会
        互相覆盖 previous(单用户 devtool,接受)。
        """
        current = capture(engine.context_manager)
        with lock:
            prev, base = state["previous"], state["baseline"]
            state["previous"] = current
        return {
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
    def set_baseline():
        """把当前上下文钉住为基线(含全文,仅存内存)。"""
        current = capture(engine.context_manager)
        with lock:
            state["baseline"] = current
        return {"ok": True, "captured_at": current["captured_at"]}

    @router.delete("/context/baseline")
    def clear_baseline():
        with lock:
            if state["baseline"] is None:
                raise HTTPException(status_code=404, detail="baseline 未设置")
            state["baseline"] = None
        return {"ok": True}

    @router.get("/context/segment/{tag}")
    def get_segment(tag: str, against: str = "previous"):
        """单个 segment 的全文(可选带上 previous / baseline 的旧文本对照)。"""
        if against not in ("previous", "baseline"):
            raise HTTPException(
                status_code=400,
                detail=f"against 只支持 previous/baseline,得到 {against!r}",
            )
        seg = engine.context_manager.get_segment(tag)
        if seg is None:
            raise HTTPException(status_code=404, detail=f"segment 不存在:{tag}")
        with lock:
            basis = state[against]
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
    def get_turn(index: int):
        """单轮对话全文。index 为负时距尾部(-1 = 最新),为正时从最旧起算。"""
        turns = engine.context_manager.conversation_turns
        if index >= len(turns) or index < -len(turns):
            raise HTTPException(
                status_code=404, detail=f"turn 越界:{index}(共 {len(turns)} 条)"
            )
        t = turns[index]
        return {"index": index, "role": t.role, "length": len(t.content), "text": t.content}

    @router.get("/context/messages")
    def get_messages():
        """编译后的最终模型输入(messages 全文,与真实请求同源)。"""
        manager = engine.context_manager
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
