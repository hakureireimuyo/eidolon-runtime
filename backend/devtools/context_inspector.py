"""DevTools 上下文检查器 —— 快照捕获与 hash 对比(纯逻辑,无 FastAPI 依赖)。

职责:
- capture(manager): 捕获 ContextManager 当前状态为快照。每个 segment /
  每轮对话 / 合并 system 前缀都计算 sha1 hash —— 上下文可能很长,
  后续所有一致性判断只比较 hash,不做文本级比较。
- diff(prev, cur): 纯 hash 序列对比,输出单元状态、缓存命中判定与对话演化状态。

边界:
- 只读诊断:绝不修改 ContextManager / 引擎的任何状态。
- 状态只存内存:快照(含全文)由调用方在进程内存中持有,本模块不写任何文件。
- 缓存前缀口径与 runtime/context/compiler.py 的 estimate_cache_boundary 一致:
  system 角色且 layer <= MID。cacheable 字段不参与判定,仅供 UI 展示。
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from runtime.context import ContextManager

# 单元对比状态
SAME = "same"
CHANGED = "changed"
ADDED = "added"
REMOVED = "removed"


def sha1(text: str) -> str:
    """文本的 sha1 十六进制摘要(一致性判断的唯一依据)。"""
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def capture(manager: ContextManager) -> dict:
    """捕获 ContextManager 当前状态为快照。

    快照含 `_text` 内部字段(供内容端点与基准对照使用),对外序列化前
    用 snapshot_public() 剔除,主列表只发 hash 与统计。
    """
    ir = manager.ir
    cache_info = manager.cache_info()
    prefix_tags = set(cache_info.get("prefix_segments", []))

    layers = []
    for layer, segments in ir.by_layer().items():
        layers.append(
            {
                "layer": layer.name,
                "label": layer.label,
                "count": len(segments),
                "chars": sum(len(s.text) for s in segments),
                "segments": [_segment_dict(s, prefix_tags) for s in segments],
            }
        )

    turns = manager.conversation_turns  # 副本,最旧在前
    n_turns = len(turns)
    conversation_turns = [
        {
            "index": -(n_turns - i),  # 负索引距尾部:-1 = 最新一条
            "role": t.role,
            "length": len(t.content),
            "hash": sha1(t.content),
            "ts": t.ts,
            "_text": t.content,
        }
        for i, t in enumerate(turns)
    ]

    # 编译布局单元:与 ContextCompiler.compile() 同数据源同排序
    # (system 段按层排序 → 非 system 段 → 对话),等价性由测试锁定。
    units = []
    for s in ir.sorted_segments():
        units.append(
            {
                "key": s.tag,
                "kind": "segment",
                "layer": s.layer.name,
                "role": s.role,
                "cacheable": s.cacheable,
                "in_prefix": s.tag in prefix_tags,
                "merged": s.role == "system",  # system 段合并进 message 0
                "length": len(s.text),
                "hash": sha1(s.text),
                "_text": s.text,
            }
        )
    for t in conversation_turns:
        units.append(
            {
                "key": f"turn:{t['index']}",
                "kind": "turn",
                "layer": None,
                "role": t["role"],
                "cacheable": None,
                "in_prefix": False,
                "merged": False,
                "length": t["length"],
                "hash": t["hash"],
                "_text": t["_text"],
            }
        )

    messages = manager.compile()
    system_content = (
        messages[0]["content"] if messages and messages[0]["role"] == "system" else ""
    )
    prefix_chars = sum(
        len(s.text)
        for s in ir.sorted_segments()
        if s.role == "system" and s.tag in prefix_tags
    )
    return {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "summary": {
            "total_segments": len(ir),
            "segment_chars": ir.total_text_length,
            "conversation_turns": n_turns,
            "conversation_chars": sum(t["length"] for t in conversation_turns),
            "conversation_max_turns": manager.conversation_max_turns,
            "prefix_chars": prefix_chars,
            # 合并 system 消息的 hash 直接取自 compile() 产物,不自行拼接
            "prefix_hash": sha1(system_content) if system_content else None,
            "message_count": len(messages),
            "compiled_chars": sum(len(m["content"]) for m in messages),
            "cache_info": cache_info,
        },
        "layers": layers,
        "conversation": {"turns": conversation_turns},
        "layout_units": units,
    }


def _segment_dict(seg, prefix_tags: set[str]) -> dict:
    return {
        "tag": seg.tag,
        "role": seg.role,
        "cacheable": seg.cacheable,
        "in_prefix": seg.tag in prefix_tags,
        "length": len(seg.text),
        "hash": sha1(seg.text),
        "_text": seg.text,
    }


def snapshot_public(snapshot: dict) -> dict:
    """序列化用:剔除快照中的 `_text` 内部字段(只发 hash 与统计)。"""
    strip = lambda d: {k: v for k, v in d.items() if k != "_text"}  # noqa: E731
    return {
        "captured_at": snapshot["captured_at"],
        "summary": snapshot["summary"],
        "layers": [
            {
                "layer": layer["layer"],
                "label": layer["label"],
                "count": layer["count"],
                "chars": layer["chars"],
                "segments": [strip(seg) for seg in layer["segments"]],
            }
            for layer in snapshot["layers"]
        ],
        "conversation": {
            "turns": [strip(t) for t in snapshot["conversation"]["turns"]]
        },
        "layout_units": [strip(u) for u in snapshot["layout_units"]],
    }


def _align_turns(prev_turns: list[dict], cur_turns: list[dict]) -> tuple[int, int]:
    """对话队列对齐:返回 (removed_count, added_count)。

    对话是队列——只从头部截断(max_turns 驱逐)、尾部追加。取最大 k 使
    cur[:k] == prev[-k:](「当前列表的最长前缀等于基准列表的某个后缀」),
    同时覆盖纯追加 / 纯截断 / 追加+截断三种情况。仅比较 hash。
    已知局限:用户重复发送完全相同文本时哈希相同,新旧轮无法区分,归入 same。
    """
    prev_hashes = [t["hash"] for t in prev_turns]
    cur_hashes = [t["hash"] for t in cur_turns]
    for k in range(min(len(prev_hashes), len(cur_hashes)), -1, -1):
        if cur_hashes[:k] == prev_hashes[-k:]:
            return len(prev_hashes) - k, len(cur_hashes) - k
    return len(prev_hashes), len(cur_hashes)


def _conversation_status(removed: int, added: int) -> str:
    if removed == 0 and added == 0:
        return "unchanged"
    if removed == 0:
        return "grew"
    if added == 0:
        return "truncated"
    return "replaced"


def _prefix_hit(prev: dict, cur: dict) -> tuple[bool, str]:
    """前缀缓存命中判定:前缀 tag 集合完全一致且逐段 hash 一致。"""
    prev_tags = prev["summary"]["cache_info"].get("prefix_segments", [])
    cur_tags = cur["summary"]["cache_info"].get("prefix_segments", [])
    if set(prev_tags) != set(cur_tags):
        if set(cur_tags) - set(prev_tags):
            return False, "segment_added"
        return False, "segment_removed"
    prev_segs = {s["tag"]: s for layer in prev["layers"] for s in layer["segments"]}
    cur_segs = {s["tag"]: s for layer in cur["layers"] for s in layer["segments"]}
    for tag in prev_tags:
        if prev_segs[tag]["hash"] != cur_segs[tag]["hash"]:
            return False, "segment_changed"
    return True, "ok"


def diff(prev: dict, cur: dict, basis: str) -> dict:
    """prev(基准)与 cur(当前)的 hash 对比结果,不触碰文本。

    匹配规则:
    - segment 单元按 tag(key)匹配;
    - 对话轮先做「最长共享后缀-前缀」对齐,再分配稳定 key 配对。
    输出 units 为合并后的当前布局顺序(removed 单元插回其基准位置),
    前端零逻辑即可直接渲染。
    """
    removed, added = _align_turns(
        prev["conversation"]["turns"], cur["conversation"]["turns"]
    )

    # 对齐后的对话 key:共享轮两侧一致,removed/added 各自独立
    def normalize(units: list[dict], turn_keys: list[str]) -> list[dict]:
        out = []
        ti = 0
        for u in units:
            u = dict(u)
            if u["kind"] == "turn":
                # 保留原 index(负索引距尾部),供 UI 与当前对话表对应
                u["index"] = int(u["key"].split(":", 1)[1])
                u["key"] = turn_keys[ti]
                ti += 1
            out.append(u)
        return out

    prev_units = normalize(
        prev["layout_units"],
        [f"removed-turn:{i}" for i in range(removed)]
        + [f"shared-turn:{i}" for i in range(len(prev["conversation"]["turns"]) - removed)],
    )
    cur_units = normalize(
        cur["layout_units"],
        [f"shared-turn:{i}" for i in range(len(cur["conversation"]["turns"]) - added)]
        + [f"added-turn:{i}" for i in range(added)],
    )

    prev_pos = {u["key"]: i for i, u in enumerate(prev_units)}
    matched = set()
    merged = []

    def diff_unit(unit: dict, status: str, *, prev_hash=None, prev_pos=None) -> dict:
        out = {
            "key": unit["key"],
            "kind": unit["kind"],
            "layer": unit.get("layer"),
            "role": unit.get("role"),
            "status": status,
            "length": unit["length"],
            "hash": unit["hash"],
            "prev_hash": prev_hash,
            "index": unit.get("index"),  # 仅 turn 单元:负索引距尾部
            "in_prefix": unit.get("in_prefix"),
            "merged": unit.get("merged"),
        }
        if prev_pos is not None:
            out["_prev_pos"] = prev_pos
        return out

    for cu in cur_units:
        if cu["key"] in prev_pos:
            pu = prev_units[prev_pos[cu["key"]]]
            matched.add(cu["key"])
            status = SAME if pu["hash"] == cu["hash"] else CHANGED
            merged.append(diff_unit(cu, status, prev_hash=pu["hash"], prev_pos=prev_pos[cu["key"]]))
        else:
            merged.append(diff_unit(cu, ADDED))

    # removed 单元插回其基准位置(其 prev 后继的当前单元之前)
    for i, pu in enumerate(prev_units):
        if pu["key"] in matched:
            continue
        insert_at = len(merged)
        for j, m in enumerate(merged):
            if m.get("_prev_pos") is not None and m["_prev_pos"] > i:
                insert_at = j
                break
        merged.insert(insert_at, diff_unit(pu, REMOVED, prev_hash=pu["hash"], prev_pos=i))

    counts = {SAME: 0, CHANGED: 0, ADDED: 0, REMOVED: 0}
    first_change = None
    for idx, u in enumerate(merged):
        counts[u["status"]] += 1
        if u["status"] != SAME and first_change is None:
            first_change = idx
        u.pop("_prev_pos", None)

    hit, reason = _prefix_hit(prev, cur)
    return {
        "basis": basis,
        "basis_captured_at": prev["captured_at"],
        "summary": {
            "same": counts[SAME],
            "changed": counts[CHANGED],
            "added": counts[ADDED],
            "removed": counts[REMOVED],
            "total_current": len(merged),
        },
        "prefix_cache_hit": hit,
        "prefix_break_reason": reason,
        "first_change_index": first_change,
        "conversation": {
            "status": _conversation_status(removed, added),
            "added": added,
            "removed": removed,
        },
        "units": merged,
    }


def snapshot_segment_text(snapshot: dict, tag: str) -> str | None:
    """从快照中取出指定 tag 的全文(内部 `_text` 字段)。"""
    for layer in snapshot["layers"]:
        for seg in layer["segments"]:
            if seg["tag"] == tag:
                return seg.get("_text")
    return None
