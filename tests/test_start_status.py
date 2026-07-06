from __future__ import annotations

import importlib


def test_collection_defaults_to_embedder_dims(monkeypatch):
    monkeypatch.delenv("MEMORY_VECTOR_COLLECTION", raising=False)

    import hyatlas_memory._start as start

    start = importlib.reload(start)
    assert start._collection({"embedder": {"dims": 384}}) == "agent_memories_384"


def test_collection_env_override(monkeypatch):
    monkeypatch.setenv("MEMORY_VECTOR_COLLECTION", "custom_collection")

    import hyatlas_memory._start as start

    start = importlib.reload(start)
    assert start._collection({"embedder": {"dims": 1024}}) == "custom_collection"


def test_collection_suffixes_base_config_name(monkeypatch):
    monkeypatch.delenv("MEMORY_VECTOR_COLLECTION", raising=False)

    import hyatlas_memory._start as start

    start = importlib.reload(start)
    cfg = {"vector_store": {"collection": "agent_memories"}, "embedder": {"dims": 1024}}
    assert start._collection(cfg) == "agent_memories_1024"


def test_collection_keeps_suffixed_config_name(monkeypatch):
    monkeypatch.delenv("MEMORY_VECTOR_COLLECTION", raising=False)

    import hyatlas_memory._start as start

    start = importlib.reload(start)
    cfg = {"vector_store": {"collection": "agent_memories_1024"}, "embedder": {"dims": 1024}}
    assert start._collection(cfg) == "agent_memories_1024"
