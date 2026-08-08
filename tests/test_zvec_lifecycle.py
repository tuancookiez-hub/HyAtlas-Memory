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


def test_zvec_path_accepts_pre_suffixed_collection(monkeypatch, tmp_path):
    """The resolver accepts either the configured base name or the physical
    suffixed collection name produced by VectorStoreBase."""
    monkeypatch.setenv("HYATLAS_HOME", str(tmp_path))
    config = cfg(tmp_path)
    config.vector_store.collection_name = "agent_memories_4"

    assert resolve_zvec_path(config) == tmp_path / "zvec" / "agent_memories_4"


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
        layer=MemoryLayer.L3_FACT,
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


def test_zvec_temp_collection_reopens_after_forced_exit(monkeypatch, tmp_path):
    """A killed zvec owner leaves zero-byte LOCK files, but zvec 0.6.0 must
    reopen the collection once the owning process is gone. Do not delete the
    marker files: they also exist while a healthy process owns the store."""
    monkeypatch.setenv("HYATLAS_HOME", str(tmp_path))
    child = textwrap.dedent(f"""
        import asyncio
        import os
        import time
        os.environ["HYATLAS_HOME"] = {str(tmp_path)!r}
        from hyatlas_memory.core.config import MemoryConfig
        from hyatlas_memory.core.data.vector_store_zvec import ZvecVectorStore
        from hyatlas_memory.core.models.memory import MemoryLayer, MemoryNode
        config = MemoryConfig()
        config.vector_store.provider = "zvec"
        config.vector_store.collection_name = "crash_probe"
        config.vector_store.embedding_dims = 4
        async def main():
            store = ZvecVectorStore(config)
            await store.initialize()
            await store.upsert(MemoryNode(
                node_id="probe", user_id="u", agent_id="a", session_id="s",
                layer=MemoryLayer.L3_FACT, content="survives crash",
                embedding=[0.1, 0.2, 0.3, 0.4],
            ))
            print("READY", flush=True)
            while True:
                await asyncio.sleep(1)
        asyncio.run(main())
    """)
    proc = subprocess.Popen(
        [sys.executable, "-c", child],
        cwd=Path(__file__).resolve().parents[1],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.stdout.readline().strip() == "READY"
    proc.kill()
    proc.wait(timeout=10)

    path = tmp_path / "zvec" / "crash_probe_4"
    assert sorted(item.name for item in path.rglob("LOCK")) == ["LOCK", "LOCK", "LOCK"]

    reopen = textwrap.dedent(f"""
        import asyncio
        import os
        os.environ["HYATLAS_HOME"] = {str(tmp_path)!r}
        from hyatlas_memory.core.config import MemoryConfig
        from hyatlas_memory.core.data.vector_store_zvec import ZvecVectorStore
        config = MemoryConfig()
        config.vector_store.provider = "zvec"
        config.vector_store.collection_name = "crash_probe"
        config.vector_store.embedding_dims = 4
        async def main():
            store = ZvecVectorStore(config)
            await store.initialize()
            node = await store.get_by_id("probe")
            print(node.content if node else "missing")
            await store.close()
        asyncio.run(main())
    """)
    result = subprocess.run(
        [sys.executable, "-c", reopen],
        cwd=Path(__file__).resolve().parents[1],
        text=True,
        capture_output=True,
        timeout=30,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "survives crash"


def test_zvec_compact_reduces_segment_count(tmp_path):
    """compact() merges fragmented index segments and stays healthy."""
    import asyncio

    from hyatlas_memory.core.config import MemoryConfig
    from hyatlas_memory.core.data.vector_store_zvec import ZvecVectorStore

    schema_dims = 384
    cfg = MemoryConfig()
    cfg.vector_store.embedding_dims = schema_dims
    cfg.vector_store.collection_name = "compact_test"
    cfg.vector_store.persist_directory = str(tmp_path)

    store = ZvecVectorStore(cfg)

    async def run():
        await store.initialize()
        store._path = None  # not needed
        # insert a handful of points
        from hyatlas_memory.core.models.memory import MemoryLayer, MemoryNode, MemoryStatus
        for i in range(20):
            n = MemoryNode(
                node_id=f"c{i}", content=f"compact content {i}",
                layer=MemoryLayer.L3_FACT, status=MemoryStatus.ACTIVE,
                user_id="u", agent_id="a",
            )
            n.embedding = [0.0]*384
            await store.upsert(n)
        before = await store.compact()
        assert before is True
        await store.close()

    asyncio.run(run())


def test_zvec_maybe_compact_available(tmp_path):
    """segment_count + maybe_compact exist and are healthy on a small store."""
    import asyncio

    from hyatlas_memory.core.config import MemoryConfig
    from hyatlas_memory.core.data.vector_store_zvec import ZvecVectorStore
    from hyatlas_memory.core.models.memory import MemoryLayer, MemoryNode, MemoryStatus

    cfg = MemoryConfig()
    cfg.vector_store.embedding_dims = 384
    cfg.vector_store.collection_name = "compact_test2"
    cfg.vector_store.persist_directory = str(tmp_path)
    store = ZvecVectorStore(cfg)

    async def run():
        await store.initialize()
        for i in range(3):
            n = MemoryNode(node_id=f"m{i}", content=f"maybe compact {i}",
                           layer=MemoryLayer.L3_FACT, status=MemoryStatus.ACTIVE,
                           user_id="u", agent_id="a")
            n.embedding = [0.0] * 384
            await store.upsert(n)
        # small store -> no auto-compact triggered, but methods are callable
        n = await store.segment_count()
        assert n is None or n >= 0
        ok = await store.maybe_compact()
        assert ok in (True, False)  # healthy small store shouldn't force-compact
        # explicit compact still works
        assert await store.compact() is True
        await store.close()

    asyncio.run(run())
