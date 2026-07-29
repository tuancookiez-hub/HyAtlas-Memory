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
