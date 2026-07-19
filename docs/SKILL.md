---
name: hyatlas-memory
description: "Long-term memory stack for Hermes Agent — install, use, and troubleshoot HyAtlas-Memory (7-layer cognitive memory with System1/System2 dual processing, profile isolation, local in-process embedder)."
version: 3.4.5
author: Tuna Dev <tuancookiez@gmail.com>
license: Apache-2.0
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [memory, hyatlas, hermes-agent, long-term-memory, kuzu, zvec, sentence-transformers, bge, local-embed, knowledge-graph, profile-isolation]
    homepage: https://github.com/tuancookiez-hub/HyAtlas-Memory
    related_skills: [hermes-agent]
---

# HyAtlas-Memory

HyAtlas-Memory is a personal, local, single-user long-term memory stack for Hermes Agent. It is forked from the Hy-Memory 7-layer cognitive memory framework (Tencent Hunyuan, `memory.hunyuan.tencent.com`) and tuned for one user's daily, multi-session use. Includes the experimental L7 intention layer. Apache 2.0 licensed.

**What it gives Hermes:**

- **Auto-recall** — relevant memories are injected into the agent's context at the start of every turn (no tool call needed).
- **Capture** — every conversation is broken into atomic facts across 7 memory layers (L1 raw → L7 intention).
- **Background evolution** — System2 digests merge duplicates, resolve contradictions, refine the model of you over time.
- **Local, private** — your memories live on your disk under `~/.hyatlas/`. No API keys required for embedding (sentence-transformers runs in-process). LLM key is BYO (OpenRouter / OpenAI / anything OpenAI-compatible).
- **Profile isolation** — multiple agents (default, research, trading, etc.) each get their own memory namespace via `agent_id`. The dashboard lets you filter to one profile at a time.
- **Dashboard** — live view at `http://127.0.0.1:8765` with layer counts, recent activity, knowledge graph, settings.

**When to use this skill:**

- User asks about HyAtlas, hy-memory, hermes memory, or "my memory stack".
- User reports a memory issue: `embed: error` on `/api/v1/status`, search returns nothing, layer counts off, dashboard not loading.
- User wants to install / upgrade / uninstall HyAtlas.
- User wants to write a memory from the CLI or read from a specific layer.
- You're about to make a change that touches how memory flows (add a tool that calls `hermes memory write`, etc.) and need to know the conventions.

## Install

Single command. No PyPI involvement as of v3.4.5 — install is GitHub-direct.

```bash
pip install git+https://github.com/tuancookiez-hub/HyAtlas-Memory.git
hyatlas setup hermes -y
hyatlas start           # server :19527 + dashboard :8765
```

Verify:

```bash
hyatlas doctor
hyatlas status          # status=vdb=embed=llm all "ok"
```

Upgrade (preserves your `~/.hyatlas/` data):

```bash
pip install --upgrade --force-reinstall git+https://github.com/tuancookiez-hub/HyAtlas-Memory.git
hyatlas stop && hyatlas start
```

## CLI quick reference

| Command | What it does |
|---|---|
| `hyatlas start` | Start server + dashboard (detached by default) |
| `hyatlas stop` | Stop both |
| `hyatlas status` | One-line health: ports, status, embed, llm |
| `hyatlas doctor` | Fail-fast health check; exit 1 on issues |
| `hyatlas add "..."` | Write a memory fact |
| `hyatlas search "..."` | Search memories, returns L4 identity layer hits |
| `hyatlas list --layer L5` | List memories from a specific layer |
| `hyatlas setup hermes -y` | Wire the plugin into Hermes (`memory.provider=hy_memory`) |
| `hyatlas --help` | Full command list |

Full help: `hyatlas <command> --help` (every subcommand is documented).

## HTTP API

Server listens on `127.0.0.1:19527`. Useful endpoints (all `GET` unless noted):

| Endpoint | Purpose |
|---|---|
| `/api/v1/status` | Health: `status`, `vdb`, `embed`, `llm`, `embed_dims`, `write_pipeline` |
| `/api/v1/search?q=<text>&agent_id=<id>` | Semantic search, returns L4 hits |
| `/api/v1/list?agent_id=<id>&limit=N` | List recent memories, with optional `include_raw=true` to see the original L1_RAW payload |
| `/api/v1/vdb/layer_count?agent_id=<id>` | Per-layer VDB counts (l1..l7) |
| `/api/v1/profiles` | List known agent profiles and their counts |
| `/api/v1/layer-health?agent_id=<id>` | Both VDB and graph layer counts in one call |
| `/api/v1/add` (POST) | Write a memory: `{"text": "...", "agent_id": "default", "user_id": "hermes-user"}` |

All endpoints accept `?agent_id=<id>` to scope to a profile. Default is `default`.

Dashboard: `http://127.0.0.1:8765` — visualizes everything above. Profile dropdown in the top-right.

## Identity contract

When Hermes is the client, writes go to:

| Field | Value |
|---|---|
| `user_id` | `hermes-user` (override via `HY_MEMORY_USER_ID` env var) |
| `agent_id` | **`default`** — Hermes TUI / gateway writes here |
| `mode` | `ultra` (full L1→L7 pipeline) |

Do NOT use `default_agent` for Hermes writes. That namespace is reserved for legacy L5 blobs from before the agent_identity unification.

To check what's in each profile: `GET /api/v1/profiles` returns counts per agent_id.

## Layer model

Memories are stored across 7 layers. Knowing what each is for helps debugging:

| Layer | Name | What lives here | When populated |
|---|---|---|---|
| L1 | `l1_raw` | Original raw text from the user/conversation | Every turn, before any LLM |
| L2 | `l2_fact` | LLM-extracted atomic facts from L1 | System2 digest (background) |
| L3 | `l3_context` | Session context, conversation windows | During sessions |
| L4 | `l4_identity` | Stable user/agent identity facts (preferences, traits) | Replayed from L1 → L2 reconciliation |
| L5 | `l5_knowledge` | Knowledge graph nodes — entities + relations | Cross-domain sweeper, weekly |
| L6 | `l6_schema` | Schema evolution records | L5 in-process extraction |
| L7 | `l7_intention` | High-level intentions / goals | Experimental, weekly digest |

**Common gotcha:** `search` returns L4 hits by default. If a memory exists at L1 but isn't surfacing in search, it means System2 hasn't promoted it to L4 yet — wait for the weekly digest, or run a search with `?include_raw=true` to see unprocessed L1 entries.

## Local embedder (no API key)

The embedder runs **in-process** via sentence-transformers. No API key needed, no provider, no external call. Model: `BAAI/bge-large-en-v1.5` (1024 dims, 3.1s load on CPU). Matches the existing `agent_memories_1024` zvec collection.

If `embed: error` shows on `/api/v1/status`, the in-process wire failed. Diagnostic:

```bash
"C:/Users/tuanc/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe" -c "
from hyatlas_memory.integrations import wire_inprocess_embed
from hyatlas_memory.core.core.embed_service import EmbedService
wire_inprocess_embed(EmbedService)
print('wired:', getattr(EmbedService, '_inprocess_embed_wired', False))
"
```

Version matrix that works (as of v3.4.5):
- `transformers==4.46.3`
- `huggingface-hub<1.0,>=0.23.2` (not 1.x — that's the bug v3.4.2 fixed, v3.4.5 enforces)
- `tokenizers<0.21,>=0.20`
- `sentence-transformers==3.0.1`
- `numpy<3`
- `torch`

## LLM config

LLM is BYO key. Default config in `D:/HyAtlas/.hyatlas/config/hy_memory.json`:

```json
"llm": {
    "api_key": "sk-or-...",
    "model": "tencent/hy3:free",
    "base_url": "https://openrouter.ai/api/v1",
    "extra_body": {"reasoning_effort": "none", "include_reasoning": false}
}
```

The `extra_body` suppresses reasoning tokens that would otherwise consume the LLM's output budget before JSON. If you swap to a different provider, keep `extra_body` or shorten the System2 prompts.

## Files and paths

| Path | What |
|---|---|
| `D:/HyAtlas/.hyatlas/config/hy_memory.json` | Active config (LLM key, embedder model, vector store) |
| `D:/HyAtlas/.hyatlas/data/kuzu_db/` | Kuzu graph (L1 raw, L2 fact, relations) |
| `D:/HyAtlas/.hyatlas/data/zvec/` | zvec in-process vector store |
| `D:/HyAtlas/.hyatlas/logs/hy_memory.log` | Server log (rotates daily) |
| `D:/HyAtlas/.hyatlas/logs/dashboard.log` | Dashboard log |

Override `HYATLAS_HOME` to relocate everything (e.g., to another drive).

## Weekly digest

A script-only cron job runs every 7 days:
- Source: `scripts/run_hyatlas_digest_launcher.py` in repo
- Installed at: `%LOCALAPPDATA%\hermes\scripts\run_hyatlas_digest.py`
- Schedule: every 168h / 10080m
- Delivery: `discord:1523091423556276365` (same thread as smart-memory-prune)

Manual trigger (Windows):
```bash
python %LOCALAPPDATA%\hermes\scripts\run_hyatlas_digest.py
```

Do NOT run the digest from git-bash in background — MSYS path mangling breaks it. Use the launcher.

## Common errors and fixes

**`embed: error` on /api/v1/status**
- sentence-transformers import chain failed.
- Check version matrix above. v3.4.5+ should have correct pins.
- Restart: `hyatlas stop && hyatlas start`.

**`vdb: error`**
- zvec store unreadable (corruption or lock file).
- `hyatlas doctor` will tell you. `hyatlas stop && rm D:/HyAtlas/.hyatlas/data/zvec/LOCK && hyatlas start` as last resort (data-preserving).

**`llm: error`**
- OpenRouter key invalid or rate-limited.
- Check `~/.hyatlas/config/hy_memory.json` `llm.api_key`.

**Search returns nothing after a write**
- New write went to L1_RAW but System2 hasn't promoted to L4 yet.
- Either wait for the weekly digest, or pass `?include_raw=true` to see L1 entries.
- Force-promote by writing directly with `layer: L4` (rare; usually let the digest do it).

**Dashboard tabs show empty**
- Check profile dropdown — you may be on a profile with no data.
- Switch to `default` to see Hermes's main namespace.

**Port :19527 already in use**
- `hyatlas stop`, wait 5s, `hyatlas start`. If still stuck, check Windows `netstat -ano | findstr :19527` and kill the orphan PID.

## Versioning and releases

Releases are GitHub-only. No PyPI. Install is `pip install git+https://...` (no version pin → gets latest). To pin a specific version: `pip install git+https://github.com/tuancookiez-hub/HyAtlas-Memory.git@v3.4.5`.

Release flow:
1. Bump version in `pyproject.toml`, `_version.py`, both `plugin.yaml` files
2. Update CHANGELOG.md
3. `git tag -a v3.X.Y -m "..."`
4. `git push origin v3.X.Y`
5. Maintainer runs `gh release create v3.X.Y --notes-file <changelog-snippet>`

Latest release: see https://github.com/tuancookiez-hub/HyAtlas-Memory/releases

## Repository conventions

- Default branch: `main`. PRs target `main`.
- Apache 2.0 license (NOT MIT — note: there's a stale MIT-licensed `hyatlas-memory` package on PyPI from before the fork, different artifact, ignore it).
- Python 3.10–3.12 (CI tests all three).
- Tests: `pytest -v -m "not integration"` for unit; `pytest -m integration` for live-stack tests (requires server running).
- Lint: `ruff check src/ tests/`. Format: `ruff format --check src/ tests/` (currently disabled in CI; 116 files need reformatting).

## When to escalate vs. handle yourself

**Handle yourself:**
- Status check, install, upgrade
- Layer counts explanation
- CLI command lookup
- Profile isolation explanation
- Reading the config file

**Stop and ask the user:**
- The user wants to delete or migrate `~/.hyatlas/` data
- The user wants to add a new provider / change LLM model
- Something fails that's not covered above
- A schema or version migration is needed

**Never do:**
- Don't `rm -rf ~/.hyatlas` without explicit confirmation
- Don't upload to PyPI (the project does not publish there)
- Don't switch embedder to a different model without user confirmation (changes 1024-dim vector compatibility)

## How to install this skill

If you found this file on GitHub and want your Hermes to load it:

```bash
# In any Hermes chat:
/skills install https://raw.githubusercontent.com/tuancookiez-hub/HyAtlas-Memory/main/docs/SKILL.md --name hyatlas-memory

# Or manually:
mkdir -p ~/.hermes/skills/hyatlas-memory
curl -fsSL https://raw.githubusercontent.com/tuancookiez-hub/HyAtlas-Memory/main/docs/SKILL.md \
  -o ~/.hermes/skills/hyatlas-memory/SKILL.md
# restart Hermes
```

After install, this skill loads whenever Hermes sees a question about HyAtlas, hy-memory, memory issues, dashboard, or this stack in general. Hermes reads the `description:` field from YAML frontmatter to decide when to load it.