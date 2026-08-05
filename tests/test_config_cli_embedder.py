from __future__ import annotations

from argparse import Namespace

from hyatlas_memory import config_cli


def test_default_local_embedder_is_small_384():
    cfg = config_cli.default_config()

    assert cfg["embedder"] == {
        "model": "BAAI/bge-small-en-v1.5",
        "dims": 384,
        "provider": "local",
    }
    assert cfg["vector_store"]["embedding_dims"] == 384


def test_embedder_preset_keeps_vector_dims_aligned(monkeypatch, tmp_path):
    monkeypatch.setenv("HYATLAS_HOME", str(tmp_path))

    assert config_cli.embedder(Namespace(preset="large", model=None, dims=None)) == 0
    large = config_cli.merged()
    assert large["embedder"]["model"] == "BAAI/bge-large-en-v1.5"
    assert large["embedder"]["dims"] == 1024
    assert large["vector_store"]["embedding_dims"] == 1024

    assert config_cli.embedder(Namespace(preset="small", model=None, dims=None)) == 0
    small = config_cli.merged()
    assert small["embedder"]["model"] == "BAAI/bge-small-en-v1.5"
    assert small["embedder"]["dims"] == 384
    assert small["vector_store"]["embedding_dims"] == 384
