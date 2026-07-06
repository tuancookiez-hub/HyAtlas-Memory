from __future__ import annotations

from types import SimpleNamespace


def test_payload_by_ids_handles_memorynode_fields():
    from hyatlas_memory.core.data.vector_store_zvec import ZvecVectorStore
    from hyatlas_memory.core.models.memory import MemoryNode, MemoryLayer, MemoryStatus

    class FakeVS(ZvecVectorStore):
        def __init__(self):
            self._coll = None
            self._path = None

    vs = FakeVS()

    # Build a minimal MemoryNode with the real attribute names
    n = MemoryNode(
        node_id="m1",
        layer=MemoryLayer.L2_FACT,
        status=MemoryStatus.ACTIVE,
        content="x",
        user_id="tuanc",
        agent_id="default_agent",
    )
    # set attributes that exist on MemoryNode after init
    n.importance = "high"
    n.access_count = 7

    class FakeClient:
        _vector_store = vs

        async def get_by_ids(self, ids):
            return [n]

    from hyatlas_memory import vdb_dashboard

    out = vdb_dashboard.payload_by_ids(FakeClient(), ["m1"])
    assert out == {"m1": {"importance": "high", "access_count": 7}}, out


def test_payload_by_ids_empty_ids():
    from hyatlas_memory import vdb_dashboard

    assert vdb_dashboard.payload_by_ids(None, []) == {}
