from __future__ import annotations

import asyncio

from hyatlas_memory.core.data.vector_store_zvec import ZvecVectorStore
from hyatlas_memory.core.models.memory import MemoryLayer, MemoryNode, MemoryStatus
from hyatlas_memory import vdb_dashboard


def test_payload_by_ids_handles_memorynode_fields():
    """payload_by_ids must read importance/access_count from MemoryNode attrs."""
    n = MemoryNode(
        node_id="m1",
        layer=MemoryLayer.L2_FACT,
        status=MemoryStatus.ACTIVE,
        content="x",
        user_id="tuanc",
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

    vs = FakeVS()
    out = asyncio.run(vdb_dashboard._payload_by_ids_async(vs, ["m1"]))
    assert out == {"m1": {"importance": "high", "access_count": 7}}, out


def test_payload_by_ids_empty_ids():
    assert vdb_dashboard.payload_by_ids(None, []) == {}
