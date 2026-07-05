# -*- coding: utf-8 -*-
"""
HY Memory - Intention (L7) 召回 + 惰性过期转换

意图（L7_INTENTION）由 extractor（System 1）直接写入 VDB，带可选 `valid_until`
截止日。reader 在 proactive 路调用本模块召回意图：

  - 召回 status=ACTIVE 的 L7 节点；
  - 对每条命中检查 `valid_until`：若已过期（now > valid_until），则**惰性**把该
    节点的 layer 改写为 l2_fact（持久化 update_payload），并从意图结果里剔除——
    过期意图变成普通历史事实，下次走 normal 召回；
  - 返回仍然有效（未过期）的意图命中。

best-effort：过期转换失败只记 debug，绝不影响搜索主流程。
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional

from ...models.memory import MemoryLayer, MemoryStatus

logger = logging.getLogger(__name__)


async def recall_intentions(
    vector_store: Any,
    query_embedding: List[float],
    *,
    user_ids: Optional[List[str]] = None,
    agent_ids: Optional[List[str]] = None,
    limit: int = 10,
    now: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """
    召回未过期的 L7_INTENTION 节点（VDB），并惰性把过期意图转成 L2_FACT。

    Args:
        vector_store: VectorStoreBase 实例
        query_embedding: 查询向量
        user_ids: 用户隔离（单/多用户）
        agent_ids: 可选 agent 过滤
        limit: 召回上限
        now: 当前时间（默认 datetime.now()，测试可注入）

    Returns:
        [{"node_id", "score", "node", "layer": "l7_intention", "source": "vdb_intention"}, ...]
    """
    if limit <= 0:
        return []
    _now = now or datetime.now()

    try:
        hits = await vector_store.search(
            query_embedding=query_embedding,
            user_ids=user_ids or None,
            agent_ids=agent_ids,
            layers=[MemoryLayer.L7_INTENTION],
            limit=limit,
            score_threshold=0.0,  # intention 不设语义门槛
            status_filter=[MemoryStatus.ACTIVE],
            only_latest=True,
        ) or []
    except Exception as e:
        logger.debug(f"[intention] recall search failed: {e}")
        return []

    survivors: List[Dict[str, Any]] = []
    for h in hits:
        node = h.get("node")
        node_id = h.get("node_id", "")
        valid_until = getattr(node, "valid_until", None) if node else None

        if valid_until and _now > valid_until:
            # 过期 → 惰性转 L2_FACT（持久化），从意图结果剔除
            try:
                await vector_store.update_payload(node_id, {"layer": MemoryLayer.L2_FACT.value})
                logger.debug(
                    f"[intention] expired L7 → L2_FACT: node_id={node_id} "
                    f"valid_until={valid_until}"
                )
            except Exception as e:
                logger.debug(f"[intention] expire conversion failed for {node_id}: {e}")
            continue

        survivors.append({
            **h,
            "layer": MemoryLayer.L7_INTENTION.value,
            "source": "vdb_intention",
        })

    return survivors
