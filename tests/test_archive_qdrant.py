from __future__ import annotations


def test_archive_qdrant_dry_layout(monkeypatch, tmp_path):
    data = tmp_path / "data" / "qdrant"
    data.mkdir(parents=True)
    (data / "meta.json").write_text("{}", encoding="utf-8")

    monkeypatch.setenv("HYATLAS_HOME", str(tmp_path))

    from argparse import Namespace

    import hyatlas_memory.archive_cli as arc

    rc = arc.archive_qdrant(Namespace(label="test", force=False))
    assert rc == 0
    zips = list((tmp_path / "archive").glob("qdrant_test.zip"))
    assert len(zips) == 1
    assert zips[0].stat().st_size > 0
