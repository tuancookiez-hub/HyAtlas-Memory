from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

try:
    import zvec as _zvec  # noqa: F401

    _zvec_available = True
except ImportError:
    _zvec_available = False

pytestmark = pytest.mark.skipif(not _zvec_available, reason="zvec not installed")

from hyatlas_memory import vdb_dashboard
from hyatlas_memory.core.data.vector_store_zvec import ZvecVectorStore
from hyatlas_memory.core.models.memory import MemoryLayer, MemoryNode


def test_payload_by_ids_handles_memorynode_fields():
    """payload_by_ids must read importance/access_count from MemoryNode attrs."""
    n = MemoryNode(
        node_id="m1",
        content="x",
        user_id="test-user",
        agent_id="default_agent",
    )
    n.importance = "high"
    n.access_count = 7

    class FakeVS(ZvecVectorStore):
        def __init__(self):
            self._coll = None
            self._path = None

        async def get_by_ids(self, ids):
            return [n]

    class FakeClient:
        _vector_store = FakeVS()

    out = asyncio.run(vdb_dashboard._payload_by_ids_async(FakeClient(), ["m1"]))
    assert out == {"m1": {"importance": "high", "access_count": 7}}, out


def test_payload_by_ids_empty_ids():
    assert vdb_dashboard.payload_by_ids(None, []) == {}


def test_scroll_l1_uses_filtered_full_scan_and_recency():
    now = datetime.now()
    newer = MemoryNode(node_id="new", content="new", user_id="u1", agent_id="default")
    older = MemoryNode(node_id="old", content="old", user_id="u2", agent_id="default")
    newer.layer = older.layer = MemoryLayer.L1_RAW
    newer.gmt_created = now
    older.gmt_created = now - timedelta(minutes=5)

    class Doc:
        def __init__(self, node):
            self.node = node

    class Coll:
        stats = SimpleNamespace(doc_count=2)

        def query(self, **kwargs):
            self.kwargs = kwargs
            return [Doc(older), Doc(newer)]

    class FakeVS(ZvecVectorStore):
        def __init__(self, coll):
            self._coll = coll
            self.config = SimpleNamespace(
                vector_store=SimpleNamespace(embedding_dims=2),
            )

        def _doc_to_node(self, doc, include_vector=False):
            return doc.node

    coll = Coll()
    client = SimpleNamespace(_vector_store=FakeVS(coll))
    out = asyncio.run(
        vdb_dashboard._scroll_l1_async(
            client,
            ["u1", "u2"],
            limit=10,
            agent_id="default",
        )
    )

    assert [item["memory_id"] for item in out] == ["new", "old"]
    assert out[0]["_source"] == "l1_raw"
    filt = coll.kwargs["filter"]
    assert 'user_id IN ("u1", "u2")' in filt
    assert 'agent_id = "default"' in filt
    assert 'layer = "l1_raw"' in filt
    assert 'status = "active"' in filt
    assert 'is_latest = "true"' in filt
