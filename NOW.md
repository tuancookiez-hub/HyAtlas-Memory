# HyAtlas v4.0 — Current State

**Date:** 2026-09-01
**Status:** v4.0.0 ready for release

## What v4 is

Pure-Go memory server. Single binary (`hyatlas-go` / `hyatlas-go.exe`), no Python, no external vDB, no HTTP embed subprocess. Loopback-only (127.0.0.1:19528).

### Stack

- **Vector store:** Chromem-go v0.7.0 (embedded, disk-persisted)
- **Embedding:** BGE-small-en-v1.5 in-process via onnxruntime-go (33M params, 384d)
- **LLM:** any OpenAI-compatible endpoint (e.g. deepseek-v4-flash via ai2api loopback)
- **HTTP:** standard `net/http`, no framework
- **Data dir:** `./data/` (chromem collections + graph.json + doc_index.json)

### 7-layer model (all active in v4)

Profile · Raw · Fact · Summary · Knowledge · Schema · Intention

L4 Summary is **enabled** (was dormant in v3.5). L4 Identity slot was retired in v3.2 — identity content lives in L3 Fact.

## Endpoints (`/api/v1/`)

- `POST /api/v1/add` — add memory (text + user_id + agent_id + session_id)
- `POST /api/v1/search` — vector search, returns 3-channel (profile/proactive/normal)
- `GET  /api/v1/list` — list memories (filter by user_id, agent_id, layer, time)
- `POST /api/v1/list` — same, body for clients that send POST
- `POST /api/v1/delete_all` — bulk delete by scope
- `POST /api/v1/reprocess` — re-run extraction on unprocessed raws
- `GET  /api/v1/status` — health + layer counts + write pipeline state
- `GET  /api/v1/metrics` — uptime + total + layers
- `POST /api/v1/digest` — graph digest
- `GET  /api/v1/graph` — L5 knowledge graph
- `GET  /healthz` — liveness

Dashboard compat endpoints at `/api/*` (no v1 prefix) for the v3.5 dashboard UI.

## Verified (during the 2026-09-01 acceptance run)

- Real v3.5 `HyMemoryClient` talks to v4 cleanly: reachable / add / list / search all pass.
- Retrieval 0.80 (v3.5 baseline 0.33) — measured against same BGE encoder.
- L4 Summary, L5 Knowledge graph, L6 Schema, L7 Intention all populating.
- Loopback bind, no external surface.
- 23.5 GB of v3.5 dead data (zvec, qdrant, archive, backups, rehearsals) purged.

## Known gaps (honest)

- `/api/v1/digest` is a stub — needs the scheduled L5/L6/L7 synthesis pass.
- `/api/v1/quality-metrics` returns `available: false` (v3.5-only feature).
- No upscaling, no codemode (those were v3.5).

## Build

```bash
# Embedded build (model weights bundled in the binary)
go build -tags embedded -o hyatlas-go.exe .

# Non-embedded build (reads models/ from disk)
go build -o hyatlas-go.exe .
```

Requires:
- Go 1.26+
- MinGW-W64 on Windows (cgo for onnxruntime-go)
- onnxruntime 1.28.1 DLL on Windows (matches `onnxruntime_go` v1.32.0's API)

## Run

```bash
HYATLAS_LLM_BASE=http://127.0.0.1:49200/v1 \
HYATLAS_LLM_MODEL=deepseek:deepseek-v4-flash \
HYATLAS_LLM_KEY=your-key \
./hyatlas-go.exe
```

On Windows, `hyatlas-v4-start.bat` reads the AI2API key from Hermes `.env` and starts the server persistently.
