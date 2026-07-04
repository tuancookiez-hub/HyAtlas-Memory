from __future__ import annotations

import importlib
import json


def test_config_model_writes_hyatlas_home(monkeypatch, tmp_path):
    monkeypatch.setenv("HYATLAS_HOME", str(tmp_path))

    import hyatlas_memory.config_cli as cli
    import hyatlas_memory.layout as layout

    cli = importlib.reload(cli)
    layout = importlib.reload(layout)

    args = type("Args", (), {
        "base_url": "https://api.example.test/v1",
        "model": "test-model",
        "key": "secret-key",
        "mode": "pro",
    })()
    assert cli.model(args) == 0

    cfg = json.loads(layout.cfgfile().read_text(encoding="utf-8"))
    assert cfg["llm"]["base_url"] == "https://api.example.test/v1"
    assert cfg["llm"]["model"] == "test-model"
    assert cfg["llm"]["api_key"] == "secret-key"
    assert cfg["mode"] == "pro"


def test_config_validate_rejects_missing_llm(monkeypatch, tmp_path):
    monkeypatch.setenv("HYATLAS_HOME", str(tmp_path))

    import hyatlas_memory.config_cli as cli
    import hyatlas_memory.layout as layout

    cli = importlib.reload(cli)
    layout = importlib.reload(layout)
    layout.ensure()
    layout.cfgfile().write_text(json.dumps({"mode": "pro", "llm": {}}), encoding="utf-8")

    assert cli.validate(type("Args", (), {})()) == 1


def test_config_validate_lite_without_llm(monkeypatch, tmp_path):
    monkeypatch.setenv("HYATLAS_HOME", str(tmp_path))

    import hyatlas_memory.config_cli as cli
    import hyatlas_memory.layout as layout

    cli = importlib.reload(cli)
    layout = importlib.reload(layout)
    layout.ensure()
    layout.cfgfile().write_text(json.dumps({"mode": "lite", "llm": {}}), encoding="utf-8")

    assert cli.validate(type("Args", (), {})()) == 0
