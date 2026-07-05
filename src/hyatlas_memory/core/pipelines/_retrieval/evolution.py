"""
演化链回溯。

原逻辑从 `reader.py` 抽出，三个 reader（legacy / hybrid / hybrid_tag）共用。

职责：
  对一批 search hits，识别其中在演化链上的节点，双向追溯完整链，
  以链头（is_latest=True）为代表返回，同链多个 hit 去重合并。

返回格式：
  每个 evolved hit 附加 `evolution_chain` 字段（list，latest→oldest 排序），
  每个元素 = {node_id, content, memory_at, gmt_created, speculate, layer}。
  时间字段统一为 Unix timestamp (float)。
"""

from datetime import datetime
from typing import Any, Dict, List, Optional, Set
import asyncio
import logging

from ...models.memory import MemoryNode

logger = logging.getLogger(__name__)


def _fmt_time(node: MemoryNode) -> Optional[float]:
    """返回 node.memory_at 的 Unix timestamp；缺失返回 None。"""
    t = node.memory_at
    if not t:
        return None
    try:
        return int(t.timestamp())
    except Exception:
        return None


def _time_key(node: MemoryNode):
    """演化链内部排序用 tiebreaker：memory_at 优先，缺失回落 gmt_created。"""
    return node.memory_at or node.gmt_created or datetime.min


def _node_to_chain_item(node: MemoryNode) -> Dict[str, Any]:
    """将 MemoryNode 转为链条目（序列化友好的 dict）。"""
    return {
        "node_id": node.node_id,
        "content": node.content,
        "memory_at": _fmt_time(node),
        "gmt_created": int(node.gmt_created.timestamp()) if node.gmt_created else None,
        "speculate": getattr(node, "speculate", None),
        "layer": node.layer.value if node.layer else "",
    }


async def _trace_full_chain(
    vector_store,
    start_node: MemoryNode,
) -> List[MemoryNode]:
    """
    从任意节点出发，**双向**追溯整条演化链。

    - 向前（supersedes）追溯祖先
    - 向后（superseded_by）追溯后继，找到真正的链头（is_latest=True）

    返回整条链（链头在 [0]）。如果只有自身则返回 [start_node]。
    """
    visited: Dict[str, MemoryNode] = {start_node.node_id: start_node}
    to_fetch: List[str] = []

    # 收集双向 ID
    if start_node.supersedes:
        to_fetch.extend(start_node.supersedes)
    if start_node.superseded_by:
        to_fetch.extend(start_node.superseded_by)

    while to_fetch:
        ids_batch = [i for i in to_fetch if i not in visited]
        if not ids_batch:
            break
        try:
            nodes = await vector_store.get_by_ids(ids_batch)
        except Exception as e:
            logger.warning(f"[evolution] get_by_ids failed: {e}")
            break
        new_to_fetch: List[str] = []
        for n in nodes:
            if n.node_id not in visited:
                visited[n.node_id] = n
                if n.supersedes:
                    new_to_fetch.extend(n.supersedes)
                if n.superseded_by:
                    new_to_fetch.extend(n.superseded_by)
        to_fetch = new_to_fetch

    if len(visited) == 1:
        return [start_node]

    all_nodes = list(visited.values())
    # 链头 = is_latest=True 的节点；如果没找到（已被删除等），取 gmt_created 最新的
    heads = [n for n in all_nodes if n.is_latest]
    if heads:
        head = max(heads, key=_time_key)
    else:
        head = max(all_nodes, key=_time_key)

    rest = sorted([n for n in all_nodes if n.node_id != head.node_id],
                  key=_time_key, reverse=True)
    return [head] + rest


async def _expand_one_chain(vector_store, head_item: Dict[str, Any]) -> Dict[str, Any]:
    """
    追溯单个 hit 的演化链（双向）。若无链或回溯失败，原样返回。

    返回：
      - 无演化：原 head_item
      - 有演化：新 dict，以链头（is_latest=True）为代表节点，附加：
          evolution_chain: [{node_id, content, memory_at, gmt_created, speculate, layer}, ...]
                           排序: latest → oldest
          is_evolved: True
          chain_node_ids: set — 链上所有 node_id（用于跨 hit 去重）
    """
    hit_node: MemoryNode = head_item.get("node")
    if not hit_node:
        return head_item

    # 如果节点既没有 supersedes 也没有 superseded_by，就不在链上
    if not hit_node.supersedes and not hit_node.superseded_by:
        return head_item

    chain = await _trace_full_chain(vector_store, hit_node)

    if len(chain) <= 1:
        return head_item

    head_node = chain[0]  # 链头（is_latest=True 的那个）

    # 防御：演化链中绝不暴露 L1_RAW 原始对话节点。
    # search 在任何路径都不返回 raw 层内容；若链上混入 raw（如跨层
    # supersede 的历史/异常 linkage），在此过滤掉，避免从 evolution_chain 泄漏。
    from ...models.memory import MemoryLayer
    chain_visible = [n for n in chain if n.layer != MemoryLayer.L1_RAW]

    # 原始链数据
    evolution_chain = [_node_to_chain_item(n) for n in chain_visible]
    chain_node_ids = {n.node_id for n in chain}

    return {
        "node_id": head_node.node_id,
        "score": head_item.get("score", 0.0),
        "node": head_node,
        "evolution_chain": evolution_chain,
        "is_evolved": True,
        "chain_node_ids": chain_node_ids,
        **{k: v for k, v in head_item.items()
           if k not in ("node_id", "score", "node",
                         "is_evolved", "evolution_chain", "chain_node_ids")},
    }


async def expand_evolution_chains(
    vector_store,
    hits: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    并发扩展一批 hits 中有演化链的节点，并去重。

    关键行为（only_latest=False 时多个链节点可能同时被召回）：
    1. 对每个在链上的 hit，双向追溯找到完整链
    2. 同一条链上的多个 hit 合并为一条结果（以链头为代表，保留最高 score）
    3. 不在链上的 hit 保持原样

    输入 hits 的元素形如 {"node_id": ..., "score": ..., "node": MemoryNode, ...}。
    输出 list 可能比输入短（因为链去重）。
    """
    # 找出需要展开的 hit（在链上的节点）
    needs: List[Dict[str, Any]] = []
    idx_of_needs: Dict[int, int] = {}  # hits_idx → needs_idx
    for i, item in enumerate(hits):
        node = item.get("node")
        if node and (getattr(node, "supersedes", None) or getattr(node, "superseded_by", None)):
            idx_of_needs[i] = len(needs)
            needs.append(item)

    if not needs:
        return list(hits)

    expanded = await asyncio.gather(
        *[_expand_one_chain(vector_store, item) for item in needs],
        return_exceptions=True,
    )

    # 链去重：同一条链的多个 hit 合并为一条（保留最高 score）
    # chain_head_id → best expanded result
    chain_dedup: Dict[str, Dict[str, Any]] = {}
    expanded_by_idx: Dict[int, Dict[str, Any]] = {}  # hits_idx → expanded result

    for i, item in enumerate(hits):
        if i not in idx_of_needs:
            continue
        exp = expanded[idx_of_needs[i]]
        if isinstance(exp, Exception):
            logger.warning(f"[evolution] expand failed for {item.get('node_id')}: {exp}")
            expanded_by_idx[i] = item  # fallback 到原始 hit
            continue

        chain_head_id = exp.get("node_id", "")
        if chain_head_id in chain_dedup:
            # 同一条链的另一个 hit — 取更高 score
            existing = chain_dedup[chain_head_id]
            if exp.get("score", 0.0) > existing.get("score", 0.0):
                chain_dedup[chain_head_id] = exp
        else:
            chain_dedup[chain_head_id] = exp

        expanded_by_idx[i] = exp

    # 收集所有已展开链覆盖的 node_id（用于过滤重复 hit）
    all_chain_node_ids: set = set()
    for exp in chain_dedup.values():
        chain_ids = exp.get("chain_node_ids", set())
        all_chain_node_ids.update(chain_ids)

    # 组装最终结果：去重 + 保持顺序
    result: List[Dict[str, Any]] = []
    seen_chain_heads: set = set()
    seen_node_ids: set = set()

    for i, item in enumerate(hits):
        nid = item.get("node_id", "")

        if i in expanded_by_idx:
            exp = expanded_by_idx[i]
            chain_head_id = exp.get("node_id", "")
            if chain_head_id in seen_chain_heads:
                continue  # 同链的另一个 hit，已经输出过了
            # 用 chain_dedup 中的最佳结果（最高 score）
            best = chain_dedup.get(chain_head_id, exp)
            seen_chain_heads.add(chain_head_id)
            seen_node_ids.add(chain_head_id)
            result.append(best)
        else:
            # 普通 hit（不在链上）——但如果它的 node_id 在某条链上，跳过
            if nid in all_chain_node_ids or nid in seen_node_ids:
                continue
            seen_node_ids.add(nid)
            result.append(item)

    return result
