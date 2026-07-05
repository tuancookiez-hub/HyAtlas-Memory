# -*- coding: utf-8 -*-
"""
Profile 证据反查 —— 从一批 VDB 命中节点反查「支撑它们的 L6 schema」。

正路（graph vector_search）找与 query 语义相近的 L6；反路（本模块）从 normal
召回命中的 VDB 节点出发，沿 DERIVED_FROM 边反向找引用它们的 L6 schema，按支撑度
（被多少个命中节点支撑）排序。一正一反，供 reader 用 RRF 融合成 profile 输出。

graph_store.find_referencing_memories 每条 (Memory, VdbRef) 边返回一行，带
evidence_vdb_id。同一 L6 被 N 个输入节点支撑 → 出现 N 行；支撑度 = 该 L6 的
distinct evidence_vdb_id 数。

free async 函数，graph_store 作首参，与 evolution.recall / intention.recall 一致；
其他 reader 可直接 import 复用。best-effort：graph 不可用 / 无该能力 / 抛错均返回 []。
"""

from typing import Any, Dict, List

import logging

logger = logging.getLogger(__name__)

_L6_LAYER = "l6_schema"


async def reverse_lookup_l6(
    graph_store: Any,
    vdb_node_ids: List[str],
    *,
    limit: int = 50,
) -> List[Dict[str, Any]]:
    """从 VDB node_id 列表反查支撑它们的 L6 schema，按支撑度降序。

    Args:
        graph_store: 图存储（None 或无 find_referencing_memories 则返回 []）。
        vdb_node_ids: normal 路命中的 VDB 节点 id 列表（反查锚点）。
        limit: 反查的边数上限（传给 find_referencing_memories）。

    Returns:
        与 reader 输出层兼容的 hit 列表，按支撑度降序：
          [{node_id, content, layer:"l6_schema", score:<support/max_support>,
            source:"profile_reverse", confidence, _support_count, node:None}, ...]
        支撑度 = distinct evidence_vdb_id 数。无结果返回 []。
    """
    if graph_store is None or not vdb_node_ids:
        return []
    if not hasattr(graph_store, "find_referencing_memories"):
        return []

    try:
        rows = await graph_store.find_referencing_memories(vdb_node_ids, limit=limit)
    except Exception as e:
        logger.debug(f"[profile-evidence] reverse_lookup_l6 failed: {e}")
        return []

    # 按 L6 node_id 聚合：support = distinct evidence_vdb_id 数
    grouped: Dict[str, Dict[str, Any]] = {}
    for row in rows or []:
        if row.get("layer") != _L6_LAYER:
            continue
        nid = row.get("node_id")
        if not nid:
            continue
        g = grouped.get(nid)
        if g is None:
            g = {
                "node_id": nid,
                "content": row.get("content", ""),
                "confidence": row.get("confidence"),
                "_evidence_ids": set(),
            }
            grouped[nid] = g
        ev = row.get("evidence_vdb_id")
        if ev:
            g["_evidence_ids"].add(ev)

    if not grouped:
        return []

    max_support = max(len(g["_evidence_ids"]) or 1 for g in grouped.values())

    hits: List[Dict[str, Any]] = []
    for g in grouped.values():
        support = len(g["_evidence_ids"]) or 1
        hits.append({
            "node_id": g["node_id"],
            "content": g["content"],
            "layer": _L6_LAYER,
            # RRF 只看 rank，score 仅作观测；归一到 [0,1]
            "score": support / max_support,
            "source": "profile_reverse",
            "confidence": g.get("confidence"),
            "_support_count": support,
            "node": None,  # graph 节点无 MemoryNode 对象，输出层从 content/layer 渲染
        })

    hits.sort(key=lambda h: h["_support_count"], reverse=True)
    return hits
