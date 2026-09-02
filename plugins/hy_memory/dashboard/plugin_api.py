"""Backend API for the HyAtlas v4 desktop pane.

Thin proxy: the desktop renderer cannot reach arbitrary localhost ports,
so the pane calls this namespace (`/api/plugins/hy_memory/...`) and this
module forwards to the HyAtlas v4 Go server at 127.0.0.1:19528.

Follows the Turbofit dashboard-plugin pattern (FastAPI APIRouter mounted
by the desktop/dashboard backend under the plugin's scoped namespace).
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import Any

from fastapi import APIRouter, HTTPException

router = APIRouter()

HYATLAS_HOST = os.environ.get("HYATLAS_HOST", "127.0.0.1")
HYATLAS_PORT = int(os.environ.get("HYATLAS_PORT", "19528"))
BASE = f"http://{HYATLAS_HOST}:{HYATLAS_PORT}"
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


@router.get("/metrics")
def metrics() -> Any:
    return _forward("GET", "/api/v1/metrics")
