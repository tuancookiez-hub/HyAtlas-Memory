![HyAtlas Memory v4.3.0 — three-gear extraction across a seven-layer memory](https://raw.githubusercontent.com/tuancookiez-hub/HyAtlas-Memory/main/assets/hyatlas-v4.3.0-banner.png)

# HyAtlas Memory — Pure-Go Memory Core

> **One binary. Seven layers. Three extraction modes.** One Go binary (the embedded release build is about 160 MB because it carries the BGE model), no Python at runtime, in-process BGE embeddings. Release binaries: **Linux amd64 · macOS arm64 · Windows amd64**. Other platforms build from source.

HyAtlas v4.0 is a complete rewrite of the HyAtlas memory system in pure Go. It replaces the Python floor (venv, zvec, Kuzu, FastAPI, HTTP embed subprocess) with a single binary: an embedded Chromem vector store, in-process BGE-small embeddings via onnxruntime-go, and LLM fact extraction, where `HYATLAS_MODE` sets how much the server reasons and `HYATLAS_SYNC_EXTRACT` sets whether a write waits for extraction. The 7-layer memory model (Profile · Raw · Fact · Summary · Knowledge · Schema · Intention) is implemented, including L4 Summary extraction, which was dormant in v3.5.

**Previous floor:** [HyAtlas v3.5.0](https://github.com/tuancookiez-hub/HyAtlas-Memory/releases/tag/v3.5.0) — Python/Zvec/Kuzu. See [V3_V4_COMPARISON.md](V3_V4_COMPARISON.md) for the full side-by-side and [CHANGELOG.md](CHANGELOG.md) for the migration history.

![v3.5 → v4.0.1](assets/hyatlas-v3.5-to-v4.0.1.png)

---

## Quick start (Linux / macOS / Windows)

### The one-liner (recommended)

```bash
curl -fsSL https://raw.githubusercontent.com/tuancookiez-hub/HyAtlas-Memory/main/scripts/install.sh | bash
```

The script looks for a prebuilt release asset for your platform
(`hyatlas-go-<tag>-<os>-<arch>`). Release assets exist for Linux amd64, macOS
arm64 and Windows amd64. On any other platform it builds from source, which needs
Go 1.26+ and a C compiler. It installs the binary to a directory on your `PATH`,
and verifies the install by starting the server and probing `/healthz`.

- **Release binaries** are embedded builds and already carry the BGE-small model
  and onnxruntime, so the script downloads nothing else for them.
- **Source builds** fetch the model (~133 MB) and the onnxruntime 1.28.1 library
  that matches your CPU (Linux x64 or aarch64, macOS arm64, Windows x64 or arm64). Intel macOS has no
  onnxruntime package for this version, so the installer stops there with a clear error.
- If the model fetch fails (for example HuggingFace is blocked), the binary is still
  installed, the manual steps are printed, and the script exits with status 1.
  A missing Go or C compiler, or a failed source build, stops the script before
  anything is installed.

Useful env vars (installer only):

| Variable | Purpose | Default |
|---|---|---|
| `HYATLAS_VERSION` | Release tag to install | `v4.3.3` |
| `HYATLAS_INSTALL_DIR` | Where the binary goes | `~/.local/bin` (Windows: `%LOCALAPPDATA%\hyatlas`) |
| `HYATLAS_MODEL_DIR` | Where the installer caches the model (source builds) | `~/.hyatlas/models` (Windows: `%LOCALAPPDATA%\hyatlas\models`) |
| `HYATLAS_MODE` | Extraction mode written to the Hermes `.env`: `lite` \| `pro` \| `ultra` | `ultra` (asked interactively) |
| `HYATLAS_LLM_BASE`, `HYATLAS_LLM_MODEL`, `HYATLAS_LLM_KEY` | LLM endpoint, model and key (asked interactively if unset) | *(none)* |
| `HYATLAS_NO_MODEL=1` | Skip the model download on source builds | (downloads) |

The server's own variables (`HYATLAS_SYNC_EXTRACT`, `HYATLAS_CONSOLIDATE_*`,
`HYATLAS_RAW_RETENTION`, and the rest) are listed in the configuration table below.

---

### Manual install (3 steps)

<details>
<summary><strong>Step 1 — Prerequisites</strong> (click to expand)</summary>

| OS | What's needed | Install command |
|---|---|---|
| **Linux** | Go 1.26+, gcc (for cgo) | `apt install golang-go gcc` (or distro equivalent) |
| **macOS** | Go 1.26+, Xcode CLI tools | `xcode-select --install` + install Go 1.26+ from [go.dev](https://go.dev/dl/) |
| **Windows** | Go 1.26+, MinGW-W64 (for cgo) | `winget install BrechtSanders.WinLibs.POSIX.UCRT` then restart terminal |

> **cgo is required** — onnxruntime-go links against the platform's C runtime via cgo. You need a C compiler (gcc, clang, or MinGW-W64).

</details>

<details>
<summary><strong>Step 2 — Build</strong></summary>

```bash
git clone https://github.com/tuancookiez-hub/HyAtlas-Memory.git
cd HyAtlas-Memory
go build -o hyatlas-go .                     # plain build (small; reads the model from a models/ folder at runtime)
go build -tags embedded -o hyatlas-go .       # embedded build (about 160 MB, model and onnxruntime bundled in)
```

> **For the embedded build to compile**, `models/` must contain four files, because
> `go:embed` lists them by name:
> `bge-small-en-v1.5.onnx`, `bge-small-en-v1.5.onnx.data` (an empty file is fine),
> `vocab.txt`, and the onnxruntime library for your platform: `libonnxruntime.so`
> on Linux, `libonnxruntime.dylib` on macOS, `onnxruntime.dll` on Windows. See
> [Model assets](#model-assets) for where to get them.

</details>

<details>
<summary><strong>Step 3 — Run</strong></summary>

```bash
# Local BGE embeddings (the "no Python" path) are the default. Set HYATLAS_MODEL_DIR
# only for a plain build whose model is not in one of the search-order folders in
# "Model assets" below. An embedded build ignores it.
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

The server listens on `127.0.0.1:19528` (loopback only by default). It has no
authentication, so keep it on loopback. Use `HYATLAS_GO_HOST` to change the bind
address only if you understand the exposure.

**Who may talk to it.** The server refuses requests that a web page could make on
your behalf, on every bind address:

- A `Host` header that names a DNS name is refused with 403, including on
  `/healthz`. Only `localhost` and IP literals pass. This blocks DNS rebinding.
  To accept a hostname, such as a LAN name or a reverse proxy, list it in
  `HYATLAS_ALLOWED_HOSTS`. Entries are hostnames (no scheme, and any port in an
  entry is ignored). Then the server accepts that name.
- An `Origin` header that is not loopback, not the request's own host, and not in
  the allowlist is refused with 403. A `null` or non-http(s) origin is refused too.
- A request with `Sec-Fetch-Site: cross-site` is refused with 403.
- Requests with no `Origin` header (curl, the Hermes plugin) are not affected by the
  `Origin` rule, and the server's own `/dashboard/` pages are not affected.

Each write returns an `extraction_status`: `pending` (extraction runs in the
background), `done` or `failed` (the write waited for extraction), `unconfigured`
(no LLM endpoint, model or key), or `skipped` (lite mode).

### Privacy — what leaves your machine

The server binds loopback only, but **loopback is not the whole story**, and the
default configuration is not fully local:

| What | Goes where | Default | How to keep it local |
|---|---|---|---|
| **Memory text** (each write's text, sent for extraction) | The extraction LLM. Each write is one request, and a failed call or unparseable reply is retried once | **Nowhere** — no endpoint is shipped, so an unconfigured server makes no LLM call and reports `unconfigured` | Already opt-in: set `HYATLAS_LLM_BASE`/`_MODEL`/`_KEY` to choose where it goes. Point them at a local OpenAI-compatible server to keep it on-machine, or use `HYATLAS_MODE=lite` |
| **Stored facts and schema patterns** (ultra only) | The same LLM, by the consolidation pass. One request per owner (`user_id`/`agent_id` pair) whose facts changed. Each request carries up to `HYATLAS_CONSOLIDATE_BATCH` (200) facts of that owner, each cut to 400 characters with its fact and turn IDs, plus up to 20 existing schema patterns cut to 200 characters | Nowhere until an endpoint is configured | Use `pro` or `lite` |
| **Embeddings** (each memory's text, and each search query) | In-process BGE-small (onnxruntime-go) | **Local** — `HYATLAS_EMBED_BASE=bge`, no network | Already local. If you set `HYATLAS_EMBED_BASE` to a URL, the text goes there instead, in every mode, including `lite` |
| Stored memories, vector index, graph | `HYATLAS_GO_DATA` (default `./data`) | **Local** | Already local |
| Telemetry / usage reporting | — | **None.** The only outbound HTTP the server makes is to the LLM and embedding endpoints you configure | — |

So out of the box: **embeddings and storage are local, and extraction stays off
until you configure an endpoint.** Once `HYATLAS_LLM_BASE`, `HYATLAS_LLM_MODEL`
and `HYATLAS_LLM_KEY` are all set, every write in `pro` or `ultra` is sent to that
endpoint to derive facts, summaries and intentions. That is the point of the
feature, and it means choosing the endpoint is choosing where the text goes. The
endpoint is yours to choose: any OpenAI-compatible API, including one on your own
machine. To keep conversation text on the machine, set `HYATLAS_MODE=lite`: no LLM
call is made at all, so only the raw trace and local embeddings are stored.
(`lite` keeps text local only while `HYATLAS_EMBED_BASE` is `bge` or `local`; an
HTTP embedder receives the text too.)

The three modes form a ladder of reasoning scope, not of latency:

| Mode | LLM calls | Reasoning scope | Layers | Consolidation |
|---|---|---|---|---|
| `lite` | none | — | **1 / 7** — L2 Raw only | no |
| `pro` | one per write | within one turn | **5 / 7** — L1, L2, L3, L4, L7 | no |
| `ultra` *(default)* | one per write **+** periodic batch | **across memories and time** | **7 / 7** at steady state | **yes** |

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
| `HYATLAS_CONSOLIDATE_BATCH` | `200` | max facts per owner per consolidation call |
| `HYATLAS_RAW_RETENTION` | *(unset = never delete)* | age after which uncited L2 Raw is deleted |

Durations are Go durations (`30m`, `6h`, `2160h`) or a plain number of seconds.
Go has no `d` unit, so write 90 days as `2160h`. A value that does not parse is
logged and treated as unset, so raw decay stays off.

`HYATLAS_CONSOLIDATE_BATCH` caps the facts one owner sends per call. An owner with
more facts than the cap is consolidated in successive windows across passes, not in
one call.

`HYATLAS_RAW_RETENTION` is opt-in because it deletes. An L2 raw row is kept if a
live graph edge cites it (as its primary source or any of its sources), or if a
live fact (L1 or L3) was extracted from it. Only raw rows that are old and cited by
nothing are removed. The decay pass runs once per consolidation pass, so raw rows
can age out only when a pass runs.

Facts that the ultra pass merges or drops are superseded, not deleted: they keep
`invalid_at` and `superseded_by`, and `GET /api/v1/list` returns them only with
`include_superseded=true`.

The `7 / 7` above is steady state, not the first minute. A fresh ultra install
sits at 5/7 until the slow path first runs, which is up to `6h` away by default. L5
and L6 are filled only when a pass finds corroborated relations and recurring
patterns. To run a pass now, instead of waiting for the tick:

```bash
curl -X POST http://127.0.0.1:19528/api/v1/digest
```

The response wraps the pass in `report`: `digest_ok: true`, then `report` with
`facts_in`, `owners_run`, `owners_unchanged`, `skipped_owners`, `merged`, `edges`,
`dropped`, `schemas`, `arc`, `pruned_raw`, `protected_raw`, `duration_ms` and
`errors`. `edges` counts distinct L5 edges created. Re-citing an existing edge adds
evidence and is not counted.

`/api/v1/status` carries `consolidations` (the number of completed passes) and
`last_consolidated` (the last report), so "the slow path ran" is checkable rather
than something you have to take on faith. Both reset when the server restarts, so
they count passes since the current process started. `consolidations` is `-1` when
the mode has no slow path (`lite` and `pro`). A pass that finds nothing to reconcile
still reports as run with zero changes, which is what distinguishes it from a pass
that never happened. An owner whose facts have not changed since its last successful
pass counts under `owners_unchanged` and is not sent to the LLM again.

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
server process. The plugin forwards only the non-secret LLM endpoint and model
settings, and only when you have set them in the plugin config. It never forwards
the LLM key, which reaches the server only if you export `HYATLAS_LLM_KEY` (or
`HYATLAS_LLM_KEY_FILE`) yourself.

**All configuration is via environment variables** — the binary takes no CLI flags:

| Variable | Default | Purpose |
|---|---|---|
| `HYATLAS_GO_PORT` | `19528` | HTTP listen port |
| `HYATLAS_GO_HOST` | `127.0.0.1` | Bind address (loopback only by default) |
| `HYATLAS_ALLOWED_HOSTS` | *(empty)* | Comma-separated hostnames the server accepts in `Host` and `Origin`, in addition to localhost and IP literals. A DNS name not listed here gets 403 on every route, including `/healthz`. Entries are hostnames; a port in an entry is ignored. Add a name only if you reach the server through it, such as a reverse proxy or a LAN hostname. |
| `HYATLAS_GO_DATA` | `./data` | Data directory (relative paths resolve against the working directory): chromem collections, `doc_index.json`, `consolidate_state.json`, and by default `graph.json` |
| `HYATLAS_GRAPH_PATH` | `<data>/graph.json` | L5 graph store location |
| `HYATLAS_EMBED_BASE` | `bge` | `bge` = local in-process BGE embedder (no network). Set to a URL for an OpenAI-compatible embedder, or `local` for a deterministic stub that is not semantic. |
| `HYATLAS_EMBED_MODEL` | `text-embedding-3-small` | Model name sent to an OpenAI-compatible embedder. Used only when `HYATLAS_EMBED_BASE` is a URL. |
| `HYATLAS_EMBED_KEY` | (empty) | Bearer token for that embedder. Used only when `HYATLAS_EMBED_BASE` is a URL. |
| `HYATLAS_MODEL_DIR` | *(unset: search order below)* | Where the BGE model lives, for a plain build. When set, it is the only directory searched. When unset, the server looks in `<cwd>/models`, then `<exe dir>/models`, then `~/.hyatlas/models` (Windows `%LOCALAPPDATA%\hyatlas\models`). An embedded build ignores this variable. |
| `HYATLAS_MODE` | `ultra` | `lite` \| `pro` \| `ultra`. An unrecognised value is fatal at startup. See the mode table above. |
| `HYATLAS_SYNC_EXTRACT` | *(follows the mode)* | `on` \| `off`. Whether a write waits for extraction. An unrecognised value is fatal at startup. |
| `HYATLAS_CONSOLIDATE_EVERY` | `6h` | Ultra only: interval between slow-path passes. Must be positive in ultra (zero or negative is fatal at startup); use `pro` to run without the pass. |
| `HYATLAS_CONSOLIDATE_BATCH` | `200` | Ultra only: max facts per owner per consolidation call. Larger owners are consolidated in successive windows. |
| `HYATLAS_RAW_RETENTION` | *(unset = never delete)* | Ultra only: age after which an L2 raw row cited by no live edge or fact is deleted |
| `HYATLAS_LLM_BASE` | *(unset)* | OpenAI-compatible LLM endpoint. **Memory text is sent here once you set it** — see *Privacy* above. Unset means no LLM call at all. |
| `HYATLAS_LLM_MODEL` | *(unset)* | LLM model name. Base, model and key must all be set for extraction to run. |
| `HYATLAS_LLM_KEY` | (empty) | LLM bearer token |
| `HYATLAS_LLM_KEY_FILE` | (empty) | Read the key live from this file on every call (rotating creds, e.g. Hermes auth.json). Accepts `providers.nous.agent_key` / `access_token` JSON, or a plain-text token. Wins over `HYATLAS_LLM_KEY`, which becomes the fallback if the file cannot be read. |

**Windows batch runner** (`scripts/hyatlas-v4-start.bat`) is a developer script
with hard-coded paths (`F:\HyAtlas-Memory-Go`, and a Hermes `auth.json` under one
user's profile). Edit those paths before using it. It sets the variables above for a
local run and reads the Nous Portal agent key from Hermes `auth.json`:
```bat
scripts\hyatlas-v4-start.bat
```

---

## Architecture

| Layer | What it holds |
|---|---|
| **L1 Profile** | User preferences, style, constraints (mirrors L3 facts labelled `user_preferences`) |
| **L2 Raw** | Every incoming memory as-is |
| **L3 Fact** | LLM-extracted factual atoms |
| **L4 Summary** | LLM-extracted session summaries (**enabled in v4; was dormant in v3.5**) |
| **L5 Knowledge** | Entity/relation graph (JSON-persisted). Built by the ultra slow path from relations corroborated across at least two turns, each with evidence citations |
| **L6 Schema** | Recurring patterns generalised by the ultra slow path |
| **L7 Intention** | LLM-extracted current goal / next step |

### Stack

- **Vector store:** [Chromem-go](https://github.com/philippgille/chromem-go) v0.7.0 — embedded, disk-persisted, no server process
- **Embeddings:** `bge/bge.go` — BGE-small-en-v1.5 (384-dim) via onnxruntime-go (cgo). WordPiece tokenizer, mean-pool, L2-normalize. No Python.
- **LLM extraction:** Via any OpenAI-compatible endpoint. One structured call per write (pro, ultra) fills L3 Fact, L4 Summary, L7 Intention and, for user preferences, L1 Profile from the L2 Raw entry. Blocking or background follows `HYATLAS_SYNC_EXTRACT`. L5 and L6 come from the separate ultra consolidation pass.
- **Graph:** `graph/graph.go` — exact-match traversal over JSON-persisted nodes and relations, scoped per owner
- **HTTP:** Standard Go `net/http`. No framework.

### Differences from v3.5 (historical)

This table lists differences that the code in this repository shows. It does not
include benchmark numbers. The v3.5 comparison and its measurements are in
[V3_V4_COMPARISON.md](V3_V4_COMPARISON.md).

| | v3.5 (Python) | v4 (Go) |
|---|---|---|
| Runtime | venv, zvec, Kuzu, FastAPI, HTTP embed subprocess | One binary, no Python |
| Release binary | — | About 160 MB, embedded (model included). A plain build is small and needs a model folder |
| Listening port | see V3_V4_COMPARISON.md | `19528` by default |
| L4 Summary | Dormant | Active |
| Restart | — | Reloads the doc index and graph from the data directory |

---

## Model assets

The BGE-small model + the platform-matching onnxruntime shared library live in `models/` (gitignored). For a plain build, the server reads them at runtime. For an embedded build, `go:embed` bundles them into the binary at compile time, so the release binary (built with `-tags embedded`) needs no model folder at all.

**Where a plain build looks** (when `HYATLAS_MODEL_DIR` is unset), in order:

1. `<cwd>/models` (a checkout run from the repo root)
2. `<exe dir>/models` (the folder the binary sits in)
3. the installer's default: `~/.hyatlas/models`, or `%LOCALAPPDATA%\hyatlas\models` on Windows

If `HYATLAS_MODEL_DIR` is set, that directory is the only one searched. A plain build without a model in the searched places refuses to start and names every directory it checked. An embedded build never searches these places: it unpacks the bundled model into a temporary directory at startup.

**Directory layout (per platform):**

| OS | Models dir contents |
|---|---|
| Windows | `bge-small-en-v1.5.onnx` + `.onnx.data` + `vocab.txt` + `onnxruntime.dll` |
| Linux | `bge-small-en-v1.5.onnx` + `.onnx.data` + `vocab.txt` + `libonnxruntime.so` |
| macOS | `bge-small-en-v1.5.onnx` + `.onnx.data` + `vocab.txt` + `libonnxruntime.dylib` |

**Where to get them:**
- Model: `https://huggingface.co/Xenova/bge-small-en-v1.5/resolve/main/onnx/model.onnx` (saved as `bge-small-en-v1.5.onnx`) and `.../resolve/main/vocab.txt`. The release workflow and the installer use these URLs.
- onnxruntime 1.28.1: the release archives that `.github/workflows/release.yml` downloads, `onnxruntime-<platform>-1.28.1` from [microsoft/onnxruntime releases](https://github.com/microsoft/onnxruntime/releases). Take the `lib/` file for your platform (`onnxruntime.dll`, `libonnxruntime.so` or `libonnxruntime.dylib`).

**To regenerate the ONNX from the HuggingFace model**, see `export_bge_onnx.py`.

### Version pins (critical)

- `onnxruntime_go` **v1.32.0** — declares ONNX API 28
- `onnxruntime` **1.28.1** shared library — must expose API 28
- The `.data` external weights resolve relative to the process **CWD**. The embedder `chdir`s to the model dir while it loads the model, then changes back.

---

## What changed since v3.5.0

- **Pure Go rewrite** — single binary (about 160 MB embedded release), no Python venv, no zvec, no Kuzu, no FastAPI
- **In-process BGE embeddings** via onnxruntime-go (cgo) — no HTTP embed subprocess
- **L4 Summary enabled** — was dormant in v3.5
- **L5 bitemporal graph (v4.1.0+)** — every L5 relation carries its source L2 memory IDs plus a bitemporal timestamp; the `/api/v1/graph-as-of?ts=<unix>` endpoint lets you rewind the graph to any past moment.
- **Mind Palace (v4.1.0+)** — a temporal visualization of the L5 knowledge graph in the Hermes Desktop `hyatlas` pane. Its design is in [`plugins/hyatlas/desktop/SPEC.md`](plugins/hyatlas/desktop/SPEC.md). The Desktop app itself is not part of this repository.

## API Reference

Base URL: `http://127.0.0.1:19528`

Method notes: the handlers read their parameters from the query string, the JSON
body, or both. Where a method is shown, that is the one the Hermes plugin and the
docs use. Some handlers also accept other methods, so check `routes()` in `server.go`
if you depend on that. Every route is behind the Host/Origin guard described above.

| Method | Path | Parameters and response |
|--------|------|-------------------------|
| `GET` | `/healthz` | Liveness. Returns `{"status":"ok"}`. |
| `GET` | `/api/v1/status` | `status`, `version`, `vdb`, `embed`, `llm` (`ok` \| `unconfigured` \| `unused`), `llm_model`, `llm_base`, `mode`, `mode_detail`, `uses_llm`, `extract_sync`, `write_pipeline` (`ok` or `degraded: …`), `writes`, `searches`, `layers` (per layer, live rows; L5 = graph node count), `graph_nodes`, `graph_edges`, `consolidations` (`-1` without a slow path; resets on restart), `last_consolidated` (last report; omitted until a pass has run). `graph_nodes` and `graph_edges` count all owners. |
| `GET` | `/api/v1/metrics` | `layers`, `total`, `graph_nodes`, `graph_edges` (all owners). |
| `POST` | `/api/v1/add` | Body: `text` (or `data`, required), `user_id`, `agent_id`, `session_id`, `metadata`. Returns `success`, `memory_id`, `extraction_status` (`done` \| `failed` \| `pending` \| `unconfigured` \| `skipped`). A missing text returns 400. |
| `POST` | `/api/v1/search` | Body: `query` (required), `limit`, `layer`, `user_ids` and `agent_ids` (arrays; only the first entry of each is used). Returns `memories`: `profile`, `proactive` and `normal` lists, each item with `memory_id`, `content`, `score`, `layer`, `gmt_created`, `user_id`, `agent_id`. |
| `GET` or `POST` | `/api/v1/list` | Query or body: `layer`, `user_id`, `agent_id`, `limit` (default 20), `offset`, `include_raw` (default on; `false` hides L2 rows when no layer is given), `include_superseded` (`true` also returns rows the ultra pass merged or dropped, with `invalid_at` and `superseded_by`). The string `all` is not special here. Returns `total`, `offset`, `limit`, `memories`, `layers`, `graph_nodes`, `graph_edges`. |
| `POST` | `/api/v1/graph` | Body: `node` (the L5 node ID). Query or body: `user_id`, `agent_id` (`all` or empty means no filter). Returns `node`, `neighbors`, `node_count`, `edge_count` (for that owner), `extract_err`. |
| `GET` | `/api/v1/edges` | Query: `n` (default 500), `k_semantic` (default 3), `user_id`, `agent_id` (`all` = no filter). Returns `nodes`, `knowledge` (L5 relations, each with `sources`), `co_session`, `semantic`, `total_edges`. |
| `GET` | `/api/v1/learning/graph` | Query: `n`, `k_semantic`, `user_id`, `agent_id`. Returns the starmap feed: `nodes`, `edges`, `memory`, `stats`. |
| `GET` | `/api/v1/graph-as-of` | Query: `ts` (unix seconds, default now), `n` (default 500), `user_id`, `agent_id`. Returns `nodes`, `relations` and `as_of`, as they were at `ts` (world validity and recording axes). |
| `POST` or `DELETE` | `/api/v1/delete_all` | Query or body: `id`, `layer` (`*` = all), `user_id`, `agent_id`, `all` (`true`), `confirm` (`wipe-all`, the older spelling of `all`). Other methods return 405. A call with no scope (no `id`, `layer`, `user_id`, `agent_id`, or `all`) returns 400. Returns `deleted_count`. |
| `POST` | `/api/v1/reprocess` | Body: `ids` (explicit raw IDs; extracted rows are re-extracted too), or `max` (default 200). Without `ids`, takes the `max` most recent raw rows and skips those already extracted. Returns `reprocessed`, `failed`, `skipped`. In `lite` it extracts nothing and says so in `note`. |
| `GET` | `/api/v1/digest` | Slow-path state. Returns `digest_ok: true`, `runs`, `last`, `last_at`, `graph_nodes`, `graph_edges`. In `lite` or `pro` it returns `digest_ok: false` with a `reason`. |
| `POST` | `/api/v1/digest` | Runs one slow-path pass now (ultra only). Returns `digest_ok` and `report` (fields in *Ultra-only tuning* above). A pass already running returns `digest_ok: false` with a `reason`. The pass is bounded by a 10-minute timeout, and a client that disconnects does not cancel it. |

**Stubs.** These endpoints exist for the v3.5-shaped dashboard and return fixed values:

| Path | Returns |
|---|---|
| `/api/quality-metrics` | `{"available": false, "reason": "quality scoring not ported to v4 yet"}` |
| `/api/coding-count` | `{"count": 0}` (the coding layer does not exist in v4) |
| `/api/coding-memories` | `{"memories": [], "total": 0}` |

**Dashboard adapter endpoints.** The web dashboard at `/dashboard/` reads these,
which return v3.5-shaped data built from the v4 store:
`/api/status`, `/api/info`, `/api/memories`, `/api/layer-counts`, `/api/storage`,
`/api/metrics`, `/api/graph-counts`, `/api/layer-health`, `/api/l6-schemas`,
`/api/l5/graph`, and the three stubs above. `/api/graph-counts` reports
`l5_knowledge` and `relation_count` as totals over all owners.

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
    "user_ids": ["default"],
    "agent_ids": ["default"],
    "limit": 5
  }'
```

`user_ids` and `agent_ids` are arrays; the server scopes by the first entry of
each. A `user_id` or `agent_id` string is ignored by search.

Response shape:
```json
{
  "memories": {
    "profile": [...],
    "proactive": [...],
    "normal": [...]
  }
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
│  hyatlas plugin     │   JSON responses   │  chromem-go + BGE    │
│  (plugins/hyatlas/  │                    │  in-process          │
│   client.py)        │                    └──────────────────────┘
└─────────────────────┘
```

The `hyatlas` plugin (Python, in your Hermes install) calls HyAtlas v4's HTTP API.

### Wire it up

**1. Run HyAtlas v4** (see Quick start above).

**2. Install the plugin and select the provider.** The install identifier must
include the plugin's subdirectory, so Hermes scans only `plugins/hyatlas`:

```bash
hermes plugins install tuancookiez-hub/HyAtlas-Memory/plugins/hyatlas
hermes plugins enable hyatlas
hermes memory setup        # then choose "hyatlas"
```

The bare name `hermes plugins install hyatlas` works once the plugin is in the
Hermes catalog. Equivalent spellings, all resolving to the same clone plus
`plugins/hyatlas`:

```bash
hermes plugins install "tuancookiez-hub/HyAtlas-Memory#plugins/hyatlas"
hermes plugins install "https://github.com/tuancookiez-hub/HyAtlas-Memory.git#plugins/hyatlas"
```

Two forms do **not** work as intended:

- `tuancookiez-hub/HyAtlas-Memory` on its own (no subdirectory) clones the
  repository *root*, which has no plugin manifest at its top level, so it does not
  install the plugin.
- A bare relative path such as `./HyAtlas-Memory/plugins/hyatlas` is not treated
  as a filesystem path — `owner/repo[/subdir]` parsing turns it into
  `https://github.com/./HyAtlas-Memory.git`. To install from a local clone, use
  a `file://` URL with an explicit `#subdir` fragment:
  `file:///C:/path/to/HyAtlas-Memory#plugins/hyatlas`.

Installing and enabling alone does **not** activate memory — `hermes memory
setup` is what writes `memory.provider: hyatlas`:

```yaml
memory:
  memory_enabled: true
  provider: hyatlas
```

`hermes memory setup` prompts for each of the plugin's 14 settings (server host and
port, user and agent IDs, auto-start, binary path, launcher script, request timeout,
data directory, LLM endpoint, model, key, extraction mode, block on extraction). The
key goes to `.env` as `HYATLAS_LLM_KEY`. The other values go to
`$HERMES_HOME/hyatlas.json`. Plugin settings also live under
`plugins.entries.hyatlas.settings` in `<HERMES_HOME>/config.yaml`, and are editable in
**Desktop → Settings → Plugins → hyatlas**. An environment variable named in the
plugin README's settings table overrides them. The defaults (`127.0.0.1:19528`,
`auto_start: false`) work with no config at all.

If `server_host` is a DNS name, the server refuses it with 403 unless the server's
`HYATLAS_ALLOWED_HOSTS` lists that name. See *Who may talk to it* above.

The `hermes hyatlas` CLI appears only while `memory.provider` is `hyatlas`.

> There is no `memory.providers.hyatlas` block — the plugin does not read one.

**3. Restart Hermes.**

### Building a native `MemoryProvider` plugin

This is what [`plugins/hyatlas`](plugins/hyatlas) already is, so there is no
wrapper left to write — see *Hermes Integration* above. If you want to build
your own, that plugin is the reference: subclass
`agent.memory_provider.MemoryProvider`, implement the HTTP calls it makes (status,
add, search, list) against the API above, and call `ctx.register_memory_provider(provider)`
from `register(ctx)`.

---

## Migration from v3.5

HyAtlas v4 is a **binary replacement** for the Python v3.5 floor. The memory data formats are incompatible (zvec → Chromem, Kuzu → JSON graph). The HTTP routes are not identical: v4 adds endpoints (`/api/v1/digest`, `/api/v1/graph-as-of`, `/api/v1/learning/graph`) and changes some defaults, such as owner scoping on graph reads. The API table above is the reference.

**If you need to keep v3.5 running while testing v4**, use different ports. v4 listens on `localhost:19528` by default (`HYATLAS_GO_PORT` changes it). See [V3_V4_COMPARISON.md](V3_V4_COMPARISON.md) for the v3.5 side.

The full v3.5 → v4 side-by-side (architecture, performance, reliability, API compatibility) is in [V3_V4_COMPARISON.md](V3_V4_COMPARISON.md).

---

## Known gaps (honest)

- The slow path runs on a timer, not at startup: a fresh ultra server shows 5/7 layers until the first pass (up to `HYATLAS_CONSOLIDATE_EVERY` later). `POST /api/v1/digest` runs one on demand.
- `/api/quality-metrics` returns `{available: false}` (v3.5-only feature; the dashboard adapter's quality endpoint, not a `/api/v1/` route)
- Upscaling and codemode are not implemented (those were v3.5 features)
- The coding layer is not in v4; the `/api/coding-*` endpoints return empty results

---

## License

Apache 2.0 — see [LICENSE](LICENSE)

---

## Repository

```
https://github.com/tuancookiez-hub/HyAtlas-Memory
```
