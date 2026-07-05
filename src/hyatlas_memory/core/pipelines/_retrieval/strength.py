# -*- coding: utf-8 -*-
"""
HY Memory - Memory Strength（基于"闲置时长"的时间衰减排序）

第一版记忆排序信号 strength，反映记忆的"活跃度"——基于使用近度 + 使用频次，
而非创建以来的绝对年龄：

    idle_days = (now - last_accessed_at) / 1 day        # 不是 age（创建至今）
    strength  = (1 + log(access_count)) * exp(-idle_days / tau)   # tau 默认 180

设计理由：2023 年创建的"用户喜欢 Kobe"若昨天刚被命中，依然重要；用 age_days 会
被衰减得很惨，但用 idle_days（自上次访问以来）就能保持强度。高频命中（access_count
大）再叠加 log 频次加权。

冷启动：last_accessed_at 为空时 idle_days 退回用 gmt_created 计算（新记忆 idle≈0
→ 满强度）。access_count=0 时 freq=1.0（不罚不奖，避免 log(0)）。

最终排序：final_score = relevance_score × strength（乘法叠加），仅作用于 normal
通道（L2/L3/L4），profile（L0/L6）/ intention（L7）不参与。
"""

from __future__ import annotations

import logging
import math
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Set

logger = logging.getLogger(__name__)

DEFAULT_TAU = 180.0


def compute_strength(node: Any, *, now: Optional[datetime] = None, tau: float = DEFAULT_TAU) -> float:
    """
    计算单个节点的 strength。

    strength = (1 + log(access_count)) * exp(-idle_days / tau)
      - access_count < 1 → 频次因子 = 1.0（避免 log(0)，不罚不奖）
      - last_accessed_at 为空 → idle_days 用 gmt_created 计算（冷启动）
      - 两者都为空 → idle_days = 0（满近度）
    """
    if tau <= 0:
        tau = DEFAULT_TAU
    _now = now or datetime.now()

    ac = getattr(node, "access_count", 0) or 0
    if ac < 0:
        ac = 0
    freq = 1.0 + math.log(ac) if ac >= 1 else 1.0

    last = getattr(node, "last_accessed_at", None) or getattr(node, "gmt_created", None)
    idle_days = 0.0
    if last is not None:
        try:
            idle_days = max(0.0, (_now - last).total_seconds() / 86400.0)
        except (TypeError, ValueError):
            idle_days = 0.0

    return freq * math.exp(-idle_days / tau)


def apply_strength_to_normal(
    hits: List[Dict[str, Any]],
    *,
    profile_layers: Optional[Set[str]] = None,
    intention_layers: Optional[Set[str]] = None,
    score_key: str = "score",
    now: Optional[datetime] = None,
    tau: float = DEFAULT_TAU,
) -> List[Dict[str, Any]]:
    """
    就地把 strength 乘进 normal 通道命中的分数；profile / intention 层原样透传。

    hits: [{"node": MemoryNode, "score": float, "node_id": ...}, ...]
    profile_layers / intention_layers: 不参与衰减的 layer value 集合（如
        {"l0_basic_info","l6_schema"} / {"l7_intention"}）。
    返回同一个 list（已就地修改），方便链式调用。
    """
    _now = now or datetime.now()
    _profile = profile_layers or set()
    _intention = intention_layers or set()

    for h in hits:
        node = h.get("node")
        if node is None:
            continue
        layer_val = getattr(getattr(node, "layer", None), "value", None) or h.get("layer", "")
        if layer_val in _profile or layer_val in _intention:
            continue
        s = compute_strength(node, now=_now, tau=tau)
        h[score_key] = float(h.get(score_key, 0.0)) * s
    return hits


async def bump_access(
    vector_store: Any,
    items: Iterable[Any],
    *,
    now: Optional[datetime] = None,
) -> int:
    """
    best-effort 把命中节点的 access_count+1、last_accessed_at=now 写回 VDB。

    items: 可迭代的 (node_id, current_access_count) 二元组。
    任何写失败只记 debug，绝不抛错（不影响搜索响应）。返回成功写入条数。
    """
    _now = now or datetime.now()
    ts = int(_now.timestamp())
    ok = 0
    for node_id, current in items:
        if not node_id:
            continue
        try:
            new_count = int(current or 0) + 1
            success = await vector_store.update_payload(
                node_id,
                {"access_count": new_count, "last_accessed_at": ts},
            )
            if success:
                ok += 1
        except Exception as e:
            logger.debug(f"[strength] bump_access failed for {node_id}: {e}")
    return ok
