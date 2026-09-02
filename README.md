![HyAtlas v4.0 — Pure-Go Memory Core](https://raw.githubusercontent.com/tuancookiez-hub/HyAtlas-Memory/main/assets/hyatlas-v4.0-banner.png)

# HyAtlas v4.0 — Pure-Go Memory Core

> **One binary. Seven layers. Cross-platform.** Single 17.6 MB Go binary, no Python at runtime, in-process BGE embeddings, 7-layer memory model fully active. **Linux ✅ · macOS ✅ · Windows ✅.**

HyAtlas v4.0 is a complete rewrite of the HyAtlas memory system in pure Go. It replaces the Python floor (venv, zvec, Kuzu, FastAPI, HTTP embed subprocess) with a single binary: an embedded Chromem vector store, in-process BGE-small embeddings via onnxruntime-go, and async LLM fact extraction. The 7-layer memory model (Profile · Raw · Fact · Summary · Knowledge · Schema · Intention) is fully active — including L4 Summary extraction which was dormant in v3.5.

**Previous floor:** [HyAtlas v3.5.0](https://github.com/tuancookiez-hub/HyAtlas-Memory/releases/tag/v3.5.0) — Python/Zvec/Kuzu. See [V3_V4_COMPARISON.md](V3_V4_COMPARISON.md) for the full side-by-side and [CHANGELOG.md](CHANGELOG.md) for the migration history.

---

## Quick start (Linux / macOS / Windows)

### The one-liner (recommended)

```bash
curl -fsSL https://raw.githubusercontent.com/tuancookiez-hub/HyAtlas-Memory/main/scripts/install.sh | bash
```

That script detects your OS, downloads a prebuilt binary for your platform
(falling back to building from source if none exists yet), fetches the
BGE-small embedding model (~133 MB), installs to a directory on your `PATH`,
and verifies the install by starting the server and probing `/healthz`.

Useful env vars:

| Variable | Purpose | Default |
|---|---|---|
| `HYATLAS_VERSION` | Release tag to install | `v4.0.0` |
| `HYATLAS_INSTALL_DIR` | Where the binary goes | `~/.local/bin` (Windows: `%LOCALAPPDATA%\hyatlas`) |
| `HYATLAS_MODEL_DIR` | Where the BGE model is cached | `~/.hyatlas/models` (Windows: `%LOCALAPPDATA%\hyatlas\models`) |
| `HYATLAS_NO_MODEL=1` | Skip the model download | (downloads) |

---

### Manual install (3 steps)

<details>
<summary><strong>Step 1 — Prerequisites</strong> (click to expand)</summary>

| OS | What's needed | Install command |
|---|---|---|
| **Linux** | Go 1.26+, gcc (for cgo) | `apt install golang-go gcc` (or distro equivalent) |
| **macOS** | Go 1.26+, Xcode CLI tools | `xcode-select --install` + install Go 1.26+ from [go.dev](https://go.dev/dl/) |
| **Windows** | Go 1.26+, MinGW-W64 (for cgo) | `winget install BrechtSanders.WinLibs.POSIX.UCRT` then restart terminal |

> **cgo is required** — onnxruntime-go links against the platform's C runtime via cgo. Every platform has a free toolchain; you just need one.

</details>

<details>
<summary><strong>Step 2 — Build</strong></summary>

```bash
git clone https://github.com/tuancookiez-hub/HyAtlas-Memory.git
cd HyAtlas-Memory
go build -o hyatlas-go .                     # plain build (~17 MB, reads ./models/ at runtime)
go build -tags embedded -o hyatlas-go .       # embedded build (one binary, model bundled in)
```

> **For the embedded build to work**, drop the platform-matching onnxruntime library into `./models/` before compiling (see [Model assets](#model-assets) below). The `go:embed` directives are platform-aware: Windows expects `models/onnxruntime.dll`, Linux expects `models/libonnxruntime.so`, macOS expects `models/libonnxruntime.dylib`.

</details>

<details>
<summary><strong>Step 3 — Run</strong></summary>

```bash
# Required for the local BGE embeddings (the "no Python" path):
export HYATLAS_EMBED_BASE=bge
export HYATLAS_MODEL_DIR=/path/to/models

# Required for LLM extraction (any OpenAI-compatible endpoint):
export HYATLAS_LLM_BASE="http://127.0.0.1:49200/v1"
export HYATLAS_LLM_MODEL="deepseek:deepseek-v4-flash"
export HYATLAS_LLM_KEY="your-key"

./hyatlas-go
```

The server listens on `127.0.0.1:19528` (loopback only — no external surface).

**All configuration is via environment variables** — the binary takes no CLI flags:

| Variable | Default | Purpose |
|---|---|---|
| `HYATLAS_GO_PORT` | `19528` | HTTP listen port |
| `HYATLAS_GO_HOST` | `127.0.0.1` | Bind address (loopback only by default) |
| `HYATLAS_GO_DATA` | `./data` | Where chromem collections + graph.json live |
| `HYATLAS_EMBED_BASE` | `http://127.0.0.1:49200/v1` | Set to `bge` for the local in-process embedder |
| `HYATLAS_MODEL_DIR` | `./models` | Where the BGE model lives |
| `HYATLAS_LLM_BASE` | `http://127.0.0.1:49200/v1` | OpenAI-compatible LLM endpoint |
| `HYATLAS_LLM_MODEL` | `deepseek:deepseek-v4-flash` | LLM model name |
| `HYATLAS_LLM_KEY` | (empty) | LLM bearer token |
| `HYATLAS_GRAPH_PATH` | `<data>/graph.json` | L5 graph store location |

**Windows batch runner** (reads the AI2API key from Hermes `.env`):
```bash
hyatlas-v4-start.bat
```

</details>

---

## Architecture

| Layer | What it holds |
|---|---|
| **L1 Profile** | User preferences, style, constraints |
| **L2 Raw** | Every incoming memory as-is |
| **L3 Fact** | LLM-extracted factual atoms |
| **L4 Summary** | LLM-extracted session summaries (**enabled in v4; was dormant in v3.5**) |
| **L5 Knowledge** | LLM-extracted entity/relation graph nodes (JSON-persisted) |
| **L6 Schema** | LLM-extracted recurring patterns |
| **L7 Intention** | LLM-extracted current goal / next step |

### Stack

- **Vector store:** [Chromem-go](https://github.com/philippgille/chromem-go) v0.7.0 — embedded, disk-persisted, no server process
- **Embeddings:** `bge/bge.go` — BGE-small-en-v1.5 (33M params, 384-dim) via onnxruntime-go (cgo). WordPiece tokenizer, mean-pool, L2-normalize. Cross-path cosine vs ground-truth BGE: **0.93**. No Python.
- **LLM extraction:** Async via OpenAI-compatible endpoint. Promotes L1 Raw → L3 Fact → L4 Summary → L5/L6/L7 in a single structured call.
- **HTTP:** Standard Go `net/http`. No framework.

### Headline metrics

| | v3.5 (Python) | v4.0 (Pure Go) |
|---|---|---|
| Retrieval quality | 0.33 | **0.80** |
| Processes | 5+ (venv, zvec, Kuzu, FastAPI, embed) | **1** |
| Binary size | ~1 GB (Python venv) | **17.6 MB** |
| Ports | 3 (19527, 19526, 19525) | **1** (19528) |
| L4 Summary | Dormant | **Active** |
| Linux/macOS support | Same (Python) | **Yes** (Go binary, no Python) |
| Restart-safe | No | **Yes** |

---

## Model assets

The BGE-small model + the platform-matching onnxruntime shared library live in `models/` (gitignored). For a plain build, the server reads them at runtime. For an embedded build, `go:embed` bundles them into the binary at compile time.

**Directory layout (per platform):**

| OS | Models dir contents |
|---|---|
| Windows | `bge-small-en-v1.5.onnx` + `.onnx.data` + `vocab.txt` + `onnxruntime.dll` |
| Linux | `bge-small-en-v1.5.onnx` + `.onnx.data` + `vocab.txt` + `libonnxruntime.so` |
| macOS | `bge-small-en-v1.5.onnx` + `.onnx.data` + `vocab.txt` + `libonnxruntime.dylib` |

**Where to get the onnxruntime library:**
- Windows: `pip install onnxruntime` then copy `onnxruntime.dll` out of the venv, OR download from [microsoft/onnxruntime releases](https://github.com/microsoft/onnxruntime/releases) (v1.28.1 to match `onnxruntime_go` v1.32.0)
- Linux: `apt install libonnxruntime-dev` (Ubuntu 22.04+), or download from the same release page
- macOS: `brew install onnxruntime`, or download from the same release page

**To regenerate the ONNX from the HuggingFace model**, see `export_bge_onnx.py`.

### Version pins (critical)

- `onnxruntime_go` **v1.32.0** — declares ONNX API 28
- `onnxruntime` **1.28.1** shared library — must expose API 28
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

**1. Run HyAtlas v4** (see Quick start above).

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

---

## Migration from v3.5

HyAtlas v4 is a **binary replacement** for the Python v3.5 floor. The memory data formats are incompatible (zvec → Chromem, Kuzu → JSON graph), but the HTTP API surface is identical.

**If you need to keep v3.5 running while testing v4**, they use different ports:
- v3.5: `localhost:19527`
- v4: `localhost:19528`

The full v3.5 → v4 side-by-side (architecture, performance, reliability, API compatibility) is in [V3_V4_COMPARISON.md](V3_V4_COMPARISON.md).

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
