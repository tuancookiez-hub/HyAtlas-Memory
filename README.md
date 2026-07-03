# HyAtlas-Memory

> A 7-layer cognitive memory for [Hermes Agent](https://github.com/NousResearch/hermes-agent), with System1/System2 dual processing, evolution chains, and a Kuzu graph backend.

<p align="center">
  <a href="https://tuancookiez-hub.github.io/tuandev-portfolio/"><img src="https://img.shields.io/badge/Built%20by-Tuan%20Dev-blueviolet?style=for-the-badge" alt="Built by Tuan Dev"></a>
  <a href="./LICENSE"><img src="https://img.shields.io/badge/License-Apache%202.0-blue?style=for-the-badge" alt="License: Apache 2.0"></a>
  <a href="https://github.com/tuancookiez-hub/HyAtlas-Memory/releases"><img src="https://img.shields.io/github/v/release/tuancookiez-hub/HyAtlas-Memory?style=for-the-badge" alt="GitHub release"></a>
</p>

<p align="center">
  <img src="./assets/01-hyatlas-system-overview.png" alt="HyAtlas-Memory complete system overview: 7-layer dual-path cognitive memory with System1/System2 processing, knowledge graph, and self-healing dashboard" width="600" />
</p>

## What it is

Hermes Agent is powerful. You tell it your preferences, your project structure, your coding conventions, but all of this only fits into 2200 char memory.md by default which is small. 

HyAtlas-Memory fixes this. It's a memory provider plugin that drops into Hermes Agent and gives it **persistent, structured memory across sessions**. After a few conversations, your agent knows your name, your stack, your working style, your active projects, and the decisions you've made — without you repeating yourself.

It doesn't just store raw text either. Every message you send flows through a pipeline that extracts facts, resolves conflicts, builds a knowledge graph, and stabilizes a long-term identity profile. The more you use it, the sharper the agent's understanding becomes.

**Three things happen automatically:**

1. **It remembers.** Every conversation is captured, broken into atomic facts, and stored across 7 memory layers.
2. **It recalls.** When you start a new message, relevant memories are injected into the agent's context before it responds — no tool call needed.
3. **It evolves.** Background processing merges duplicates, resolves contradictions, and refines the agent's model of you over time.

See it in action — a 19-second walkthrough of the live dashboard:

<p align="center">
  <img src="./assets/dashboard-demo.gif" alt="HyAtlas-Memory dashboard demo: animated 19-second walkthrough showing the splash screen, Overview tab with KPI cards and L0-L7 memory composition bar chart, navigation to Memory Observatory with the layered knowledge graph visualization, and recent ingestion feed" width="100%" />
</p>

## Quick start

```bash
git clone https://github.com/tuancookiez-hub/HyAtlas-Memory.git
cd HyAtlas-Memory
pip install -e .
hyatlas setup hermes        # installs plugin + config + tests auto-start
```

That's it. The first time you run Hermes after setup, the provider automatically starts Qdrant, the upstream server, and the dashboard in the background — no manual commands needed.

**If you want to see what's happening:**

```bash
hyatlas start           # start the stack (safe to Ctrl+C after "ready")
hyatlas console         # open the live status window (Ctrl+C to close)
hyatlas stop            # shut down the stack
```

> **`hyatlas start` is safe to close.** Once you see "ready", Ctrl+C or close the terminal — the services keep running detached. Use `hyatlas stop` when you actually want to shut them down.
>
> **`hyatlas console`** is read-only. Shows live service health and recent memory activity (writes, recalls, errors). Closing it does NOT stop the stack.

**Need Qdrant?** The setup wizard detects whether Qdrant is installed and guides you through installing it (download from [qdrant.tech](https://qdrant.tech/documentation/guides/install/), or `docker run -d -p 6333:6333 qdrant/qdrant`).

**Want Docker instead?** See [Path A — Docker](#path-a--docker-recommended) below.

**Configure (optional):** edit `~/.hermes/hy_memory.json` to add your LLM key:

```json
{
  "llm": {
    "api_key": "YOUR_LLM_API_KEY_HERE",
    "model": "gpt-4o-mini",
    "base_url": "https://api.openai.com/v1"
  },
  "embedder": {
      "model": "BAAI/bge-large-en-v1.5",
      "dims": 1024,
      "provider": "local"
    },
  "mode": "ultra",
  "vector_store": {"provider": "qdrant", "host": "127.0.0.1", "port": 6333}
}
```

> **Three modes:** `lite` (no LLM, embedding-only) · `pro` (LLM extraction per `add`) · `ultra` (pro + System 2 cognitive layer with Kuzu graph — default).

> **New in 2.0 (S-Class):** hybrid_v2 retrieval + optional cross-encoder rerank, 1024-d embeddings, **in-process L5** knowledge-graph writer (no batch lock), and **L4 identity** dedup + evolution chains. Upgrading from 1.x with existing data? See **[`docs/MIGRATION_v2_SCLASS.md`](docs/MIGRATION_v2_SCLASS.md)**.

---

### Path A — Docker (alternative)

For users who prefer containers. Everything isolated, no Python/Qdrant setup on host.

```bash
# 1. Get docker-compose.yml (one-time)
curl -O https://raw.githubusercontent.com/tuancookiez-hub/HyAtlas-Memory/main/docker-compose.yml

# 2. Configure your LLM key (one-time)
echo 'HY_MEMORY_LLM_API_KEY=***' > .env

# 3. Start the stack (Qdrant + upstream server + dashboard)
docker-compose up -d

# 4. Wait ~15s, then verify
curl http://127.0.0.1:8765/api/health
curl http://127.0.0.1:19527/info

# 5. Open the dashboard
open http://127.0.0.1:8765
```

**Common commands:**

```bash
docker-compose ps           # what's running
docker-compose logs -f      # follow all logs (Ctrl+C to exit)
docker-compose restart      # restart everything
docker-compose down         # stop (keeps data)
docker-compose down -v      # stop AND wipe all data
```

Data lives in `./qdrant_storage` and `~/.hy_memory` (mounted to the host), so it survives `docker-compose down`. To fully reset, use `down -v`.

### What's running where

| Service | Port | URL | Purpose |
|---|---|---|---|
| Qdrant | 6333 | `http://127.0.0.1:6333/dashboard` | Vector store (raw vectors + payload) |
| Upstream hy-memory | 19527 | `http://127.0.0.1:19527/info` | Embedding + LLM extraction + search |
| HyAtlas dashboard | 8765 | `http://127.0.0.1:8765` | Web UI: explore, observe, manage |

The dashboard is the main thing you'll interact with. The other two are infrastructure.

---

### Next: see your memories

Once running, your conversations automatically start filling the system with memories. To explore what's been captured:

1. Open the dashboard at `http://127.0.0.1:8765`
2. **Overview** — KPIs (total memories, by-layer breakdown, recent activity)
3. **Memory Observatory** — visual 3D graph of your memory corpus
4. **Explore Memory** — semantic search across all 7 layers
5. **Layers / Today / Settings / L5 Knowledge Graph** — deeper views

Logs go to `logs/` in the project root (local) or `docker-compose logs -f` (Docker).

### Memory recall is transparent

When your agent receives a message, HyAtlas-Memory injects relevant memories into the prompt as a `<relevant-memories>` block. The agent sees your past context without you doing anything.

### Search tool

Agents (or you in the TUI) can explicitly search memories:

```text
> /hy_memory_search preferences
[profile] User is Tuan, prefers direct action
[profile] User uses Hermes Agent
[normal] Working on HyAtlas extraction (2026-06-16)
```

### CLI

**Stack management** — the bundled `hyatlas` entry point:

```bash
hyatlas start           # start the full stack (Qdrant → server → dashboard)
hyatlas stop            # stop all services
hyatlas status          # check what's running
hyatlas console         # open live status window (Ctrl+C to close)
hyatlas doctor          # full health check
hyatlas setup hermes    # install plugin + config
hyatlas init            # interactive setup wizard
hyatlas --help          # show help
```

> **`hyatlas start`** — services run **detached**, so it's safe to Ctrl+C or close the terminal window once you see "ready". Use `hyatlas stop` when you actually want to shut down.
>
> **`hyatlas console`** — read-only status window. Shows service health and live memory activity (writes, recalls, errors). Closing it does **not** stop the stack.

**Memory operations** — read/write memories from any shell, cron job,
or another session. Mirrors Hindsight's `retain|recall|reflect` and
Memories.sh's `add|search|recall` patterns:

```bash
hyatlas memory write    "the fact to remember"
hyatlas memory recall   "your search query" --limit 5
hyatlas memory list     [--layer l2_fact] [--limit 20]
hyatlas memory reflect  "your query" --limit 10
hyatlas memory status

# Aliases for muscle memory:
hyatlas memory add      "..."    # same as write
hyatlas memory retain   "..."    # same as write (Hindsight-style)
hyatlas memory search   "..."    # same as recall
hyatlas memory find     "..."    # same as recall
hyatlas memory ls                   # same as list
```

The `write` command goes through the same LLM fact-extraction pipeline
as a Hermes conversation turn, so the memory lands in qdrant with proper
`layer`, `importance`, and `access_count` populated.

The `reflect` command outputs the exact `<relevant-memories>` block the
agent would inject into the system prompt for the same query — useful
for debugging recall quality.

**Provider config** — Hermes Agent's memory command:

```bash
hermes memory status   # show current memory provider config
hermes memory setup    # interactive provider selection
hermes memory off      # disable external provider
hermes memory reset    # erase built-in MEMORY.md / USER.md (NOT VDB)
```

### Manually writing memories from another session

If you prefer Python over a shell command (e.g., from a cron job, an
agent script, or Jupyter):

```python
from hyatlas_memory import HyMemoryProvider

provider = HyMemoryProvider()
provider.initialize(
    session_id="my-session-id",
    user_id="hermes-user",
    agent_identity="default",
)

provider.sync_turn(
    user_content="The user prefers Vue 3 + Composition API for new projects.",
    assistant_content="Noted.",
    session_id="my-session-id",
)
```

The upstream `hy-memory` server handles LLM-based fact extraction,
importance scoring, and qdrant indexing automatically. ~8s indexing
delay before the memory shows on the dashboard.

For thin-client control (no provider, just the HTTP wrapper):

```python
result = provider._client.add(
    data=[{"role": "user", "content": "..."}, {"role": "assistant", "content": "..."}],
    user_id="hermes-user",
    agent_id="default",
    session_id="my-session-id",
)
```

**Important:** `user_id` and `agent_identity` must match your Hermes
profile. The default is `hermes-user` for the main profile.

## How it works

Memory flows through two parallel paths — a fast path for real-time awareness, and a slow path for deep consolidation:

<p align="center">
  <img src="./assets/02-dual-path-memory.png" alt="Dual-path memory: System 1 online fast path (L1 raw, L2 fact, fast recall injection) and System 2 background consolidation (L3 summary, L4 identity, L5 Kuzu graph, L6 schema, L7 intention)" width="900" />
</p>

**System 1 — Fast Path** handles every message you send. It captures raw text, extracts atomic facts via LLM, and injects relevant context back into the agent. This happens in milliseconds — you never wait for memory.

**System 2 — Background Consolidation** runs asynchronously. It takes the accumulated facts and builds something deeper: session summaries, identity profiles, a relationship graph, domain schemas, and proactive intent detection. This is where raw data becomes understanding.

## The 7 memory layers

Every piece of memory lives in one of seven layers, each with a specific purpose and trigger:

| Layer | Purpose | Triggers |
|-------|---------|----------|
| **L0 basic info** | Stable user facts (location, employer, equipment) | automatic |
| **L1 raw** | Verbatim session entries, time-ordered | every `add` |
| **L2 fact** | Atomic facts extracted by LLM | every `add` |
| **L3 summary** | Periodic L2 rollups (coherent narratives) | every 20 adds |
| **L4 identity** | Long-lived user/agent facts (preferences, persona) | automatic |
| **L5 pipeline** | Async ingest into Kuzu graph for relational queries | background |
| **L6 schema** | Typed entity/relationship schema | L5 step |
| **L7 intention** | Proactive intent detection, async tasks | L5 step |

L0–L2 run on the fast path (every message). L3–L7 run on the background path (async). L7 is an experimental extension — proactive intent detection that surfaces follow-up questions and task suggestions the agent should consider.

### Retrieval scoring (4-factor MemoryScorer)

The upstream `hy-memory` server ships a 4-factor `MemoryScorer` that ranks recalled memories:

```
0.50 × semantic    (vector similarity)
+ 0.30 × recency    (decay over time)
+ 0.15 × importance (per-memory score, this layer)
+ 0.05 × access     (recall-count boost)
```

HyAtlas-Memory populates the `importance` and `access` factors that upstream leaves zero by default, so the full scorer is active out of the box.

| Field | How it's populated | Default |
|---|---|---|
| `importance` | Layer-derived: `l4_identity=1.0`, `l2_fact=0.8`, `l3_summary=0.6`, `l0_basic_info=0.5`, `l1_raw=0.3` | **ON** |
| `access_count` | Incremented on every recall (fire-and-forget thread) | **ON** |

Both run on existing points too — a one-shot backfill (`scripts/backfill_importance.py`) populates them across the corpus, and new memories pick them up automatically on write.

**Disable either** with `=0`:

```bash
HYATLAS_MEMORY_IMPORTANCE=0    # disable layer-as-importance
HYATLAS_MEMORY_ACCESS_COUNT=0  # disable access-count tracking
```

No LLM cost. No added latency. Just better recall ordering — high-priority identity/fact memories no longer get outranked by raw fragments.

### Knowledge graph

The L5 pipeline builds a living graph of entities and their relationships — not just keyword matches, but typed semantic connections you can query:

<p align="center">
  <img src="./assets/03-knowledge-graph.png" alt="Knowledge graph: 8 node types (user, facts, preferences, events, projects, constraints, decisions, goals) connected by typed semantic edges, with LINK / QUERY / TRACE / REASON operations" width="900" />
</p>

The graph centers on the user and connects to facts, preferences, projects, events, goals, decisions, and constraints — each with typed edges like "works on", "likes", "drives", "limited by". You can query it directly:

- What are the user's top priorities?
- Show all constraints affecting Project X.
- What decisions influenced Goal Y?

## Memory evolution

HyAtlas-Memory doesn't just accumulate — it refines. Each `add` flows through a deterministic evolution pipeline:

1. **Extract** — pull atomic facts, entities, and context from new material
2. **Merge / dedupe** — combine duplicates, normalize, unify meaning
3. **Resolve conflicts** — weigh recency, confidence, and user feedback
4. **Stabilize identity** — update the long-term profile only when confidence crosses a threshold

The result is a signal-to-noise ratio that improves with every session. Raw conversation fragments get distilled into a small, queryable, evolving model of the user.

<p align="center">
  <img src="./assets/04-memory-evolution.png" alt="Memory evolution: raw fragments flow through extract → merge/dedupe → resolve conflicts into a stable identity profile; signal rises, noise falls" width="900" />
</p>

## Architecture

```text
   ┌──────────── Hermes Agent CLI / TUI ────────────┐
   │                                                │
   │   conversation →  MemoryProvider interface     │
   │                          │                     │
   └──────────────────────────┼─────────────────────┘
                              ▼
   ┌────────── HyAtlas-Memory (this package) ──────────┐
   │                                                    │
   │   L1 raw  →  L2 fact  →  L3 summary (every 20)    │
   │       │           │              │                 │
   │       └───── L4 identity  ◄──────┘                 │
   │                  │                                 │
   │            L5 pipeline (async, Kuzu graph)         │
   │            L6 schema                              │
   │            L7 intention (proactive)                │
   │                                                    │
   └────────────────────────────────────────────────────┘
```

### Source layout

```text
src/hyatlas_memory/        # the plugin (Python package)
  __init__.py              # HyMemoryProvider — entry point, registers with Hermes
  __main__.py              # `python -m hyatlas_memory` entry
  _start.py                # full stack startup logic (bundled for `hyatlas` CLI)
  _version.py              # version string (no-dep import)
  client.py                # HTTP client to the local server (urllib, zero deps)
  patches.py               # 9 carried patches + layer-as-importance + access-count
  context_pressure.py      # 4-tier token budget monitor (fastpath → emergency)
  process.py               # subprocess lifecycle for the local server
  embed_server.py          # local SentenceTransformers embedder (OpenAI-compatible)
  init_wizard.py           # first-run interactive setup wizard
  installer.py             # one-time pip-deps installer
  cli.py                   # `hermes hy-memory doctor|add|search|list|init|reset`
  start.py                 # thin wrapper for `hyatlas` console_scripts entry
  plugin.yaml              # legacy plugin manifest (kept for back-compat)

server/                    # standalone server (auto-started by plugin)
  start_server.py          # uvicorn launcher, reads hy_memory.json + .env
  bin/                     # L5 pipeline scripts (7-step graph rebuild)
  dashboard/               # local web UI (port 8765)
    dashboard.html         # HTML shell + page templates
    dashboard.py           # http server, auth, API endpoints
    app.js                 # shared state, navigation, overview, explore, layers, today, system
    styles.css             # all CSS
    js/l5.js               # L5 Knowledge Graph page
    js/observatory.js      # Three.js memory observatory (split from app.js for size)

tests/                     # pytest suite (16 unit + 4 integration = 20 tests)
  test_standalone.py       # version + plugin manifest + importable checks
  test_hy_memory_search.py # recall formatting, layered response shape
  test_integration.py      # end-to-end against live Qdrant + upstream server

scripts/                   # one-off ops scripts (out of CI lint scope)
  backfill_importance.py   # populate importance + access_count across corpus

docs/                      # architecture + migration notes
assets/                    # infographic images
```

### How the pieces fit

- **Plugin** (`src/hyatlas_memory/`) is a thin client. It implements the `MemoryProvider` interface that Hermes Agent calls. It doesn't do heavy lifting — it talks to a local server over HTTP.
- **Server** (auto-started on port 19527) runs the upstream `hy-memory` SDK. This is where embedding, LLM extraction, and vector search happen. The plugin manages its lifecycle as a subprocess.
- **L5 pipeline** (`server/bin/`) is a 7-step batch job that rebuilds the Kuzu graph: stop server → extract facts → resolve entities → quality review → rebuild graph → export JSON → restart server. Runs async, takes minutes for thousands of facts.
- **Context pressure** (`context_pressure.py`) monitors the agent's context window. At 50% usage it starts compressing old tool outputs to ref files. At 95% it aggressively prunes to prevent overflow. This is plugin-layer — no SDK changes needed.
- **16 patches** (`patches.py`) are applied at import time. They fix and extend the upstream SDK: LLMConfig env-loading, cross-encoder rerank, in-process embedding, L1 dedup/shadow/rolling-delete, **L5 in-process knowledge-graph extraction** (`l5_inprocess.py`, gated by `MEMORY_L5_VERSION=2`), **L4 identity** (pre-write dedup, `identity_type`, evolution-chain enrichment), a VDB circuit breaker, fast/smart LLM model split, and **robust S2 JSON parsing** (`s2_operations_json`). Each patch is idempotent and documented inline. The active set is logged at startup.

## Documentation

- **[docs/DASHBOARD.md](docs/DASHBOARD.md)** — Web UI reference (all 6 pages, Observatory controls, animations)
- **[docs/API.md](docs/API.md)** — HTTP API reference (every endpoint, request/response shapes)
- **[docs/LAYERS.md](docs/LAYERS.md)** — Per-layer deep-dive (L0–L7: what, where, when)
- **[docs/architecture.md](docs/architecture.md)** — System design + layer mapping vs official spec
- **[docs/TROUBLESHOOTING.md](docs/TROUBLESHOOTING.md)** — Common issues + fixes
- **[CONTRIBUTING.md](CONTRIBUTING.md)** — How to contribute, dev setup, PR process
- **[CHANGELOG.md](CHANGELOG.md)** — Version history

## Development

```bash
# 1. Clone + editable install
git clone https://github.com/tuancookiez-hub/HyAtlas-Memory.git
cd HyAtlas-Memory
uv pip install -e ".[dev,test]"

# 2. Run tests
pytest                     # 16 unit tests, ~0.1s, no external deps
pytest -m integration      # 4 integration tests, needs Qdrant + upstream running

# 3. Lint
ruff check .
mypy src/

# 4. Live reload during plugin dev
uv pip install -e . --force-reinstall
```

## Migration from in-fork plugin

If you were running the previous in-fork version (`plugins/memory/hy_memory/` inside the hermes-agent fork):

```bash
# 1. Backup the old plugin dir
mv hermes-agent/plugins/memory/hy_memory ~/hy_memory_archive_$(date +%Y%m%d)

# 2. Install this package
git clone https://github.com/tuancookiez-hub/HyAtlas-Memory.git
cd HyAtlas-Memory
pip install -e .

# 3. Your config and data stay where they were
#    ~/.hy_memory/      (data, Kuzu DB)
#    ~/.hy_memory.json  (config) -- the new package reads this unchanged
```

No data migration needed for the in-fork → package move. The Kuzu graph at `~/.hy_memory/data/kuzu_db` is forward-compatible. The carried patches from the fork are now part of the package, applied at import time via the `patches.py` module.

> **Upgrading 1.x → 2.0?** The v2 S-Class stack changes the default embedding dimension (1024-d) and adds the in-process L5 writer. If you have an existing 384-d Qdrant collection or an old Kuzu graph, **read [`docs/MIGRATION_v2_SCLASS.md`](docs/MIGRATION_v2_SCLASS.md) before upgrading production data.**

## License

Apache 2.0. See `LICENSE`.

## Credits

Built by [Tuan Dev](https://tuancookiez-hub.github.io/tuandev-portfolio/). Architecture inspired by the [Hy-Memory framework](https://memory.hunyuan.tencent.com) (Tencent Hunyuan) and the cognitive-architecture literature on dual-process theory (Kahneman's System 1 / System 2). The L7 intention layer is an independent extension not part of the official spec.

Uses:

- [Kuzu](https://kuzudb.com/) — embedded graph database (L1 raw + L5 graph)
- [Qdrant](https://qdrant.tech/) / [Chroma](https://www.trychroma.com/) / [FAISS](https://faiss.ai/) — vector store backends
- [SentenceTransformers](https://www.sbert.net/) — local embedding model
- [Hermes Agent](https://github.com/NousResearch/hermes-agent) — the host agent runtime

**Not affiliated with Tencent.** HyAtlas-Memory is an independent project; the Hy-Memory name is referenced to credit the architectural inspiration.
