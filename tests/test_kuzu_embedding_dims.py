from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

pytest.importorskip("kuzu")

from hyatlas_memory.core.config import MemoryConfig
from hyatlas_memory.core.data.graph_store_kuzu import KuzuGraphStore
from hyatlas_memory.core.models.memory import MemoryLayer, MemoryNode


def cfg(path: Path, dims: int):
    config = MemoryConfig()
    config.graph_store.provider = "kuzu"
    config.graph_store.db_path = str(path)
    config.vector_store.embedding_dims = dims
    return config


def node(node_id: str, dims: int):
    item = MemoryNode(
        node_id=node_id,
        user_id="user-a",
        agent_id="default",
        session_id="session-a",
        layer=MemoryLayer.L6_SCHEMA,
        content=f"schema {node_id}",
    )
    item._graph_embedding = [0.1] * dims
    item._graph_beh_embedding = [0.2] * dims
    return item


def rows(conn, query: str):
    result = conn.execute(query)
    found = []
    while result.has_next():
        found.append(result.get_next())
    return found


def test_existing_1024_graph_adds_active_384_lane_without_reindex(tmp_path):
    path = tmp_path / "kuzu"
    old = KuzuGraphStore(cfg(path, 1024))
    asyncio.run(old.initialize())
    asyncio.run(old.upsert_memory_node(node("old-node", 1024)))
    asyncio.run(old.upsert_memory_node(node("old-peer", 1024)))
    asyncio.run(old.add_edge("old-node", "old-peer", "RELATED_TO"))
    before = rows(old._conn, "MATCH (m:Memory) RETURN count(m);")[0][0]
    edges = rows(old._conn, "MATCH ()-[r:RELATED_TO]->() RETURN count(r);")[0][0]
    asyncio.run(old.close())

    current = KuzuGraphStore(cfg(path, 384))
    asyncio.run(current.initialize())

    props = {row[1]: row[2] for row in rows(current._conn, "CALL table_info('Memory') RETURN *;")}
    indexes = {row[1]: row[3] for row in rows(current._conn, "CALL show_indexes() RETURN *;")}
    assert props["embedding"] == "FLOAT[1024]"
    assert props["beh_embedding"] == "FLOAT[1024]"
    assert props["embedding_384"] == "FLOAT[384]"
    assert props["beh_embedding_384"] == "FLOAT[384]"
    assert indexes["memory_content_idx_384"] == ["embedding_384"]

    asyncio.run(current.upsert_memory_node(node("new-node", 384)))
    after = rows(current._conn, "MATCH (m:Memory) RETURN count(m);")[0][0]
    after_edges = rows(current._conn, "MATCH ()-[r:RELATED_TO]->() RETURN count(r);")[0][0]
    assert before == 2
    assert after == 3
    assert edges == 2
    assert after_edges == edges

    hits = asyncio.run(
        current.vector_search(
            query_embedding=[0.1] * 384,
            isolation_key="",
            user_id="user-a",
            layers=["l6_schema"],
            limit=5,
        )
    )
    assert [hit["node_id"] for hit in hits] == ["new-node"]

    stats = asyncio.run(current.get_stats())
    assert stats["vector_schema_compatible"] is True
    assert stats["embedding_dims"] == 384
    assert stats["embedding_property"] == "embedding_384"
    assert stats["legacy_embedding_dims"] == [1024]
    asyncio.run(current.close())


def test_fresh_384_graph_uses_canonical_embedding_lane(tmp_path):
    store = KuzuGraphStore(cfg(tmp_path / "kuzu", 384))
    asyncio.run(store.initialize())

    props = {row[1]: row[2] for row in rows(store._conn, "CALL table_info('Memory') RETURN *;")}
    assert props["embedding"] == "FLOAT[384]"
    assert props["beh_embedding"] == "FLOAT[384]"

    stats = asyncio.run(store.get_stats())
    assert stats["vector_schema_compatible"] is True
    assert stats["embedding_property"] == "embedding"
    assert stats["legacy_embedding_dims"] == []
    asyncio.run(store.close())
