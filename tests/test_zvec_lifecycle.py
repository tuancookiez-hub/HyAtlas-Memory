from __future__ import annotations

import asyncio
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

try:
    import zvec as _zvec  # noqa: F401
    _zvec_available = True
except ImportError:
    _zvec_available = False

pytestmark = pytest.mark.skipif(not _zvec_available, reason="zvec not installed")

from hyatlas_memory.core.config import MemoryConfig
from hyatlas_memory.core.data.vector_store_zvec import ZvecVectorStore, resolve_zvec_path
from hyatlas_memory.core.models.memory import MemoryLayer, MemoryNode


def cfg(tmp_path: Path):
    config = MemoryConfig()
    config.vector_store.provider = "zvec"
    config.vector_store.collection_name = "agent_memories"
    config.vector_store.embedding_dims = 4
    os.environ["HYATLAS_HOME"] = str(tmp_path)
    return config


def test_zvec_path_uses_base_collection_once(monkeypatch, tmp_path):
    monkeypatch.setenv("HYATLAS_HOME", str(tmp_path))
    config = cfg(tmp_path)

    assert resolve_zvec_path(config) == tmp_path / "zvec" / "agent_memories_4"


def test_zvec_path_rejects_pre_suffixed_collection(monkeypatch, tmp_path):
    monkeypatch.setenv("HYATLAS_HOME", str(tmp_path))
    config = cfg(tmp_path)
    config.vector_store.collection_name = "agent_memories_4"

    with pytest.raises(ValueError, match="base collection name"):
        resolve_zvec_path(config)


def test_initialize_does_not_delete_lock_files(monkeypatch, tmp_path):
    monkeypatch.setenv("HYATLAS_HOME", str(tmp_path))
    config = cfg(tmp_path)
    path = resolve_zvec_path(config)
    lock = path / "nested" / "LOCK"
    lock.parent.mkdir(parents=True)
    lock.write_text("owned", encoding="utf-8")

    store = ZvecVectorStore(config)

    with pytest.raises((RuntimeError, ValueError, OSError)):
        asyncio.run(store.initialize())

    assert lock.read_text(encoding="utf-8") == "owned"


def test_zvec_temp_collection_reopens_after_close(monkeypatch, tmp_path):
    monkeypatch.setenv("HYATLAS_HOME", str(tmp_path))
    config = cfg(tmp_path)
    store = ZvecVectorStore(config)
    node = MemoryNode(
        node_id="node-a",
        user_id="user-a",
        agent_id="agent-a",
        session_id="session-a",
        layer=MemoryLayer.L2_FACT,
        content="zvec lifecycle proof",
        embedding=[0.1, 0.2, 0.3, 0.4],
    )

    asyncio.run(store.initialize())
    asyncio.run(store.upsert(node))
    assert asyncio.run(store.get_by_id("node-a")).content == "zvec lifecycle proof"
    asyncio.run(store.close())

    code = textwrap.dedent(f"""
        import asyncio
        import os
        os.environ["HYATLAS_HOME"] = {str(tmp_path)!r}
        from hyatlas_memory.core.config import MemoryConfig
        from hyatlas_memory.core.data.vector_store_zvec import ZvecVectorStore
        config = MemoryConfig()
        config.vector_store.provider = "zvec"
        config.vector_store.collection_name = "agent_memories"
        config.vector_store.embedding_dims = 4
        async def main():
            store = ZvecVectorStore(config)
            await store.initialize()
            node = await store.get_by_id("node-a")
            print(node.content if node else "missing")
            await store.close()
        asyncio.run(main())
    """)
    result = subprocess.run(
        [sys.executable, "-c", code],
        cwd=Path(__file__).resolve().parents[1],
        text=True,
        capture_output=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "zvec lifecycle proof"
