"""Dashboard-oriented vector DB reads via the live HyMemoryClient (zvec or qdrant)."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


def _run(client, coro):
    return client._loop_thread.run(coro)


async def _layer_count_async(client, layer: str, *, require_is_latest: bool, agent_id: str = "") -> int:
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
            parts.append(f'status = {_quote("active")}')
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
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        from .core.data.vector_store_zvec import _run_in_vdb_pool

        must = [FieldCondition(key="layer", match=MatchValue(value=layer))]
        if agent_id:
            must.append(FieldCondition(key="agent_id", match=MatchValue(value=agent_id)))
        if require_is_latest:
            must.append(FieldCondition(key="is_latest", match=MatchValue(value=True)))
        elif layer == "l5_knowledge":
            must.append(FieldCondition(key="status", match=MatchValue(value="active")))

        def _count():
            return vs._client.count(
                collection_name=vs._collection_name,
                count_filter=Filter(must=must),
                exact=True,
            ).count

        return int(await _run_in_vdb_pool(_count))

    return 0


def layer_count(client, layer: str, *, require_is_latest: bool = True, agent_id: str = "") -> int:
    try:
        return _run(client, _layer_count_async(client, layer, require_is_latest=require_is_latest, agent_id=agent_id))
    except Exception as e:
        logger.warning("[vdb_dashboard] layer_count failed: %s", e)
        return 0


async def _scroll_l1_async(client, user_ids: list[str], limit: int, agent_id: str = "") -> list[dict]:
    from .core.models.memory import MemoryLayer

    vs = client._vector_store
    out: list[dict] = []
    for uid in user_ids:
        nodes = await vs.list_by_user(
            user_id=uid,
            agent_id=agent_id or None,
            layers=[MemoryLayer.L1_RAW],
            limit=min(limit, 500),
        )
        for n in nodes:
            out.append({
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
            })
            if len(out) >= limit:
                return out
    return out


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
