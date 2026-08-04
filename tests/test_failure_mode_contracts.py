"""Failure-mode certification: component-down behavior for the dashboard health surface.

Phase 7.2 contract tests. Prove that when a backend component is unavailable
(embedder, VDB, or Kuzu graph), the dashboard health/status surfaces report
degraded/error — never a false green — and name the broken component.

These test the contract without touching the live stores (no zvec/Kuzu
renames, no LOCK-corruption risk). The dashboard functions under test are the
same ones the live server uses.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

PATH = (
    Path(__file__).parents[1] / "src" / "hyatlas_memory" / "server" / "dashboard" / "dashboard.py"
)


def load():
    spec = importlib.util.spec_from_file_location("hyatlas_dashboard_failure_test", PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(mod)
    return mod


def test_health_rejects_embedder_unavailable(monkeypatch):
    mod = load()
    broken = {"status": "ok", "vdb": "ok", "embed": "error: model load failed", "kuzu": "ok", "llm": "ok"}
    monkeypatch.setattr(mod, "_upstream_status", lambda: (200, broken))

    code, payload = mod._upstream_health()

    assert code == 503
    assert payload["status"] == "error"
    assert "embed" in payload["components"]


def test_health_rejects_vdb_unavailable(monkeypatch):
    mod = load()
    broken = {"status": "ok", "vdb": "error: Can't lock read-write collection", "embed": "ok", "kuzu": "ok", "llm": "ok"}
    monkeypatch.setattr(mod, "_upstream_status", lambda: (200, broken))

    code, payload = mod._upstream_health()

    assert code == 503
    assert payload["status"] == "error"
    assert "vdb" in payload["components"]


def test_health_rejects_kuzu_unavailable(monkeypatch):
    mod = load()
    broken = {"status": "ok", "vdb": "ok", "embed": "ok", "kuzu": "error: file lock not acquired", "llm": "ok"}
    monkeypatch.setattr(mod, "_upstream_status", lambda: (200, broken))

    code, payload = mod._upstream_health()

    assert code == 503
    assert payload["status"] == "error"
    assert "kuzu" in payload["components"]


def test_status_surface_propagates_kuzu_error(monkeypatch):
    """/api/status is the dashboard's green/red surface — it must not be green
    when Kuzu is down even if VDB/embed/LLM are healthy."""
    mod = load()
    broken = {"status": "error", "vdb": "ok", "embed": "ok", "kuzu": "error: lock", "llm": "ok"}
    monkeypatch.setattr(mod, "hy", lambda *args, **kwargs: (503, broken))

    code, payload = mod._upstream_status()

    assert code == 503
    assert payload["status"] == "error"
    assert payload["kuzu"].startswith("error")


def test_health_still_green_when_all_components_ok(monkeypatch):
    mod = load()
    healthy = {"status": "ok", "vdb": "ok", "embed": "ok", "kuzu": "ok", "llm": "ok"}
    monkeypatch.setattr(mod, "_upstream_status", lambda: (200, healthy))

    code, payload = mod._upstream_health()

    assert code == 200
    assert payload["status"] == "ok"
