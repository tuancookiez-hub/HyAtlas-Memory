---
name: hyatlas-memory
description: "Long-term memory stack for Hermes Agent — install, use, and troubleshoot HyAtlas-Memory (7-layer cognitive memory with System1/System2 dual processing, profile isolation, local in-process embedder)."
version: 3.5.0
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
- **Capture** — every conversation is broken into atomic facts across 7 memory layers (L1 profile → L7 intention, contiguous).
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

Single command. No PyPI involvement as of v3.5.0 — install is GitHub-direct.

```bash
pip install git+https://github.com/tuancookiez-hub/HyAtlas-Memory.git
hyatlas setup hermes -y
hyatlas venv setup      # recommended: isolated venv so heavy deps don't fight the host app
hyatlas start           # server :19527 + dashboard :8765
```

`hyatlas venv setup` requires [`uv`](https://docs.astral.sh/uv/) on PATH and creates `$HYATLAS_HOME/venv` with `[zvec,local-embed]`. After setup, `hyatlas start` uses it automatically and falls back to the current interpreter if no dedicated venv exists.

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
| `hyatlas search "..."` | Search memories (normal channel: L3 facts + L4 summaries + L5 knowledge) |
| `hyatlas list --layer L5` | List memories from a specific layer |
| `hyatlas venv setup` | Create the isolated dedicated venv (`[zvec,local-embed]`) |
| `hyatlas setup hermes -y` | Wire the plugin into Hermes (`memory.provider=hy_memory`) |
| `hyatlas --help` | Full command list |

Full help: `hyatlas <command> --help` (every subcommand is documented).

## HTTP API

Server listens on `127.0.0.1:19527`. Useful endpoints (all `GET` unless noted):

| Endpoint | Purpose |
|---|---|
| `/api/v1/status` | Health: `status`, `vdb`, `embed`, `llm`, `embed_dims`, `write_pipeline` |
| `/api/v1/search?q=<text>&agent_id=<id>` | Semantic search across the normal channel (L3/L4/L5) |
| `/api/v1/list?agent_id=<id>&limit=N` | List recent memories, with optional `include_raw=true` to see the original L2_RAW payload |
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

| Layer | Name | What lives here | Store | When populated |
|---|---|---|---|---|
| L1 | `l1_profile` | Stable profile snippets (name, locale) | Zvec | Seeded / extracted |
| L2 | `l2_raw` | Original raw text from the user/conversation | Zvec | Every turn, before any LLM |
| L3 | `l3_fact` | LLM-extracted atomic facts (**primary capture layer**; identity lives here too) | Zvec | System 1 on `add` (pro/ultra) |
| L4 | `l4_summary` | Session / rollup summaries | Zvec | Periodic rollups |
| L5 | `l5_knowledge` | Knowledge graph nodes — entities + relations | Kuzu | System 2 digest |
| L6 | `l6_schema` | Behavioral schemas ("When X, user Y…") | Kuzu | System 2 digest |
| L7 | `l7_intention` | High-level intentions / goals (experimental) | Kuzu | System 2 digest |

> **Layers are contiguous `L1-L7`.** The former `L0` numbering and `L4 IDENTITY` slot were retired; the indices were renumbered so there is no gap. L1-L4 live in zvec, L5-L7 in Kuzu.

**Search channels** (`client.search`): **Profile** = L1 + L6 · **Normal** = L3 + L4 + L5 · **Proactive** = L7 (off by default).

**Common gotcha:** a fresh write lands in **L2_RAW** first; System 1 promotes it to **L3** when extraction succeeds. If a memory exists at L2 but isn't surfacing in search, the LLM extractor likely rejected noisy input — list with `?include_raw=true` to see unprocessed L2 entries. Durable recall comes from L3 facts and the L5/L6 graph built by the digest.

## Local embedder (no API key)

The embedder runs **in-process** via sentence-transformers. No API key needed, no provider, no external call. The recommended local floor is `BAAI/bge-small-en-v1.5` (384 dims), stored in `agent_memories_384`. `hyatlas config embedder --preset large` remains available for users who deliberately want 1024d and accept the heavier model/runtime footprint.

If `embed: error` shows on `/api/v1/status`, the in-process wire failed. Diagnostic:

```bash
python -c "
from hyatlas_memory.integrations import wire_inprocess_embed
from hyatlas_memory.core.core.embed_service import EmbedService
wire_inprocess_embed(EmbedService)
print('wired:', getattr(EmbedService, '_inprocess_embed_wired', False))
"
```

Version matrix that works (as of v3.5.0 — the real floor):
- `transformers==5.14.1`
- `sentence-transformers==5.7.0` (the only line compatible with transformers 5)
- `huggingface-hub>=0.23.2` (transformers ≥5.5 requires hub ≥1.5)
- `tokenizers` (pulled to 0.22.x by transformers 5)
- `numpy<3`
- `torch`
- `zvec>=0.6.0` (Windows LOCK reopen fix; 0.5.1 could not reopen collections after a crash)

## LLM config

LLM is BYO key. Example configuration lives under `$HYATLAS_HOME/config/hy_memory.json` (the model below is an OpenRouter example — swap for whatever OpenAI-compatible endpoint you use):

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
| `$HYATLAS_HOME/config/hy_memory.json` | Active config (LLM key, embedder model, vector store) |
| `$HYATLAS_HOME/data/kuzu_db/` | Kuzu graph (L5–L7 entities, schemas, intentions, relations) |
| `$HYATLAS_HOME/zvec/agent_memories_<dims>/` | zvec in-process vector collection (L1–L4) |
| `$HYATLAS_HOME/logs/hy-memory_server.log` | Server log |
| `$HYATLAS_HOME/logs/dashboard.log` | Dashboard log |

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

**`vdb: error` or `Can't lock read-write collection`**
- First run `hyatlas stop`, verify no HyAtlas server process remains, then run `hyatlas start --detach`.
- Do **not** delete zvec `LOCK` marker files. They are zero-byte even while a healthy process owns the collection. zvec 0.6.0 reopens a force-killed collection once the prior process is actually gone; the v3.5.0 regression suite proves the record survives.

**`llm: error`**
- OpenRouter key invalid or rate-limited.
- Check `~/.hyatlas/config/hy_memory.json` `llm.api_key`.

**Search returns nothing after a write**
- New write went to L2_RAW but System 1 hasn't promoted it to L3 yet (or the extractor rejected noisy input).
- Pass `?include_raw=true` to see L2 entries, or write a clean factual sentence.
- Durable recall comes from L3 facts and the L5/L6 graph (built by the digest).

**Dashboard tabs show empty**
- Check profile dropdown — you may be on a profile with no data.
- Switch to `default` to see Hermes's main namespace.

**Port :19527 already in use**
- `hyatlas stop`, wait 5s, `hyatlas start`. If still stuck, check Windows `netstat -ano | findstr :19527` and kill the orphan PID.

## Versioning and releases

Releases are GitHub-only. No PyPI. Install is `pip install git+https://...` (no version pin → gets latest). To pin a specific version: `pip install git+https://github.com/tuancookiez-hub/HyAtlas-Memory.git@v3.5.0`.

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
- Python 3.10–3.12 (`requires-python = ">=3.10"`; CI tests 3.10/3.11/3.12).
- Tests: `pytest -v -m "not integration"` for unit (snapshot 2026-07-29: 102 passed, 14 skipped); `pytest -m integration` for live-stack tests (requires server running).
- Lint: `ruff check src/ tests/`. Format: `ruff format --check src/ tests/` (currently disabled in CI; many files need reformatting).

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