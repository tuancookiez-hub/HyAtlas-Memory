![HyAtlas Memory v4.3.0 — three-gear extraction across a seven-layer memory](https://raw.githubusercontent.com/tuancookiez-hub/HyAtlas-Memory/main/assets/hyatlas-v4.3.0-banner.png)

# HyAtlas Memory — Pure-Go Memory Core

> **One binary. Seven layers. Three extraction modes. Cross-platform.** Single 17.6 MB Go binary, no Python at runtime, in-process BGE embeddings, 7-layer memory model fully active. **Linux ✅ · macOS ✅ · Windows ✅.**

HyAtlas v4.0 is a complete rewrite of the HyAtlas memory system in pure Go. It replaces the Python floor (venv, zvec, Kuzu, FastAPI, HTTP embed subprocess) with a single binary: an embedded Chromem vector store, in-process BGE-small embeddings via onnxruntime-go, and LLM fact extraction whose timing you choose with `HYATLAS_MODE`. The 7-layer memory model (Profile · Raw · Fact · Summary · Knowledge · Schema · Intention) is fully active — including L4 Summary extraction which was dormant in v3.5.

**Previous floor:** [HyAtlas v3.5.0](https://github.com/tuancookiez-hub/HyAtlas-Memory/releases/tag/v3.5.0) — Python/Zvec/Kuzu. See [V3_V4_COMPARISON.md](V3_V4_COMPARISON.md) for the full side-by-side and [CHANGELOG.md](CHANGELOG.md) for the migration history.

![v3.5 → v4.0.1](assets/hyatlas-v3.5-to-v4.0.1.png)

---

## Quick start (Linux / macOS / Windows)

### The one-liner (recommended)

```bash
curl -fsSL https://raw.githubusercontent.com/tuancookiez-hub/HyAtlas-Memory/main/scripts/install.sh | bash
```
(falling back to building from source if none exists yet), fetches the
BGE-small embedding model (~133 MB), installs to a directory on your `PATH`,
and verifies the install by starting the server and probing `/healthz`.

Useful env vars:

| Variable | Purpose | Default |
|---|---|---|
| `HYATLAS_VERSION` | Release tag to install | `v4.3.2` |
| `HYATLAS_INSTALL_DIR` | Where the binary goes | `~/.local/bin` (Windows: `%LOCALAPPDATA%\hyatlas`) |
| `HYATLAS_MODEL_DIR` | Where the BGE model is cached | `~/.hyatlas/models` (Windows: `%LOCALAPPDATA%\hyatlas\models`) |
| `HYATLAS_MODE` | Extraction mode: `lite` \| `pro` \| `ultra` | `ultra` |
| `HYATLAS_SYNC_EXTRACT` | Whether a write blocks on extraction: `on` \| `off` | *(follows the mode)* |
| `HYATLAS_CONSOLIDATE_EVERY` | Ultra only: how often the slow path runs | `6h` |
| `HYATLAS_CONSOLIDATE_BATCH` | Ultra only: max facts per consolidation call | `200` |
| `HYATLAS_RAW_RETENTION` | Ultra only: decay uncited L2 Raw older than this | *(never delete)* |
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

# Required for LLM extraction in pro/ultra. No endpoint is assumed: without all
# three the server stores the raw trace only and reports llm=unconfigured.
# Any OpenAI-compatible API works, including a local one.
export HYATLAS_LLM_BASE="https://inference-api.nousresearch.com/v1"   # example
export HYATLAS_LLM_MODEL="poolside/laguna-s-2.1:free"                 # example
export HYATLAS_LLM_KEY="your-nous-agent-key"

./hyatlas-go
```

The server listens on `127.0.0.1:19528` (loopback only — no external surface).

### Privacy — what leaves your machine

The server binds loopback only, but **loopback is not the whole story**, and the
default configuration is not fully local:

| What | Goes where | Default | How to keep it local |
|---|---|---|---|
| **Memory text** (the turn being extracted) | Sent to the extraction LLM | **Nowhere** — no endpoint is shipped, so an unconfigured server makes no LLM call and reports `unconfigured` | Already opt-in: set `HYATLAS_LLM_BASE`/`_MODEL`/`_KEY` to choose where it goes. Point them at a local OpenAI-compatible server to keep it on-machine, or use `HYATLAS_MODE=lite` |
| Embeddings | In-process BGE-small (onnxruntime-go) | **Local** — `HYATLAS_EMBED_BASE=bge`, no network | Already local |
| Stored memories, vector index, graph | `HYATLAS_GO_DATA` (default `./data`) | **Local** | Already local |
| Telemetry / usage reporting | — | **None** | — |

So out of the box: **embeddings and storage are local, extraction is not.**
Every conversation turn the memory system ingests is sent to the configured LLM
endpoint to derive facts, summaries and intentions. That is the point of the
feature — but it means the default install transmits conversation text to Nous
Research's inference API unless you change `HYATLAS_LLM_BASE`.

The extraction endpoint is yours to choose per the tier you are on — set
`HYATLAS_LLM_BASE`, `HYATLAS_LLM_MODEL` and `HYATLAS_LLM_KEY` to any
OpenAI-compatible API. To keep conversation text on the machine, set `HYATLAS_MODE=lite`: no LLM
call is made at all, so only the raw trace and local embeddings are stored.

The three modes form a ladder of reasoning scope, not of latency:

| Mode | LLM calls | Reasoning scope | Layers | Consolidation |
|---|---|---|---|---|
| `lite` | none | — | **1 / 7** — L2 Raw only | no |
| `pro` | one per write | within one turn | **5 / 7** — L1, L2, L3, L4, L7 | no |
| `ultra` *(default)* | one per write **+** periodic batch | **across memories and time** | **7 / 7** | **yes** |

The two systems own disjoint layers:

- **System1 (per turn)** — L1 Profile, L2 Raw, L3 Fact, L4 Summary, L7 Intention.
  What one turn can actually evidence.
- **System2 (slow path)** — L5 Knowledge, L6 Schema. A relation worth keeping is
  corroborated by more than one turn, and a schema is a *recurring* pattern, so
  neither can come from a single turn. Ultra is the only mode that runs System2,
  which is why it is the only one that fills L5 and L6.

Whether a write *blocks* on its extraction is a separate knob
(`HYATLAS_SYNC_EXTRACT=on|off`), not part of the mode. Pro blocks by default and
ultra does not, but either can be overridden — capability follows the mode,
latency follows the knob.

| `HYATLAS_SYNC_EXTRACT` | Effect |
|---|---|
| *(unset)* | follow the mode: `pro` blocks, `ultra` returns immediately |
| `on` | the write waits for extraction and reports `done` / `failed` |
| `off` | the write returns `pending`; extraction runs behind it |

Ultra-only tuning:

| Variable | Default | Meaning |
|---|---|---|
| `HYATLAS_CONSOLIDATE_EVERY` | `6h` | how often the slow path runs |
| `HYATLAS_CONSOLIDATE_BATCH` | `200` | max facts per consolidation call |
| `HYATLAS_RAW_RETENTION` | *(unset = never delete)* | age after which uncited L2 Raw is decayed |

`HYATLAS_RAW_RETENTION` is opt-in because it deletes. Raw memories cited by a
live L5 graph edge are always protected, so decay cannot leave the knowledge
graph pointing at a memory that no longer exists.

The `7 / 7` above is steady state, not the first minute. A fresh ultra install
sits at 5/7 until the slow path first runs, which is up to `6h` away by default.
To see L5 and L6 immediately rather than waiting for the tick, trigger a pass
yourself:

```bash
curl -X POST http://127.0.0.1:19528/api/v1/digest
```

The response reports exactly what the pass changed (`facts_in`, `merged`,
`edges`, `dropped`, `schemas`, `arc`), and `/api/v1/status` carries the same
shape as `consolidations` plus `last_consolidated`, so "the slow path ran" is
checkable rather than something you have to take on faith. A pass that finds
nothing to reconcile still reports as run with zero changes, which is what
distinguishes it from a pass that never happened.


Otherwise the endpoint is yours to choose per the tier you are on — set
`HYATLAS_LLM_BASE`, `HYATLAS_LLM_MODEL` and `HYATLAS_LLM_KEY` to any
OpenAI-compatible API, or point `HYATLAS_LLM_BASE` at a server you host
(ollama, vLLM, llama.cpp, LM Studio) and leave `HYATLAS_EMBED_BASE=bge`.

**Nothing is transmitted until all three are set.** There is no default endpoint,
so a fresh install makes no LLM call at all: writes store the raw trace plus
local embeddings, status reports `llm: "unconfigured"`, and each write reports
`extraction_status: "unconfigured"`. Once you configure an endpoint, extraction
runs on every write in `pro` and `ultra` and your turn text goes there — so
choosing the endpoint is choosing where memory text leaves the machine. Set
`HYATLAS_MODE=lite` to opt out entirely regardless of configuration.

When the Hermes plugin spawns this server it passes an explicitly allowlisted
environment — OS essentials plus `HYATLAS_*` only — rather than a copy of the
agent's environment, so provider API keys the agent holds do not reach the
server process. The plugin sets no `HYATLAS_LLM_*` value and forwards no
credential.

**All configuration is via environment variables** — the binary takes no CLI flags:

| Variable | Default | Purpose |
|---|---|---|
| `HYATLAS_GO_PORT` | `19528` | HTTP listen port |
| `HYATLAS_GO_HOST` | `127.0.0.1` | Bind address (loopback only by default) |
| `HYATLAS_GO_DATA` | `./data` | Where chromem collections + graph.json live |
| `HYATLAS_EMBED_BASE` | `bge` | `bge` = local in-process BGE embedder (no network). Set to a URL for an OpenAI-compatible embedder, or `local` for a deterministic stub. |
| `HYATLAS_MODEL_DIR` | `./models` | Where the BGE model lives |
| `HYATLAS_LLM_BASE` | *(unset)* | OpenAI-compatible LLM endpoint. **Memory text is sent here once you set it** — see *Privacy* above. Unset means no LLM call at all. |
| `HYATLAS_LLM_MODEL` | *(unset)* | LLM model name. Base, model and key must all be set for extraction to run. |
| `HYATLAS_LLM_KEY` | (empty) | LLM bearer token |
| `HYATLAS_LLM_KEY_FILE` | (empty) | Read the key live from this file per call (rotating creds, e.g. Hermes auth.json). Accepts `providers.nous.agent_key`/`access_token` JSON or a plain-text token. Wins over `HYATLAS_LLM_KEY`, which becomes the fallback |
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

## What changed since v3.5.0

- **Pure Go rewrite** — single 17.6 MB binary, no Python venv, no zvec, no Kuzu, no FastAPI
- **In-process BGE embeddings** via onnxruntime-go (cgo) — no HTTP embed subprocess
- **L4 Summary enabled** — was dormant in v3.5
- **L5 bitemporal graph (v4.1.0+)** — every fact carries a citation back to its source L2 memory plus a bitemporal timestamp; the new `/api/v1/graph-as-of?ts=<unix>` endpoint lets you rewind the graph to any past moment.
- **Mind Palace (v4.1.0+)** — a temporal visualization of the L5 knowledge graph in the Hermes Desktop `hyatlas` pane. Toggle List / Spatial on the Memories tab; drag-to-pan, click-to-select, bitemporal mode. See [`plugins/hyatlas/desktop/SPEC.md`](plugins/hyatlas/desktop/SPEC.md) for the design.

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
| `GET` | `/api/v1/graph-as-of?ts=<unix>&n=500` | Bitemporal query — returns graph as it was at unix time `ts` (world validity + recording axes) |

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

HyAtlas v4 is the **backend HTTP server** (`127.0.0.1:19528`). The
`hyatlas` plugin in [`plugins/hyatlas`](plugins/hyatlas) *is* a native Hermes
memory provider — it subclasses `agent.memory_provider.MemoryProvider` and
registers through `ctx.register_memory_provider()` in `register(ctx)`, so
`memory.provider: hyatlas` works directly. The plugin is a thin HTTP client
over this server; the server does the vector store, embeddings, extraction and
graph work.

The plugin installs as a normal Hermes plugin
(`<HERMES_HOME>/plugins/hyatlas/` — `AppData\Local\hermes\plugins\` on
Windows, `~/.hermes/plugins/` elsewhere), not under a `memory/` subdirectory.

### How it works

```
┌─────────────────────┐     HTTP/JSON      ┌──────────────────────┐
│  Hermes Agent       │ ─────────────────► │  hyatlas-go (v4)     │
│  (Python)           │   /api/v1/*        │  127.0.0.1:19528     │
│                     │ ◄───────────────── │  Pure Go binary      │
│  hyatlas plugin   │   JSON responses   │  (this release)      │
│  (~/.hermes/plugins/                        │  chromem-go + BGE    │
│   memory/hyatlas/                          │  in-process          │
│   client.py)                                └──────────────────────┘
└─────────────────────┘
```

The `hyatlas` plugin (Python, in your Hermes install) calls HyAtlas v4's HTTP API. Switching from the v3.5 Python floor to v4 is a port change — same client, new backend.

### Wire it up

**1. Run HyAtlas v4** (see Quick start above).

**2. Install the plugin and select the provider.** Once the catalog entry is
merged ([PR #134419](https://github.com/NousResearch/hermes-agent/pull/134419)),
this is the supported path:

```bash
hermes plugins install hyatlas
hermes plugins enable hyatlas
hermes memory setup        # then choose "hyatlas"
```

Until it merges, install with the `owner/repo/subdir` shorthand — the plugin
lives in the `plugins/hyatlas` subdirectory, and the subdirectory has to be part
of the identifier so the scan is scoped to it:

```bash
hermes plugins install tuancookiez-hub/HyAtlas-Memory/plugins/hyatlas
hermes plugins enable hyatlas
hermes memory setup
```

Equivalent spellings, all resolving to the same clone plus `plugins/hyatlas`:

```bash
hermes plugins install "tuancookiez-hub/HyAtlas-Memory#plugins/hyatlas"
hermes plugins install "https://github.com/tuancookiez-hub/HyAtlas-Memory.git#plugins/hyatlas"
```

Two forms do **not** work, and both fail quietly enough to be confusing:

- `tuancookiez-hub/HyAtlas-Memory` on its own (no subdirectory) clones the
  repository *root*, so the security scan sees the prebuilt `dashboard/dist`
  bundle and the large `assets/` images and returns a CAUTION verdict instead of
  the `safe` verdict the plugin subdirectory gets.
- A bare relative path such as `./HyAtlas-Memory/plugins/hyatlas` is not treated
  as a filesystem path — `owner/repo[/subdir]` parsing turns it into
  `https://github.com/./HyAtlas-Memory.git`. To install from a local clone, use
  a `file://` URL with an explicit `#subdir` fragment:
  `file:///C:/path/to/HyAtlas-Memory#plugins/hyatlas`.

The catalog entry carries the same scoping as `subdir: plugins/hyatlas`, which
is why the merged entry is just the bare name.

Installing and enabling alone does **not** activate memory — `hermes memory
setup` is what writes `memory.provider: hyatlas`:

```yaml
memory:
  memory_enabled: true
  provider: hyatlas
```

Plugin settings (server host/port, user/agent id, `auto_start`,
`binary_path`, `launcher_path`, timeout) live under
`plugins.entries.hyatlas.settings` in `<HERMES_HOME>/config.yaml`, are
editable in **Desktop → Settings → Plugins → hyatlas**, or can be set per
variable via `HYATLAS_*` env vars (env wins). The defaults
(`127.0.0.1:19528`, `auto_start: false`) work with no config at all.

> There is no `memory.providers.hyatlas` block — settings do not go there.

**3. Restart Hermes.**

The `hyatlas` plugin (Python client) is already wire-compatible with v4. Verified against the real v3.5 `HyMemoryClient` — all four operations (reachable / add / list / search) pass cleanly.

### Building a native `MemoryProvider` plugin

This is what [`plugins/hyatlas`](plugins/hyatlas) already is, so there is no
wrapper left to write — see *Hermes Integration* above. If you want to build
your own, that plugin is the reference: subclass
`agent.memory_provider.MemoryProvider`, implement the four client operations
against the HTTP API, and call `ctx.register_memory_provider(provider)` from
`register(ctx)`.

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
