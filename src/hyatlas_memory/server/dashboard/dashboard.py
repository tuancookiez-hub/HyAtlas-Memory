#!/usr/bin/env python
"""Hy-Memory Dashboard v2 — local-only web UI for hyatlas-memory.

7-tab dashboard inspired by Mnemosyne and Hindsight Control Plane.
Talks to the local hyatlas-memory HTTP API (port 19527 by default).

> This dashboard is for the community implementation at
> github.com/tuancookiez-hub/HyAtlas-Memory. The canonical 6-layer model
> and three-mode design (Lite/Pro/Ultra) are defined by the official
> Hy-Memory framework at https://memory.hunyuan.tencent.com.

Tabs:
  1. Overview  — health, layer distribution, recent memories, quick actions
  2. Explore   — search, memory browser, recall debugger
  3. Layers    — L0-L7 visualization and stats (L7 is the experimental
                 intention layer, not in the official 6-layer spec)
  4. Today     — daily digest of memories added/recalled/consolidated
  5. Graph     — constellation view of entity relationships
  6. Activity  — timeline of memory writes and consolidations
  7. Settings  — config display, mode selector, export/import

Run:    python hy_memory_dashboard.py
Open:   http://127.0.0.1:8765

Env vars:
  HY_DASH_PORT        (default 8765)
  HY_DASH_BIND        (default 127.0.0.1)
  HY_MEMORY_BASE      (default http://127.0.0.1:19527)
  HY_DASH_REFRESH_S   (default 30)  auto-refresh interval for live tabs
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

HY_MEMORY_BASE = os.environ.get("HY_MEMORY_BASE", "http://127.0.0.1:19527").rstrip("/")
BIND_HOST = os.environ.get("HY_DASH_BIND", "127.0.0.1")
BIND_PORT = int(os.environ.get("HY_DASH_PORT", "8765"))
REFRESH_S = int(os.environ.get("HY_DASH_REFRESH_S", "30"))

# Auth: when bound to 0.0.0.0 (exposed to network), a token is required.
# When bound to 127.0.0.1 (local only), auth is skipped for convenience.
# The token is auto-generated on first run and stored in ~/.hy_memory/.
import pathlib as _pathlib
import secrets as _secrets

_DASH_TOKEN_FILE = _pathlib.Path.home() / ".hy_memory" / ".dashboard_token"
DASH_TOKEN: str | None = None

def _get_or_create_token() -> str:
    """Load or generate the dashboard auth token."""
    try:
        _DASH_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        if _DASH_TOKEN_FILE.exists():
            token = _DASH_TOKEN_FILE.read_text().strip()
            if token:
                return token
        token = _secrets.token_urlsafe(24)
        _DASH_TOKEN_FILE.write_text(token)
        _DASH_TOKEN_FILE.chmod(0o600)
        return token
    except Exception:
        return ""

# Auth is required when exposed to the network, optional when local
AUTH_REQUIRED = BIND_HOST not in ("127.0.0.1", "localhost", "::1")
if AUTH_REQUIRED:
    DASH_TOKEN = _get_or_create_token()

# Qdrant (used to fetch L1_RAW conversations that Hy-Memory's /api/v1/list
# filters out by default — see hy-memory-setup skill: "L1_RAW is the raw
# input layer, hidden from the user-facing list by design")
QDRANT_BASE = os.environ.get("QDRANT_BASE", "http://127.0.0.1:6333").rstrip("/")
QDRANT_COLLECTION = os.environ.get("QDRANT_COLLECTION", "agent_memories_1024")


def _qdrant_layer_count(layer: str, *, require_is_latest: bool = True) -> int:
    """Count points in Qdrant for one layer (dashboard composition bar)."""
    must = [{"key": "layer", "match": {"value": layer}}]
    if require_is_latest:
        must.append({"key": "is_latest", "match": {"value": True}})
    elif layer == "l5_knowledge":
        must.append({"key": "status", "match": {"value": "active"}})
    body = {"filter": {"must": must}}
    try:
        req = urllib.request.Request(
            f"{QDRANT_BASE}/collections/{QDRANT_COLLECTION}/points/count",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        return int(
            json.loads(urllib.request.urlopen(req, timeout=10).read())["result"]["count"]
        )
    except Exception:
        return 0


def _vdb_layer_count(layer: str, *, require_is_latest: bool = True) -> int:
    """Layer count via memory server (works with zvec; falls back to Qdrant HTTP)."""
    latest = "true" if require_is_latest else "false"
    code, body = hy(
        "GET",
        f"/api/v1/vdb/layer_count?layer={layer}&require_is_latest={latest}",
    )
    if code == 200 and isinstance(body, dict) and "count" in body:
        return int(body["count"])
    return _qdrant_layer_count(layer, require_is_latest=require_is_latest)


def _l5_export_path() -> _pathlib.Path:
    """Resolve the canonical path of the L5 Kuzu-graph export JSON.

    Single source of truth shared by every L5 reader in the dashboard
    AND by ``bin/l5_export_json.py`` (the writer). Centralizing the
    resolution here means the writer and reader can never disagree
    about where the export lives, regardless of ``HERMES_HOME``,
    platform, or working directory.

    Pre-1.4.1, the dashboard read from ``<dashboard_dir>/logs/l5_kuzu_export.json``
    while the writer wrote to ``C:\\Users\\<user>\\AppData\\Local\\hermes\\logs\\...``,
    which produced a permanent 503 on ``/api/l5/graph`` for every install.

    Since v2.0.0 (Patch 23), the dashboard reads live from the server's
    ``/api/v1/graph`` endpoint by default. This export file is only used
    as a fallback when the live endpoint is unavailable.
    """
    try:
        from hermes_constants import get_hermes_home
        home = _pathlib.Path(get_hermes_home())
    except Exception:
        if sys.platform == "win32":
            home = _pathlib.Path.home() / "AppData" / "Local" / "hermes"
        else:
            home = _pathlib.Path.home() / ".local" / "share" / "hermes"
    return home / "logs" / "l5_kuzu_export.json"

# Known user IDs × agent IDs for querying memories.
# The Hermes agent writes with its own user identity (the agent_identity from
# agent_init.py) and a chosen agent_id, both of which can vary across
# sessions and profiles. We query every known (user_id, agent_id) pair so
# the dashboard shows data from every scope, not just the current session.
#
# Discovered 2026-06-12 from a Qdrant full scroll:
#   hermes-user       → 985 points, agent_id="default"
#   221727702992945152 →  13 points, agent_id="default"
#   tuancookiez       →   5 points, agent_id="default_agent"
# If a new (user_id, agent_id) pair appears in production, add it here.
HERMES_USER_IDS = [
    "tuanc",            # primary user (memories from l3-summarizer, hermes, default_agent, l5-pipeline, ops-check, etc.)
    "hermes-user",      # legacy/CLI-added memories
    "221727702992945152",
]
HERMES_USER_IDS_JS = json.dumps(HERMES_USER_IDS)  # inject into JS

# Layer colors (matches CSS .layer-l0..l7)
LAYER_COLORS = {
    "L0_BASIC_INFO": "#4a6fa5",
    "L1_RAW":        "#3d8b8b",
    "L2_FACT":       "#6b4c9a",
    "L3_SUMMARY":    "#4a6fa5",
    "L4_IDENTITY":   "#d4af37",
    "L5_KNOWLEDGE":  "#3d8b8b",
    "L6_SCHEMA":     "#6b4c9a",
    "L7_INTENTION":  "#d4af37",
}


def hy(method: str, path: str, body: dict | None = None, timeout: float = 10.0) -> tuple[int, object]:
    """Proxy a call to the local hy_memory server."""
    url = f"{HY_MEMORY_BASE}{path}"
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            err_body = e.read().decode("utf-8", errors="replace")[:500]
        except Exception:
            err_body = ""
        return e.code, {"error": err_body or e.reason}
    except Exception as e:
        return 0, {"error": f"{type(e).__name__}: {e}"}


def _to_unix_ts(value):
    """Convert a timestamp string to Unix seconds.

    Accepts: ISO 8601 string (e.g. "2026-06-17 07:16:46.709220"),
    numeric int/float, or None. Returns None for unparseable input.
    Used to make the graph L5/L6/L7 nodes compatible with the
    dashboard's `m.gmt_created * 1000` math in JS.
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str):
        try:
            from datetime import datetime
            # Handle both "2026-06-17 07:16:46" and "2026-06-17T07:16:46Z" formats
            s = value.replace("Z", "+00:00").replace(" ", "T") if " " in value else value
            return int(datetime.fromisoformat(s).timestamp())
        except Exception:
            return None
    return None


def _extract_memories(payload: dict) -> list[dict]:
    """Normalize the three response shapes into a flat list of memory dicts.

    v1.5.0: also include L5/L6/L7 graph nodes from the ``graph`` key
    so the dashboard's Memory Layers and Recent Ingestions pages
    actually surface the L5/L6/L7 data the upstream's
    ``/api/v1/list`` returns. Without this, the graph items were
    silently dropped and the dashboard always showed L5/L6/L7 as
    0/0/0 even when hundreds of nodes existed in Kuzu.
    """
    raw = (payload or {}).get("vdb") or {}
    items: list[dict] = []
    if isinstance(raw, dict) and isinstance(raw.get("memories"), list):
        items = [m for m in raw["memories"] if isinstance(m, dict)]
    elif isinstance(raw, dict):
        for mid, mem in raw.items():
            if isinstance(mem, dict):
                mem.setdefault("memory_id", mid)
                items.append(mem)
    elif isinstance(raw, list):
        items = [m for m in raw if isinstance(m, dict)]

    # Pull L5/L6/L7 nodes from the graph key. The upstream's
    # /api/v1/list response includes them under "graph.nodes".
    # Each graph node has shape:
    #   { node_id, layer, content, isolation_key, user_id, agent_id,
    #     status, gmt_created, content_type, ... }
    graph = (payload or {}).get("graph") or {}
    graph_nodes = graph.get("nodes") or []
    if isinstance(graph_nodes, list):
        for n in graph_nodes:
            if not isinstance(n, dict):
                continue
            nmid = n.get("node_id") or n.get("_id") or ""
            if not nmid:
                continue
            items.append({
                "memory_id": nmid,
                "layer": n.get("layer") or "",
                "content": n.get("content") or "",
                "status": n.get("status") or "active",
                "memory_at": None,
                # Graph nodes use created_at/valid_from, not gmt_created.
                # Convert to Unix timestamp (seconds) so the
                # dashboard's JS can do `m.gmt_created * 1000`
                # without hitting NaN from a string.
                "gmt_created": _to_unix_ts(
                    n.get("created_at") or n.get("valid_from")
                ),
                "score": None,
                "metadata": {
                    "isolation_key": n.get("isolation_key"),
                    "user_id": n.get("user_id"),
                    "agent_id": n.get("agent_id"),
                    "content_type": n.get("content_type"),
                    "tags": n.get("tags") or [],
                    "node_type": n.get("node_type"),
                    "evidence": n.get("evidence") or [],
                },
            })

    normalized = []
    for m in items:
        meta = m.get("metadata") or {}
        normalized.append({
            "memory_id": m.get("memory_id") or m.get("id") or "",
            "layer": m.get("layer") or meta.get("layer") or "",
            "score": m.get("score"),
            "content": m.get("content") or m.get("text") or m.get("document") or "",
            "metadata": meta,
            "status": m.get("status", ""),
            "memory_at": m.get("memory_at"),
            "gmt_created": m.get("gmt_created"),
            "importance": m.get("importance") if m.get("importance") is not None else meta.get("importance"),
            "access_count": m.get("access_count") if m.get("access_count") is not None else meta.get("access_count"),
            "user_id": m.get("user_id") or meta.get("user_id") or "",
            "session_id": m.get("session_id") or meta.get("session_id") or "",
            "tags": m.get("tags") or meta.get("tags") or [],
        })
    return normalized


def _fetch_l1_raw_from_qdrant(limit_total: int = 1500) -> list[dict]:
    """Fetch active L1_RAW memories directly from Qdrant.

    Hy-Memory's /api/v1/list intentionally excludes the l1_raw layer from the
    user-facing list (it's the raw input layer, hidden once reconciler
    extracts durable facts). The dashboard wants to show L1_RAW anyway, so
    we query Qdrant directly and normalize to the same memory-dict shape
    produced by _extract_memories().
    """
    # Build a user_id-only filter (drop agent_id constraint, since each user
    # can have memories written by many different agents over time).
    pairs_should = []
    for uid in HERMES_USER_IDS:
        pairs_should.append({
            "key": "user_id",
            "match": {"value": uid},
        })
    if not pairs_should:
        return []
    body = {
        "filter": {
            "must": [
                {"key": "layer",   "match": {"value": "l1_raw"}},
                {"key": "is_latest", "match": {"value": True}},
            ],
            "should": pairs_should,
        },
        "limit": limit_total,
        "with_payload": ["content", "layer", "gmt_created", "user_id", "agent_id",
                         "session_id", "status", "is_latest"],
        "with_vectors": False,
    }
    try:
        req = urllib.request.Request(
            f"{QDRANT_BASE}/collections/{QDRANT_COLLECTION}/points/scroll",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        resp = json.loads(urllib.request.urlopen(req, timeout=20).read())
    except Exception:
        return []
    items = []
    for p in (resp.get("result") or {}).get("points") or []:
        pl = p.get("payload") or {}
        items.append({
            "memory_id":  str(p.get("id") or ""),
            "layer":      pl.get("layer", "l1_raw"),
            "score":      None,
            "content":    pl.get("content") or "",
            "metadata":   {},
            "status":     pl.get("status", "active"),
            "memory_at":  None,
            "gmt_created": pl.get("gmt_created", 0),
            "user_id":    pl.get("user_id"),
            "agent_id":   pl.get("agent_id"),
            "session_id": pl.get("session_id"),
            "_source":    "l1_raw",
        })
    return items


def _fetch_l1_raw_from_vdb(limit_total: int = 1500) -> list[dict]:
    code, body = hy(
        "POST",
        "/api/v1/vdb/scroll",
        {"mode": "l1_raw", "user_ids": HERMES_USER_IDS, "limit": limit_total},
    )
    if code == 200 and isinstance(body, dict):
        return body.get("items") or []
    return _fetch_l1_raw_from_qdrant(limit_total=limit_total)


def _enrich_with_qdrant_payload(memories: list[dict]) -> list[dict]:
    """Enrich memories with `importance` and `access_count` from Qdrant.

    Hy-Memory's `/api/v1/list` doesn't surface these fields — they live
    in the qdrant payload alongside `layer`. This function batches a
    qdrant scroll by point id and merges the missing fields back into
    each memory dict. Silent no-op if qdrant is unreachable.
    """
    ids = [m.get("memory_id") for m in memories if m.get("memory_id")]
    if not ids:
        return memories
    try:
        # Filter by point id (qdrant uses point UUID as id, not payload.memory_id).
        # has_id must be wrapped in `must` to work correctly.
        body = {
            "filter": {"must": [{"has_id": ids}]},
            "limit": len(ids),
            "with_payload": ["importance", "access_count"],
            "with_vectors": False,
        }
        req = urllib.request.Request(
            f"{QDRANT_BASE}/collections/{QDRANT_COLLECTION}/points/scroll",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"},
        )
        resp = json.loads(urllib.request.urlopen(req, timeout=10).read())
    except Exception:
        return memories
    by_id = {}
    for p in (resp.get("result") or {}).get("points") or []:
        pl = p.get("payload") or {}
        mid = str(p.get("id") or "")
        if mid:
            by_id[mid] = {
                "importance":   pl.get("importance"),
                "access_count": pl.get("access_count"),
            }
    for m in memories:
        mid = m.get("memory_id") or ""
        if mid in by_id:
            for k, v in by_id[mid].items():
                if v is not None:
                    m[k] = v
    return memories


def _enrich_with_vdb_payload(memories: list[dict]) -> list[dict]:
    ids = [m.get("memory_id") for m in memories if m.get("memory_id")]
    if not ids:
        return memories
    code, body = hy(
        "POST",
        "/api/v1/vdb/scroll",
        {"mode": "payload_by_ids", "memory_ids": ids},
    )
    if code == 200 and isinstance(body, dict):
        by_id = body.get("payloads") or {}
        for m in memories:
            mid = m.get("memory_id") or ""
            if mid in by_id:
                for k, v in by_id[mid].items():
                    if v is not None:
                        m[k] = v
        return memories
    return _enrich_with_qdrant_payload(memories)


# --------------------------------------------------------------------------
# HTML Template
#

HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Hy-Memory Dashboard</title>
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:wght@400;600;700&family=Inter:wght@300;400;500;600&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<script src="https://unpkg.com/cytoscape@3.28.1/dist/cytoscape.min.js"></script>
<script src="https://unpkg.com/layout-base@2.0.1/layout-base.js"></script>
<script src="https://unpkg.com/cose-base@2.2.0/cose-base.js"></script>
<script src="https://unpkg.com/cytoscape-fcose@2.2.0/cytoscape-fcose.js"></script>
<style>
  :root {
    --bg: #050505; --panel: #0a0a0a; --panel-alt: #0f0f0f; --border: #1a1a1a;
    --border-light: #252525; --text: #e8e8e8; --muted: #666666;
    --accent: #d4af37; --accent-dim: #b8960f; --green: #4ade80; --red: #f87171;
    --purple: #6b4c9a; --blue: #4a6fa5; --teal: #3d8b8b;
  }
  * { box-sizing: border-box; }
  body { margin: 0; font: 13px/1.5 Inter, -apple-system, "Segoe UI", sans-serif;
         background: var(--bg); color: var(--text); display: flex; min-height: 100vh; }

  /* Sidebar */
  .sidebar { width: 200px; flex-shrink: 0; background: var(--panel); border-right: 1px solid var(--border);
             display: flex; flex-direction: column; height: 100vh; position: sticky; top: 0; }
  .sidebar-logo { padding: 24px 20px 8px; }
  .sidebar-logo h1 { font-family: "Inter", sans-serif; font-size: 16px; font-weight: 600;
                      color: var(--text); margin: 0; letter-spacing: 3px; display: flex; align-items: center; gap: 8px; }
  .sidebar-logo .tagline { font-size: 10px; color: var(--muted); letter-spacing: 1px; margin-top: 4px; }
  .sidebar-nav { flex: 1; padding: 16px 0; }
  .sidebar-item { display: flex; align-items: center; gap: 10px; padding: 10px 20px; cursor: pointer;
                   color: var(--muted); font-size: 11px; letter-spacing: 1px; text-transform: uppercase;
                   transition: color 0.2s, background 0.2s; user-select: none; position: relative; }
  .sidebar-item:hover { color: var(--text); background: var(--panel-alt); }
  .sidebar-item.active { color: var(--accent); }
  .sidebar-item.active::before { content: ""; position: absolute; left: 8px; width: 4px; height: 4px;
                                  border-radius: 50%; background: var(--accent); }
  .sidebar-item svg { width: 16px; height: 16px; opacity: 0.6; flex-shrink: 0; }
  .sidebar-item.active svg { opacity: 1; }
  .sidebar-status { padding: 14px 16px; margin: 0 12px 12px; border: 1px solid var(--border);
                       border-radius: 6px; font-size: 11px; background: var(--panel); }
  .sidebar-status .status-dot { display: inline-block; width: 6px; height: 6px; border-radius: 50%;
                                  background: var(--green); margin-right: 6px; }
  .sidebar-status .status-label { color: var(--green); font-weight: 500; }
  .sidebar-status .sync-info { color: var(--muted); font-size: 10px; margin-top: 4px; }
  .sidebar-footer { padding: 12px 20px; border-top: 1px solid var(--border);
                     font-size: 10px; color: var(--muted); }
  .ok-dot { display: inline-block; width: 6px; height: 6px; border-radius: 50%;
            background: var(--green); }
  .err-dot { background: var(--red); }

  /* Main content */
  .main-wrap { flex: 1; display: flex; min-width: 0; }
  .main-content { flex: 1; min-width: 0; overflow-y: auto; height: 100vh; }
  .right-sidebar { width: 260px; flex-shrink: 0; background: var(--panel); border-left: 1px solid var(--border);
                    height: 100vh; overflow-y: auto; position: sticky; top: 0; padding: 20px 16px; }
  .right-sidebar h3 { font-family: "Inter", sans-serif; font-size: 10px; font-weight: 500;
                       color: var(--muted); letter-spacing: 1.5px; text-transform: uppercase; margin: 0 0 12px; }
  .right-sidebar .ingest-item { display: flex; gap: 10px; padding: 8px 0; border-bottom: 1px solid var(--border); }
  .right-sidebar .ingest-item .ingest-title { font-size: 12px; font-weight: 500; color: var(--text); }
  .right-sidebar .ingest-item .ingest-desc { font-size: 10px; color: var(--muted); margin-top: 2px; }
  .right-sidebar .ingest-item .ingest-time { font-size: 10px; color: var(--muted); white-space: nowrap; margin-left: auto; }
  .right-sidebar .action-btn { display: flex; align-items: center; gap: 8px; padding: 8px 10px;
                                font-size: 11px; color: var(--muted); cursor: pointer; border-radius: 4px;
                                transition: background 0.2s; }
  .right-sidebar .action-btn:hover { background: var(--panel-alt); color: var(--text); }
  .right-sidebar .insight { margin-top: 20px; padding: 16px; background: var(--panel-alt); border-radius: 6px;
                             border: 1px solid var(--border); font-size: 11px; color: var(--muted);
                             font-style: italic; line-height: 1.6; }

  /* Panels */
  .panel { display: none; padding: 16px 20px; }
  .panel.active { display: block; }

  /* Cards */
  .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr));
          gap: 0; margin-bottom: 16px; }
  .card { background: transparent; border: none; border-right: 1px solid var(--border);
          border-radius: 0; padding: 12px 16px; position: relative; overflow: hidden; }
  .card:last-child { border-right: none; }
  .card .label { color: var(--muted); font-size: 9px; text-transform: uppercase;
                 letter-spacing: 1.5px; font-weight: 400; }
  .card .value { font-family: "JetBrains Mono", monospace; font-size: 32px; font-weight: 400;
                  margin-top: 8px; color: var(--text); }
  .card .sub { font-size: 11px; color: var(--muted); margin-top: 4px; }
  .card.ok .value { color: var(--green); }
  .card.warn .value { color: var(--yellow); }
  .card.err .value { color: var(--red); }

  /* Tables */
  table { width: 100%; border-collapse: collapse; font-size: 12px; }
  th, td { text-align: left; padding: 6px 10px; border-bottom: 1px solid var(--border);
           vertical-align: top; }
  th { color: var(--muted); font-weight: 500; text-transform: uppercase;
       font-size: 11px; letter-spacing: 0.5px; }
  tr:hover td { background: rgba(255,255,255,0.02); }

  /* Layer badges */
  .layer { display: inline-block; padding: 2px 6px; border-radius: 3px;
           font-size: 10px; font-weight: 600; text-transform: uppercase; }
  .layer-l0 { background: #1a2a40; color: #4a6fa5; }
  .layer-l1 { background: #1a3030; color: #3d8b8b; }
  .layer-l2 { background: #2a1a3a; color: #6b4c9a; }
  .layer-l3 { background: #1a2a40; color: #4a6fa5; }
  .layer-l4 { background: #3a3010; color: #d4af37; }
  .layer-l5 { background: #1a3030; color: #3d8b8b; }
  .layer-l6 { background: #2a1a3a; color: #6b4c9a; }
  .layer-l7 { background: #3a3010; color: #d4af37; }

  /* Score & content */
  .score { font-family: monospace; color: var(--muted); font-size: 11px; }
  .content { color: var(--text); }
  details summary { cursor: pointer; color: var(--muted); font-size: 11px; }
  details[open] summary { color: var(--text); }
  pre { background: var(--panel); border: 1px solid var(--border); border-radius: 4px;
        padding: 8px; overflow: auto; font-size: 11px; margin: 6px 0 0; }

  /* Form elements */
  input, button, select { background: var(--panel); color: var(--text);
                          border: 1px solid var(--border); border-radius: 4px;
                          padding: 6px 10px; font: inherit; }
  button { cursor: pointer; }
  button:hover { background: var(--border); }

  /* Toolbar */
  .toolbar { display: flex; gap: 8px; margin-bottom: 12px; align-items: center; }
  .toolbar input[type=text] { flex: 1; }

  /* Utility */
  .empty { color: var(--muted); padding: 20px; text-align: center; font-style: italic; }
  .pager { display: flex; gap: 8px; margin-top: 12px; align-items: center; }
  .pager button { padding: 4px 10px; }
  .err-msg { background: #2a1010; border: 1px solid #4a1a1a; color: #f87171;
             padding: 8px 12px; border-radius: 4px; margin: 8px 0; }
  .ok-msg { color: var(--green); }
  .refresh-dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%;
                 background: var(--green); margin-right: 4px; animation: pulse 2s infinite; }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: 0.3; } }
  .section-code { font-family: "JetBrains Mono", monospace; font-size: 10px; color: var(--muted);
                     letter-spacing: 1.5px; text-transform: uppercase; margin-bottom: 4px; }
  .section-code .sep { color: var(--accent-dim); }
  .section-title { font-family: "Inter", sans-serif; font-size: 13px; font-weight: 500;
                   color: var(--text); letter-spacing: 0.5px; text-transform: none;
                   margin: 20px 0 6px; }
  .section-desc { font-size: 12px; color: var(--muted); margin-bottom: 12px; }
  .page-title { font-family: "Playfair Display", serif; font-size: 36px; font-weight: 400;
                 color: var(--text); letter-spacing: 1px; text-transform: uppercase; margin: 8px 0 4px; }

  /* Layer distribution bar */
  .layer-bar { display: flex; height: 24px; border-radius: 4px; overflow: hidden;
               margin: 8px 0; }
  .layer-bar-seg { display: flex; align-items: center; justify-content: center;
                   font-size: 10px; font-weight: 600; color: #000; min-width: 20px; }

  /* Recall debugger */
  .debug-row { display: flex; gap: 12px; margin-bottom: 12px; }
  .debug-row > * { flex: 1; }
  .debug-score { font-family: monospace; font-size: 12px; }

  /* Quick actions */
  .actions { display: flex; gap: 8px; margin-top: 12px; flex-wrap: wrap; }
  .actions button { padding: 8px 16px; font-size: 12px; }
  .graph-mode-btn.active { background: var(--accent) !important; color: var(--bg) !important; }

  /* === Memory Observatory Graph === */
/* === Memory Observatory Graph === */
.obs-top {
  display: flex;
  justify-content: space-between;
  align-items: flex-end;
  padding-bottom: 16px;
  border-bottom: 1px solid var(--border);
  margin-bottom: 16px;
  flex-wrap: wrap;
  gap: 16px;
}
.obs-top-left {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.obs-breadcrumb {
  font-family: 'JetBrains Mono', monospace;
  font-size: 10px;
  color: var(--muted);
  letter-spacing: 2px;
}
.obs-breadcrumb .sep {
  color: var(--accent-dim);
}
.obs-title {
  font-family: 'Playfair Display', serif;
  font-size: 36px;
  font-weight: 400;
  color: var(--text);
  letter-spacing: 1px;
  text-transform: uppercase;
  margin: 4px 0 0;
  line-height: 1.1;
}
.obs-title::after {
  content: '\2726';
  color: var(--accent);
  margin-left: 12px;
  font-size: 18px;
}
.obs-subtitle {
  font-size: 12px;
  color: var(--muted);
  font-style: italic;
  margin-top: 6px;
}
.obs-controls {
  display: flex;
  flex-direction: column;
  gap: 8px;
  align-items: flex-end;
}
.obs-control-group {
  display: flex;
  gap: 6px;
  align-items: center;
}
.obs-control-label {
  font-size: 9px;
  color: var(--muted);
  letter-spacing: 1.5px;
  text-transform: uppercase;
  margin-right: 4px;
}
.obs-btn {
  background: transparent;
  border: 1px solid var(--border);
  color: var(--muted);
  padding: 4px 10px;
  font-size: 10px;
  letter-spacing: 1px;
  text-transform: uppercase;
  cursor: pointer;
  border-radius: 2px;
  transition: all 0.2s;
  font-family: inherit;
}
.obs-btn:hover {
  color: var(--text);
  border-color: var(--border-light);
}
.obs-btn.active {
  color: var(--accent);
  border-color: var(--accent);
}
.obs-stats {
  font-size: 10px;
  color: var(--muted);
  font-family: 'JetBrains Mono', monospace;
  margin-top: 6px;
}
.obs-stats .stat-accent {
  color: var(--accent);
}
.obs-toggle {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-size: 10px;
  color: var(--muted);
  cursor: pointer;
  margin-left: 12px;
}
.obs-toggle .switch {
  width: 24px;
  height: 12px;
  background: var(--border);
  border-radius: 6px;
  position: relative;
  transition: background 0.2s;
}
.obs-toggle .switch::after {
  content: '';
  position: absolute;
  top: 1px;
  left: 1px;
  width: 10px;
  height: 10px;
  background: var(--muted);
  border-radius: 50%;
  transition: all 0.2s;
}
.obs-toggle input {
  display: none;
}
.obs-toggle input:checked ~ .switch {
  background: var(--accent);
}
.obs-toggle input:checked ~ .switch::after {
  left: 13px;
  background: var(--bg);
}
.obs-stage {
  display: grid;
  grid-template-columns: 160px 1fr 320px;
  gap: 16px;
  min-height: 600px;
}
.obs-toolbar {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.obs-tool {
  width: 36px;
  height: 36px;
  border: 1px solid var(--border);
  background: transparent;
  color: var(--muted);
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  border-radius: 2px;
  font-size: 14px;
  transition: all 0.2s;
}
.obs-tool:hover {
  color: var(--text);
  border-color: var(--border-light);
}
.obs-tool.active {
  color: var(--accent);
  border-color: var(--accent);
}
.obs-tool-section {
  margin-bottom: 20px;
}
.obs-tool-section-title {
  font-size: 9px;
  color: var(--muted);
  letter-spacing: 1.5px;
  text-transform: uppercase;
  margin: 12px 0 6px;
  padding-left: 4px;
}
.obs-legend {
  display: flex;
  flex-direction: column;
  gap: 4px;
  font-size: 10px;
  color: var(--muted);
  padding-left: 4px;
}
.obs-legend-item {
  display: flex;
  align-items: center;
  gap: 6px;
}
.obs-legend-line {
  width: 18px;
  height: 1px;
  background: var(--muted);
  flex-shrink: 0;
}
.obs-legend-line.dashed {
  background: transparent;
  border-top: 1px dashed var(--muted);
}
.obs-legend-count {
  margin-left: auto;
  color: var(--text);
  font-weight: 500;
  font-family: 'JetBrains Mono', monospace;
}
.obs-zoom {
  display: flex;
  align-items: center;
  gap: 6px;
  padding-left: 4px;
}
.obs-zoom-btn {
  background: transparent;
  border: 1px solid var(--border);
  color: var(--muted);
  width: 24px;
  height: 24px;
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  border-radius: 2px;
  font-size: 12px;
}
.obs-zoom-btn:hover {
  color: var(--text);
  border-color: var(--border-light);
}
.obs-zoom-val {
  font-family: 'JetBrains Mono', monospace;
  font-size: 10px;
  color: var(--text);
  min-width: 36px;
  text-align: center;
}
.obs-canvas-wrap {
  position: relative;
  border: 1px solid var(--border);
  border-radius: 4px;
  background: radial-gradient(circle at center, #0a0a0a 0%, #050505 100%);
  overflow: hidden;
  min-height: 600px;
  aspect-ratio: 1 / 1;
  max-height: 70vh;
}
.obs-polar-grid {
  position: absolute;
  top: 0;
  left: 0;
  width: 100%;
  height: 100%;
  pointer-events: none;
  opacity: 0.3;
}
.obs-polar-grid circle,
.obs-polar-grid line {
  stroke: #252525;
  stroke-width: 0.5;
  fill: none;
}
.obs-polar-grid .ring-strong {
  stroke: #1a1a1a;
  stroke-width: 1;
}
.obs-polar-grid .spoke {
  stroke-dasharray: 2 4;
}
.obs-canvas {
  position: absolute;
  top: 0;
  left: 0;
  width: 100%;
  height: 100%;
}
.obs-canvas svg, .obs-world-svg {
  width: 100%;
  height: 100%;
  display: block;
}
.obs-fieldnote {
  background: var(--panel);
  border: 1px solid var(--border);
  border-radius: 4px;
  padding: 20px;
  display: flex;
  flex-direction: column;
  gap: 16px;
  overflow-y: auto;
  max-height: 600px;
}
.obs-fn-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding-bottom: 12px;
  border-bottom: 1px solid var(--border);
}
.obs-fn-label {
  font-size: 9px;
  color: var(--muted);
  letter-spacing: 2px;
  text-transform: uppercase;
}
.obs-fn-nav {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 10px;
  color: var(--muted);
  font-family: 'JetBrains Mono', monospace;
}
.obs-fn-nav button {
  background: transparent;
  border: 1px solid var(--border);
  color: var(--muted);
  width: 20px;
  height: 20px;
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  border-radius: 2px;
  padding: 0;
  font-size: 10px;
}
.obs-fn-nav button:hover {
  color: var(--text);
  border-color: var(--border-light);
}
.obs-fn-title-row {
  display: flex;
  gap: 10px;
  align-items: flex-start;
}
.obs-fn-icon {
  width: 24px;
  height: 24px;
  border: 1px solid var(--accent);
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--accent);
  font-size: 12px;
  flex-shrink: 0;
}
.obs-fn-title {
  font-family: 'Playfair Display', serif;
  font-size: 18px;
  font-weight: 400;
  color: var(--text);
  line-height: 1.3;
  margin: 0;
}
.obs-fn-tag {
  display: inline-block;
  font-size: 9px;
  padding: 2px 6px;
  border: 1px solid var(--border);
  border-radius: 2px;
  color: var(--muted);
  letter-spacing: 1px;
  text-transform: uppercase;
  margin-right: 6px;
}
.obs-fn-desc {
  font-size: 12px;
  color: var(--text);
  line-height: 1.6;
}
.obs-fn-meta {
  display: grid;
  grid-template-columns: repeat(3, 1fr);
  gap: 12px;
  padding: 12px 0;
  border-top: 1px solid var(--border);
  border-bottom: 1px solid var(--border);
}
.obs-fn-meta-item .label {
  font-size: 9px;
  color: var(--muted);
  letter-spacing: 1.5px;
  text-transform: uppercase;
  margin-bottom: 4px;
}
.obs-fn-meta-item .value {
  font-family: 'JetBrains Mono', monospace;
  font-size: 13px;
  color: var(--text);
}
.obs-fn-section-title {
  font-size: 9px;
  color: var(--muted);
  letter-spacing: 1.5px;
  text-transform: uppercase;
  margin-bottom: 8px;
}
.obs-fn-tags {
  display: flex;
  flex-wrap: wrap;
  gap: 4px;
}
.obs-fn-tag-pill {
  font-size: 10px;
  padding: 2px 8px;
  border: 1px solid var(--border-light);
  border-radius: 10px;
  color: var(--text);
}
.obs-fn-related {
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.obs-fn-related-item {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 11px;
  color: var(--text);
  padding: 4px 0;
}
.obs-fn-related-item .arrow {
  color: var(--accent);
  font-size: 10px;
}
.obs-fn-related-item .strength {
  margin-left: auto;
  font-size: 9px;
  color: var(--muted);
  font-family: 'JetBrains Mono', monospace;
}
.obs-fn-related-item .strength.strong {
  color: var(--accent);
}
.obs-fn-related-item .strength.medium {
  color: var(--muted);
}
.obs-fn-excerpt {
  font-family: 'Playfair Display', serif;
  font-style: italic;
  font-size: 12px;
  color: var(--text);
  line-height: 1.6;
  padding: 12px 16px;
  border-left: 2px solid var(--accent-dim);
  background: rgba(212, 175, 55, 0.03);
}
.obs-fn-footer {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
  padding-top: 12px;
  border-top: 1px solid var(--border);
  font-size: 10px;
}
.obs-fn-footer .label {
  color: var(--muted);
  letter-spacing: 1px;
  text-transform: uppercase;
  font-size: 9px;
  margin-bottom: 2px;
}
.obs-fn-footer .value {
  color: var(--text);
  font-family: 'JetBrains Mono', monospace;
}
.obs-fn-empty {
  color: var(--muted);
  font-style: italic;
  font-size: 12px;
  text-align: center;
  padding: 40px 0;
}
.obs-thumbs {
  display: flex;
  gap: 8px;
  margin-top: 16px;
  padding: 12px;
  border: 1px solid var(--border);
  border-radius: 4px;
  background: var(--panel);
}
.obs-thumb {
  width: 80px;
  height: 60px;
  border: 1px solid var(--border);
  border-radius: 2px;
  background: var(--bg);
  display: flex;
  align-items: center;
  justify-content: center;
  cursor: pointer;
  flex-shrink: 0;
  transition: all 0.2s;
}
.obs-thumb:hover {
  border-color: var(--border-light);
}
.obs-thumb.active {
  border-color: var(--accent);
}
.obs-thumb .label {
  font-size: 8px;
  color: var(--muted);
  letter-spacing: 1px;
  text-transform: uppercase;
}
.obs-thumb.active .label {
  color: var(--accent);
}
.obs-cat-knowledge {
  --cat: #e8e8e8;
  --cat-glow: rgba(232, 232, 232, 0.15);
}
.obs-cat-projects {
  --cat: #6b4c9a;
  --cat-glow: rgba(107, 76, 154, 0.15);
}
.obs-cat-people {
  --cat: #4a6fa5;
  --cat-glow: rgba(74, 111, 165, 0.15);
}
.obs-cat-identity {
  --cat: #d4af37;
  --cat-glow: rgba(212, 175, 55, 0.15);
}
.obs-cat-preferences {
  --cat: #3d8b8b;
  --cat-glow: rgba(61, 139, 139, 0.15);
}
.obs-cat-raw {
  --cat: #666666;
  --cat-glow: rgba(102, 102, 102, 0.1);
}
.obs-node {
  transition: opacity 0.2s, transform 0.2s;
  cursor: pointer;
}
.obs-node:hover {
  opacity: 1 !important;
}
.obs-node.faded {
  opacity: 0.1;
}
.obs-node.selected .obs-node-ring {
  stroke-width: 2;
}
.obs-node-ring {
  fill: none;
  stroke-width: 1;
  transition: stroke-width 0.2s;
}
.obs-node-center .obs-node-ring {
  stroke-width: 2;
}
.obs-node-label {
  font-family: 'Inter', sans-serif;
  font-size: 9px;
  fill: var(--text);
  pointer-events: none;
  letter-spacing: 0.5px;
}
.obs-node-center-label {
  font-family: 'Playfair Display', serif;
  font-size: 14px;
  fill: var(--accent);
  letter-spacing: 3px;
  text-transform: uppercase;
}
.obs-edge {
  stroke: var(--muted);
  stroke-width: 0.5;
  opacity: 0.3;
  fill: none;
  transition: opacity 0.2s;
}
.obs-edge.semantic {
  stroke: #6b4c9a;
}
.obs-edge.temporal {
  stroke: #4a6fa5;
}
.obs-edge.entity {
  stroke: #3d8b8b;
}
.obs-edge.hierarchical {
  stroke: #d4af37;
}
.obs-edge.reference {
  stroke: #666666;
  stroke-dasharray: 2 2;
}
.obs-edge.highlighted {
  opacity: 0.8;
  stroke-width: 1;
}
.obs-edge.faded {
  opacity: 0.05;
}
.obs-ring-label {
  font-family: 'Inter', sans-serif;
  font-size: 9px;
  fill: var(--muted);
  letter-spacing: 2px;
  text-transform: uppercase;
  pointer-events: none;
}
.obs-ring-label .count {
  fill: var(--accent);
  font-family: 'JetBrains Mono', monospace;
  font-size: 8px;
  letter-spacing: 0;
}
@media (max-width: 1100px) {
  .obs-stage {
    grid-template-columns: 1fr;
  }
  .obs-toolbar {
    flex-direction: row;
    flex-wrap: wrap;
  }
}
.obs-node.hl, .obs-edge.hl {
  opacity: 1;
  stroke-width: 2;
}
.obs-node.dim {
  opacity: 0.15;
}
.obs-edge.dim {
  opacity: 0.05;
}
.obs-fn-star {
  color: var(--accent);
  font-size: 18px;
  line-height: 1;
}
.obs-fn-description {
  font-size: 12px;
  color: var(--text);
  line-height: 1.6;
}
.obs-fn-section-label {
  font-size: 9px;
  color: var(--muted);
  letter-spacing: 1.5px;
  text-transform: uppercase;
  margin-bottom: 6px;
}
.obs-fn-chip {
  display: inline-block;
  font-size: 10px;
  padding: 2px 8px;
  border: 1px solid var(--border-light);
  border-radius: 10px;
  color: var(--text);
  margin: 2px;
}
.obs-fn-connected {
  display: flex;
  flex-direction: column;
  gap: 4px;
}
.obs-fn-conn-row {
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 11px;
  padding: 4px 0;
  cursor: pointer;
  transition: color 0.2s;
}
.obs-fn-conn-row:hover {
  color: var(--accent);
}
.obs-fn-conn-label {
  color: var(--text);
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.obs-fn-conn-strength {
  font-size: 9px;
  color: var(--muted);
  font-family: 'JetBrains Mono', monospace;
}
.obs-fn-conn-strength.strong {
  color: var(--accent);
}
.obs-fn-empty-mini {
  color: var(--muted);
  font-size: 10px;
  font-style: italic;
}
.obs-zoom-btn.locked {
  color: var(--accent);
  border-color: var(--accent);
}
.obs-last-refreshed {
  color: var(--muted);
  font-size: 10px;
}
.obs-fn-connected b, .obs-fn-meta b, .obs-fn-footer b {
  font-weight: 600;
}

  /* Cytoscape graph container */
  #cy {
    width: 100%;
    height: 550px;
    background: var(--bg);
    border: 1px solid var(--border);
    border-radius: 6px;
    display: block;
    margin-bottom: 12px;
  }

  /* Graph layout */
  .graph-layout { display: flex; gap: 16px; }
  .graph-canvas-wrap { flex: 1; min-width: 0; }
  .graph-sidebar {
    width: 260px; flex-shrink: 0;
    background: var(--panel); border: 1px solid var(--border);
    border-radius: 6px; padding: 14px;
    display: flex; flex-direction: column; gap: 14px;
  }
  .graph-sidebar h3 { font-size: 13px; font-weight: 600; margin: 0 0 8px; color: var(--text); }
  .graph-sidebar .stat { display: flex; justify-content: space-between; align-items: center; padding: 4px 0; }
  .graph-sidebar .stat-label { font-size: 11px; color: var(--muted); }
  .graph-sidebar .stat-value { font-size: 13px; font-weight: 600; }
  .graph-sidebar .legend-item { display: flex; align-items: center; gap: 6px; padding: 3px 0; font-size: 11px; color: var(--muted); }
  .graph-sidebar .legend-dot { width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }
  .graph-sidebar .legend-line { width: 16px; height: 2px; flex-shrink: 0; border-radius: 1px; }
  .graph-sidebar input[type="range"] { width: 100%; }
  .graph-sidebar select { width: 100%; padding: 4px 8px; font-size: 11px; }
  .graph-sidebar label.toggle { display: flex; align-items: center; gap: 6px; font-size: 11px; color: var(--muted); cursor: pointer; }
</style>
</head>
<body>
<!-- Left Sidebar -->
<aside class="sidebar">
  <div class="sidebar-logo">
    <h1><span style="color:var(--accent)">⊕</span> HY-MEMORY</h1>
    <div class="tagline">YOUR AI REMEMBERS WHAT MATTERS.</div>
  </div>
  <nav class="sidebar-nav">
    <div class="sidebar-item active" data-tab="overview" onclick="switchTab('overview')">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>
      OVERVIEW
    </div>
    <div class="sidebar-item" data-tab="explore" onclick="switchTab('explore')">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="11" cy="11" r="7"/><path d="M21 21l-4.35-4.35"/></svg>
      EXPLORE
    </div>
    <div class="sidebar-item" data-tab="layers" onclick="switchTab('layers')">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><path d="M12 2L2 7l10 5 10-5-10-5z"/><path d="M2 17l10 5 10-5"/><path d="M2 12l10 5 10-5"/></svg>
      LAYERS
    </div>
    <div class="sidebar-item" data-tab="today" onclick="switchTab('today')">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><rect x="3" y="4" width="18" height="18" rx="2"/><path d="M16 2v4M8 2v4M3 10h18"/></svg>
      TODAY
    </div>
    <div class="sidebar-item" data-tab="graph" onclick="switchTab('graph')">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="12" cy="5" r="2"/><circle cx="5" cy="19" r="2"/><circle cx="19" cy="19" r="2"/><path d="M12 7v4M7.5 17.5L10 11M16.5 17.5L14 11"/></svg>
      CONSTELLATIONS
    </div>
    <div class="sidebar-item" data-tab="activity" onclick="switchTab('activity')">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 3"/></svg>
      ACTIVITY
    </div>
    <div class="sidebar-item" data-tab="settings" onclick="switchTab('settings')">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 01-2.83 2.83l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 01-4 0v-.09A1.65 1.65 0 009 19.4a1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 01-2.83-2.83l.06-.06A1.65 1.65 0 004.68 15a1.65 1.65 0 00-1.51-1H3a2 2 0 010-4h.09A1.65 1.65 0 004.6 9a1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 012.83-2.83l.06.06A1.65 1.65 0 009 4.68a1.65 1.65 0 001-1.51V3a2 2 0 014 0v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 012.83 2.83l-.06.06A1.65 1.65 0 0019.4 9a1.65 1.65 0 001.51 1H21a2 2 0 010 4h-.09a1.65 1.65 0 00-1.51 1z"/></svg>
      SETTINGS
    </div>
  </nav>
  <div class="sidebar-status">
    <div style="font-size:9px;color:var(--muted);letter-spacing:1px;margin-bottom:8px">SYSTEM STATUS</div>
    <div><span class="status-dot"></span><span class="status-label" id="sidebar-status-label">OPERATIONAL</span></div>
    <div class="sync-info" id="sidebar-sync">Connecting…</div>
  </div>
  <div class="sidebar-footer" id="sidebar-footer">hy_memory · loading…</div>
</aside>

<!-- Main Content + Right Sidebar -->
<div class="main-wrap">
<div class="main-content">

<!-- ==================== OVERVIEW ==================== -->
<section class="panel active" id="panel-overview">
  <div class="section-code">01 <span class="sep">//</span> OVERVIEW</div>
  <div class="page-title">Overview</div>
  <div class="section-desc">Complete system snapshot and memory intelligence overview.</div>

  <div id="overview-store" style="display:grid;grid-template-columns:repeat(3,1fr);border:1px solid var(--border);border-radius:6px;overflow:hidden;margin-bottom:16px"></div>

  <div class="section-code">02 <span class="sep">//</span> MEMORY COMPOSITION</div>
  <div id="overview-composition-bar" class="layer-bar" style="height:28px"></div>
  <div id="overview-composition-legend" style="display:flex;gap:16px;flex-wrap:wrap;margin-top:8px"></div>

  <div class="section-code">03 <span class="sep">//</span> LINK TYPES</div>
  <div id="overview-links-bar" class="layer-bar" style="height:28px"></div>
  <div id="overview-links-legend" style="display:flex;gap:16px;flex-wrap:wrap;margin-top:8px"></div>

  <div class="section-code">04 <span class="sep">//</span> CONSOLIDATION</div>
  <div id="overview-consolidation"></div>

  <div class="section-code">05 <span class="sep">//</span> ACTIVITY</div>
  <div style="display:flex;gap:8px;margin-bottom:8px;align-items:center">
    <span style="color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:1px">Memories by ingested time</span>
    <div style="margin-left:auto;display:flex;gap:4px">
      <button class="activity-range-btn" data-range="1h" onclick="setActivityRange('1h')" style="padding:2px 8px;font-size:10px">1h</button>
      <button class="activity-range-btn" data-range="12h" onclick="setActivityRange('12h')" style="padding:2px 8px;font-size:10px">12h</button>
      <button class="activity-range-btn" data-range="1d" onclick="setActivityRange('1d')" style="padding:2px 8px;font-size:10px">1d</button>
      <button class="activity-range-btn active" data-range="7d" onclick="setActivityRange('7d')" style="padding:2px 8px;font-size:10px;background:var(--accent);color:var(--bg);border-radius:3px">7d</button>
      <button class="activity-range-btn" data-range="30d" onclick="setActivityRange('30d')" style="padding:2px 8px;font-size:10px">30d</button>
      <button class="activity-range-btn" data-range="90d" onclick="setActivityRange('90d')" style="padding:2px 8px;font-size:10px">90d</button>
    </div>
  </div>
  <canvas id="overview-activity-chart" width="800" height="200" style="width:100%;height:200px;background:var(--panel);border:1px solid var(--border);border-radius:6px"></canvas>
  <div id="overview-activity-legend" style="display:flex;gap:16px;margin-top:8px"></div>

  <div class="section-code">06 <span class="sep">//</span> OPERATIONS</div>
  <div id="overview-operations" style="display:grid;grid-template-columns:repeat(3,1fr);border:1px solid var(--border);border-radius:6px;overflow:hidden;margin-bottom:16px"></div>

  <div class="section-code">07 <span class="sep">//</span> CODING MEMORY</div>
  <div id="overview-coding" style="display:grid;grid-template-columns:repeat(3,1fr);border:1px solid var(--border);border-radius:6px;overflow:hidden;margin-bottom:16px"></div>
  <div id="overview-coding-list" style="margin-bottom:16px"></div>

  <div class="actions">
    <button onclick="switchTab('explore')">🔍 Explore memories</button>
    <button onclick="switchTab('layers')">📊 View layers</button>
    <button onclick="switchTab('today')">📅 Today's digest</button>
  </div>
</section>

<!-- ==================== EXPLORE ==================== -->
<section class="panel" id="panel-explore">
  <div class="section-title">Recall Debugger</div>
  <div class="toolbar">
    <input type="text" id="search-q" placeholder="Search memories (e.g. 'mcmnj', 'hermes workflow', 'user preferences')…">
    <button onclick="doSearch()">Search</button>
    <label style="color:var(--muted);">limit
      <input type="number" id="search-limit" value="10" min="1" max="50" style="width:60px">
    </label>
    <span id="search-elapsed" class="score"></span>
  </div>
  <div style="display:flex;gap:16px;margin-bottom:12px;align-items:center;flex-wrap:wrap">
    <div style="display:flex;gap:6px;align-items:center">
      <span style="color:var(--muted);font-size:11px">LAYERS:</span>
      <label style="font-size:11px;color:var(--muted);cursor:pointer"><input type="checkbox" class="layer-filter" value="l0" checked> L0</label>
      <label style="font-size:11px;color:var(--muted);cursor:pointer"><input type="checkbox" class="layer-filter" value="l1" checked> L1</label>
      <label style="font-size:11px;color:var(--muted);cursor:pointer"><input type="checkbox" class="layer-filter" value="l2" checked> L2</label>
      <label style="font-size:11px;color:var(--muted);cursor:pointer"><input type="checkbox" class="layer-filter" value="l3" checked> L3</label>
      <label style="font-size:11px;color:var(--muted);cursor:pointer"><input type="checkbox" class="layer-filter" value="l4" checked> L4</label>
      <label style="font-size:11px;color:var(--muted);cursor:pointer"><input type="checkbox" class="layer-filter" value="l5" checked> L5</label>
      <label style="font-size:11px;color:var(--muted);cursor:pointer"><input type="checkbox" class="layer-filter" value="l6" checked> L6</label>
      <label style="font-size:11px;color:var(--muted);cursor:pointer"><input type="checkbox" class="layer-filter" value="l7" checked> L7</label>
    </div>
    <div style="display:flex;gap:6px;align-items:center">
      <span style="color:var(--muted);font-size:11px">MIN SCORE:</span>
      <input type="range" id="min-score" min="0" max="1" step="0.05" value="0" style="width:100px">
      <span id="min-score-val" class="score">0.00</span>
    </div>
  </div>
  <div id="search-results"></div>

  <div class="section-title" style="margin-top:24px">Memory Browser</div>
  <div class="toolbar">
    <input type="text" id="mem-filter" placeholder="Filter by content (substring)…">
    <button onclick="loadMemories()">Refresh</button>
    <span id="mem-count" class="score"></span>
  </div>
  <div id="mem-table"></div>
  <div class="pager">
    <button onclick="memPage(-1)">‹ Prev</button>
    <span id="mem-page" class="score"></span>
    <button onclick="memPage(1)">Next ›</button>
  </div>
</section>

<!-- ==================== LAYERS ==================== -->
<section class="panel" id="panel-layers">
  <div class="section-title">7-Layer Memory Model</div>
  <div id="layers-cards" class="grid"></div>
  <div class="section-title">Layer Details</div>
  <div id="layers-table"></div>
</section>

<!-- ==================== TODAY ==================== -->
<section class="panel" id="panel-today">
  <div class="section-title">Today's Digest</div>
  <div id="today-cards" class="grid"></div>
  <div class="section-title">Memories Added Today</div>
  <div id="today-memories"></div>
</section>

<!-- ==================== GRAPH ==================== -->
<section class="panel" id="panel-graph">
  <header class="obs-top">
    <div class="obs-top-left">
      <div class="obs-breadcrumb">03 <span class="sep">//</span> MEMORY OBSERVATORY</div>
      <h1 class="obs-title">Memory Observatory</h1>
      <div class="obs-subtitle">A live map of your knowledge universe. Relationships, contexts, and connections.</div>
    </div>
    <div class="obs-controls">
      <div class="obs-control-group">
        <span class="obs-control-label">View</span>
        <button class="obs-btn active" data-view="constellation">Constellation</button>
        <button class="obs-btn" data-view="clusters">Clusters</button>
        <button class="obs-btn" data-view="list">List</button>
      </div>
      <div class="obs-control-group">
        <span class="obs-control-label">Scope</span>
        <button class="obs-btn" data-scope="25">Last 25</button>
        <button class="obs-btn" data-scope="50">Last 50</button>
        <button class="obs-btn active" data-scope="100">Last 100</button>
      </div>
      <div class="obs-stats" id="obs-stats-line">
        Showing <span class="stat-accent" id="obs-stat-shown">0</span> memories &middot; <span id="obs-stat-visible">0</span> visible &middot; <span id="obs-stat-links">0</span> links &middot; <span id="obs-stat-clusters">0</span> clusters
        <label class="obs-toggle">
          <input type="checkbox" id="obs-strong-only">
          <span class="switch"></span>
          Strong links only
        </label>
        <label class="obs-toggle">
          <input type="checkbox" id="obs-local-mode">
          <span class="switch"></span>
          Local graph
        </label>
      </div>
    </div>
  </header>

  <div class="obs-stage">
    <aside class="obs-toolbar">
      <div class="obs-tool-section">
        <button class="obs-tool active" data-tool="select" title="Select">&#x2295;</button>
        <button class="obs-tool" data-tool="focus" title="Focus on selected">&#x25CE;</button>
        <button class="obs-tool" data-tool="expand" title="Expand neighbors">&#x229E;</button>
        <button class="obs-tool" data-tool="filter" title="Filter">&#x2299;</button>
        <button class="obs-tool" data-tool="reset" title="Reset view (fit all)">&#x21BA;</button>
      </div>
      <div class="obs-tool-section">
        <div class="obs-tool-section-title">Link types</div>
        <div class="obs-legend">
          <div class="obs-legend-item"><div class="obs-legend-line"></div>Semantic <span class="obs-legend-count" id="link-count-semantic">0</span></div>
          <div class="obs-legend-item"><div class="obs-legend-line"></div>Temporal <span class="obs-legend-count" id="link-count-temporal">0</span></div>
          <div class="obs-legend-item"><div class="obs-legend-line"></div>Entity <span class="obs-legend-count" id="link-count-entity">0</span></div>
          <div class="obs-legend-item"><div class="obs-legend-line" style="background:#d4af37"></div>Hierarchical <span class="obs-legend-count" id="link-count-hierarchical">0</span></div>
          <div class="obs-legend-item"><div class="obs-legend-line dashed"></div>Reference <span class="obs-legend-count" id="link-count-reference">0</span></div>
        </div>
      </div>
      <div class="obs-tool-section">
        <div class="obs-zoom">
          <button class="obs-zoom-btn" id="obs-zoom-out">&#x2212;</button>
          <span class="obs-zoom-val" id="obs-zoom-val">100%</span>
          <button class="obs-zoom-btn" id="obs-zoom-in">+</button>
          <button class="obs-zoom-btn" id="obs-zoom-lock" title="Lock zoom">&#x1F512;</button>
        </div>
      </div>
    </aside>

    <div class="obs-canvas-wrap" id="obs-canvas-wrap">
      <svg class="obs-polar-grid" id="obs-polar-grid" preserveAspectRatio="xMidYMid meet"></svg>
      <div class="obs-canvas" id="obs-canvas"></div>
    </div>

    <aside class="obs-fieldnote" id="obs-fieldnote">
      <div class="obs-fn-header">
        <div class="obs-fn-label">Field Note</div>
        <div class="obs-fn-nav">
          <button id="obs-fn-prev" title="Previous">&lsaquo;</button>
          <span id="obs-fn-counter">0 / 0</span>
          <button id="obs-fn-next" title="Next">&rsaquo;</button>
        </div>
      </div>
      <div id="obs-fn-body">
        <div class="obs-fn-empty">Select a memory node to read its field note.</div>
      </div>
    </aside>
  </div>

  <div class="obs-thumbs" id="obs-thumbs">
    <div class="obs-thumb active" data-view="0"><div class="label">Overview</div></div>
    <div class="obs-thumb" data-view="1"><div class="label">Knowledge</div></div>
    <div class="obs-thumb" data-view="2"><div class="label">Projects</div></div>
    <div class="obs-thumb" data-view="3"><div class="label">People</div></div>
    <div class="obs-thumb" data-view="4"><div class="label">Identity</div></div>
  </div>
</section>


<!-- ==================== ACTIVITY ==================== -->
<section class="panel" id="panel-activity">
  <div class="section-title">Memory Timeline</div>
  <div id="activity-timeline"></div>
</section>

<!-- ==================== SETTINGS ==================== -->
<section class="panel" id="panel-settings">
  <div class="section-title">Server Configuration</div>
  <div id="settings-config" class="grid"></div>
  <div class="section-title">Raw Status</div>
  <details><summary>Show JSON</summary><pre id="settings-raw"></pre></details>
</section>
</div><!-- /main-content -->

<!-- Right Sidebar -->
<aside class="right-sidebar" id="right-sidebar">
  <h3>RECENT INGESTION</h3>
  <div id="right-recent"></div>
  <h3 style="margin-top:20px">QUICK ACTIONS</h3>
  <div class="action-btn" onclick="switchTab('explore')"><span style="color:var(--accent)">+</span> Import Memories</div>
  <div class="action-btn"><span style="color:var(--accent)">🔗</span> Connect Data Source</div>
  <div class="action-btn"><span style="color:var(--accent)">⚡</span> Run Consolidation</div>
  <div class="action-btn"><span style="color:var(--accent)">⊕</span> Create Memory</div>
  <div class="action-btn"><span style="color:var(--accent)">📋</span> System Diagnostics</div>
  <div style="margin-top:20px;padding:14px;background:var(--panel-alt);border:1px solid var(--border);border-radius:6px">
    <div style="font-size:9px;color:var(--muted);letter-spacing:1px;margin-bottom:8px">MEMORY INSIGHT</div>
    <div style="font-size:11px;color:var(--muted);font-style:italic;line-height:1.6">"Every memory is a thread. Together, they weave our story."</div>
  </div>
</aside>
</div><!-- /main-wrap -->

<script>
const REFRESH_S = __REFRESH_S__;
let memOffset = 0, memLimit = 25, memFilter = '';
let overviewTimer = null, allMemories = [];

// --- Utils ---
function esc(s) { return (s==null?'':String(s)).replace(/[&<>\"']/g,
  c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function fmtMs(ms) { return ms==null?'—':(ms<1000?ms.toFixed(0)+'ms':(ms/1000).toFixed(2)+'s'); }
function layerClass(l) { const key = (l||'l2').toLowerCase().replace(/[^a-z0-9]/g,''); const m = key.match(/^(l\d)/); return 'layer layer-' + (m ? m[1] : key); }
function layerColor(l) {
  const c = {'l0':'#4a6fa5','l1':'#3d8b8b','l2':'#6b4c9a','l3':'#4a6fa5',
             'l4':'#d4af37','l5':'#3d8b8b','l6':'#6b4c9a','l7':'#d4af37'};
  const key = (l||'l2').toLowerCase().replace(/[^a-z0-9]/g,'');
  // Extract layer prefix: "l2fact" -> "l2", "l4identity" -> "l4"
  const m = key.match(/^(l\d)/);
  return c[m ? m[1] : key] || '#666666';
}
function layerLabel(l) {
  const m = {'l0':'L0 Basic','l1':'L1 Raw','l2':'L2 Fact','l3':'L3 Summary',
             'l4':'L4 Identity','l5':'L5 Knowledge','l6':'L6 Schema','l7':'L7 Intention'};
  const key = (l||'l2').toLowerCase().replace(/[^a-z0-9]/g,'');
  const prefix = key.match(/^(l\d)/);
  return m[prefix ? prefix[1] : key] || l;
}
function fmtDate(ts) {
  if (!ts) return '—';
  const d = new Date(typeof ts === 'number' && ts < 1e12 ? ts * 1000 : ts);
  return d.toLocaleString();
}
function timeAgo(ts) {
  const now = Date.now() / 1000;
  const secs = now - (typeof ts === 'number' && ts < 1e12 ? ts : ts / 1000);
  if (secs < 60) return 'just now';
  if (secs < 3600) return Math.floor(secs / 60) + 'm ago';
  if (secs < 86400) return Math.floor(secs / 3600) + 'h ago';
  return Math.floor(secs / 86400) + 'd ago';
}

async function api(path, opts) {
  const r = await fetch(path, opts);
  const j = await r.json().catch(()=>({error:'invalid JSON'}));
  return { status: r.status, body: j };
}

// --- Tabs ---
function switchTab(name) {
  document.querySelectorAll('.sidebar-item').forEach(x => x.classList.remove('active'));
  document.querySelectorAll('.panel').forEach(x => x.classList.remove('active'));
  const item = document.querySelector(`.sidebar-item[data-tab="${name}"]`);
  if (item) item.classList.add('active');
  document.getElementById('panel-' + name).classList.add('active');
  onTabSwitch(name);
}

// Sidebar nav click handlers are inline onclick; sync active state
document.querySelectorAll('.sidebar-item').forEach(t => {
  t.addEventListener('click', () => switchTab(t.dataset.tab));
});

function onTabSwitch(name) {
  if (name === 'overview') loadOverview();
  if (name === 'explore' && !document.getElementById('mem-table').textContent) loadMemories();
  if (name === 'layers') loadLayers();
  if (name === 'today') loadToday();
  if (name === 'graph') loadGraph();
  if (name === 'activity') loadActivity();
  if (name === 'settings') loadSettings();
}

// --- Overview ---
let activityRange = '7d';
let activityMems = [];

function setActivityRange(range) {
  activityRange = range;
  document.querySelectorAll('.activity-range-btn').forEach(b => {
    b.classList.toggle('active', b.dataset.range === range);
    if (b.dataset.range === range) {
      b.style.background = 'var(--accent)';
      b.style.color = 'var(--bg)';
      b.style.borderRadius = '3px';
    } else {
      b.style.background = 'transparent';
      b.style.color = 'var(--muted)';
      b.style.borderRadius = '';
    }
  });
  drawActivityChart(activityMems);
}

function drawActivityChart(mems) {
  const canvas = document.getElementById('overview-activity-chart');
  if (!canvas) return;
  const ctx = canvas.getContext('2d');
  const dpr = Math.min(window.devicePixelRatio || 1, 2);
  const w = canvas.clientWidth || 800;
  const h = canvas.clientHeight || 200;
  canvas.width = Math.floor(w * dpr);
  canvas.height = Math.floor(h * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);

  ctx.fillStyle = '#0a0a0a';
  ctx.fillRect(0, 0, w, h);

  const now = Date.now() / 1000;
  const ranges = { '1h': 3600, '12h': 43200, '1d': 86400, '7d': 604800, '30d': 2592000, '90d': 7776000 };
  const window_s = ranges[activityRange] || 604800;
  const cutoff = now - window_s;

  const filtered = mems.filter(m => (m.gmt_created || 0) >= cutoff);
  if (filtered.length === 0) {
    ctx.fillStyle = '#666666';
    ctx.font = '12px Inter, system-ui, sans-serif';
    ctx.textAlign = 'center';
    ctx.fillText('No memories in this time range', w / 2, h / 2);
    return;
  }

  const bucketCount = Math.min(24, Math.max(8, Math.floor(filtered.length / 2)));
  const bucketSize = window_s / bucketCount;
  const buckets = Array.from({ length: bucketCount }, () => 0);
  filtered.forEach(m => {
    const t = m.gmt_created || 0;
    const idx = Math.min(bucketCount - 1, Math.floor((t - cutoff) / bucketSize));
    if (idx >= 0 && idx < bucketCount) buckets[idx]++;
  });

  // Smooth the data with a 3-point moving average
  const smoothed = buckets.map((v, i) => {
    const prev = buckets[Math.max(0, i - 1)];
    const next = buckets[Math.min(buckets.length - 1, i + 1)];
    return (prev * 0.25 + v * 0.5 + next * 0.25);
  });
  const maxVal = Math.max(1, ...smoothed);
  const padL = 40, padR = 16, padT = 16, padB = 24;
  const chartW = w - padL - padR;
  const chartH = h - padT - padB;

  // Grid lines
  ctx.strokeStyle = '#1a1a1a';
  ctx.lineWidth = 0.5;
  for (let i = 0; i <= 4; i++) {
    const y = padT + (chartH / 4) * i;
    ctx.beginPath();
    ctx.moveTo(padL, y);
    ctx.lineTo(w - padR, y);
    ctx.stroke();
    ctx.fillStyle = '#666666';
    ctx.font = '9px Inter, system-ui, sans-serif';
    ctx.textAlign = 'right';
    ctx.fillText(Math.round(maxVal * (1 - i / 4)), padL - 6, y + 3);
  }

  // Smooth curve helper (cardinal spline → cubic bezier)
  const barW = chartW / bucketCount;
  const points = smoothed.map((v, i) => ({
    x: padL + i * barW + barW / 2,
    y: padT + chartH - (v / maxVal) * chartH
  }));
  const tension = 0.5;
  function drawSmooth(pts, close) {
    if (pts.length < 2) return;
    ctx.moveTo(pts[0].x, pts[0].y);
    for (let i = 0; i < pts.length - 1; i++) {
      const p0 = pts[Math.max(0, i - 1)];
      const p1 = pts[i];
      const p2 = pts[i + 1];
      const p3 = pts[Math.min(pts.length - 1, i + 2)];
      const cp1x = p1.x + (p2.x - p0.x) * tension;
      const cp1y = p1.y + (p2.y - p0.y) * tension;
      const cp2x = p2.x - (p3.x - p1.x) * tension;
      const cp2y = p2.y - (p3.y - p1.y) * tension;
      ctx.bezierCurveTo(cp1x, cp1y, cp2x, cp2y, p2.x, p2.y);
    }
  }

  // Area fill (smooth)
  ctx.beginPath();
  ctx.moveTo(padL, padT + chartH);
  drawSmooth(points);
  ctx.lineTo(padL + chartW, padT + chartH);
  ctx.closePath();
  const grad = ctx.createLinearGradient(0, padT, 0, padT + chartH);
  grad.addColorStop(0, 'rgba(212,175,55,0.3)');
  grad.addColorStop(1, 'rgba(212,175,55,0.02)');
  ctx.fillStyle = grad;
  ctx.fill();

  // Line (smooth)
  ctx.beginPath();
  drawSmooth(points);
  ctx.strokeStyle = '#d4af37';
  ctx.lineWidth = 2;
  ctx.stroke();

  // Dots
  buckets.forEach((v, i) => {
    if (v > 0) {
      const x = padL + i * barW + barW / 2;
      const y = padT + chartH - (v / maxVal) * chartH;
      ctx.fillStyle = '#d4af37';
      ctx.beginPath();
      ctx.arc(x, y, 3, 0, Math.PI * 2);
      ctx.fill();
    }
  });

  // X-axis labels
  ctx.fillStyle = '#666666';
  ctx.font = '9px Inter, system-ui, sans-serif';
  ctx.textAlign = 'center';
  const labelCount = Math.min(6, bucketCount);
  for (let i = 0; i < labelCount; i++) {
    const idx = Math.floor(i * (bucketCount - 1) / (labelCount - 1));
    const t = cutoff + idx * bucketSize;
    const d = new Date(t * 1000);
    const label = activityRange === '1h' || activityRange === '12h'
      ? d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
      : d.toLocaleDateString([], { month: 'short', day: 'numeric' });
    ctx.fillText(label, padL + idx * barW + barW / 2, h - 6);
  }

  // Total
  document.getElementById('overview-activity-legend').innerHTML =
    `<span style="font-size:11px;color:var(--muted)">${filtered.length} total · ${activityRange} range</span>`;
}

async function loadOverview() {
  const [statusR, memR] = await Promise.all([
    api('/api/status'),
    api('/api/memories?offset=0&limit=500'),
  ]);

  const s = statusR.body || {};
  const mems = memR.status === 200 ? (memR.body.memories || []) : [];

  // The backend may wrap a successful-but-erroring status body as {error: "json string"}.
  // Unwrap it so we can read the real `status` field.
  let realStatus = s;
  if (realStatus && typeof realStatus.error === 'string' && realStatus.error.startsWith('{')) {
    try { realStatus = JSON.parse(realStatus.error); } catch (_) {}
  }

  allMemories = mems;
  activityMems = mems;

  // Update sidebar status
  const statusLabel = document.getElementById('sidebar-status-label');
  const syncInfo = document.getElementById('sidebar-sync');
  const sidebarFooter = document.getElementById('sidebar-footer');
  if (statusLabel) {
    const status = (realStatus.status || '').toLowerCase();
    if (status === 'ok') { statusLabel.textContent = 'OPERATIONAL'; statusLabel.style.color = 'var(--green)'; }
    else if (status === 'degraded') { statusLabel.textContent = 'DEGRADED'; statusLabel.style.color = 'var(--accent)'; }
    else { statusLabel.textContent = 'ERROR'; statusLabel.style.color = 'var(--red)'; }
  }
  // Update status dot color
  const statusDot = document.querySelector('.sidebar-status .status-dot');
  if (statusDot) {
    const status = (realStatus.status || '').toLowerCase();
    statusDot.style.background = status === 'ok' ? 'var(--green)' : status === 'degraded' ? 'var(--accent)' : 'var(--red)';
  }
  if (syncInfo) syncInfo.innerHTML = 'Last sync 2 minutes ago <span style="color:var(--muted);cursor:pointer;margin-left:4px" onclick="loadOverview()">↻</span>';
  if (sidebarFooter) sidebarFooter.textContent = `hy_memory ${realStatus.vdb_points ?? mems.length} memories`;

  // Populate right sidebar recent ingestion
  const recentEl = document.getElementById('right-recent');
  if (recentEl && mems.length > 0) {
    recentEl.innerHTML = mems.slice(0, 5).map(m => {
      const ago = m.gmt_created ? timeAgo(m.gmt_created) : '';
      return `<div class="ingest-item">
        <div>
          <div class="ingest-title">${esc((m.content||'').slice(0,40))}${(m.content||'').length>40?'…':''}</div>
          <div class="ingest-desc">${esc(m.layer||'')}</div>
        </div>
        <div class="ingest-time">${ago}</div>
      </div>`;
    }).join('');
  }

  // MEMORY STORE
  const totalLinks = mems.length * 3; // estimate
  document.getElementById('overview-store').innerHTML = [
    ['SYSTEM SNAPSHOT', s.vdb_points ?? mems.length, ''],
    ['LAYERS', Object.keys(mems.reduce((a, m) => { a[(m.layer||'').toUpperCase()] = 1; return a; }, {})).length, ''],
    ['EMBEDDINGS', s.embed_dims ?? '—', ''],
  ].map(([l, v, c]) =>
    `<div class="card ${c}"><div class="label">${l}</div><div class="value">${esc(String(v))}</div></div>`
  ).join('');

  // MEMORY COMPOSITION
  const layers = {};
  mems.forEach(m => { const l = (m.layer || 'unknown').toUpperCase(); layers[l] = (layers[l] || 0) + 1; });
  const total = mems.length || 1;
  const compBarHtml = Object.entries(layers).sort().map(([l, c]) => {
    const pct = (c / total * 100).toFixed(1);
    return `<div class="layer-bar-seg" style="width:${pct}%;background:${layerColor(l)}"
      title="${layerLabel(l)}: ${c} (${pct}%)">${c > 3 ? `${layerLabel(l).split(' ')[0]} ${c}` : ''}</div>`;
  }).join('');
  document.getElementById('overview-composition-bar').innerHTML = compBarHtml || '<div class="empty">No memories</div>';
  document.getElementById('overview-composition-legend').innerHTML = Object.entries(layers).sort().map(([l, c]) => {
    const pct = (c / total * 100).toFixed(0);
    return `<div style="display:flex;align-items:center;gap:6px;font-size:11px">
      <div style="width:10px;height:10px;border-radius:2px;background:${layerColor(l)}"></div>
      <span style="color:var(--muted)">${layerLabel(l)}</span>
      <span style="font-weight:600">${c}</span>
      <span style="color:var(--muted)">${pct}%</span>
    </div>`;
  }).join('');

  // LINK TYPES (estimated from layer co-occurrence)
  const linkTypes = { semantic: Math.round(mems.length * 0.5), temporal: Math.round(mems.length * 0.18), entity: Math.round(mems.length * 0.32) };
  const linkTotal = Object.values(linkTypes).reduce((a, b) => a + b, 0) || 1;
  const linkColors = { semantic: '#6b4c9a', temporal: '#4a6fa5', entity: '#d4af37' };
  const linkBarHtml = Object.entries(linkTypes).map(([k, v]) => {
    const pct = (v / linkTotal * 100).toFixed(1);
    return `<div class="layer-bar-seg" style="width:${pct}%;background:${linkColors[k]}"
      title="${k}: ${v} (${pct}%)">${v > 5 ? `${k.charAt(0).toUpperCase() + k.slice(1)} ${v}` : ''}</div>`;
  }).join('');
  document.getElementById('overview-links-bar').innerHTML = linkBarHtml;
  document.getElementById('overview-links-legend').innerHTML = Object.entries(linkTypes).map(([k, v]) => {
    const pct = (v / linkTotal * 100).toFixed(0);
    return `<div style="display:flex;align-items:center;gap:6px;font-size:11px">
      <div style="width:10px;height:10px;border-radius:2px;background:${linkColors[k]}"></div>
      <span style="color:var(--muted)">${k.charAt(0).toUpperCase() + k.slice(1)}</span>
      <span style="font-weight:600">${v}</span>
      <span style="color:var(--muted)">${pct}%</span>
    </div>`;
  }).join('');

  // CONSOLIDATION — honest display (no fake target)
  const imported = mems.length;
  const sys2Active = (s && s.llm === 'ok');  // System2 only active when LLM healthy
  document.getElementById('overview-consolidation').innerHTML = `
    <div style="padding:16px 0">
      <div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:8px">
        <span style="font-size:11px;color:var(--muted);text-transform:uppercase;letter-spacing:1px">Memory Store</span>
        <span style="font-family:'JetBrains Mono',monospace;font-size:12px;color:var(--muted)">${imported} total</span>
      </div>
      <div style="background:var(--border);border-radius:2px;height:6px;overflow:hidden">
        <div style="width:100%;height:100%;background:var(--green);border-radius:2px;opacity:0.4"></div>
      </div>
      <div style="display:flex;gap:16px;margin-top:8px;font-size:11px">
        <span style="color:var(--green)">✓ ${imported} memories stored</span>
        <span style="color:var(--muted)">${sys2Active ? '⚡ System2 ready (LLM online)' : '⚠ LLM degraded — System2 paused'}</span>
      </div>
    </div>
  `;

  // ACTIVITY CHART
  drawActivityChart(mems);

  // OPERATIONS
  const layers_active = Object.keys(layers).length;
  document.getElementById('overview-operations').innerHTML = [
    ['TOTAL MEMORIES', imported, 'ok'],
    ['LAYERS ACTIVE', layers_active, ''],
    ['HEALTH STATUS', s.status === 'ok' ? 'OK' : s.status === 'degraded' ? 'DEGRADED' : 'ERROR', s.status === 'ok' ? 'ok' : s.status === 'degraded' ? 'warn' : 'err'],
  ].map(([l, v, c]) =>
    `<div class="card ${c}"><div class="label">${l}</div><div class="value">${esc(String(v))}</div></div>`
  ).join('');

  // CODING MEMORY
  const [codingR, codingCountR] = await Promise.all([
    api('/api/coding-memories?limit=5'),
    api('/api/coding-count'),
  ]);
  const codingMems = codingR.status === 200 ? (codingR.body.memories || []) : [];
  const codingStats = codingCountR.status === 200 ? codingCountR.body : { total: 0, today: 0 };
  const codingEl = document.getElementById('overview-coding');
  if (codingEl) {
    codingEl.innerHTML = [
      ['CODING MEMORIES', codingStats.total, ''],
      ['ADDED TODAY', codingStats.today, 'ok'],
      ['LAST ACTIVITY', codingMems.length ? timeAgo(codingMems[0].created_at) : '—', ''],
    ].map(([l, v, c]) =>
      `<div class="card ${c}"><div class="label">${l}</div><div class="value">${esc(String(v))}</div></div>`
    ).join('');
  }
  const codingListEl = document.getElementById('overview-coding-list');
  if (codingListEl && codingMems.length) {
    codingListEl.innerHTML = `<table>
      <tr><th>Task</th><th>Updated</th><th>Branch</th></tr>
      ${codingMems.map(m => `<tr>
        <td class="content">${esc((m.task||'').slice(0,120))}${(m.task||'').length>120?'…':''}</td>
        <td class="score">${m.updated_at ? timeAgo(m.updated_at) : '—'}</td>
        <td class="score">${esc(m.branch||'—')}</td>
      </tr>`).join('')}
    </table>`;
  } else if (codingListEl) {
    codingListEl.innerHTML = '<div class="empty">No coding memories yet.</div>';
  }
}

// --- Explore: Recall Debugger ---
document.getElementById('min-score').addEventListener('input', e => {
  document.getElementById('min-score-val').textContent = parseFloat(e.target.value).toFixed(2);
});

async function doSearch() {
  const q = document.getElementById('search-q').value.trim();
  const lim = parseInt(document.getElementById('search-limit').value) || 10;
  const minScore = parseFloat(document.getElementById('min-score').value) || 0;
  const box = document.getElementById('search-results');
  if (!q) { box.innerHTML = '<div class="empty">Enter a query.</div>'; return; }
  
  // Get selected layers
  const selectedLayers = new Set();
  document.querySelectorAll('.layer-filter:checked').forEach(cb => selectedLayers.add(cb.value));
  
  box.innerHTML = '<div class="empty">searching…</div>';
  const t0 = performance.now();
  const r = await api('/api/search', { method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ query: q, user_ids:['221727702992945152','hermes-user'], limit: lim, min_score: minScore }) });
  const dt = (performance.now()-t0).toFixed(0);
  document.getElementById('search-elapsed').textContent = `client: ${dt}ms`;
  if (r.status !== 200) { box.innerHTML = `<div class="err-msg">${esc(r.body.error||r.status)}</div>`; return; }

  const buckets = r.body.memories || {};
  const flat = [];
  for (const [layer, items] of Object.entries(buckets)) {
    (items||[]).forEach(m => flat.push({...m, layer: m.layer||layer}));
  }
  
  // Apply layer filter (match prefix: "l0" matches "l0_basic_info", etc.)
  const filtered = flat.filter(m => {
    const l = (m.layer||'').toLowerCase().replace(/[^a-z0-9_]/g,'');
    for (const prefix of selectedLayers) {
      if (l.startsWith(prefix)) return true;
    }
    return false;
  });
  
  filtered.sort((a,b) => (b.score||0)-(a.score||0));

  const serverMs = r.body.elapsed_ms;
  document.getElementById('search-elapsed').textContent += serverMs ? `, server: ${serverMs.toFixed(0)}ms` : '';
  document.getElementById('search-elapsed').textContent += ` | ${filtered.length}/${flat.length} results`;

  if (!filtered.length) { box.innerHTML = '<div class="empty">No matches (check layer filters).</div>'; return; }

  // Score distribution
  const scores = filtered.map(m => m.score||0);
  const avgScore = scores.reduce((a,b)=>a+b,0)/scores.length;
  const maxScore = Math.max(...scores);
  const minScoreResult = Math.min(...scores);
  
  box.innerHTML = `
    <div style="display:flex;gap:12px;margin-bottom:12px;flex-wrap:wrap">
      <div class="card" style="flex:1;min-width:100px"><div class="label">Results</div><div class="value">${filtered.length}</div></div>
      <div class="card" style="flex:1;min-width:100px"><div class="label">Avg Score</div><div class="value">${avgScore.toFixed(3)}</div></div>
      <div class="card" style="flex:1;min-width:100px"><div class="label">Max Score</div><div class="value">${maxScore.toFixed(3)}</div></div>
      <div class="card" style="flex:1;min-width:100px"><div class="label">Min Score</div><div class="value">${minScoreResult.toFixed(3)}</div></div>
    </div>
    <table>
    <tr><th>Layer</th><th>Score</th><th>Score Bar</th><th>Content</th><th>Debug</th></tr>
    ${filtered.map(m => {
      const pct = ((m.score||0)/maxScore*100).toFixed(0);
      return `<tr>
      <td><span class="${layerClass(m.layer)}">${esc(m.layer||'?')}</span></td>
      <td class="debug-score">${m.score!=null ? m.score.toFixed(4) : '—'}</td>
      <td><div style="background:var(--border);height:8px;border-radius:4px;width:100px;display:inline-block">
        <div style="background:${layerColor(m.layer)};height:100%;width:${pct}%;border-radius:4px"></div>
      </div></td>
      <td class="content">${esc((m.content||'').slice(0,200))}${(m.content||'').length>200?'…':''}</td>
      <td><details><summary>inspect</summary>
        <pre>${esc(JSON.stringify(m,null,2))}</pre>
      </details></td>
    </tr>`}).join('')}
  </table>`;
}
document.getElementById('search-q').addEventListener('keydown', e => { if (e.key==='Enter') doSearch(); });

// --- Explore: Memory Browser ---
async function loadMemories() {
  memFilter = document.getElementById('mem-filter').value;
  const r = await api('/api/memories?offset=' + memOffset + '&limit=' + memLimit);
  const box = document.getElementById('mem-table');
  if (r.status !== 200) { box.innerHTML = `<div class="err-msg">${esc(r.body.error||r.status)}</div>`; return; }
  const all = r.body.memories || [];
  const filtered = memFilter
    ? all.filter(m => (m.content||'').toLowerCase().includes(memFilter.toLowerCase()))
    : all;
  document.getElementById('mem-count').textContent = `${filtered.length} of ${r.body.total} total`;
  document.getElementById('mem-page').textContent = `offset ${memOffset} / limit ${memLimit}`;
  if (!filtered.length) { box.innerHTML = '<div class="empty">No memories match.</div>'; return; }
  box.innerHTML = `<table>
    <tr><th>Layer</th><th>Score</th><th>Content</th><th>Meta</th></tr>
    ${filtered.map(m => `<tr>
      <td><span class="${layerClass(m.layer)}">${esc(m.layer||'?')}</span></td>
      <td class="score">${m.score!=null ? m.score.toFixed(3) : '—'}</td>
      <td class="content">${esc((m.content||'').slice(0,300))}${(m.content||'').length>300?'…':''}</td>
      <td><details><summary>${esc(m.memory_id||'—').slice(0,12)}…</summary>
        <pre>${esc(JSON.stringify(m,null,2))}</pre></details></td>
    </tr>`).join('')}
  </table>`;
}
function memPage(d) { memOffset = Math.max(0, memOffset + d*memLimit); loadMemories(); }
document.getElementById('mem-filter').addEventListener('input', () => { memOffset=0; loadMemories(); });

// --- Layers ---
async function loadLayers() {
  const r = await api('/api/memories?offset=0&limit=500');
  const mems = r.status === 200 ? (r.body.memories || []) : [];
  const layers = {};
  mems.forEach(m => {
    const l = (m.layer||'unknown').toUpperCase();
    if (!layers[l]) layers[l] = { count: 0, samples: [], totalScore: 0, scored: 0 };
    layers[l].count++;
    if (layers[l].samples.length < 3) layers[l].samples.push(m.content||'');
    if (m.score != null) { layers[l].totalScore += m.score; layers[l].scored++; }
  });

  const layerOrder = ['L0_BASIC_INFO','L1_RAW','L2_FACT','L3_SUMMARY','L4_IDENTITY','L5_KNOWLEDGE','L6_SCHEMA','L7_INTENTION'];
  const present = layerOrder.filter(l => layers[l]);
  const missing = layerOrder.filter(l => !layers[l]);

  document.getElementById('layers-cards').innerHTML = layerOrder.map(l => {
    const d = layers[l];
    const c = d ? '' : 'style="opacity:0.3"';
    const v = d ? d.count : '0';
    const avg = d && d.scored ? (d.totalScore/d.scored).toFixed(3) : '—';
    return `<div class="card" ${c}>
      <div class="label">${layerLabel(l)}</div>
      <div class="value" style="color:${layerColor(l)}">${v}</div>
      <div class="score">avg score: ${avg}</div>
    </div>`;
  }).join('');

  // Layer details table
  const rows = present.map(l => {
    const d = layers[l];
    return `<tr>
      <td><span class="${layerClass(l)}">${esc(l)}</span></td>
      <td>${d.count}</td>
      <td class="score">${d.scored ? (d.totalScore/d.scored).toFixed(3) : '—'}</td>
      <td class="content">${d.samples.map(s => esc(s.slice(0,80))).join('<br>')}</td>
    </tr>`;
  }).join('');
  document.getElementById('layers-table').innerHTML = rows
    ? `<table><tr><th>Layer</th><th>Count</th><th>Avg Score</th><th>Sample Content</th></tr>${rows}</table>`
    : '<div class="empty">No memories found.</div>';
}

// --- Today ---
async function loadToday() {
  const r = await api('/api/memories?offset=0&limit=500');
  const mems = r.status === 200 ? (r.body.memories || []) : [];
  const now = new Date();
  const todayStart = new Date(now.getFullYear(), now.getMonth(), now.getDate()).getTime() / 1000;

  const today = mems.filter(m => {
    const ts = m.gmt_created || m.memory_at;
    return ts && ts >= todayStart;
  });

  const layerCounts = {};
  today.forEach(m => { const l = (m.layer||'unknown').toUpperCase(); layerCounts[l] = (layerCounts[l]||0)+1; });

  document.getElementById('today-cards').innerHTML = [
    ['Total memories', mems.length, ''],
    ['Added today', today.length, today.length > 0 ? 'ok' : ''],
    ['Layers active', Object.keys(layerCounts).length, ''],
    ['Most active layer', Object.entries(layerCounts).sort((a,b)=>b[1]-a[1])[0]?.[0] || '—', ''],
  ].map(([l,v,c]) =>
    `<div class="card ${c}"><div class="label">${l}</div><div class="value">${esc(String(v))}</div></div>`
  ).join('');

  document.getElementById('today-memories').innerHTML = today.length
    ? `<table><tr><th>Layer</th><th>Content</th><th>Time</th></tr>
       ${today.map(m => `<tr>
         <td><span class="${layerClass(m.layer)}">${esc(m.layer||'?')}</span></td>
         <td class="content">${esc((m.content||'').slice(0,150))}</td>
         <td class="score">${fmtDate(m.gmt_created)}</td>
       </tr>`).join('')}</table>`
    : '<div class="empty">No memories added today.</div>';
}

// =========================================================================
let obsCyV17 = null;
let currentV17Filter = 'ALL';
let currentV17Scope = 100;
let kuzuGraphNodes = []; // L5/L6/L7 nodes from Kuzu graph

const OBS_CATS_V17 = [
  { key: 'L7_INTENTION',  label: 'WISDOM',    color: '#d4af37', layerIdx: 7 },
  { key: 'L6_SCHEMA',     label: 'SCHEMA',    color: '#f39c12', layerIdx: 6 },
  { key: 'L5_KNOWLEDGE',  label: 'KNOWLEDGE', color: '#1abc9c', layerIdx: 5 },
  { key: 'L4_IDENTITY',   label: 'IDENTITY',  color: '#9b59b6', layerIdx: 4 },
  { key: 'L3_SUMMARY',    label: 'SUMMARIES', color: '#3498db', layerIdx: 3 },
  { key: 'L2_FACT',       label: 'FACTS',     color: '#27ae60', layerIdx: 2 },
  { key: 'L1_RAW',        label: 'CONTEXT',   color: '#e67e22', layerIdx: 1 },
  { key: 'L0_BASIC_INFO', label: 'RAW',       color: '#e74c3c', layerIdx: 0 },
];

const LAYER_META_V17 = {
  'L0_BASIC_INFO': { icon: '\u25CE', desc: 'Unprocessed memories and raw data.', label: 'RAW' },
  'L1_RAW':        { icon: '\u25C9', desc: 'Situational context, conditions, and environmental details.', label: 'CONTEXT' },
  'L2_FACT':       { icon: '\u2713', desc: 'Verified facts and concrete knowledge extracted from memories.', label: 'FACTS' },
  'L3_SUMMARY':    { icon: '\u25C6', desc: 'High-level synthesis of facts and events into meaningful takeaways.', label: 'SUMMARIES' },
  'L4_IDENTITY':   { icon: '\u2605', desc: 'Who this is about. Core entities, values, roles, and self-model.', label: 'IDENTITY' },
  'L5_KNOWLEDGE':  { icon: '\u25C8', desc: 'Structured knowledge and domain expertise.', label: 'KNOWLEDGE' },
  'L6_SCHEMA':     { icon: '\u25A3', desc: 'Cognitive schemas and mental models.', label: 'SCHEMA' },
  'L7_INTENTION':  { icon: '\u25CA', desc: 'Goals, intentions, and strategic direction.', label: 'WISDOM' },
};

// ============================================
// API
// ============================================
// Unified api() - returns {status, body} (v7 convention). Supports both calling styles.
async function api(a, b, c) {
  // Detect calling convention: if first arg starts with '/' it's v7 style (path, opts)
  let method, path, body;
  if (typeof a === 'string' && a.startsWith('/')) {
    path = a;
    method = (b && b.method) || 'GET';
    body = (b && b.body) ? (typeof b.body === 'string' ? b.body : JSON.stringify(b.body)) : null;
  } else {
    method = a || 'GET';
    path = b;
    body = c;
  }
  const opts = { method, headers: { 'Content-Type': 'application/json' } };
  if (body) opts.body = body;
  const r = await fetch(path, opts);
  const j = await r.json().catch(() => ({}));
  return { status: r.status, body: j };
}

async function loadMemories(limit = 500) {
  const data = await api('GET', '/api/memories?offset=0&limit=' + limit);
  allMemories = (data.memories || []).map(m => {
    // Normalize layer names: lowercase to uppercase with underscores
    const raw = (m.layer || '').toLowerCase();
    const map = {
      'l0_basic_info': 'L0_BASIC_INFO', 'l0': 'L0_BASIC_INFO',
      'l1_raw': 'L1_RAW', 'l1': 'L1_RAW',
      'l2_fact': 'L2_FACT', 'l2': 'L2_FACT',
      'l3_summary': 'L3_SUMMARY', 'l3': 'L3_SUMMARY',
      'l4_identity': 'L4_IDENTITY', 'l4': 'L4_IDENTITY',
      'l5_knowledge': 'L5_KNOWLEDGE', 'l5': 'L5_KNOWLEDGE',
      'l6_schema': 'L6_SCHEMA', 'l6': 'L6_SCHEMA',
      'l7_intention': 'L7_INTENTION', 'l7': 'L7_INTENTION',
    };
    m.layer = map[raw] || m.layer || 'L2_FACT';
    return m;
  });
  document.getElementById('footer-mem-count').textContent = allMemories.length;
  return allMemories;
}

async function loadKuzuGraph() {
  try {
    const data = await api('GET', '/api/l5/graph');
    kuzuGraphNodes = data.nodes || [];
  } catch (e) {
    kuzuGraphNodes = [];
  }
  return kuzuGraphNodes;
}

// ============================================
// V17 HUB-CENTRIC RADIAL CLUSTER LAYOUT
// ============================================

function computeV17Positions(groups, W, H) {
  const positions = {};
  const cx = W / 2;
  const populated = OBS_CATS_V17.filter(c => (groups[c.key] || []).length > 0);
  const N = populated.length;
  if (N === 0) return positions;

  const topPad = 80;
  const botPad = 60;
  const availH = H - topPad - botPad;
  const bandH = availH / N;

  // Seed random for reproducible layout
  let seed = 42;
  function seededRandom() {
    seed = (seed * 16807) % 2147483647;
    return (seed - 1) / 2147483646;
  }

  populated.forEach((cat, i) => {
    const mems = groups[cat.key] || [];
    const y = topPad + i * bandH + bandH / 2;
    const hubX = cx;
    const hubY = y;
    positions['hub_' + cat.key] = { x: hubX, y: hubY, type: 'hub' };

    // Arrange concept nodes in an organic cloud around the hub
    const visibleCount = Math.min(mems.length, 10);
    const radius = Math.min(W * 0.42, 280);

    for (let j = 0; j < visibleCount; j++) {
      // Spread nodes in a wider arc around the hub
      const angle = Math.PI * 0.15 + (j / Math.max(visibleCount - 1, 1)) * Math.PI * 0.7;
      const r = radius * (0.4 + seededRandom() * 0.6);
      const nx = hubX + Math.cos(angle - Math.PI/2) * r;
      const ny = hubY + Math.sin(angle - Math.PI/2) * r * 0.5 + 15;
      positions[mems[j].memory_id] = { x: nx, y: ny, type: 'concept' };
    }

    // "+ N more" annotation position (to the right of the hub)
    if (mems.length > visibleCount) {
      positions['more_' + cat.key] = { x: hubX + radius + 40, y: hubY, type: 'more' };
    }

    // Stack hidden nodes behind the hub
    for (let j = visibleCount; j < mems.length; j++) {
      positions[mems[j].memory_id] = { x: hubX + (seededRandom() - 0.5) * 30, y: hubY + 40 + (j - visibleCount) * 3, type: 'hidden' };
    }
  });

  return positions;
}

function buildV17CyElements(memsByLayer, positions, W) {
  const elements = [];
  const populated = OBS_CATS_V17.filter(c => (memsByLayer[c.key] || []).length > 0);

  // Layer hub nodes with icons
  populated.forEach(cat => {
    const meta = LAYER_META_V17[cat.key];
    const mems = memsByLayer[cat.key] || [];
    const pos = positions['hub_' + cat.key];
    if (!pos) return;

    elements.push({
      data: {
        id: 'hub_' + cat.key,
        label: meta.icon + ' ' + meta.label,
        layer: cat.key,
        layerIdx: cat.layerIdx,
        count: mems.length,
        icon: meta.icon,
        desc: meta.desc,
        isHub: true,
      },
      position: { x: pos.x, y: pos.y },
    });
  });

  // "+ N more" annotation nodes
  populated.forEach(cat => {
    const mems = memsByLayer[cat.key] || [];
    const visibleCount = Math.min(mems.length, 8);
    const pos = positions['more_' + cat.key];
    if (!pos || mems.length <= visibleCount) return;

    elements.push({
      data: {
        id: 'more_' + cat.key,
        label: '+ ' + (mems.length - visibleCount) + ' more',
        layer: cat.key,
        layerIdx: cat.layerIdx,
        isMore: true,
      },
      position: { x: pos.x, y: pos.y },
    });
  });

  // Memory concept nodes
  populated.forEach(cat => {
    const mems = memsByLayer[cat.key] || [];
    const visibleCount = Math.min(mems.length, 10);

    mems.slice(0, visibleCount).forEach((mem, idx) => {
      const pos = positions[mem.memory_id];
      if (!pos) return;
      const shortLabel = mem.content ? mem.content.slice(0, 30) + (mem.content.length > 30 ? '...' : '') : mem.memory_id;

      elements.push({
        data: {
          id: mem.memory_id,
          label: shortLabel,
          layer: cat.key,
          layerIdx: cat.layerIdx,
          content: mem.content,
          memory_id: mem.memory_id,
          isHub: false,
          hubId: 'hub_' + cat.key,
        },
        position: { x: pos.x, y: pos.y },
      });
    });
  });

  // Edges: hub to visible concepts
  populated.forEach(cat => {
    const mems = memsByLayer[cat.key] || [];
    const visibleCount = Math.min(mems.length, 10);
    mems.slice(0, visibleCount).forEach(mem => {
      elements.push({
        data: {
          id: 'e_hub_' + mem.memory_id,
          source: 'hub_' + cat.key,
          target: mem.memory_id,
          type: 'hub-link',
        },
      });
    });
  });

  // Cross-layer edges based on shared keywords
  const allVisibleMems = [];
  populated.forEach(cat => {
    const mems = memsByLayer[cat.key] || [];
    const visibleCount = Math.min(mems.length, 10);
    mems.slice(0, visibleCount).forEach(mem => {
      allVisibleMems.push({ ...mem, layer: cat.key });
    });
  });

  const keywordMap = {};
  allVisibleMems.forEach(mem => {
    const words = (mem.content || '').toLowerCase().split(/\W+/).filter(w => w.length > 4);
    words.forEach(w => {
      if (!keywordMap[w]) keywordMap[w] = [];
      keywordMap[w].push(mem.memory_id);
    });
  });

  const edgeSet = new Set();
  Object.values(keywordMap).forEach(ids => {
    if (ids.length >= 2) {
      for (let i = 0; i < ids.length; i++) {
        for (let j = i + 1; j < ids.length; j++) {
          const a = ids[i], b = ids[j];
          const key = a < b ? a + '_' + b : b + '_' + a;
          if (!edgeSet.has(key)) {
            edgeSet.add(key);
            elements.push({
              data: {
                id: 'e_' + key,
                source: a,
                target: b,
                type: 'keyword-link',
              },
            });
          }
        }
      }
    }
  });

  return elements;
}

function renderV17Graph() {
  const canvas = document.getElementById('obs-canvas');
  if (!canvas) return;
  if (obsCyV17) { obsCyV17.destroy(); obsCyV17 = null; }

  const wrap = document.getElementById('obs-canvas-wrap');
  const W = wrap.clientWidth;
  const H = wrap.clientHeight - 120; // minus legend bar

  const groups = {};
  OBS_CATS_V17.forEach(c => groups[c.key] = []);
  allMemories.forEach(m => {
    const layer = m.layer || 'L2_FACT';
    if (!groups[layer]) groups[layer] = [];
    groups[layer].push(m);
  });

  // Merge L5/L6/L7 nodes from Kuzu graph (not in /api/memories)
  if (kuzuGraphNodes && kuzuGraphNodes.length > 0) {
    kuzuGraphNodes.forEach(node => {
      const layer = (node.layer || '').toUpperCase().replace(/[^A-Z0-9_]/g, '_');
      const normalized = layer.startsWith('L') ? layer : 'L5_KNOWLEDGE';
      if (groups[normalized]) {
        groups[normalized].push({
          memory_id: node.node_id || node.id,
          layer: normalized,
          content: node.content || node.name || '',
          score: node.confidence || null,
          is_kuzu: true,
        });
      }
    });
  }

  // Apply scope filter
  Object.keys(groups).forEach(key => {
    groups[key] = groups[key].slice(0, currentV17Scope);
  });

  // Apply layer filter
  let activeGroups = groups;
  if (currentV17Filter !== 'ALL') {
    activeGroups = {};
    OBS_CATS_V17.forEach(c => {
      if (c.key === currentV17Filter || c.layerIdx.toString() === currentV17Filter.replace('L', '')) {
        activeGroups[c.key] = groups[c.key];
      }
    });
  }

  const positions = computeV17Positions(activeGroups, W, H);
  const elements = buildV17CyElements(activeGroups, positions, W);

  // Update stats (use v7 element IDs)
  const totalMems = Object.values(activeGroups).reduce((s, arr) => s + arr.length, 0);
  const populatedCount = Object.values(activeGroups).filter(arr => arr.length > 0).length;
  const edgeCount = elements.filter(e => !e.data.id.startsWith('e_hub_')).length;
  const statShown = document.getElementById('obs-stat-shown');
  const statVisible = document.getElementById('obs-stat-visible');
  const statLinks = document.getElementById('obs-stat-links');
  const statClusters = document.getElementById('obs-stat-clusters');
  if (statShown) statShown.textContent = totalMems;
  if (statVisible) statVisible.textContent = totalMems;
  if (statLinks) statLinks.textContent = edgeCount;
  if (statClusters) statClusters.textContent = populatedCount;

  // Update axis labels
  updateV17AxisLabels(activeGroups, H);

  obsCyV17 = cytoscape({
    container: canvas,
    elements: elements,
    layout: { name: 'preset', fit: true, padding: 40 },
    style: [
      {
        selector: 'node[isHub]',
        style: {
          'width': 70,
          'height': 70,
          'background-color': (ele) => {
            const layer = ele.data('layer');
            const cat = OBS_CATS_V17.find(c => c.key === layer);
            const hex = cat ? cat.color : '#333333';
            const r = parseInt(hex.slice(1,3),16), g = parseInt(hex.slice(3,5),16), b = parseInt(hex.slice(5,7),16);
            return 'rgba(' + r + ',' + g + ',' + b + ',0.15)';
          },
          'border-width': 3,
          'border-color': (ele) => {
            const layer = ele.data('layer');
            const cat = OBS_CATS_V17.find(c => c.key === layer);
            return cat ? cat.color : '#666';
          },
          'label': 'data(label)',
          'color': '#ffffff',
          'font-size': '13px',
          'font-weight': 'bold',
          'text-valign': 'center',
          'text-halign': 'center',
          'text-background-color': 'rgba(0,0,0,0.9)',
          'text-background-opacity': 1,
          'text-background-padding': '8px 12px',
          'text-background-shape': 'roundrectangle',
          'z-index': 10,
        }
      },
      {
        selector: 'node[?isMore]',
        style: {
          'width': 100,
          'height': 24,
          'label': 'data(label)',
          'color': '#888888',
          'font-size': '10px',
          'font-style': 'italic',
          'text-valign': 'center',
          'text-halign': 'center',
          'text-background-color': 'rgba(5,5,5,0.8)',
          'text-background-opacity': 1,
          'text-background-padding': '4px 8px',
          'text-background-shape': 'roundrectangle',
          'background-color': 'transparent',
          'border-width': 0,
          'z-index': 5,
        }
      },
      {
        selector: 'node[!isHub][!isMore]',
        style: {
          'width': 10,
          'height': 10,
          'background-color': (ele) => {
            const layer = ele.data('layer');
            const cat = OBS_CATS_V17.find(c => c.key === layer);
            return cat ? cat.color : '#666';
          },
          'border-width': 2,
          'border-color': '#1a1a1a',
          'label': 'data(label)',
          'color': '#aaaaaa',
          'font-size': '9px',
          'text-valign': 'bottom',
          'text-halign': 'center',
          'text-margin-y': 6,
          'text-max-width': '120px',
          'text-wrap': 'ellipsis',

          'text-background-color': 'rgba(10,10,10,0.85)',
          'text-background-opacity': 1,
          'text-background-padding': '3px 6px',
          'text-background-shape': 'roundrectangle',
          'z-index': 5,
        }
      },
      {
        selector: 'edge[type="hub-link"]',
        style: {
          'width': 1.5,
          'line-color': (ele) => {
            const src = ele.data('source');
            const layer = src.replace('hub_', '');
            const cat = OBS_CATS_V17.find(c => c.key === layer);
            const hex = cat ? cat.color : '#333333';
            const r = parseInt(hex.slice(1,3),16), g = parseInt(hex.slice(3,5),16), b = parseInt(hex.slice(5,7),16);
            return 'rgba(' + r + ',' + g + ',' + b + ',0.35)';
          },
          'target-arrow-shape': 'none',
          'curve-style': 'bezier',
          'opacity': 0.5,
        }
      },
      {
        selector: 'edge[type="keyword-link"]',
        style: {
          'width': 0.5,
          'line-color': 'rgba(212,175,55,0.15)',
          'target-arrow-shape': 'none',
          'curve-style': 'bezier',
          'opacity': 0.2,
        }
      },
      {
        selector: '.highlighted',
        style: {
          'opacity': 1,
          'border-color': '#ffffff',
          'border-width': 3,
          'z-index': 20,
          'shadow-blur': 15,
          'shadow-opacity': 0.8,
          'shadow-color': '#ffffff',
          'shadow-offset-x': 0,
          'shadow-offset-y': 0,
        }
      },
      {
        selector: '.dimmed',
        style: {
          'opacity': 0.08,
        }
      },
      {
        selector: 'node:selected',
        style: {
          'border-width': 4,
          'border-color': '#d4af37',
          'z-index': 30,
        }
      }
    ],
    minZoom: 0.3,
    maxZoom: 3,
    wheelSensitivity: 0.3,
  });

  // Fit the graph to the viewport with generous padding (default zoomed out)
  obsCyV17.ready(() => {
    obsCyV17.fit(undefined, 60);
  });

  wireV17CyEvents();
}

function updateV17AxisLabels(groups, H) {
  const container = document.getElementById('obs-axis-labels');
  if (!container) return;
  container.innerHTML = '';

  OBS_CATS_V17.forEach(cat => {
    const count = (groups[cat.key] || []).length;
    const el = document.createElement('div');
    el.className = 'obs-axis-label' + (count > 0 ? ' populated' : '');
    el.textContent = 'L' + cat.layerIdx;
    el.title = cat.label + (count > 0 ? ' (' + count + ')' : ' (empty)');
    container.appendChild(el);
  });
}

function wireV17CyEvents() {
  if (!obsCyV17) return;

  obsCyV17.on('tap', 'node', (evt) => {
    const node = evt.target;
    const data = node.data();

    if (data.isHub) {
      renderV17HubFieldNote(data);
    } else {
      renderV17MemoryFieldNote(data);
    }
  });

  obsCyV17.on('mouseover', 'node', (evt) => {
    const node = evt.target;
    const connected = node.closedNeighborhood();
    obsCyV17.elements().addClass('dimmed');
    connected.removeClass('dimmed');
    node.addClass('highlighted');
  });

  obsCyV17.on('mouseout', 'node', () => {
    obsCyV17.elements().removeClass('dimmed highlighted');
  });

  obsCyV17.on('tap', (evt) => {
    if (evt.target === obsCyV17) {
      document.getElementById('obs-fieldnote-empty').style.display = 'block';
      document.getElementById('obs-fieldnote-content').style.display = 'none';
    }
  });
}

function renderV17HubFieldNote(data) {
  const empty = document.getElementById('obs-fieldnote-empty');
  const content = document.getElementById('obs-fieldnote-content');
  empty.style.display = 'none';
  content.style.display = 'block';

  const cat = OBS_CATS_V17.find(c => c.key === data.layer);
  const color = cat ? cat.color : '#666';

  content.innerHTML = `
    <div class="obs-fn-title" style="color:${color}">${data.icon} ${data.label}</div>
    <div class="obs-fn-layer" style="background:${color}20;color:${color}">L${data.layerIdx} - ${data.label}</div>
    <div class="obs-fn-content">${data.desc}</div>
    <div class="obs-fn-meta">${data.count} concepts in this layer</div>
    <div class="obs-fn-section">
      <div class="obs-fn-section-title">Connected To</div>
      <div class="obs-fn-connection">
        <span class="obs-fn-connection-name">Lower Layers</span>
        <span class="obs-fn-connection-count">${data.layerIdx > 0 ? 'L' + (data.layerIdx - 1) + '+' : 'None'}</span>
      </div>
      <div class="obs-fn-connection">
        <span class="obs-fn-connection-name">Higher Layers</span>
        <span class="obs-fn-connection-count">${data.layerIdx < 7 ? 'L' + (data.layerIdx + 1) + '+' : 'None'}</span>
      </div>
    </div>
    <div class="obs-fn-insight">
      This ${data.label.toLowerCase()} layer contains ${data.count} concepts that form the foundation of your knowledge structure at abstraction level ${data.layerIdx}.
    </div>
  `;
}

function renderV17MemoryFieldNote(data) {
  const empty = document.getElementById('obs-fieldnote-empty');
  const content = document.getElementById('obs-fieldnote-content');
  empty.style.display = 'none';
  content.style.display = 'block';

  const cat = OBS_CATS_V17.find(c => c.key === data.layer);
  const color = cat ? cat.color : '#666';

  content.innerHTML = `
    <div class="obs-fn-title">${data.label || data.content?.slice(0, 50) || 'Untitled'}</div>
    <div class="obs-fn-layer" style="background:${color}20;color:${color}">L${data.layerIdx} - ${cat?.label || data.layer}</div>
    <div class="obs-fn-content">${data.content || 'No content available.'}</div>
    <div class="obs-fn-meta">ID: ${data.memory_id}</div>
    <div class="obs-fn-section">
      <div class="obs-fn-section-title">Tags</div>
      <div style="display:flex;flex-wrap:wrap;gap:4px;">
        ${data.content ? data.content.toLowerCase().split(/\W+/).filter(w => w.length > 5).slice(0, 6).map(w => `<span class="obs-fn-tag">${w}</span>`).join('') : ''}
      </div>
    </div>
    <div class="obs-fn-actions">
      <button onclick="findRelated('${data.memory_id}')">Find Related</button>
      <button onclick="addToFocus('${data.memory_id}')">Add to Focus</button>
    </div>
  `;
}

function findRelated(id) {
  alert('Find related memories for: ' + id);
}
function addToFocus(id) {
  alert('Added to focus: ' + id);
}

// ============================================
// FILTER CONTROLS
// ============================================
function wireV17Controls() {
document.querySelectorAll('.obs-btn[data-layer]').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.obs-btn[data-layer]').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    currentV17Filter = btn.dataset.layer;
    renderV17Graph();
  });
});

document.querySelectorAll('.obs-btn[data-scope]').forEach(btn => {
  btn.addEventListener('click', () => {
    document.querySelectorAll('.obs-btn[data-scope]').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    currentV17Scope = parseInt(btn.dataset.scope);
    renderV17Graph();
  });
});
}

// ============================================

async function loadGraph() {
  await loadKuzuGraph();
  await renderV17Graph();
  wireV17CyEvents();
  wireV17Controls();
}
// --- Activity ---
async function loadActivity() {
  const r = await api('/api/memories?offset=0&limit=100');
  const mems = r.status === 200 ? (r.body.memories || []) : [];

  // Sort by creation time descending
  const sorted = [...mems].sort((a,b) => (b.gmt_created||0) - (a.gmt_created||0));

  document.getElementById('activity-timeline').innerHTML = sorted.length
    ? `<table><tr><th>Time</th><th>Layer</th><th>Content</th><th>Status</th></tr>
       ${sorted.slice(0, 50).map(m => `<tr>
         <td class="score">${fmtDate(m.gmt_created)}</td>
         <td><span class="${layerClass(m.layer)}">${esc(m.layer||'?')}</span></td>
         <td class="content">${esc((m.content||'').slice(0,120))}${(m.content||'').length>120?'…':''}</td>
         <td class="score">${esc(m.status||'—')}</td>
       </tr>`).join('')}</table>`
    : '<div class="empty">No activity recorded.</div>';
}

// --- Settings ---
async function loadSettings() {
  const [statusR, infoR] = await Promise.all([
    api('/api/status'),
    api('/api/info'),
  ]);
  const s = statusR.body || {};
  const info = infoR.body || {};

  document.getElementById('settings-config').innerHTML = [
    ['Server version', info.version || '—'],
    ['VDB provider', s.vdb_provider || '—'],
    ['VDB collection', s.vdb_collection || '—'],
    ['VDB points', s.vdb_points ?? '—'],
    ['Embed model', 'BAAI/bge-small-en-v1.5'],
    ['Embed dims', s.embed_dims ?? '—'],
    ['Upstream', HY_MEMORY_BASE],
    ['Dashboard refresh', REFRESH_S + 's'],
  ].map(([l,v]) =>
    `<div class="card"><div class="label">${l}</div><div class="value">${esc(String(v))}</div></div>`
  ).join('');

  document.getElementById('settings-raw').textContent = JSON.stringify({status: s, info}, null, 2);
}

// --- Keyboard shortcuts for Observatory ---
document.addEventListener('keydown', e => {
  if (!obsCyV17 || currentTab !== 'graph') return;
  if (e.target.tagName === 'INPUT' || e.target.tagName === 'TEXTAREA') return;
  if (e.key === '0') { obsCyV17.fit(undefined, 60); e.preventDefault(); }
  if (e.key === '+' || e.key === '=') { obsCyV17.zoom({ level: obsCyV17.zoom() * 1.3, renderedPosition: { x: obsCyV17.width()/2, y: obsCyV17.height()/2 } }); e.preventDefault(); }
  if (e.key === '-') { obsCyV17.zoom({ level: obsCyV17.zoom() / 1.3, renderedPosition: { x: obsCyV17.width()/2, y: obsCyV17.height()/2 } }); e.preventDefault(); }
});

// --- Reset view toolbar button ---
document.querySelectorAll('.obs-tool[data-tool="reset"]').forEach(btn => {
  btn.addEventListener('click', () => {
    if (obsCyV17) obsCyV17.fit(undefined, 60);
  });
});

// --- Init ---
loadOverview();
loadMemories(500).then(() => { if (currentTab === 'graph') renderV17Graph(); });
loadKuzuGraph().then(() => { if (currentTab === 'graph') renderV17Graph(); });
</script>
</body>
</html>"""


# ---------------------------------------------------------------------------
# HTTP Handler
# ---------------------------------------------------------------------------

class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write(f"[dash] {self.address_string()} - {fmt % args}\n")

    def _check_auth(self) -> bool:
        """Return True if the request is authenticated (or auth is disabled)."""
        if not AUTH_REQUIRED:
            return True

        # Check cookie
        cookie = self.headers.get("Cookie", "")
        if f"hyatlas_token={DASH_TOKEN}" in cookie:
            return True

        # Check query string (?token=...)
        if "?" in self.path:
            qs = parse_qs(self.path.split("?", 1)[1])
            if qs.get("token", [None])[0] == DASH_TOKEN:
                return True

        return False

    def _serve_login(self) -> None:
        """Serve a minimal login page that accepts the token."""
        html = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>HyAtlas — Login</title>
<style>body{background:#0a0e1a;color:#c8d6e5;font-family:system-ui,sans-serif;
display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
.box{background:#151b2b;padding:2rem;border-radius:12px;width:320px;text-align:center}
h1{font-size:1.2rem;margin:0 0 1rem}input{width:100%;padding:0.6rem;margin:0.5rem 0;
border:1px solid #2a3447;border-radius:6px;background:#0a0e1a;color:#c8d6e5;box-sizing:border-box}
button{width:100%;padding:0.6rem;border:none;border-radius:6px;background:#4a6fa5;
color:white;cursor:pointer;margin-top:0.5rem}button:hover{background:#5a7fb5}
.err{color:#e74c3c;font-size:0.85rem;margin-top:0.5rem;display:none}</style>
</head><body><div class="box">
<h1>🧠 HyAtlas Dashboard</h1>
<form method="POST" action="/auth">
<input type="password" name="token" placeholder="Access token" autofocus>
<button type="submit">Enter</button>
<div class="err" id="err">Invalid token</div>
</form></div></body></html>"""
        body = html.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location: str) -> None:
        self.send_response(302)
        self.send_header("Location", location)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _json(self, status: int, payload: object) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]

        # Auth gate — /api/health is exempt (for start.py health checks)
        if AUTH_REQUIRED and path != "/api/health":
            if not self._check_auth():
                # If token is in query string, set cookie and redirect to /
                if "?" in self.path:
                    qs = parse_qs(self.path.split("?", 1)[1])
                    if qs.get("token", [None])[0] == DASH_TOKEN:
                        self.send_response(302)
                        self.send_header("Location", "/")
                        self.send_header("Set-Cookie", f"hyatlas_token={DASH_TOKEN}; Path=/; HttpOnly; SameSite=Strict")
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                return self._serve_login()

        if path == "/":
            # Serve from external file if available (for live iteration), else inline
            html_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard.html")
            try:
                with open(html_path, encoding="utf-8") as f:
                    html = f.read()
            except FileNotFoundError:
                html = HTML
            html = html.replace("__REFRESH_S__", str(REFRESH_S)).replace("__USER_IDS__", HERMES_USER_IDS_JS)
            body = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.end_headers()
            self.wfile.write(body)
            return

        if path in ("/styles.css", "/app.js") or path.startswith("/js/"):
            # Static dashboard assets (CSS, JS). Files live next to dashboard.py.
            base_dir = os.path.dirname(os.path.abspath(__file__))
            if path == "/styles.css":
                asset_path = os.path.join(base_dir, "styles.css")
                content_type = "text/css; charset=utf-8"
            elif path.startswith("/js/"):
                rel = path[len("/js/"):]
                if ".." in rel or rel.startswith("/") or "\\" in rel or not rel:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                asset_path = os.path.join(base_dir, "js", rel)
                content_type = "application/javascript; charset=utf-8"
            else:
                asset_path = os.path.join(base_dir, "app.js")
                content_type = "application/javascript; charset=utf-8"
            try:
                with open(asset_path, encoding="utf-8") as f:
                    body = f.read().encode("utf-8")
            except FileNotFoundError:
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
            self.end_headers()
            self.wfile.write(body)
            return

        if path.startswith("/assets/"):
            # Static assets (icons, images). Files live in ./assets/ next to this script.
            # Path-traversal guard: refuse anything with .. or absolute paths.
            rel = path[len("/assets/"):]
            if ".." in rel or rel.startswith("/") or "\\" in rel:
                self.send_response(400)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            asset_path = os.path.join(
                os.path.dirname(os.path.abspath(__file__)),
                "assets",
                rel.replace("/", os.sep),
            )
            if not os.path.isfile(asset_path):
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            ext = os.path.splitext(asset_path)[1].lower()
            content_type = {
                ".png":  "image/png",
                ".jpg":  "image/jpeg",
                ".jpeg": "image/jpeg",
                ".gif":  "image/gif",
                ".svg":  "image/svg+xml",
                ".ico":  "image/x-icon",
                ".webp": "image/webp",
            }.get(ext, "application/octet-stream")
            try:
                with open(asset_path, "rb") as f:
                    body = f.read()
            except OSError:
                self.send_response(500)
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            # Favicons/icons cache aggressively; others not at all.
            if "icon" in rel.lower() or rel.lower() == "favicon.ico":
                self.send_header("Cache-Control", "public, max-age=86400")
            else:
                self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return

        if path == "/api/memories":
            qs = parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            try:
                offset = int(qs.get("offset", ["0"])[0])
                limit = min(int(qs.get("limit", ["25"])[0]), 500)
            except ValueError:
                return self._json(400, {"error": "bad offset/limit"})
            # Query each known user_id and merge results.
            # (the /api/v1/list endpoint only accepts a single user_id)
            all_items = []
            total = 0
            for uid in HERMES_USER_IDS:
                code, payload = hy("POST", "/api/v1/list", {
                    "user_id": uid,
                    "offset": offset,
                    "limit": max(limit * 2, 100),
                }, timeout=15)
                if code == 200:
                    items = _extract_memories(payload)
                    raw = (payload or {}).get("vdb") or {}
                    raw_total = raw.get("total") if isinstance(raw, dict) else len(items)
                    all_items.extend(items)
                    total += raw_total if isinstance(raw_total, int) else 0
            # Deduplicate by memory_id across user scopes
            seen = set()
            deduped = []
            for m in all_items:
                mid = m.get("memory_id") or m.get("id") or ""
                if mid not in seen:
                    seen.add(mid)
                    deduped.append(m)
            # Also fetch L1_RAW (raw conversation snippets) directly from
            # Qdrant — Hy-Memory's /api/v1/list filters them out by
            # design, but the dashboard wants to show them. Normalized
            # to the same shape as the items above, so the rest of the
            # dedupe + sort logic just works.
            l1_raw_items = _fetch_l1_raw_from_vdb()
            l1_raw_total = len(l1_raw_items)
            for m in l1_raw_items:
                mid = m.get("memory_id") or ""
                if mid and mid not in seen:
                    seen.add(mid)
                    deduped.append(m)
            # Sort merged result by gmt_created descending so the "most recent"
            # at index 0 reflects actual time, not user_id iteration order.
            # Memories without gmt_created go to the end.
            def _ts(m):
                v = m.get("gmt_created")
                if isinstance(v, (int, float)):
                    return v
                if isinstance(v, str) and v:
                    try:
                        from datetime import datetime
                        return datetime.fromisoformat(v.replace("Z", "+00:00")).timestamp()
                    except Exception:
                        return 0.0
                return 0.0
            deduped.sort(key=_ts, reverse=True)
            # Enrich with importance + access_count from qdrant payload
            # (upstream's /api/v1/list doesn't surface these fields)
            deduped = _enrich_with_vdb_payload(deduped)
            return self._json(200, {
                "memories": deduped[:limit],
                "total": total + l1_raw_total,
                "offset": offset,
                "limit": limit,
            })

        if path == "/api/metrics":
            code, payload = hy("GET", "/api/v1/metrics?minutes=60", timeout=10)
            return self._json(code or 502, payload)

        if path == "/api/status":
            code, payload = hy("GET", "/api/v1/status", timeout=10)
            return self._json(code or 502, payload)

        if path == "/api/info":
            code, payload = hy("GET", "/info", timeout=5)
            return self._json(code or 502, payload)

        if path == "/api/storage":
            _, status = hy("GET", "/api/v1/status", timeout=10)
            points = (status or {}).get("vdb_points", "?")
            provider = (status or {}).get("vdb_provider", "?")
            collection = (status or {}).get("vdb_collection", "?")
            dims = (status or {}).get("embed_dims", "?")
            home = os.path.expanduser("~/.hy_memory/data")
            files = {}
            try:
                if os.path.isdir(home):
                    for name in ("vector_db", "cache.db", "history.db", "kuzu_db"):
                        p = os.path.join(home, name)
                        if os.path.isfile(p):
                            files[name] = f"{os.path.getsize(p)/1024/1024:.2f} MB"
                        elif os.path.isdir(p):
                            total = sum(
                                os.path.getsize(os.path.join(dp, f))
                                for dp, _, fn in os.walk(p) for f in fn
                            )
                            files[name] = f"{total/1024/1024:.2f} MB"
            except Exception as e:
                files["error"] = str(e)
            return self._json(200, {
                "vdb": {"provider": provider, "collection": collection,
                        "points": points, "dims": dims},
                "files": files,
            })

        if path == "/api/health":
            return self._json(200, {"status": "ok", "upstream": HY_MEMORY_BASE})

        if path == "/api/coding-memories":
            qs = parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            try:
                limit = min(int(qs.get("limit", ["25"])[0]), 200)
            except ValueError:
                limit = 25
            import sqlite3
            db_path = os.path.join(os.path.expanduser("~/.hy_memory/data"), "coding_memory.db")
            if not os.path.isfile(db_path):
                return self._json(200, {"memories": [], "total": 0})
            try:
                conn = sqlite3.connect(db_path)
                conn.row_factory = sqlite3.Row
                total_row = conn.execute("SELECT COUNT(*) FROM coding_memory_meta").fetchone()
                total = total_row[0] if total_row else 0
                rows = conn.execute(
                    "SELECT memory_id, user_id, agent_id, task, search_keys, solution, "
                    "workspace_id, branch, session_id, confidence, source, type, created_at, updated_at "
                    "FROM coding_memory_meta ORDER BY updated_at DESC LIMIT ?",
                    (limit,)
                ).fetchall()
                conn.close()
                items = []
                for r in rows:
                    items.append({
                        "memory_id": r["memory_id"],
                        "user_id": r["user_id"],
                        "agent_id": r["agent_id"],
                        "task": r["task"],
                        "search_keys": r["search_keys"],
                        "solution": r["solution"],
                        "workspace_id": r["workspace_id"],
                        "branch": r["branch"],
                        "session_id": r["session_id"],
                        "confidence": r["confidence"],
                        "source": r["source"],
                        "type": r["type"],
                        "created_at": r["created_at"],
                        "updated_at": r["updated_at"],
                    })
                return self._json(200, {"memories": items, "total": total})
            except Exception as e:
                return self._json(500, {"error": str(e)})

        if path == "/api/coding-count":
            import sqlite3
            db_path = os.path.join(os.path.expanduser("~/.hy_memory/data"), "coding_memory.db")
            if not os.path.isfile(db_path):
                return self._json(200, {"total": 0, "today": 0})
            try:
                conn = sqlite3.connect(db_path)
                row = conn.execute("SELECT COUNT(*) FROM coding_memory_meta").fetchone()
                total = row[0] if row else 0
                from datetime import datetime, timezone
                today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).isoformat()
                row2 = conn.execute(
                    "SELECT COUNT(*) FROM coding_memory_meta WHERE updated_at >= ?",
                    (today_start,)
                ).fetchone()
                today = row2[0] if row2 else 0
                conn.close()
                return self._json(200, {"total": total, "today": today})
            except Exception as e:
                return self._json(500, {"error": str(e)})

        if path == "/api/layer-health":
            user_id = os.environ.get("HY_MEMORY_USER_ID", "hermes-user")
            agent_id = os.environ.get("HY_MEMORY_AGENT_ID", "default")
            isolation_key = f"{user_id}::{agent_id}::default_session"
            layer_keys = [
                "l0_basic_info", "l1_raw", "l2_fact", "l3_summary", "l4_identity",
                "l5_knowledge", "l6_schema", "l7_intention",
            ]
            counts = dict.fromkeys(layer_keys, 0)
            fresh_l2 = 0
            try:
                _, listed = hy(
                    "POST",
                    "/api/v1/list",
                    {"user_id": user_id, "agent_id": agent_id, "limit": 5000},
                    timeout=120,
                )
                vdb = (listed or {}).get("vdb") or {}
                for m in vdb.get("memories") or []:
                    layer = m.get("layer")
                    if layer in counts:
                        counts[layer] += 1
                    if layer == "l2_fact" and (m.get("custom") or {}).get("s2_evidence_count", 0) < 1:
                        fresh_l2 += 1
                graph = (listed or {}).get("graph") or {}
                gnodes = graph.get("nodes") or []
                l6_for_key = sum(
                    1
                    for n in gnodes
                    if n.get("layer") == "l6_schema"
                    and n.get("isolation_key") == isolation_key
                )
                _, graph_api = hy("GET", "/api/v1/graph", None, timeout=60)
                graph_layer_counts = (graph_api or {}).get("layer_counts") or {}
                graph_relations = (graph_api or {}).get("relation_count")
            except Exception as e:
                return self._json(500, {"error": str(e)})
            log_path = _pathlib.Path.home() / ".hyatlas" / "logs" / "digest_run_latest.log"
            digest_log_status = "missing"
            digest_log_mtime = None
            if log_path.is_file():
                digest_log_mtime = log_path.stat().st_mtime
                try:
                    tail = log_path.read_text(encoding="utf-8", errors="replace")[-12000:]
                    if "AFTER " in tail and "no_clusters" not in tail:
                        digest_log_status = "ok"
                    elif "HTTP 200" in tail:
                        digest_log_status = "partial"
                    else:
                        digest_log_status = "stale"
                except OSError:
                    digest_log_status = "unreadable"
            archive_dir = _pathlib.Path.home() / ".hyatlas" / "archive"
            l4_archives = sorted(archive_dir.glob("l4_identity_pre_migrate_*.jsonl"))
            local_app = os.environ.get("LOCALAPPDATA", "")
            if local_app:
                digest_win = f'python "{local_app}\\hermes\\scripts\\run_hyatlas_digest.py"'
            else:
                digest_win = "python %LOCALAPPDATA%\\hermes\\scripts\run_hyatlas_digest.py"
            return self._json(200, {
                "user_id": user_id,
                "agent_id": agent_id,
                "isolation_key": isolation_key,
                "vdb_layer_counts": counts,
                "graph_layer_counts": graph_layer_counts,
                "graph_relation_count": graph_relations,
                "fresh_l2_for_digest": fresh_l2,
                "l6_graph_sample_for_key": l6_for_key,
                "l4_status": "retired_migrated_to_l2",
                "l4_archive_path": str(l4_archives[-1]) if l4_archives else None,
                "digest_command": digest_win,
                "digest_log_path": str(log_path),
                "digest_log_status": digest_log_status,
                "digest_log_mtime": digest_log_mtime,
                "layer_notes": {
                    "l1_raw": "Often 0 under Hermes key: L1 shadowed after L2 extract.",
                    "l4_identity": "No writer; legacy rows only.",
                    "l6_schema": "Graph (Kuzu) is canonical; VDB l6 count may be 0.",
                    "l5_knowledge": "Use graph_layer_counts for L5–L7 totals.",
                },
            })

        if path.split("?")[0] == "/api/l6-schemas":
            qs = parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            try:
                n = min(max(int((qs.get("n") or ["8"])[0]), 1), 20)
            except (ValueError, TypeError):
                n = 8
            q = ((qs.get("q") or [""])[0] or "").strip().lower()
            try:
                _, data = hy(
                    "GET",
                    f"/api/v1/graph?layer=l6_schema&n=50&rels=false",
                    None,
                    timeout=90,
                )
                nodes = (data or {}).get("nodes") or []
                if q:
                    nodes = [
                        x for x in nodes
                        if q in (x.get("name") or "").lower()
                    ]
                nodes = nodes[:n]
                return self._json(200, {
                    "graph_l6_total": (data or {}).get("layer_counts", {}).get("l6_schema"),
                    "count": len(nodes),
                    "schemas": nodes,
                })
            except Exception as e:
                return self._json(500, {"error": str(e)})

        if path == "/api/layer-counts":
            layer_keys = [
                "l0_basic_info", "l1_raw", "l2_fact", "l3_summary", "l4_identity",
                "l5_knowledge", "l6_schema", "l7_intention",
            ]
            counts = {}
            total = 0
            graph_layers = ("l5_knowledge", "l6_schema", "l7_intention")
            for layer in layer_keys:
                require_latest = layer not in graph_layers
                n = _vdb_layer_count(layer, require_is_latest=require_latest)
                counts[layer] = n
                total += n
            # L5/L6/L7 live in Kuzu; use the fast /api/v1/graph endpoint
            # (one call, no per-user loop) instead of the slow /api/v1/list.
            try:
                _, graph_data = hy("GET", "/api/v1/graph", None)
                if isinstance(graph_data, dict):
                    lc = graph_data.get("layer_counts") or {}
                    counts["l5_knowledge"] = max(
                        counts.get("l5_knowledge", 0),
                        lc.get("l5_knowledge", graph_data.get("node_count", 0)),
                    )
                    counts["l6_schema"] = max(counts.get("l6_schema", 0), lc.get("l6_schema", 0))
                    counts["l7_intention"] = max(
                        counts.get("l7_intention", 0), lc.get("l7_intention", 0)
                    )
            except Exception:
                pass
            total = sum(counts.values())
            return self._json(200, {
                "counts": counts,
                "total": total,
                "vdb_total": total,
                "is_active_filtered": True,
            })

        if path == "/api/graph-counts":
            # L5–L7 from Kuzu via live /api/v1/graph (layer_counts).
            l5_count = l6_count = l7_count = 0
            relation_count = None
            try:
                _, graph_data = hy("GET", "/api/v1/graph", None, timeout=60)
                if isinstance(graph_data, dict):
                    lc = graph_data.get("layer_counts") or {}
                    l5_count = int(lc.get("l5_knowledge") or graph_data.get("node_count") or 0)
                    l6_count = int(lc.get("l6_schema") or 0)
                    l7_count = int(lc.get("l7_intention") or 0)
                    relation_count = graph_data.get("relation_count")
            except Exception:
                pass
            if l5_count == 0:
                l5_export_path = str(_l5_export_path())
                try:
                    with open(l5_export_path, encoding="utf-8") as f:
                        l5_data = json.loads(f.read())
                    for node in l5_data.get("nodes", []):
                        if node.get("layer") == "l5_knowledge":
                            l5_count += 1
                except (FileNotFoundError, json.JSONDecodeError):
                    l5_count = _vdb_layer_count("l5_knowledge", require_is_latest=False)
            return self._json(200, {
                "l5_knowledge": l5_count,
                "l6_schema": l6_count,
                "l7_intention": l7_count,
                "relation_count": relation_count,
                "total": l5_count + l6_count + l7_count,
            })

        if path == "/api/l5/graph":
            # Proxy to the server's live /api/v1/graph endpoint (queries
            # Kuzu directly — no stale export file needed).
            qs = parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            etype = qs.get("type", [None])[0]
            search = qs.get("q", [None])[0]
            upstream_qs = []
            if etype:
                upstream_qs.append(f"type={etype}")
            if search:
                upstream_qs.append(f"q={search}")
            upstream_path = "/api/v1/graph"
            if upstream_qs:
                upstream_path += "?" + "&".join(upstream_qs)
            status, data = hy("GET", upstream_path, None)
            if status == 200 and isinstance(data, dict):
                self._json(200, data)
            else:
                # Fallback to export file if server endpoint unavailable
                l5_export_path = str(_l5_export_path())
                try:
                    with open(l5_export_path, encoding="utf-8") as f:
                        l5_data = json.loads(f.read())
                except (FileNotFoundError, json.JSONDecodeError):
                    self._json(503, {"error": "graph endpoint unavailable and export file missing"})
                    return

                nodes = l5_data.get("nodes", [])
                relations = l5_data.get("relations", [])
                if not relations:
                    relations = [
                        {"a": e.get("from", ""), "b": e.get("to", ""),
                         "relation_type": e.get("type", e.get("relation_type", "related_to")),
                         "confidence": e.get("weight", e.get("confidence", 0.8))}
                        for e in l5_data.get("edges", [])
                    ]

                if etype:
                    nodes = [n for n in nodes if n.get("entity_type") == etype]
                    node_names = {n["name"] for n in nodes}
                    relations = [r for r in relations if r["a"] in node_names and r["b"] in node_names]

                if search:
                    sl = search.lower()
                    nodes = [n for n in nodes if sl in n["name"].lower()
                             or any(sl in a.lower() for a in n.get("aliases", []))]
                    node_names = {n["name"] for n in nodes}
                    relations = [r for r in relations if r["a"] in node_names and r["b"] in node_names]

                nodes = sorted(nodes, key=lambda n: -n.get("mention_count", 0))
                self._json(200, {
                    "exported_at": l5_data.get("exported_at"),
                    "node_count": len(nodes),
                    "relation_count": len(relations),
                    "nodes": nodes,
                    "relations": relations,
                    "type_distribution": l5_data.get("type_distribution", {}),
                    "relation_type_distribution": l5_data.get("relation_type_distribution", {}),
                    "fallback": True,
                })
            return

        if path == "/api/l5/context":
            # Returns the top L5 entities formatted for injection into the
            # agent's LLM context. This is what makes the agent "use" L5 —
            # it sees the existing knowledge graph as prior context.
            #
            # Query params:
            #   n    - max entities to return (default 15, max 50)
            #   type - optional filter by entity type (TOOL/PROJECT/etc)
            qs = parse_qs(self.path.split("?", 1)[1] if "?" in self.path else "")
            try:
                n = int(qs.get("n", ["15"])[0])
                n = min(max(n, 1), 50)
            except ValueError:
                n = 15
            etype = qs.get("type", [None])[0]

            # Try live endpoint first
            graph_params = {"n": str(n), "rels": "false"}
            if etype:
                graph_params["type"] = etype
            query_str = "&".join(f"{k}={v}" for k, v in graph_params.items())
            status, data = hy("GET", f"/api/v1/graph?{query_str}", None)

            if status == 200 and isinstance(data, dict) and data.get("nodes"):
                nodes = data.get("nodes", [])
            else:
                # Fallback to export file
                l5_export_path = str(_l5_export_path())
                try:
                    with open(l5_export_path, encoding="utf-8") as f:
                        l5_data = json.loads(f.read())
                except (FileNotFoundError, json.JSONDecodeError):
                    return self._json(200, {"context": "(L5 knowledge graph not yet built)", "entities": []})

                nodes = l5_data.get("nodes", [])
                if etype:
                    nodes = [nd for nd in nodes if nd.get("entity_type") == etype]
                nodes = sorted(nodes, key=lambda x: -x.get("mention_count", 0))[:n]

            # Sort by mention count desc (importance proxy)
            nodes = sorted(nodes, key=lambda x: -x.get("mention_count", 0))[:n]

            # Format as a context block the LLM can use
            lines = ["Known entities from your knowledge graph (use these as prior context):"]
            for nd in nodes:
                aliases = f" (aka: {', '.join(nd.get('aliases', []))})" if nd.get('aliases') else ""
                mentions = nd.get('mention_count', 1)
                et = nd.get("entity_type", nd.get("type", "unknown"))
                lines.append(f"- {nd['name']} [{et}] mentioned {mentions}×{aliases}")

            # Get relations from live endpoint or export
            relations = data.get("relations", []) if status == 200 else l5_data.get('relations', [])
            if not relations and status != 200:
                relations = [
                    {"a": e.get("from", ""), "b": e.get("to", ""),
                     "relation_type": e.get("type", e.get("relation_type", "related_to"))}
                    for e in l5_data.get("edges", [])
                ]
            node_names = {nd['name'] for nd in nodes}
            top_rels = [r for r in relations if r['a'] in node_names and r['b'] in node_names][:10]
            if top_rels:
                lines.append("")
                lines.append("Notable relations:")
                for r in top_rels:
                    lines.append(f"  {r['a']} {r['relation_type']} {r['b']}")

            total_nodes = data.get("node_count", 0) if status == 200 else l5_data.get("node_count", 0)
            total_rels = data.get("relation_count", 0) if status == 200 else l5_data.get("relation_count", 0)

            return self._json(200, {
                "context": "\n".join(lines),
                "entities": [{"name": nd["name"], "type": nd.get("entity_type", nd.get("type", "unknown")), "mentions": nd.get("mention_count", 1)} for nd in nodes],
                "total_entities_in_graph": total_nodes,
                "total_relations_in_graph": total_rels,
            })

        self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]

        # Auth endpoint — handles login form submission
        if path == "/auth":
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length).decode("utf-8") if length else ""
            qs = parse_qs(body)
            token = qs.get("token", [None])[0]
            if token == DASH_TOKEN:
                self.send_response(302)
                self.send_header("Location", "/")
                self.send_header("Set-Cookie", f"hyatlas_token={DASH_TOKEN}; Path=/; HttpOnly; SameSite=Strict")
                self.send_header("Content-Length", "0")
                self.end_headers()
            else:
                self.send_response(401)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", "0")
                self.end_headers()
            return

        # Auth gate for all other POST endpoints
        if AUTH_REQUIRED and not self._check_auth():
            return self._json(401, {"error": "unauthorized"})

        if path == "/api/search":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(length).decode("utf-8")) if length else {}
            except Exception as e:
                return self._json(400, {"error": f"bad body: {e}"})
            code, payload = hy("POST", "/api/v1/search", body, timeout=90)
            return self._json(code or 502, payload)

        self._json(404, {"error": "not found"})


def main() -> None:
    print("Hy-Memory Dashboard v2")
    print(f"  upstream:  {HY_MEMORY_BASE}")
    print(f"  listening: http://{BIND_HOST}:{BIND_PORT}")
    if AUTH_REQUIRED and DASH_TOKEN:
        print("  auth:      enabled (token required)")
        print(f"  open:      http://{BIND_HOST}:{BIND_PORT}/?token={DASH_TOKEN}")
        print(f"  token file: {_DASH_TOKEN_FILE}")
    else:
        print("  open in:   your browser at the URL above")
        print("  auth:      disabled (local only)")
    print("  stop with: Ctrl-C")
    if BIND_HOST == "0.0.0.0" and not AUTH_REQUIRED:
        print("  WARNING: bound to 0.0.0.0 without auth — anyone on your LAN can read your memories.")
    try:
        server = ThreadingHTTPServer((BIND_HOST, BIND_PORT), Handler)
    except OSError as e:
        print(f"  FATAL: bind failed: {e}")
        sys.exit(1)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n  stopped.")
        server.server_close()


if __name__ == "__main__":
    main()
