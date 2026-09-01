![HyAtlas v4.0 — Pure-Go Memory Core](https://raw.githubusercontent.com/tuancookiez-hub/HyAtlas-Memory/main/assets/hyatlas-v4.0-banner.png)

# HyAtlas v4.0 — Pure-Go Memory Core

> **One binary. Seven layers. No Python.**

HyAtlas v4.0 is a complete rewrite of the HyAtlas memory system in pure Go. It replaces the Python floor (venv, zvec, Kuzu, FastAPI, HTTP embed subprocess) with a single 17.6 MB binary: an embedded Chromem vector store, in-process BGE-small embeddings via onnxruntime-go, and async LLM fact extraction. The 7-layer memory model (Profile · Raw · Fact · Summary · Knowledge · Schema · Intention) is fully active — including L4 Summary extraction which was dormant in v3.5.

**Previous floor:** [HyAtlas v3.5.0](https://github.com/tuancookiez-hub/HyAtlas-Memory/releases/tag/v3.5.0) — Python/Zvec/Kuzu. See [V3_V4_COMPARISON.md](V3_V4_COMPARISON.md) for the full side-by-side and [CHANGELOG.md](CHANGELOG.md) for the migration history.

---

## Install

### 1. One-time: MinGW-W64 (required for onnxruntime-go / cgo)

```bash
winget install BrechtSanders.WinLibs.POSIX.UCRT
```

Restart your terminal after installation.

### 2. Clone

```bash
git clone https://github.com/tuancookiez-hub/HyAtlas-Memory.git
cd HyAtlas-Memory
```

### 3. Build

```bash
# Embedded build — model weights bundled into the binary (recommended)
go build -tags embedded -o hyatlas-go.exe .
```

> **Non-embedded build** (model files on disk at `./models/`):
> `go build -o hyatlas-go.exe .`

### 4. Run

```bash
# Required environment variables
export HYATLAS_LLM_BASE="http://127.0.0.1:49200/v1"
export HYATLAS_LLM_MODEL="deepseek:deepseek-v4-flash"
export HYATLAS_LLM_KEY="your-key"

./hyatlas-go.exe
```

The server listens on `127.0.0.1:19528` (loopback only — no external surface).

**Windows batch runner** (reads AI2API key from Hermes `.env`):
```bash
hyatlas-v4-start.bat
```

---

## Architecture

| Layer | What it holds |
|---|---|
| **L1 Profile** | User preferences, style, constraints |
| **L2 Raw** | Every incoming memory as-is |
| **L3 Fact** | LLM-extracted factual atoms |
| **L4 Summary** | LLM-extracted session summaries (**enabled in v4; was dormant in v3.5**) |
| **L5 Knowledge** | LLM-extracted entity/relation graph nodes |
| **L6 Schema** | LLM-extracted structured schemas |
| **L7 Intention** | LLM-extracted goals and next steps |

### Stack

- **Vector store:** [Chromem-go](https://github.com/philipjkim/chromem-go) v0.7.0 — embedded, disk-persisted, no server process
- **Embeddings:** `bge/bge.go` — BGE-small-en-v1.5 (33 M params, 384-dim) via onnxruntime-go (cgo → `onnxruntime.dll`). WordPiece tokenizer, mean-pool, L2-normalize. Cross-path cosine vs. ground-truth BGE: **0.93**. No Python.
- **LLM extraction:** Async via ai2api loopback proxy (port 49200). Promotes L1 Raw → L3 Fact → L4 Summary → L5/L6/L7.
- **HTTP:** Standard Go `net/http`. No FastAPI.

### Model assets

The ~133 MB BGE `.data` weights + ~15 MB `onnxruntime.dll` are **not committed** (`.gitignore` excludes `models/`). The `-tags embedded` build embeds them at compile time via `go:embed`. To regenerate the ONNX from the HuggingFace model, see `export_bge_onnx.py`.

### Version pins (critical)

- `onnxruntime_go` **v1.32.0** — declares ONNX API 28
- `onnxruntime` **1.28.1** `onnxruntime.dll` — must expose API 28
- The `.data` external weights resolve relative to the process **CWD** — the embedder `chdir`s to the model dir on load

---

## API Reference

Base URL: `http://127.0.0.1:19528`

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/healthz` | Liveness check |
| `GET` | `/api/v1/status` | Full health: VDB, embedder, LLM, write pipeline, layer counts |
| `GET` | `/api/v1/metrics` | Uptime, total memories, per-layer counts |
| `POST` | `/api/v1/add` | Add memory (text + user_id + agent_id + session_id) |
| `POST` | `/api/v1/search` | Vector search — returns 3-channel (profile / proactive / normal) |
| `GET` | `/api/v1/list` | List memories, filterable by user_id, agent_id, layer, time |
| `POST` | `/api/v1/list` | Same as GET but body for clients that send POST |
| `POST` | `/api/v1/delete_all` | Bulk delete by scope |
| `POST` | `/api/v1/reprocess` | Re-run extraction on unprocessed raw entries |
| `POST` | `/api/v1/digest` | Trigger L5/L6/L7 synthesis pass |
| `GET` | `/api/v1/graph` | L5 knowledge graph (nodes + edges) |

### Add a memory

```bash
curl -X POST http://127.0.0.1:19528/api/v1/add \
  -H "Content-Type: application/json" \
  -d '{
    "text": "The user prefers concise responses and pushes back on hype",
    "user_id": "default",
    "agent_id": "default",
    "session_id": "session-001"
  }'
```

### Search

```bash
curl -X POST http://127.0.0.1:19528/api/v1/search \
  -H "Content-Type: application/json" \
  -d '{
    "query": "user communication style",
    "user_id": "default",
    "agent_id": "default",
    "limit": 5
  }'
```

Response shape:
```json
{
  "profile": [...],
  "proactive": [...],
  "normal": [...]
}
```

---

## Hermes Plugin Wiring

HyAtlas v4 implements the Hermes memory provider plugin contract. Once your Hermes `memory.providers.hy_memory` config points at `server_port: 19528`, a Hermes restart routes all memory reads/writes through v4.

```json
{
  "memory": {
    "providers": {
      "hy_memory": {
        "provider": "hy_memory",
        "server_port": 19528,
        "auto_start": false
      }
    }
  }
}
```

After updating the config, restart Hermes. The plugin-compatible server was verified against the real v3.5 HyMemoryClient — all four operations (reachable / add / list / search) pass cleanly.

---

## Migration from v3.5

HyAtlas v4 is a **binary replacement** for the Python v3.5 floor. The memory data formats are incompatible (zvec → Chromem, Kuzu → JSON graph), but the HTTP API surface is identical.

**If you need to keep v3.5 running while testing v4**, they use different ports:
- v3.5: `localhost:19527`
- v4: `localhost:19528`

The full v3.5 → v4 side-by-side (architecture, performance, reliability, API compatibility) lives in [V3_V4_COMPARISON.md](V3_V4_COMPARISON.md).

---

## Known gaps (honest)

- `/api/v1/digest` is a stub — the scheduled L5/L6/L7 synthesis pass is not yet wired
- `/api/v1/quality-metrics` returns `{available: false}` (v3.5-only feature)
- Upscaling and codemode are not implemented (those were v3.5 features)

---

## License

Apache 2.0 — see [LICENSE](LICENSE)

---

## Repository

```
https://github.com/tuancookiez-hub/HyAtlas-Memory
```
