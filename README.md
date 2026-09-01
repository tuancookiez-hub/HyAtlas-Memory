![HyAtlas v4.0 — Pure-Go Memory Core](https://raw.githubusercontent.com/tuancookiez-hub/HyAtlas-Memory/main/assets/hyatlas-v4.0-banner.png)

# HyAtlas v4.0 — Pure-Go Memory Core

> **One binary. Seven layers. No Python.**

HyAtlas v4.0 is a complete rewrite of the HyAtlas memory system in pure Go. It replaces the Python floor (venv, zvec, Kuzu, FastAPI, HTTP embed subprocess) with a single 17.6 MB binary: an embedded Chromem vector store, in-process BGE-small embeddings via onnxruntime-go, and async LLM fact extraction. The 7-layer memory model (Profile · Raw · Fact · Summary · Knowledge · Schema · Intention) is fully active — including L4 Summary extraction which was dormant in v3.5.

**Previous floor:** [HyAtlas v3.5.0](https://github.com/tuancookiez-hub/HyAtlas-Memory/releases/tag/v3.5.0) — Python/Zvec/Kuzu. See [V3_V4_COMPARISON.md](V3_V4_COMPARISON.md) for the full side-by-side and [CHANGELOG.md](CHANGELOG.md) for the migration history.

---

## Install (v3.5 vs v4.0)

| Step | v3.5 (Python) | v4.0 (Pure Go) |
|------|---------------|-----------------|
| 1. One-time prereq | Python 3.11, venv tooling | MinGW-W64 (cgo for onnxruntime-go) |
| 2. Get the code | `git clone` | `git clone` (same) |
| 3. Install | `pip install hyatlas-memory` (or `uv sync`) | `go build -tags embedded -o hyatlas-go.exe .` |
| 4. Run | `python start.py` (5 processes) | `./hyatlas-go.exe` (1 process) |
| Disk footprint | ~1 GB venv | **17.6 MB binary** |
| External services | zvec + Kuzu + FastAPI + embed subprocess | none — all in-process |

**v3.5 install:**
```bash
git clone https://github.com/tuancookiez-hub/HyAtlas-Memory.git
cd HyAtlas-Memory
pip install hyatlas-memory
python -m hyatlas_memory.start
```

**v4.0 install (this release):**
```bash
# One-time: MinGW-W64 (Windows) for cgo / onnxruntime-go
winget install BrechtSanders.WinLibs.POSIX.UCRT
# Restart your terminal after.

git clone https://github.com/tuancookiez-hub/HyAtlas-Memory.git
cd HyAtlas-Memory
go build -tags embedded -o hyatlas-go.exe .
HYATLAS_LLM_BASE=http://127.0.0.1:49200/v1 \
HYATLAS_LLM_MODEL=deepseek:deepseek-v4-flash \
HYATLAS_LLM_KEY=your-key \
./hyatlas-go.exe
```

> **Linux / macOS:** replace `winget install ...` with your distro's MinGW package (e.g. `apt install gcc-mingw-w64-x86-64` for cross-compile, or just use the system Go toolchain — cgo works on Linux/macOS with `gcc`/`clang`).

---

## Hermes Integration

HyAtlas v4 is the **backend HTTP server** (`127.0.0.1:19528`) that backs the existing Hermes `hy_memory` memory provider plugin. It is **not** a native Hermes `MemoryProvider` ABC plugin — those are Python classes that subclass `agent.memory_provider.MemoryProvider` and live in `~/.hermes/plugins/memory/<name>/`.

### How it works

```
┌─────────────────────┐     HTTP/JSON      ┌──────────────────────┐
│  Hermes Agent       │ ─────────────────► │  hyatlas-go (v4)     │
│  (Python)           │   /api/v1/*        │  127.0.0.1:19528     │
│                     │ ◄───────────────── │  Pure Go binary      │
│  hy_memory plugin   │   JSON responses   │  (this release)      │
│  (~/.hermes/plugins/                        │  chromem-go + BGE    │
│   memory/hy_memory/                          │  in-process          │
│   client.py)                                └──────────────────────┘
└─────────────────────┘
```

The `hy_memory` plugin (Python, in your Hermes install) calls HyAtlas v4's HTTP API. Switching from the v3.5 Python floor to v4 is a port change — same client, new backend.

### Wire it up

**1. Run HyAtlas v4** (see Install above).

**2. Configure Hermes to use the v4 port** in `~/.hermes/config.yaml`:

```yaml
memory:
  enabled: true
  provider: hy_memory
  providers:
    hy_memory:
      provider: hy_memory
      server_port: 19528
      auto_start: false
```

> If you previously pointed at v3.5, just change `server_port: 19527` → `server_port: 19528`.

**3. Restart Hermes.**

The `hy_memory` plugin (Python client) is already wire-compatible with v4. Verified against the real v3.5 `HyMemoryClient` — all four operations (reachable / add / list / search) pass cleanly.

### Building a native `MemoryProvider` plugin

If you want a **true native** Hermes memory plugin (Python, subclasses `MemoryProvider`, lives in `~/.hermes/plugins/memory/`), you can write a thin wrapper that calls HyAtlas v4 over HTTP. This is a future-work item — it would let `memory.provider: hyatlas` work directly. For now, the `hy_memory` plugin is the path of least resistance.

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

## Hermes Integration

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
