from __future__ import annotations

import importlib
import json
import os
from pathlib import Path


def test_hyatlas_home_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HYATLAS_HOME", str(tmp_path / "home"))

    import hyatlas_memory.layout as layout

    layout = importlib.reload(layout)
    assert layout.home() == tmp_path / "home"
    assert layout.cfgfile() == tmp_path / "home" / "config" / "hy_memory.json"
    assert layout.qdata() == tmp_path / "home" / "data" / "qdrant"


def test_config_prefers_hyatlas_home(monkeypatch, tmp_path):
    root = tmp_path / "hyatlas"
    legacy = tmp_path / "hermes"
    monkeypatch.setenv("HYATLAS_HOME", str(root))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(legacy))

    import hyatlas_memory.layout as layout

    layout = importlib.reload(layout)
    layout.cfgdir().mkdir(parents=True)
    legacy.mkdir(parents=True)
    (layout.cfgfile()).write_text(json.dumps({"mode": "ultra"}), encoding="utf-8")
    (legacy / "hy_memory.json").write_text(json.dumps({"mode": "lite"}), encoding="utf-8")

    assert layout.active_config_path() == layout.cfgfile()
    assert layout.read_config()["mode"] == "ultra"


def test_config_falls_back_to_legacy(monkeypatch, tmp_path):
    root = tmp_path / "hyatlas"
    legacy = tmp_path / "hermes"
    monkeypatch.setenv("HYATLAS_HOME", str(root))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(legacy))

    import hyatlas_memory.layout as layout

    layout = importlib.reload(layout)
    legacy.mkdir(parents=True)
    (legacy / "hy_memory.json").write_text(json.dumps({"mode": "pro"}), encoding="utf-8")

    assert layout.active_config_path() == legacy / "hy_memory.json"
    assert layout.read_config()["mode"] == "pro"


def test_load_env_precedence(monkeypatch, tmp_path):
    root = tmp_path / "hyatlas"
    legacy = tmp_path / "hermes"
    monkeypatch.setenv("HYATLAS_HOME", str(root))
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(legacy))

    import hyatlas_memory.layout as layout

    layout = importlib.reload(layout)
    layout.cfgdir().mkdir(parents=True)
    legacy.mkdir(parents=True)
    layout.envfile().write_text("HY_MEMORY_MODE=ultra\n", encoding="utf-8")
    (legacy / ".env").write_text("HY_MEMORY_MODE=lite\nHY_MEMORY_AGENT_ID=legacy\n", encoding="utf-8")

    layout.load_envs()

    assert Path(layout.envfile()).exists()
    assert os.environ["HY_MEMORY_MODE"] == "ultra"
    assert os.environ["HY_MEMORY_AGENT_ID"] == "legacy"
