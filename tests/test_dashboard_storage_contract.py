from __future__ import annotations

import importlib.util
from pathlib import Path

PATH = (
    Path(__file__).parents[1] / "src" / "hyatlas_memory" / "server" / "dashboard" / "dashboard.py"
)
APP = PATH.with_name("app.js")


def load():
    spec = importlib.util.spec_from_file_location("hyatlas_dashboard_storage_test", PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(mod)
    return mod


def test_storage_contract_uses_active_hyatlas_home(monkeypatch, tmp_path):
    mod = load()
    (tmp_path / "zvec").mkdir()
    (tmp_path / "zvec" / "segment.bin").write_bytes(b"x" * 1024)
    monkeypatch.setattr(mod, "hy_home", lambda: tmp_path)
    monkeypatch.setattr(mod, "hy", lambda *args, **kwargs: (200, {"vdb_points": 4, "vdb_provider": "zvec"}))

    payload = mod._storage_status()

    assert payload["home"] == str(tmp_path)
    assert payload["files"]["zvec"]["available"] is True
    assert payload["coding"]["status"] == "not_configured"
    assert payload["runtime"]["backend"] == mod.HY_MEMORY_BASE
    assert payload["runtime"]["bind_port"] == mod.BIND_PORT


def test_frontend_fetch_rejects_http_errors_and_has_domain_results():
    text = APP.read_text(encoding="utf-8")

    assert "if (!resp.ok)" in text
    assert "error.status = resp.status" in text
    assert "async function fetchResult" in text
    assert "coreResult" in text
    assert "graphResult" in text
    assert "qualityResult" in text
    assert "function renderLoadErrors" in text
    assert "Showing the last known values" in text
    assert "runtime.backend" in text
    assert "http://127.0.0.1:19527</div>" not in text
