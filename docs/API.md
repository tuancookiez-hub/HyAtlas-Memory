# HTTP API Reference

> **HyAtlas v3.2.1** — Two local HTTP servers: memory **19527**, dashboard **8765**. Vector data is **Zvec** (via server `/api/v1/vdb/*`); graph **L5–L7** is **Kuzu** (`GET /api/v1/graph`).

See also: [DASHBOARD.md](./DASHBOARD.md) (UI), [HYATLAS_HERMES.md](./HYATLAS_HERMES.md) (identity + digest).

---

## Architecture

```text
Hermes plugin / CLI
        │ HTTP
        ▼
┌───────────────────────────────┐  :19527
│ Hy-Memory server              │
│  Zvec VDB · embed · LLM ·     │
│  Kuzu graph · digest          │
└───────────────┬───────────────┘
                │ HTTP (proxy)
                ▼
┌───────────────────────────────┐  :8765
│ dashboard.py                  │
│  aggregates · layer-health    │
│  static SPA                   │
└───────────────────────────────┘
```

The dashboard is a **thin aggregator** — it proxies the memory server, enriches layer counts, and serves `dashboard.html`.

---

## Server 1: Memory server (`127.0.0.1:19527`)

Stdlib HTTP server (`hyatlas_memory.core.server`). Integrations add graph + VDB dashboard routes.

### Core routes

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/info` | Name, version, mode, uptime, data_dir |
| `GET` | `/healthz` | Deep check (VDB, embedder, LLM) |
| `POST` | `/api/v1/add` | Write memory |
| `POST` | `/api/v1/search` | Hybrid search (`query`, `user_id`, `agent_id`, `top_k`, …) |
| `POST` | `/api/v1/list` | List memories (VDB + optional graph payload) |
| `GET` | `/api/v1/memories/:id` | Single memory |
| `PUT` | `/api/v1/memories/:id` | Update content |
| `DELETE` | `/api/v1/memories/:id` | Delete |
| `POST` | `/api/v1/delete_all` | Delete all for user |
| `POST` | `/api/v1/digest` | **System 2 digest** (ultra) — body: `{"user_id","agent_id"}` |

### HyAtlas integration routes

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/v1/graph` | Kuzu graph. Default layer **L5**. Query: `layer=l5_knowledge\|l6_schema\|l7_intention`, `n`, `rels`, `type`, `q` |
| `GET` | `/api/v1/breaker` | VDB circuit breaker status |
| `GET` | `/api/v1/vdb/layer_count` | Per-layer VDB count (`layer`, `require_is_latest`) |
| `POST` | `/api/v1/vdb/scroll` | Scroll VDB points (dashboard L1 feed) |

**Graph response (typical):**

```json
{
  "nodes": [ { "node_id": "...", "name": "...", "layer": "l5_knowledge", "entity_type": "..." } ],
  "relations": [ { "source": "...", "target": "...", "type": "..." } ],
  "layer_counts": { "l5_knowledge": 1594, "l6_schema": 568, "l7_intention": 188 },
  "node_count": 1594,
  "relation_count": 8128
}
```

**Digest example:**

```bash
curl -X POST http://127.0.0.1:19527/api/v1/digest \
  -H 'Content-Type: application/json' \
  -d '{"user_id":"hermes-user","agent_id":"default"}'
```

---

## Server 2: Dashboard (`127.0.0.1:8765`)

Loopback only, no auth. JSON unless serving HTML/assets.

### Health & proxy

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/` | SPA HTML |
| `GET` | `/api/health` | `{status, upstream}` |
| `GET` | `/api/status` | Proxy `/api/v1/status` |
| `GET` | `/api/info` | Proxy `/info` |
| `GET` | `/api/storage` | VDB provider + on-disk sizes (`zvec`, Kuzu, …) |

### Memory & metrics

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/memories` | Paginated list (`offset`, `limit`) |
| `GET` | `/api/metrics?minutes=N` | Activity metrics |
| `POST` | `/api/search` | Proxy `/api/v1/search` |

### Layer & graph counts

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/layer-counts` | L0–L4 via `/api/v1/vdb/layer_count`; L5–L7 merged from `/api/v1/graph` `layer_counts` |
| `GET` | `/api/graph-counts` | L5/L6/L7 + `relation_count` from live graph |
| `GET` | `/api/layer-health` | Hermes digest readiness (namespace, `fresh_l2_for_digest`, graph counts, digest log, L6 sample hints) |
| `GET` | `/api/l6-schemas?n=8&q=` | Sample L6 nodes from graph |
| `GET` | `/api/l5/graph` | Proxy live `/api/v1/graph` (fallback: export JSON) |
| `GET` | `/api/l5/context` | L5 context string for agent injection |

**`/api/layer-health` fields (v3.2):**

- `vdb_layer_counts`, `graph_layer_counts`, `graph_relation_count`
- `fresh_l2_for_digest` — L2 with `s2_evidence_count < 1`
- `digest_log_status` — `ok` | `partial` | `stale` | `missing`
- `l4_status` — `retired_migrated_to_l2`
- `layer_notes` — L1 shadowing, L6 graph-canonical, etc.

### Coding memory (optional sqlite)

| Method | Path | Purpose |
|--------|------|---------|
| `GET` | `/api/coding-count` | Totals from `coding_memory.db` |
| `GET` | `/api/coding-memories` | List coding memories |

---

## Errors

| Status | Meaning |
|--------|---------|
| `400` | Bad request |
| `404` | Not found |
| `500` | Dashboard/internal error |
| `502` | Upstream :19527 unreachable |

---

## Programmatic examples

```bash
curl http://127.0.0.1:8765/api/health
curl http://127.0.0.1:19527/api/v1/graph?layer=l6_schema&n=5
curl 'http://127.0.0.1:8765/api/layer-health'
```

---

## Versioning

Response fields may grow without a major bump. Breaking path/shape changes are noted in [CHANGELOG.md](../CHANGELOG.md).

## `/api/v1/list` — `include_raw` flag (added 2026-07-16)

Request body now accepts:
- `include_raw: bool` (default `True`) — when `True`, L1_RAW (unprocessed user input) is included; when `False`, only extracted memories (L2_FACT, L5_KNOWLEDGE, etc.) are returned.

Response: each memory entry now has an `extracted: bool` field indicating whether the LLM extractor has processed it.

## `/api/v1/status` — 3-tier response (added 2026-07-16)

See SERVER.md for full schema. Key change: LLM throttling returns `status: warning` instead of `status: error: 503`. Persisted memory remains readable.
