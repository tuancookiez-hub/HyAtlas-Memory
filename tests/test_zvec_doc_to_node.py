from __future__ import annotations

from datetime import datetime, timezone


def test_doc_to_node_normalizes_epoch_strings():
    """Migrated Qdrant timestamps arrive as epoch strings like '1782866658'."""

    from hyatlas_memory.core.data.vector_store_zvec import ZvecVectorStore

    class FakeDoc:
        def __init__(self, fields):
            self._fields = fields

        def field(self, name):
            return self._fields.get(name)

        def vector(self, _):
            return None

    fields = {
        "node_id": "n1",
        "layer": "l2_fact",
        "gmt_created": "1782866658",
        "memory_at": "1782866658",
        "is_latest": "true",
        "status": "active",
        "user_id": "tuanc",
        "agent_id": "default_agent",
        "content": "test",
    }
    doc = FakeDoc(fields)

    class FakeVS(ZvecVectorStore):
        def __init__(self):
            self._coll = None
            self._path = None

    vs = FakeVS()
    node = vs._doc_to_node(doc)
    assert node.node_id == "n1"
    # _payload_to_node may coerce to datetime; normalize comparison via str
    gmt = node.gmt_created
    gmt_str = gmt.isoformat() if hasattr(gmt, "isoformat") else str(gmt)
    assert gmt_str.startswith("2026-")
    iso = datetime.fromtimestamp(1782866658, tz=timezone.utc).isoformat()
    assert gmt_str == iso
