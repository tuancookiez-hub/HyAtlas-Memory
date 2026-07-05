# -*- coding: utf-8 -*-
"""
记忆去重核心逻辑（纯函数，无 I/O，可单测）。

线上 L2_FACT / L4_IDENTITY 经常出现重复（extractor 抽重 / reconcile 漏合并）。
本模块用 embedding cosine 相似度（> 阈值，默认 0.95）识别重复组，并按确定性
规则决定保留/删除，供 extractor / reconcile / search 三条链路共用。

判重范围（调用方负责传入）：
  只在「普通 memory + 链头（is_latest=True）」之间判重；链身（SUPERSEDED，
  is_latest=False）不参与——它们是历史，由其链头代表。

删除决策（每个重复组，确定性，非随机）：
  - 全是非链节点          → 保留 gmt_created 最早的一条，删其余
  - 1 个链头 + 其余非链   → 保留链头，删非链
  - n 个链头(n≥2) + 非链  → 保留 gmt_created 最早的链头，删其余链头 + 非链；
                            被删链头**连带删除其整条 SUPERSEDED 历史链**
                            （chain_node_ids 由调用方用 _trace_full_chain 预先补全）

相似度计算：把参与节点的向量堆成矩阵一次算 M @ M.T（行先 L2 归一化），
30 条 4096 维约 0.3ms，不调 embedding API、不需预筛。
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

try:
    import numpy as np
except Exception:  # pragma: no cover
    np = None

logger = logging.getLogger(__name__)

DEDUP_LOG_STEP = "DEDUP"

DEFAULT_DEDUP_THRESHOLD = 0.95


def get_dedup_threshold() -> float:
    """去重 cosine 阈值，env MEMORY_DEDUP_THRESHOLD 覆盖，默认 0.95。"""
    try:
        return float(os.environ.get("MEMORY_DEDUP_THRESHOLD", DEFAULT_DEDUP_THRESHOLD))
    except (TypeError, ValueError):
        return DEFAULT_DEDUP_THRESHOLD


@dataclass
class DedupItem:
    """去重输入项（调用方组装）。

    is_chain_head / chain_node_ids 由调用方预先判定/补全：
      - 普通节点：is_chain_head=False, chain_node_ids=[node_id]
      - 链头：    is_chain_head=True,  chain_node_ids=[head, ...SUPERSEDED bodies]
    """
    node_id: str
    embedding: List[float]
    content: str = ""
    is_latest: bool = True
    is_chain_head: bool = False
    gmt_created: Optional[float] = None  # Unix ts；用于确定性保留（越小越早）
    chain_node_ids: List[str] = field(default_factory=list)


def _gmt_key(item: DedupItem) -> float:
    """gmt_created 排序键：缺失视为 +inf（最晚），保证有时间的优先被保留。"""
    return item.gmt_created if item.gmt_created is not None else float("inf")


def _pairwise_cosine(vectors: List[List[float]]):
    """行 L2 归一化后 M @ M.T，返回相似度矩阵（numpy）。"""
    M = np.asarray(vectors, dtype=np.float32)
    norms = np.linalg.norm(M, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    M = M / norms
    return M @ M.T


def _union_find_groups(n: int, sim, threshold: float) -> List[List[int]]:
    """对 sim > threshold 的成对索引做并查集连通，返回每组的 index 列表（含单点）。"""
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i in range(n):
        for j in range(i + 1, n):
            if float(sim[i][j]) > threshold:
                union(i, j)

    groups: Dict[int, List[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return list(groups.values())


def _decide_group(members: List[DedupItem]) -> Dict[str, Any]:
    """对一个重复组决定保留/删除，返回组明细（含审计字段）。"""
    heads = [m for m in members if m.is_chain_head]

    if heads:
        # 有链头：保留 gmt 最早的链头；其余（含其他链头 + 非链）全删
        kept = min(heads, key=_gmt_key)
        reason = "earliest_chain_head" if len(heads) > 1 else "chain_head"
    else:
        # 全非链：保留 gmt 最早的一条
        kept = min(members, key=_gmt_key)
        reason = "earliest_gmt"

    deleted = []
    delete_ids: List[str] = []
    for m in members:
        if m.node_id == kept.node_id:
            continue
        # 被删链头连带删除整条历史链（chain_node_ids 含 head 自身 + bodies）
        cascade = [cid for cid in (m.chain_node_ids or [m.node_id]) if cid != kept.node_id]
        if not cascade:
            cascade = [m.node_id]
        also = [cid for cid in cascade if cid != m.node_id]
        deleted.append({
            "node_id": m.node_id,
            "content": m.content,
            "is_chain_head": m.is_chain_head,
            "also_deleted_chain": also,
        })
        delete_ids.extend(cascade)

    return {
        "members": [
            {
                "node_id": m.node_id,
                "content": m.content,
                "is_latest": m.is_latest,
                "is_chain_head": m.is_chain_head,
            }
            for m in members
        ],
        "kept": {"node_id": kept.node_id, "content": kept.content, "reason": reason},
        "deleted": deleted,
        "delete_ids": delete_ids,
    }


def plan_dedup(
    items: List[DedupItem],
    *,
    threshold: Optional[float] = None,
) -> Dict[str, Any]:
    """
    对一组候选项做去重规划（纯函数，不执行删除）。

    只比较 is_latest=True 的项（普通节点 + 链头）；is_latest=False 的链身被忽略
    （但若某链头被删，其 chain_node_ids 里的链身会进 delete_ids 连带删）。

    Returns:
        {
          "threshold": float,
          "groups": [ {members, kept, deleted, delete_ids}, ... ]  # 只含有删除的组
          "delete_ids": [所有待删 node_id（含连带链），去重后],
          "kept_ids":   [所有保留的代表 node_id],
        }
    """
    thr = threshold if threshold is not None else get_dedup_threshold()
    empty = {"threshold": thr, "groups": [], "delete_ids": [], "kept_ids": [],
             "compared_count": 0, "max_cosine": None, "avg_cosine": None}

    if np is None or not items:
        return empty

    # 只参与 is_latest=True 且有 embedding 的项
    parts = [it for it in items if it.is_latest and it.embedding]
    if len(parts) < 2:
        return {**empty, "compared_count": len(parts)}

    sim = _pairwise_cosine([it.embedding for it in parts])
    idx_groups = _union_find_groups(len(parts), sim, thr)

    # 上三角余弦统计（排除对角 1.0），供审计：最高 / 平均
    n = len(parts)
    iu = np.triu_indices(n, k=1)
    pair_cos = sim[iu]
    max_cos = float(pair_cos.max()) if pair_cos.size else None
    avg_cos = float(pair_cos.mean()) if pair_cos.size else None

    groups_out: List[Dict[str, Any]] = []
    all_delete: List[str] = []
    all_kept: List[str] = []
    seen_del = set()

    for idxs in idx_groups:
        if len(idxs) < 2:
            continue  # 非重复，跳过
        members = [parts[i] for i in idxs]
        decided = _decide_group(members)
        all_kept.append(decided["kept"]["node_id"])
        for did in decided["delete_ids"]:
            if did not in seen_del:
                seen_del.add(did)
                all_delete.append(did)
        groups_out.append({
            "members": decided["members"],
            "kept": decided["kept"],
            "deleted": decided["deleted"],
        })

    return {
        "threshold": thr,
        "groups": groups_out,
        "delete_ids": all_delete,
        "kept_ids": all_kept,
        "compared_count": len(parts),
        "max_cosine": max_cos,
        "avg_cosine": avg_cos,
    }


def _build_dedup_content(trigger: str, plan: Dict[str, Any]) -> str:
    """构造 DEDUP step 的人类可读 content。

    无删除：trigger + 比较条数 + max/avg cosine + 阈值。
    有删除：上面这些 + 每个重复组「重复的是哪些 / 删了哪些 / 保留哪条」。
    """
    thr = plan.get("threshold")
    compared = plan.get("compared_count", 0)
    max_c = plan.get("max_cosine")
    avg_c = plan.get("avg_cosine")
    delete_ids = plan.get("delete_ids", [])

    def _fmt(v):
        return f"{v:.4f}" if isinstance(v, (int, float)) else "n/a"

    lines = [
        f"[DEDUP] trigger={trigger} threshold={thr}",
        f"compared_count={compared} max_cosine={_fmt(max_c)} avg_cosine={_fmt(avg_c)}",
        f"deleted_count={len(delete_ids)}",
    ]

    groups = plan.get("groups", [])
    if groups:
        for gi, g in enumerate(groups):
            members = g.get("members", [])
            kept = g.get("kept", {})
            deleted = g.get("deleted", [])
            lines.append(f"-- group#{gi} ({len(members)} duplicates) --")
            for m in members:
                lines.append(
                    f"   dup: id={m.get('node_id')} head={m.get('is_chain_head')} "
                    f"content={(m.get('content') or '')[:120]}"
                )
            lines.append(
                f"   KEEP: id={kept.get('node_id')} reason={kept.get('reason')} "
                f"content={(kept.get('content') or '')[:120]}"
            )
            for d in deleted:
                also = d.get("also_deleted_chain") or []
                also_s = f" also_chain={also}" if also else ""
                lines.append(
                    f"   DELETE: id={d.get('node_id')} head={d.get('is_chain_head')}{also_s} "
                    f"content={(d.get('content') or '')[:120]}"
                )
        lines.append(f"all_deleted_ids={delete_ids}")

    return "\n".join(lines)


async def _emit_dedup_log(
    cache: Any,
    *,
    request_id: str,
    user_id: str,
    agent_id: str,
    trigger: str,
    plan: Dict[str, Any],
) -> None:
    """写 DEDUP pipeline log（删除审计明细）。best-effort。"""
    if cache is None or not getattr(cache, "store_pipeline_log", None):
        return
    try:
        parsed = {
            "trigger": trigger,
            "threshold": plan.get("threshold"),
            "groups": plan.get("groups", []),
            "deleted_count": len(plan.get("delete_ids", [])),
            "kept_count": len(plan.get("kept_ids", [])),
            "compared_count": plan.get("compared_count", 0),
            "max_cosine": plan.get("max_cosine"),
            "avg_cosine": plan.get("avg_cosine"),
        }
        await cache.store_pipeline_log(
            request_id=request_id or "",
            user_id=user_id or "",
            agent_id=agent_id or "default_agent",
            step=DEDUP_LOG_STEP,
            prompt="",
            response=_build_dedup_content(trigger, plan),
            parsed=json.dumps(parsed, ensure_ascii=False, default=str),
            memory_ids=plan.get("delete_ids", []) or None,
        )
    except Exception as e:
        logger.debug(f"[dedup] emit DEDUP log failed: {e}")


async def execute_dedup(
    items: List[DedupItem],
    *,
    vector_store: Any,
    cache: Any = None,
    trigger: str,
    request_id: str = "",
    user_id: str = "",
    agent_id: str = "",
    delete_from_store: bool = True,
    threshold: Optional[float] = None,
) -> Dict[str, Any]:
    """
    去重编排：plan → （可选）删库 → 写 DEDUP log。整体 best-effort，绝不抛错影响主流程。

    Args:
        items: 同一 isolation 范围内的候选（调用方保证不跨 user/agent）。
        delete_from_store: True 时对 plan.delete_ids 执行 vector_store.delete；
            extractor 阶段（结果还没入库）传 False，只记录被丢弃项。
        trigger: "extractor" / "reconcile" / "search"，落进 DEDUP log。

    Returns:
        plan dict（含 delete_ids / kept_ids / groups）。无重复时 delete_ids 为空。
    """
    try:
        plan = plan_dedup(items, threshold=threshold)
    except Exception as e:
        logger.warning(f"[dedup] plan_dedup failed (trigger={trigger}): {e}")
        return {"threshold": threshold, "groups": [], "delete_ids": [], "kept_ids": []}

    delete_ids = plan.get("delete_ids", [])
    if not delete_ids:
        # 无重复：仍落一条 DEDUP step（记录 compared_count / max_cosine / avg_cosine），
        # 方便确认 dedup 确实跑过、最接近的一对离阈值有多远。
        await _emit_dedup_log(
            cache,
            request_id=request_id,
            user_id=user_id,
            agent_id=agent_id,
            trigger=trigger,
            plan=plan,
        )
        return plan

    if delete_from_store and vector_store is not None:
        for nid in delete_ids:
            try:
                await vector_store.delete(nid)
            except Exception as e:
                logger.warning(f"[dedup] delete {nid} failed (trigger={trigger}): {e}")

    await _emit_dedup_log(
        cache,
        request_id=request_id,
        user_id=user_id,
        agent_id=agent_id,
        trigger=trigger,
        plan=plan,
    )

    logger.info(
        f"[dedup] trigger={trigger} groups={len(plan.get('groups', []))} "
        f"deleted={len(delete_ids)} (delete_from_store={delete_from_store})"
    )
    return plan
