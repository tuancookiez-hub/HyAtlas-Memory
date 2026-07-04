from __future__ import annotations

from hyatlas_memory.client import HyMemoryClient


def test_search_defaults_to_legacy_reader(monkeypatch):
    seen = {}

    def fake(method, path, body=None, timeout=None):
        seen["method"] = method
        seen["path"] = path
        seen["body"] = body
        return {"memories": []}

    client = HyMemoryClient()
    monkeypatch.setattr(client, "_request", fake)

    client.search("favorite color", user_ids=["u"])

    assert seen["method"] == "POST"
    assert seen["path"] == "/api/v1/search"
    assert seen["body"]["reader"] == "legacy"


def test_search_allows_reader_override(monkeypatch):
    seen = {}

    def fake(method, path, body=None, timeout=None):
        seen["body"] = body
        return {"memories": []}

    client = HyMemoryClient()
    monkeypatch.setattr(client, "_request", fake)

    client.search("favorite color", user_ids=["u"], reader="hybrid_v2")

    assert seen["body"]["reader"] == "hybrid_v2"
