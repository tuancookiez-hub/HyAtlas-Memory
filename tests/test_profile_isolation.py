"""Profile isolation + include_raw client contract tests (no live server)."""
from __future__ import annotations

from hyatlas_memory.client import HyMemoryClient


def test_list_memories_forwards_agent_id(monkeypatch):
    seen = {}

    def fake(method, path, body=None, timeout=None):
        seen["method"] = method
        seen["path"] = path
        seen["body"] = body
        return {"memories": []}

    client = HyMemoryClient()
    monkeypatch.setattr(client, "_request", fake)

    client.list_memories(user_id="hermes-user", agent_id="research", limit=10)

    assert seen["method"] == "POST"
    assert seen["path"] == "/api/v1/list"
    assert seen["body"]["user_id"] == "hermes-user"
    assert seen["body"]["agent_id"] == "research"
    assert seen["body"]["limit"] == 10


def test_add_forwards_specialist_agent_id(monkeypatch):
    seen = {}

    def fake(method, path, body=None, timeout=None):
        seen["body"] = body
        return {"success": True, "memory_id": "m1"}

    client = HyMemoryClient()
    monkeypatch.setattr(client, "_request", fake)

    client.add(
        "Research profile canary fact.",
        user_id="hermes-user",
        agent_id="research",
        session_id="profile-canary",
    )

    assert seen["body"]["user_id"] == "hermes-user"
    assert seen["body"]["agent_id"] == "research"
    assert seen["body"]["session_id"] == "profile-canary"
    assert seen["body"]["data"] == "Research profile canary fact."


def test_search_forwards_agent_ids_and_hybrid_reader(monkeypatch):
    seen = {}

    def fake(method, path, body=None, timeout=None):
        seen["body"] = body
        return {"memories": []}

    client = HyMemoryClient()
    monkeypatch.setattr(client, "_request", fake)

    client.search(
        "profile canary",
        user_id="hermes-user",
        agent_ids=["research"],
        reader="hybrid_v2",
    )

    assert seen["body"]["user_id"] == "hermes-user"
    assert seen["body"]["agent_ids"] == ["research"]
    assert seen["body"]["reader"] == "hybrid_v2"


def test_reader_resolve_hybrid_v2(monkeypatch):
    monkeypatch.setenv("HY_MEMORY_READER", "hybrid_v2")
    from hyatlas_memory.core.pipelines._retrieval import config as cfg

    assert cfg.resolve_reader_name() == "hybrid_v2"
    assert cfg.resolve_reader_name("legacy") == "legacy"
    assert cfg.resolve_reader_name("not-a-reader") == "legacy"
