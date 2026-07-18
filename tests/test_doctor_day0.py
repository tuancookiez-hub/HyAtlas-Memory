"""Unit tests for hyatlas doctor Day-0 gates (fail-fast, no live stack required).

Avoids importing hyatlas_memory.cli at collection time so CI without
hermes_constants (hermes-agent not installed) can still run the suite.
"""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from types import SimpleNamespace


def _install_hermes_constants_stub() -> None:
    if "hermes_constants" in sys.modules:
        return
    stub = types.ModuleType("hermes_constants")

    def get_hermes_home():
        return Path.home() / ".hermes"

    stub.get_hermes_home = get_hermes_home  # type: ignore[attr-defined]
    sys.modules["hermes_constants"] = stub


_install_hermes_constants_stub()

import hyatlas_memory.cli as cli  # noqa: E402


class _FakeClient:
    def __init__(self, *, reachable=True, status=None, fail_status=False):
        self.base_url = "http://127.0.0.1:19527"
        self._reachable = reachable
        self._status = status or {
            "status": "ok",
            "vdb": "ok",
            "embed": "ok",
            "llm": "ok",
            "write_pipeline": "ok",
            "vdb_points": 1,
        }
        self._fail_status = fail_status

    def is_reachable(self) -> bool:
        return self._reachable

    def status(self) -> dict:
        if self._fail_status:
            raise TimeoutError("status timeout")
        return self._status


class _FakeManager:
    def __init__(self, hy_cfg: dict):
        self._hy_cfg = hy_cfg

    def _read_hy_memory_json(self) -> dict:
        return self._hy_cfg


def _write_hermes_home(tmp: Path, *, provider: str = "hy_memory") -> Path:
    home = tmp / "hermes"
    home.mkdir()
    (home / "config.yaml").write_text(
        f"memory:\n  provider: {provider}\n  memory_enabled: true\n",
        encoding="utf-8",
    )
    plug = home / "plugins" / "hy_memory"
    plug.mkdir(parents=True)
    (plug / "__init__.py").write_text("# shim\n", encoding="utf-8")
    (plug / "plugin.yaml").write_text("name: hy_memory\nversion: 3.4.1\n", encoding="utf-8")
    return home


def test_doctor_passes_when_stack_healthy(monkeypatch, tmp_path, capsys):
    home = _write_hermes_home(tmp_path)
    hy_home = tmp_path / "hyatlas"
    (hy_home / "zvec").mkdir(parents=True)
    (hy_home / "config").mkdir(parents=True)
    cfg = {
        "mode": "ultra",
        "llm": {"api_key": "sk-test"},
        "vector_store": {"provider": "zvec"},
    }
    (hy_home / "config" / "hy_memory.json").write_text(json.dumps(cfg), encoding="utf-8")

    monkeypatch.setattr(cli, "get_hermes_home", lambda: home)
    monkeypatch.setattr(cli.layout, "home", lambda: hy_home)
    monkeypatch.setattr(cli.layout, "logs", lambda: hy_home / "logs")
    monkeypatch.setattr(cli, "_get_client", lambda: _FakeClient())
    monkeypatch.setattr(cli, "_port_open", lambda host, port, timeout=0.5: True)

    import hyatlas_memory.process as process_mod

    monkeypatch.setattr(process_mod, "StackManager", lambda **kw: _FakeManager(cfg))

    rc = cli._cmd_doctor(SimpleNamespace())
    out = capsys.readouterr().out
    assert rc == 0
    assert "all checks passed" in out or "ok with" in out
    assert "Upstream server reachable" in out


def test_doctor_fails_when_upstream_down(monkeypatch, tmp_path, capsys):
    home = _write_hermes_home(tmp_path)
    hy_home = tmp_path / "hyatlas"
    hy_home.mkdir()
    cfg = {"mode": "lite", "vector_store": {"provider": "zvec"}}

    monkeypatch.setattr(cli, "get_hermes_home", lambda: home)
    monkeypatch.setattr(cli.layout, "home", lambda: hy_home)
    monkeypatch.setattr(cli.layout, "logs", lambda: hy_home / "logs")
    monkeypatch.setattr(cli, "_get_client", lambda: _FakeClient(reachable=False))
    monkeypatch.setattr(cli, "_port_open", lambda host, port, timeout=0.5: False)

    import hyatlas_memory.process as process_mod

    monkeypatch.setattr(process_mod, "StackManager", lambda **kw: _FakeManager(cfg))

    rc = cli._cmd_doctor(SimpleNamespace())
    out = capsys.readouterr().out
    assert rc == 1
    assert "Upstream not reachable" in out
    assert "hyatlas start" in out


def test_doctor_flags_wrong_provider(monkeypatch, tmp_path, capsys):
    home = _write_hermes_home(tmp_path, provider="none")
    hy_home = tmp_path / "hyatlas"
    (hy_home / "zvec").mkdir(parents=True)
    cfg = {"mode": "lite", "vector_store": {"provider": "zvec"}, "llm": {"api_key": "x"}}

    monkeypatch.setattr(cli, "get_hermes_home", lambda: home)
    monkeypatch.setattr(cli.layout, "home", lambda: hy_home)
    monkeypatch.setattr(cli.layout, "logs", lambda: hy_home / "logs")
    monkeypatch.setattr(cli, "_get_client", lambda: _FakeClient())
    monkeypatch.setattr(cli, "_port_open", lambda host, port, timeout=0.5: True)

    import hyatlas_memory.process as process_mod

    monkeypatch.setattr(process_mod, "StackManager", lambda **kw: _FakeManager(cfg))

    rc = cli._cmd_doctor(SimpleNamespace())
    out = capsys.readouterr().out
    assert rc == 1
    assert "memory.provider is not hy_memory" in out


def test_has_llm_key_reads_env(monkeypatch):
    monkeypatch.setenv("HY_MEMORY_LLM_API_KEY", "sk-x")
    assert cli._has_llm_key({}) is True
    monkeypatch.delenv("HY_MEMORY_LLM_API_KEY", raising=False)
    assert cli._has_llm_key({"llm": {"api_key": "sk-y"}}) is True
    assert cli._has_llm_key({"llm": {}}) is False
