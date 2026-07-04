# HTTP API Reference

> Complete reference for the local HTTP server embedded in HyAtlas-Memory. Two servers run on your machine — this doc covers both.

## Architecture

```
┌─────────────────────┐
│ Hermes Agent        │  (the agent runtime)
│   + plugin client   │
└──────────┬──────────┘
           │ HTTP
           ▼
┌─────────────────────┐  ← port 19527 (upstream SDK)
│ Hy-Memory (upstream)│     (uvicorn, port 19527)
│   - embedding       │
│   - LLM extraction  │
│   - vector search   │
└──────────┬──────────┘
           │ HTTP
           ▼
┌─────────────────────┐  ← port 8765 (this server)
│ dashboard.py        │     (BaseHTTPRequestHandler)
│   - aggregation     │
│   - L1_RAW from     │
│     Qdrant directly │
│   - L5 from Kuzu    │
│   - coding from     │
│     sqlite          │
└──────────┬──────────┘
           │ static
           ▼
┌─────────────────────┐
│ Browser @ :8765     │  (dashboard.html + Three.js)
└─────────────────────┘
```

The dashboard is a **thin aggregator** — it calls the upstream Hy-Memory SDK server, merges data from Qdrant/Kuzu directly, and serves a single-page JS app.

---

## Server 1: Upstream Hy-Memory SDK (`127.0.0.1:19527`)

This is the unmodified upstream SDK. You don't need to call it directly — the plugin and dashboard both proxy to it. Documented here for completeness.

### Endpoints (passthrough)

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/` | Server info |
| `GET` | `/info` | Build info |
| `GET` | `/api/v1/status` | VDB + LLM status |
| `GET` | `/api/v1/metrics?minutes=N` | Activity metrics |
| `POST` | `/api/v1/list` | List memories (paginated, single user_id) |
| `POST` | `/api/v1/search` | Vector search (body: `{query, user_id, top_k, ...}`) |
| `POST` | `/api/v1/add` | Add a memory |
| `POST` | `/api/v1/delete` | Delete a memory |

The plugin uses `src/hyatlas_memory/client.py` (urllib-based, zero deps) to call these. See the [client source](../src/hyatlas_memory/client.py) for request/response shapes.

---

## Server 2: Dashboard (`127.0.0.1:8765`)

This is what your browser talks to. Full endpoint reference below.

### Conventions

- All responses are JSON unless noted
- Errors return `{error: "message"}` with appropriate HTTP status
- CORS is **not** enabled — the dashboard expects same-origin
- No authentication — the server only listens on `127.0.0.1` (loopback only)

### Authentication model

There is **no auth**. The dashboard binds exclusively to `127.0.0.1` (loopback), meaning it's only reachable from the same machine. Don't expose it to a network without putting it behind a reverse proxy with auth.

---

### `GET /`

Serves `server/dashboard/dashboard.html` as `text/html`. The page is a self-contained SPA — HTML + CSS + JavaScript + Three.js (CDN).

**Response headers:**
- `Content-Type: text/html; charset=utf-8`
- `Cache-Control: no-store, no-cache, must-revalidate` (force fresh load)

---

### `GET /assets/{path}`

Static files from `server/dashboard/assets/`. Used for favicons and icons.

**Path-traversal guard:** any path containing `..`, starting with `/`, or containing `\` returns `400 Bad Request`.

**Content-Type:** mapped by extension (`.png` → `image/png`, `.svg` → `image/svg+xml`, etc.)

**Caching:**
- Paths containing "icon" or `favicon.ico`: `Cache-Control: public, max-age=86400`
- Everything else: `Cache-Control: no-store`

**Example:**
```bash
curl http://127.0.0.1:8765/assets/hy-memory-icon.png -o icon.png
```

---

### `GET /api/health`

Health check. Always returns `200 OK` if the dashboard is running.

**Response:**
```json
{
  "status": "ok",
  "upstream": "http://127.0.0.1:19527"
}
```

---

### `GET /api/status`

Proxies `/api/v1/status` from the upstream server.

**Response:** passthrough from upstream — see the upstream SDK docs.

---

### `GET /api/info`

Proxies `/info` from the upstream server.

---

### `GET /api/storage`

Storage stats — both VDB metadata and on-disk file sizes.

**Response:**
```json
{
  "vdb": {
    "provider": "qdrant",
    "collection": "l1_raw",
    "points": 1234,
    "dims": 768
  },
  "files": {
    "vector_db": "12.34 MB",
    "cache.db": "0.56 MB",
    "history.db": "0.12 MB",
    "kuzu_db": "4.78 MB"
  }
}
```

File sizes are computed by walking the directory tree (for Kuzu's multi-file layout).

---

### `GET /api/memories`

Paginated list of memories across **all user scopes** (deduplicated by `memory_id`).

**Query parameters:**
- `offset` (int, default `0`) — pagination offset
- `limit` (int, default `25`, max `500`) — max items to return

**Behavior:**
1. Queries `/api/v1/list` for each user in `HERMES_USER_IDS` (comma-separated env var)
2. Fetches L1_RAW directly from Qdrant (upstream filters these out)
3. Merges, deduplicates by `memory_id`
4. Sorts by `gmt_created` descending

**Response:**
```json
{
  "memories": [
    {
      "memory_id": "abc123",
      "user_id": "tuan",
      "layer": "l2_fact",
      "content": "The user prefers TypeScript",
      "gmt_created": 1718726400,
      "gmt_updated": 1718726400,
      "score": null,
      "session_id": "...",
      ...
    }
  ],
  "total": 1234,
  "offset": 0,
  "limit": 25
}
```

---

### `GET /api/metrics?minutes=N`

Activity metrics. Proxies `/api/v1/metrics?minutes=N` upstream.

**Query parameters:**
- `minutes` (int, default `60`) — time window

---

### `GET /api/coding-count`

Count of coding-session memories. Reads from `~/.hy_memory/data/coding_memory.db` (sqlite).

**Response:**
```json
{
  "total": 200,
  "today": 5
}
```

---

### `GET /api/coding-memories?limit=N`

List of coding-session memories. Reads from `coding_memory.db`.

**Query parameters:**
- `limit` (int, default `25`, max `200`)

**Response:**
```json
{
  "memories": [
    {
      "memory_id": "...",
      "task": "...",
      "solution": "...",
      "search_keys": "...",
      "workspace_id": "...",
      "branch": "...",
      "session_id": "...",
      "confidence": 0.95,
      "source": "...",
      "type": "...",
      "created_at": "...",
      "updated_at": "..."
    }
  ],
  "total": 200
}
```

---

### `GET /api/layer-counts`

Active memory counts per layer, queried directly from Qdrant.

**Behavior:** For each L0–L4 collection, runs a `count` query. L5–L7 come from Kuzu (via `/api/graph-counts`).

**Response:**
```json
{
  "counts": {
    "l0_basic_info": 4,
    "l1_raw": 52,
    "l2_fact": 288,
    "l3_summary": 127,
    "l4_identity": 29
  },
  "total": 500
}
```

---

### `GET /api/graph-counts`

Counts for L5/L6/L7 from the Kuzu graph.

**Response:**
```json
{
  "l5_knowledge": 5,
  "l6_schema": 282,
  "l7_intention": 173,
  "total": 460
}
```

---

### `GET /api/l5/graph`

Full L5 knowledge graph (nodes + relations).

> **v2.0.0+ (Patch 23):** the dashboard proxies to the live server endpoint
> `GET /api/v1/graph`, which queries Kuzu directly via the server's open
> graph-store connection. Returns real-time data — no stale export file.
> Falls back to `l5_kuzu_export.json` only if the upstream server is down.

**Response:**
```json
{
  "nodes": [
    {
      "node_id": "abc",
      "name": "TypeScript",
      "entity_type": "TECHNOLOGY",
      "layer": "l5_knowledge",
      "mention_count": 42,
      "confidence": 0.95,
      "aliases": ["TS", "tsc"]
    }
  ],
  "relations": [
    {
      "source": "abc",
      "target": "def",
      "type": "uses",
      "weight": 0.8
    }
  ],
  "edges": [...],  // alias for relations (for compat)
  "stats": {
    "node_count": 460,
    "relation_count": 1200
  }
}
```

---

### `GET /api/l5/context?n=15&type=TOOL`

Format L5 entities for injection into the agent's LLM context. This is what the agent "sees" when it uses L5 memory.

**Query parameters:**
- `n` (int, default `15`, max `50`) — max entities
- `type` (string, optional) — filter by `entity_type`

**Response:**
```json
{
  "context": "Known entities: TypeScript (TECHNOLOGY, mentioned 42x)...\nRelations: TypeScript -> used_in -> ProjectX",
  "entities": [ /* same shape as /api/l5/graph nodes */ ]
}
```

---

### `POST /api/search`

Passthrough to upstream `/api/v1/search`.

**Request body:**
```json
{
  "query": "TypeScript preferences",
  "user_id": "tuan",
  "top_k": 10
}
```

**Response:** passthrough from upstream.

---

## Error responses

| Status | Meaning |
|---|---|
| `400` | Bad request (malformed params, path traversal, etc.) |
| `404` | Endpoint not found |
| `500` | Internal server error (e.g., sqlite/db failure) |
| `502` | Upstream Hy-Memory SDK server unreachable |

All error responses are JSON:
```json
{
  "error": "human-readable message"
}
```

---

## Programmatic access

You can call these endpoints from any HTTP client. Examples:

```bash
# Health check
curl http://127.0.0.1:8765/api/health

# Get 50 most recent memories
curl 'http://127.0.0.1:8765/api/memories?limit=50' | jq

# Search
curl -X POST http://127.0.0.1:8765/api/search \
  -H 'Content-Type: application/json' \
  -d '{"query": "TypeScript", "user_id": "tuan", "top_k": 5}'

# L5 context
curl 'http://127.0.0.1:8765/api/l5/context?n=10' | jq .entities
```

```python
import urllib.request
import json

req = urllib.request.Request("http://127.0.0.1:8765/api/health")
with urllib.request.urlopen(req) as resp:
    data = json.loads(resp.read())
    print(data)
```

---

## Versioning

These endpoints are **stable** for the current major version. Breaking changes (path, request shape, response shape) will bump a major version and be noted in the [CHANGELOG](../CHANGELOG.md).

New fields added to responses are **non-breaking** — clients should ignore unknown fields.
