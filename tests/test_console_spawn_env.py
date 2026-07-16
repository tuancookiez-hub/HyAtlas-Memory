"""Unit tests for status-console spawn environment hygiene."""
from __future__ import annotations

import importlib
import os
import sys


def test_console_spawn_env_strips_msys_keys(monkeypatch):
    monkeypatch.setenv("MSYSTEM", "MINGW64")
    monkeypatch.setenv("MINGW_PREFIX", "C:/msys64/mingw64")
    monkeypatch.setenv("CYGWIN", "1")
    monkeypatch.setenv("TERM_PROGRAM", "mintty")
    monkeypatch.setenv("ORIGINAL_PATH", "C:\\Windows")
    monkeypatch.setenv("HYATLAS_HOME", "C:/tmp/hyatlas-test-home")

    import hyatlas_memory._start as start

    start = importlib.reload(start)
    env = start._console_spawn_env()

    for key in (
        "MSYSTEM",
        "MSYSTEM_CARCH",
        "MSYSTEM_CHOST",
        "MSYSTEM_PREFIX",
        "MINGW_PREFIX",
        "MINGW_CHOST",
        "MINGW_PACKAGE_PREFIX",
        "CYGWIN",
        "TERM_PROGRAM",
        "ORIGINAL_PATH",
    ):
        assert key not in env, f"{key} should be stripped from console spawn env"

    assert "PYTHONPATH" in env
    # Editable package root (parent of hyatlas_memory/) must be on PYTHONPATH.
    assert any("hyatlas" in p.lower() or "src" in p.lower() or p for p in env["PYTHONPATH"].split(os.pathsep))


def test_console_log_path_uses_layout(monkeypatch, tmp_path):
    monkeypatch.setenv("HYATLAS_HOME", str(tmp_path))
    # Re-import console module so LOG_PATH is re-evaluated if needed.
    if "hyatlas_memory.console" in sys.modules:
        del sys.modules["hyatlas_memory.console"]
    import hyatlas_memory.console as console

    console = importlib.reload(console)
    path = console._log_path()
    assert path == tmp_path / "logs" / "hy-memory_server.log"
