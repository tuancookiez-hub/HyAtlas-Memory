from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

from hyatlas_memory.core.config import MemoryConfig
from hyatlas_memory.core.data.vector_store_zvec import ZvecVectorStore, resolve_zvec_path

spec = importlib.util.spec_from_file_location(
    "migrate_qdrant_to_zvec",
    Path(__file__).resolve().parents[1] / "scripts" / "migrate_qdrant_to_zvec.py",
)
mod = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = mod
spec.loader.exec_module(mod)


def test_migration_import_uses_runtime_schema_and_releases_store(monkeypatch, tmp_path):
    monkeypatch.setenv("HYATLAS_HOME", str(tmp_path))
    config = MemoryConfig()
    config.vector_store.provider = "zvec"
    config.vector_store.collection_name = "agent_memories"
    config.vector_store.embedding_dims = 4
    path = resolve_zvec_path(config)

    count = mod.import_zvec(str(path), [
        {
            "id": "node-a",
            "vector": [0.1, 0.2, 0.3, 0.4],
            "payload": {
                "node_id": "node-a",
                "user_id": "user-a",
                "agent_id": "agent-a",
                "session_id": "session-a",
                "layer": "l2_fact",
                "content": "migrated zvec proof",
                "status": "active",
                "is_latest": True,
                "gmt_created": 1782703943,
                "memory_at": "1783273685",
                "custom": {"k": "v"},
            },
        }
    ], dims=4)

    assert count == 1

    store = ZvecVectorStore(config)
    asyncio.run(store.initialize())
    node = asyncio.run(store.get_by_id("node-a"))
    asyncio.run(store.close())

    assert node is not None
    assert node.content == "migrated zvec proof"
    assert node.custom == {"k": "v"}
    assert node.gmt_created is not None
    assert node.memory_at is not None
