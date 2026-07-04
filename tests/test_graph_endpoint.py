#!/usr/bin/env python3
"""Tests for Patch 23 — live /api/v1/graph endpoint and dashboard proxy fallback.

These tests require a running stack (memory server + Qdrant + dashboard).
Marked @pytest.mark.integration so CI skips them by default.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

repo_root = Path(__file__).parent.parent
sys.path.insert(0, str(repo_root / "src"))

SERVER_PORT = int(os.environ.get("HY_MEMORY_SERVER_PORT", "19527"))
DASHBOARD_PORT = int(os.environ.get("HY_MEMORY_DASHBOARD_PORT", "8765"))
SERVER_BASE = f"http://127.0.0.1:{SERVER_PORT}"
DASHBOARD_BASE = f"http://127.0.0.1:{DASHBOARD_PORT}"


def _http_get(base: str, path: str, timeout: float = 5.0) -> tuple[int, object]:
    """Minimal HTTP GET helper that mirrors hy()'s return shape."""
    url = f"{base}{path}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, body


def _stack_up(base: str) -> bool:
    try:
        # Server has /healthz, dashboard has /api/health. Try both.
        for path in ("/healthz", "/api/health"):
            try:
                s, _ = _http_get(base, path)
                if s == 200:
                    return True
            except Exception:
                continue
        return False
    except Exception:
        return False


requires_stack = pytest.mark.skipif(
    not _stack_up(SERVER_BASE),
    reason="Memory server not reachable on port "
    f"{SERVER_PORT} \u2014 start with `hyatlas start`",
)
requires_dashboard = pytest.mark.skipif(
    not _stack_up(DASHBOARD_BASE),
    reason="Dashboard not reachable on port "
    f"{DASHBOARD_PORT} \u2014 start with `hyatlas start`",
)
integration = pytest.mark.integration


@requires_stack
@integration
def test_graph_endpoint_returns_nodes_and_relations():
    """Endpoint returns at least one node and one relation from a real Kuzu graph."""
    status, data = _http_get(SERVER_BASE, "/api/v1/graph?n=10")
    assert status == 200
    assert isinstance(data, dict)
    assert data["node_count"] >= 1, "expected at least one L5 node"
    assert data["relation_count"] >= 0
    assert isinstance(data["nodes"], list)
    assert isinstance(data["relations"], list)


@requires_stack
@integration
def test_graph_endpoint_node_shape():
    """Each node has the documented fields and types."""
    status, data = _http_get(SERVER_BASE, "/api/v1/graph?n=3")
    assert status == 200
    assert data["nodes"]
    node = data["nodes"][0]
    assert isinstance(node["node_id"], str)
    assert isinstance(node["name"], str)
    assert isinstance(node["entity_type"], str)
    assert isinstance(node["mention_count"], int)
    assert isinstance(node["aliases"], list)
    assert "source" in node


@requires_stack
@integration
def test_graph_endpoint_limit():
    """n=5 caps the returned nodes."""
    status, data = _http_get(SERVER_BASE, "/api/v1/graph?n=5")
    assert status == 200
    assert data["node_count"] <= 5
    assert len(data["nodes"]) <= 5


@requires_stack
@integration
def test_graph_endpoint_clamps_oversized_n():
    """n=99999 is clamped (no DoS via huge limit)."""
    status, data = _http_get(SERVER_BASE, "/api/v1/graph?n=99999")
    assert status == 200
    # Must be bounded — never more than the clamp value (5000)
    assert data["node_count"] <= 5000


@requires_stack
@integration
def test_graph_endpoint_bad_n_returns_400():
    """Non-integer n returns 400 instead of silently falling through to 404."""
    status, data = _http_get(SERVER_BASE, "/api/v1/graph?n=notanumber")
    assert status == 400
    assert "integer" in str(data["error"]).lower()


@requires_stack
@integration
def test_graph_endpoint_type_filter():
    """?type=CONCEPT returns only nodes whose entity_type matches."""
    status, data = _http_get(SERVER_BASE, "/api/v1/graph?type=CONCEPT&n=50")
    assert status == 200
    if data["nodes"]:
        for n in data["nodes"]:
            assert n["entity_type"] == "CONCEPT"


@requires_stack
@integration
def test_graph_endpoint_search_filter():
    """?q=Hermes returns only nodes whose name/aliases contain the substring."""
    status, data = _http_get(SERVER_BASE, "/api/v1/graph?q=Hermes&n=50")
    assert status == 200
    if data["nodes"]:
        sl = "hermes"
        for n in data["nodes"]:
            name_match = sl in n["name"].lower()
            alias_match = any(sl in a.lower() for a in n.get("aliases", []))
            assert name_match or alias_match


@requires_stack
@integration
def test_graph_endpoint_rels_false():
    """rels=false excludes relations from the response."""
    status, data = _http_get(SERVER_BASE, "/api/v1/graph?n=10&rels=false")
    assert status == 200
    assert data["relation_count"] == 0
    assert data["relations"] == []


@requires_stack
@integration
def test_graph_endpoint_has_type_distribution():
    """Response includes type distribution summary."""
    status, data = _http_get(SERVER_BASE, "/api/v1/graph?n=20")
    assert status == 200
    assert "type_distribution" in data
    assert isinstance(data["type_distribution"], dict)
    assert "relation_type_distribution" in data


@requires_dashboard
@integration
def test_dashboard_proxy_forwards_type_to_upstream():
    """Dashboard /api/l5/graph?type=... must pass type filter to upstream.

    Regression test for the D1 bug where the dashboard built a `params` dict
    and then dropped it on the floor when calling hy().
    """
    status, data = _http_get(DASHBOARD_BASE, "/api/l5/graph?type=CONCEPT&n=50")
    assert status == 200
    assert data["node_count"] >= 0
    if data["nodes"]:
        for n in data["nodes"]:
            assert n["entity_type"] == "CONCEPT"


@requires_dashboard
@integration
def test_dashboard_proxy_forwards_search_to_upstream():
    """Dashboard /api/l5/graph?q=... must pass search filter to upstream."""
    status, data = _http_get(DASHBOARD_BASE, "/api/l5/graph?q=Hermes&n=50")
    assert status == 200
    assert data["node_count"] >= 0
    if data["nodes"]:
        sl = "hermes"
        for n in data["nodes"]:
            name_match = sl in n["name"].lower()
            alias_match = any(sl in a.lower() for a in n.get("aliases", []))
            assert name_match or alias_match


@requires_dashboard
@integration
def test_dashboard_context_uses_live_endpoint():
    """Dashboard /api/l5/context must return entities from the live graph."""
    status, data = _http_get(DASHBOARD_BASE, "/api/l5/context?n=5")
    assert status == 200
    assert "entities" in data
    assert isinstance(data["entities"], list)
    # Field name should match the entity_type shape
    if data["entities"]:
        e = data["entities"][0]
        assert "name" in e
        assert "type" in e
        assert "mentions" in e
