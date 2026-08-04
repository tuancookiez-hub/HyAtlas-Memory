from __future__ import annotations

import importlib.util
from pathlib import Path

PATH = (
    Path(__file__).parents[1] / "src" / "hyatlas_memory" / "server" / "dashboard" / "dashboard.py"
)


def load():
    spec = importlib.util.spec_from_file_location("hyatlas_dashboard_status_test", PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(mod)
    return mod


def test_status_proxy_preserves_upstream_warning(monkeypatch):
    mod = load()
    payload = {"status": "warning", "llm": "error: upstream 503", "write_pipeline": "error"}
    monkeypatch.setattr(mod, "hy", lambda *args, **kwargs: (200, payload))

    assert mod._upstream_status() == (200, payload)


def test_status_proxy_reports_transport_failure(monkeypatch):
    mod = load()
    monkeypatch.setattr(mod, "hy", lambda *args, **kwargs: (0, {"error": "timed out"}))

    code, payload = mod._upstream_status()

    assert code == 502
    assert payload["status"] == "error"
    assert payload["write_pipeline"] == "error"
    assert "timed out" in payload["error"]


def test_health_contract_cannot_be_green_when_upstream_is_offline(monkeypatch):
    mod = load()
    monkeypatch.setattr(mod, "hy", lambda *args, **kwargs: (0, {"error": "connection refused"}))

    code, payload = mod._upstream_health()

    assert code == 503
    assert payload["status"] == "error"
    assert "connection refused" in payload["error"]


def test_dashboard_liveness_is_separate_from_backend_readiness():
    mod = load()

    assert mod._dashboard_live() == (200, {"status": "ok", "service": "dashboard"})


def test_launcher_probes_dashboard_liveness_not_readiness():
    start = PATH.parents[3] / "hyatlas_memory" / "_start.py"
    text = start.read_text(encoding="utf-8")

    assert 'f"http://127.0.0.1:{DASHBOARD_PORT}/api/live"' in text


def test_readiness_preserves_provider_limited_warning(monkeypatch):
    mod = load()
    warning = {"status": "warning", "vdb": "ok", "embed": "ok", "kuzu": "ok", "llm": "rate_limited"}
    monkeypatch.setattr(mod, "_upstream_status", lambda: (200, warning))

    code, payload = mod._upstream_health()

    assert code == 200
    assert payload["status"] == "warning"
    assert payload["backend"] == warning


def test_readiness_rejects_broken_core_components(monkeypatch):
    mod = load()
    broken = {"status": "ok", "vdb": "error", "embed": "ok", "kuzu": "ok", "llm": "ok"}
    monkeypatch.setattr(mod, "_upstream_status", lambda: (200, broken))

    code, payload = mod._upstream_health()

    assert code == 503
    assert payload["status"] == "error"
