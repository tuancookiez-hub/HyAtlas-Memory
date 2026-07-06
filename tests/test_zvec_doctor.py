from __future__ import annotations

import importlib
import json


def test_zvec_doctor_registered(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HYATLAS_HOME", str(tmp_path))

    import hyatlas_memory._cli as cli
    import hyatlas_memory.layout as layout

    cli = importlib.reload(cli)
    layout = importlib.reload(layout)
    layout.ensure()
    layout.cfgfile().write_text(json.dumps({
        "embedder": {"dims": 4},
        "vector_store": {"provider": "zvec", "collection": "agent_memories"},
    }), encoding="utf-8")

    assert cli.main(["zvec", "doctor"]) == 0
    out = capsys.readouterr().out
    assert "provider: zvec" in out
    assert "resolved path:" in out
    assert "fresh subprocess reopen:" in out


def test_zvec_doctor_warns_on_wrong_provider(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("HYATLAS_HOME", str(tmp_path))

    import hyatlas_memory._cli as cli
    import hyatlas_memory.layout as layout

    cli = importlib.reload(cli)
    layout = importlib.reload(layout)
    layout.ensure()
    layout.cfgfile().write_text(json.dumps({
        "embedder": {"dims": 4},
        "vector_store": {"provider": "qdrant", "collection": "agent_memories"},
    }), encoding="utf-8")

    assert cli.main(["zvec", "doctor"]) == 1
    assert "provider: qdrant" in capsys.readouterr().out
