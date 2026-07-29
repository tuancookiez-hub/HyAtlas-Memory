"""Dashboard-oriented vector DB reads via the live HyMemoryClient.

v3.4+: only the ZvecVectorStore path is supported. The legacy
``QdrantVectorStore`` branch has been removed alongside
``vector_store_qdrant.py``. If a downstream caller somehow still has a
Qdrant client connected, this module returns ``0`` rather than crashing
the dashboard.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _run(client, coro):
    return client._loop_thread.run(coro)


async def _layer_count_async(
    client, layer: str, *, require_is_latest: bool, agent_id: str = ""
) -> int:
    vs = client._vector_store
    name = type(vs).__name__
    if name == "ZvecVectorStore":
        import zvec

        from .core.data.vector_store_zvec import _quote, _run_in_vdb_pool

        parts = [f"layer = {_quote(layer)}"]
        if agent_id:
            parts.append(f"agent_id = {_quote(agent_id)}")
        if require_is_latest:
            parts.append(f"is_latest = {_quote('true')}")
        elif layer == "l5_knowledge":
            parts.append(f"status = {_quote('active')}")
        filt = " AND ".join(parts)
        dims = vs.config.vector_store.embedding_dims or 1024

        def _q():
            rows = vs._coll.query(
                queries=zvec.Query(field_name="embedding", vector=[0.0] * dims),
                topk=100_000,
                filter=filt,
            )
            return len(rows)

        return int(await _run_in_vdb_pool(_q))

    if name == "QdrantVectorStore":
        # Defensive: vector_store_qdrant.py was deleted in v3.4+, so this
        # branch should be unreachable. Keep the guard so the dashboard
        # still returns a sane number if someone wires up a Qdrant store
        # out-of-tree.
        logger.warning(
            "[vdb_dashboard] QdrantVectorStore is no longer supported in v3.4+ "
            "(layer=%s); returning 0 for the dashboard count.",
            layer,
        )
        return 0

    return 0


def layer_count(client, layer: str, *, require_is_latest: bool = True, agent_id: str = "") -> int:
    try:
        return _run(
            client,
            _layer_count_async(
                client, layer, require_is_latest=require_is_latest, agent_id=agent_id
            ),
        )
    except Exception as e:
        logger.warning("[vdb_dashboard] layer_count failed: %s", e)
        return 0


def _stamp(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    if hasattr(value, "timestamp"):
        try:
            return float(value.timestamp())
        except (OSError, OverflowError, ValueError):
            return 0.0
    if isinstance(value, str) and value:
        try:
            from datetime import datetime

            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError):
            return 0.0
    return 0.0


def _item(n) -> dict:
    return {
        "memory_id": n.node_id,
        "layer": n.layer.value if hasattr(n.layer, "value") else str(n.layer),
        "score": None,
        "content": n.content or "",
        "metadata": {},
        "status": n.status.value if hasattr(n.status, "value") else str(n.status),
        "memory_at": None,
        "gmt_created": getattr(n, "gmt_created", 0),
        "user_id": n.user_id,
        "agent_id": n.agent_id,
        "session_id": n.session_id,
        "_source": "l1_raw",
    }


async def _scroll_l1_async(
    client, user_ids: list[str], limit: int, agent_id: str = ""
) -> list[dict]:
    from .core.data.vector_store_zvec import (
        ZvecVectorStore,
        _list_to_in,
        _quote,
        _run_in_vdb_pool,
        _safe_topk,
    )
    from .core.models.memory import MemoryLayer, MemoryStatus

    vs = client._vector_store
    limit = max(1, min(int(limit), 100_000))
    nodes = []
    if isinstance(vs, ZvecVectorStore):
        import zvec

        parts = []
        if user_ids:
            parts.append(f"user_id {_list_to_in(user_ids)}")
        if agent_id:
            parts.append(f"agent_id = {_quote(agent_id)}")
        parts.extend(
            [
                f"layer = {_quote(MemoryLayer.L1_RAW.value)}",
                f"status = {_quote(MemoryStatus.ACTIVE.value)}",
                f"is_latest = {_quote('true')}",
            ]
        )
        filt = " AND ".join(parts)
        dims = vs.config.vector_store.embedding_dims or 1024

        def _scroll():
            return vs._coll.query(
                queries=zvec.Query(field_name="embedding", vector=[0.0] * dims),
                topk=_safe_topk(vs._coll, limit),
                filter=filt,
            )

        rows = await _run_in_vdb_pool(_scroll)
        nodes = [vs._doc_to_node(row, include_vector=False) for row in rows]
    else:
        for uid in user_ids:
            nodes.extend(
                await vs.list_by_user(
                    user_id=uid,
                    agent_id=agent_id or None,
                    layers=[MemoryLayer.L1_RAW],
                    limit=min(limit, 500),
                )
            )

    nodes.sort(key=lambda n: _stamp(getattr(n, "gmt_created", None)), reverse=True)
    return [_item(n) for n in nodes[:limit]]


def scroll_l1(client, user_ids: list[str], limit: int = 1500, agent_id: str = "") -> list[dict]:
    try:
        return _run(client, _scroll_l1_async(client, user_ids, limit, agent_id))
    except Exception as e:
        logger.warning("[vdb_dashboard] scroll_l1 failed: %s", e)
        return []


async def _payload_by_ids_async(client, memory_ids: list[str]) -> dict[str, dict[str, Any]]:
    vs = client._vector_store
    nodes = await vs.get_by_ids(memory_ids)
    by_id: dict[str, dict[str, Any]] = {}
    for n in nodes:
        imp = getattr(n, "importance", None)
        acc = getattr(n, "access_count", None)
        by_id[n.node_id] = {
            "importance": imp,
            "access_count": acc,
        }
    return by_id


def payload_by_ids(client, memory_ids: list[str]) -> dict[str, dict[str, Any]]:
    if not memory_ids:
        return {}
    try:
        return _run(client, _payload_by_ids_async(client, memory_ids))
    except Exception as e:
        logger.warning("[vdb_dashboard] payload_by_ids failed: %s", e)
        return {}
