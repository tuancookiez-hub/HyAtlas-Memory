"""Backend API for the HyAtlas v4 desktop pane.

Thin proxy: the desktop renderer cannot reach arbitrary localhost ports,
so the pane calls this namespace (`/api/plugins/hyatlas/...`) and this
module forwards to the HyAtlas v4 Go server at 127.0.0.1:19528.

Follows the Turbofit dashboard-plugin pattern (FastAPI APIRouter mounted
by the desktop/dashboard backend under the plugin's scoped namespace).
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException

router = APIRouter()


def _settings() -> dict[str, Any]:
    """Load the plugin's settings module, resolving the sibling by file path.

    The dashboard backend imports this file with
    ``importlib.util.spec_from_file_location`` and no parent package, so a
    relative import is not available here. Loading ``settings.py`` the same way
    — by path, from this file's own directory — reaches the same module the
    provider uses without touching ``sys.path``, which would leak the plugin
    directory into every other module's import search.

    The provider and this dashboard must agree on the server's host and port, so
    both read it through one module rather than each keeping its own defaults.
    """
    path = Path(__file__).resolve().parent.parent / "settings.py"
    if not path.is_file():
        return {}
    name = "hyatlas_dashboard_settings"
    if name in sys.modules:
        return sys.modules[name].__dict__
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        return {}
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:
        sys.modules.pop(name, None)
        return {}
    return mod.__dict__


def _origin() -> str:
    """Server origin from the plugin's real settings, falling back to loopback.

    ``HYATLAS_HOST`` / ``HYATLAS_PORT`` are kept as an explicit escape hatch for
    pointing the dashboard at a server the plugin is not configured to use, but
    the defaults now come from the same settings the provider reads rather than
    being hardcoded here.
    """
    cfg = _settings()
    load = cfg.get("load")
    resolved = load() if callable(load) else {}
    host = (os.environ.get("HYATLAS_HOST") or "").strip() or resolved.get("server_host") or "127.0.0.1"
    port = (os.environ.get("HYATLAS_PORT") or "").strip() or resolved.get("server_port") or 19528
    return f"http://{host}:{port}"


BASE = _origin()
TIMEOUT = 15.0


def _forward(method: str, path: str, body: dict[str, Any] | None = None) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=data,
        method=method,
        headers={"Content-Type": "application/json"} if body else {},
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:200]
        raise HTTPException(status_code=exc.code, detail=detail) from None
    except (urllib.error.URLError, OSError) as exc:
        raise HTTPException(
            status_code=503,
            detail=f"HyAtlas v4 unreachable at {BASE}: {exc}",
        ) from None
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw.decode(errors="replace")}


@router.get("/status")
def status() -> Any:
    """Health + layer counts + graph sizes from the v4 server."""
    return _forward("GET", "/api/v1/status")


@router.get("/healthz")
def healthz() -> Any:
    return _forward("GET", "/healthz")


@router.get("/memories")
def memories(limit: int = 30, layer: str = "", include_raw: bool = False) -> Any:
    """Recent memories (list endpoint), optionally filtered by layer.

    user_id is optional: when omitted the v4 list endpoint returns rows
    across every user scope, which is what an at-a-glance pane wants.
    """
    body: dict[str, Any] = {
        "limit": max(1, min(limit, 200)),
        "include_raw": include_raw,
    }
    if layer:
        body["layer"] = layer
    return _forward("POST", "/api/v1/list", body)


@router.get("/search")
def search(q: str, limit: int = 10, layer: str = "") -> Any:
    body: dict[str, Any] = {
        "query": q,
        "limit": max(1, min(limit, 50)),
    }
    if layer:
        body["layer"] = layer
    return _forward("POST", "/api/v1/search", body)


@router.post("/add")
def add(payload: dict[str, Any]) -> Any:
    text = (payload.get("text") or "").strip()
    if not text:
        raise HTTPException(status_code=400, detail="text required")
    body = {
        "text": text,
        "user_id": payload.get("user_id") or "default",
        "agent_id": payload.get("agent_id") or "default",
        "session_id": payload.get("session_id") or "",
    }
    return _forward("POST", "/api/v1/add", body)


@router.get("/graph")
def graph() -> Any:
    """L5 knowledge graph counts (nodes/edges). Full snapshot stays on the Go dashboard."""
    return _forward("GET", "/api/v1/graph")


@router.get("/learning/graph")
def learning_graph(n: int = 500, k_semantic: int = 2) -> Any:
    """StarmapGraph-shape payload for the hyatlas plugin's Graph tab.

    Matches the Hermes Desktop built-in Memory Graph shape (nodes with
    timestamp/category/label, edges with source/target/type), so the same
    polar-radial canvas renders the entire memory system (all 7 layers).

    Edge types in the response:
      - knowledge:  L5 explicit graph triples
      - co_session: memories sharing a session_id
      - semantic:   top-K VDB nearest neighbors
    """
    return _forward("GET", f"/api/v1/learning/graph?n={n}&k_semantic={k_semantic}")


@router.get("/metrics")
def metrics() -> Any:
    return _forward("GET", "/api/v1/metrics")
