from __future__ import annotations

import importlib


def test_services_omit_qdrant_when_zvec(monkeypatch, tmp_path):
    cfg = tmp_path / "config" / "hy_memory.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        '{"vector_store": {"provider": "zvec", "collection": "agent_memories"}, '
        '"embedder": {"dims": 1024}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("HYATLAS_HOME", str(tmp_path))

    import hyatlas_memory._start as start

    start = importlib.reload(start)
    names = [s["name"] for s in start._services("/tmp/project")]
    assert "Qdrant" not in names
    assert "Hy-Memory Server" in names


def test_services_reject_qdrant_runtime(monkeypatch, tmp_path):
    cfg = tmp_path / "config" / "hy_memory.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        '{"vector_store": {"provider": "qdrant"}, "embedder": {"dims": 1024}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("HYATLAS_HOME", str(tmp_path))

    import hyatlas_memory._start as start

    start = importlib.reload(start)
    names = [s["name"] for s in start._services("/tmp/project")]
    assert "Qdrant" not in names
    assert "Hy-Memory Server" in names
