from __future__ import annotations

import importlib.util
from pathlib import Path

PATH = (
    Path(__file__).parents[1] / "src" / "hyatlas_memory" / "server" / "dashboard" / "dashboard.py"
)


def load():
    spec = importlib.util.spec_from_file_location("hyatlas_dashboard_quality_test", PATH)
    mod = importlib.util.module_from_spec(spec)
    assert spec.loader
    spec.loader.exec_module(mod)
    return mod


def handler(mod):
    obj = object.__new__(mod.Handler)
    obj._load_quality_history = lambda: []
    obj._record_quality_snapshot = lambda snap: [snap]
    return obj


def test_quality_does_not_award_perfect_latency_without_samples(monkeypatch, tmp_path):
    mod = load()

    def fake_hy(method, path, body=None, timeout=10):
        if path.startswith("/api/v1/metrics"):
            return 200, {
                "requests": {"completed": 0},
                "sys2_requests": {"completed": 0},
                "avg_latency_ms": {},
                "llm_tokens": {},
            }
        if path == "/api/v1/status":
            return 200, {"vdb_points": 10}
        if path.startswith("/api/v1/graph"):
            return 200, {"layer_counts": {}, "relation_count": 0}
        if path == "/api/v1/list":
            return 200, {"vdb": {"memories": []}}
        raise AssertionError(path)

    monkeypatch.setattr(mod, "hy", fake_hy)
    monkeypatch.setattr(mod, "hy_home", lambda: tmp_path)

    snap = handler(mod)._build_quality_metrics()["snapshot"]

    assert snap["scores"]["latency"] is None
    assert snap["score_breakdown"]["latency"][0]["points"] is None
    assert snap["score_breakdown"]["latency"][0]["detail"] == "no samples"


def test_quality_uses_durable_activity_when_process_metrics_are_unavailable(monkeypatch, tmp_path):
    import time

    mod = load()

    def fake_hy(method, path, body=None, timeout=10):
        if path.startswith("/api/v1/metrics"):
            return 503, {"error": "metrics unavailable"}
        if path == "/api/v1/status":
            return 200, {"vdb_points": 10}
        if path.startswith("/api/v1/graph"):
            return 200, {"layer_counts": {}, "relation_count": 0}
        if path == "/api/v1/list":
            return 200, {"vdb": {"memories": [{"gmt_created": time.time()}]}}
        raise AssertionError(path)

    monkeypatch.setattr(mod, "hy", fake_hy)
    monkeypatch.setattr(mod, "hy_home", lambda: tmp_path)

    snap = handler(mod)._build_quality_metrics()["snapshot"]

    assert snap["scores"]["activity"] == 2
    assert snap["score_breakdown"]["activity"][0]["points"] == 2
    assert snap["evidence"]["metrics_7d"]["available"] is False
    assert snap["evidence"]["activity_7d"]["available"] is True
    assert "activity" in snap["score_coverage"]["available"]
    assert snap["evidence"]["scope"] == {"infrastructure": "global", "memory": "default"}


def test_quality_marks_old_digest_log_stale(monkeypatch, tmp_path):
    import os
    import time

    mod = load()

    def fake_hy(method, path, body=None, timeout=10):
        if path.startswith("/api/v1/metrics"):
            return 200, {"requests": {}, "sys2_requests": {}, "avg_latency_ms": {}, "llm_tokens": {}}
        if path == "/api/v1/status":
            return 200, {"vdb_points": 10}
        if path.startswith("/api/v1/graph"):
            return 200, {"layer_counts": {}, "relation_count": 0}
        if path == "/api/v1/list":
            return 200, {"vdb": {"memories": []}}
        raise AssertionError(path)

    log = tmp_path / "logs" / "digest_run_latest.log"
    log.parent.mkdir(parents=True)
    log.write_text("HTTP 200\nAFTER done", encoding="utf-8")
    old = time.time() - 9 * 86400
    os.utime(log, (old, old))
    monkeypatch.setattr(mod, "hy", fake_hy)
    monkeypatch.setattr(mod, "hy_home", lambda: tmp_path)

    snap = handler(mod)._build_quality_metrics()["snapshot"]

    assert snap["digest_log_status"] == "stale"
    assert snap["digest_log_age_hours"] >= 216
    assert snap["evidence"]["digest_log"]["source"] == str(log)


def test_quality_glance_does_not_coach_positively_on_stale_evidence(monkeypatch, tmp_path):
    import os
    import time

    mod = load()

    def fake_hy(method, path, body=None, timeout=10):
        if path.startswith("/api/v1/metrics"):
            return 503, {"error": "metrics unavailable"}
        if path == "/api/v1/status":
            return 200, {"vdb_points": 10}
        if path.startswith("/api/v1/graph"):
            return 200, {"layer_counts": {"l6_schema": 100}, "relation_count": 1000}
        if path == "/api/v1/list":
            return 200, {"vdb": {"memories": []}}
        raise AssertionError(path)

    log = tmp_path / "logs" / "digest_run_latest.log"
    log.parent.mkdir(parents=True)
    log.write_text("HTTP 200\nAFTER done", encoding="utf-8")
    old = time.time() - 9 * 86400
    os.utime(log, (old, old))
    monkeypatch.setattr(mod, "hy", fake_hy)
    monkeypatch.setattr(mod, "hy_home", lambda: tmp_path)

    result = handler(mod)._build_quality_metrics()

    assert result["at_a_glance"]["grade"] == "N/A"
    assert result["at_a_glance"]["tone"] == "caution"
    assert "stale" in result["at_a_glance"]["headline"].lower()
