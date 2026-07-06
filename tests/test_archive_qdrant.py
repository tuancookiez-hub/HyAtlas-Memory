from __future__ import annotations

from pathlib import Path


def test_archive_qdrant_dry_layout(monkeypatch, tmp_path):
    data = tmp_path / "data" / "qdrant"
    data.mkdir(parents=True)
    (data / "meta.json").write_text("{}", encoding="utf-8")

    monkeypatch.setenv("HYATLAS_HOME", str(tmp_path))

    import hyatlas_memory.archive_cli as arc
    from argparse import Namespace

    rc = arc.archive_qdrant(Namespace(label="test", force=False))
    assert rc == 0
    zips = list((tmp_path / "archive").glob("qdrant_test.zip"))
    assert len(zips) == 1
    assert zips[0].stat().st_size > 0