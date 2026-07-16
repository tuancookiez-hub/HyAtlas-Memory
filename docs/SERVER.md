# HyAtlas-Memory Server Components

> **This is a community implementation of the official [Hy-Memory framework](https://memory.hunyuan.tencent.com) (Tencent Hunyuan) for Hermes Agent.** The 6-layer model, the three operating modes (Lite/Pro/Ultra), and the evolution chain semantics are defined by the official framework; this server is the Hermes-targeted runtime that supports them. See the [official docs](https://memory.hunyuan.tencent.com) for the canonical architecture and the [project README](../README.md) for the local community-implementation notes.

This directory contains the standalone HyAtlas server launcher, the L5
async pipeline scripts, and the local dashboard. The plugin (in
`src/hyatlas_memory/`) auto-starts the server when needed, but you can
also run any of these scripts by hand for debugging, batch jobs, or
custom pipelines.

## Files

```text
start_server.py               # the standalone FastAPI/Uvicorn launcher
bin/hymemory.py               # the canonical CLI wrapper (start/stop/status)
bin/l5_batch_extract.py       # L5 step 1: extract facts from raw L1 entries
bin/l5_entity_resolver.py     # L5 step 2: resolve entity mentions to canonical IDs
bin/l5_relation_prototype.py  # L5 step 3: classify entity-pair relationships
bin/l5_ner_prototype.py       # L5 step 4: NER-based relation fallback
bin/l5_ingest_kuzu.py         # L5 step 5: ingest resolved facts into Kuzu graph
bin/l5_export_json.py         # L5 step 6: export graph state to JSON (backup/snapshot — no longer needed for dashboard viewing since Patch 23's live /api/v1/graph endpoint)
bin/l5_digest_writer.py       # L5 step 7: write digests to the L1 raw layer
bin/l5_full_pipeline.py       # runs all L5 steps in sequence
bin/l5_quality_review.py      # manual review tool for L5 outputs
bin/test_l5_trigger.py        # smoke test for the L5 trigger mechanism
dashboard/dashboard.py        # the local web dashboard (port 8765)
dashboard/dashboard.html      # the dashboard UI (single page)
```

## Quick start

```bash
# 1. Start the server (auto-managed by the plugin; manual is rare)
python server/start_server.py

# 2. Or use the CLI wrapper
python -m server.bin.hymemory start
python -m server.bin.hymemory status
python -m server.bin.hymemory stop

# 3. Run the L5 pipeline once
python -m server.bin.l5_full_pipeline

# 4. Open the dashboard
python -m server.dashboard.dashboard
# then open http://127.0.0.1:8765
```

## When to use each script manually

| Script | When to run by hand |
|--------|---------------------|
| `start_server.py` | When the plugin's auto-start fails, or for debugging port conflicts |
| `hymemory.py` | When the plugin thinks the server is running but it's actually down |
| `l5_full_pipeline.py` | After a large batch of adds, to force L5 ingestion immediately (otherwise runs on schedule) |
| `l5_quality_review.py` | When L5 outputs look off, to spot-check the extraction quality |
| `dashboard.py` | Always safe to run; useful for visualizing state without opening the TUI |

## Configuration

All scripts read the same config as the plugin:

```text
~/.hyatlas/config/hy_memory.json   # primary config (mode, vector store, etc.)
~/.env                        # API keys (HY_MEMORY_LLM_API_KEY, etc.)
```

The server uses `~/.hyatlas/` as its data directory by default. Override
with `HERMES_HOME=/path/to/alt/home` if needed.


## 3-tier status (added 2026-07-16)

`/api/v1/status` now returns 3 levels instead of binary ok/error:

```json
{
  "status": "ok|warning|error",
  "vdb": "ok",
  "embed": "ok",
  "llm": "ok|warning: ...|rate_limited: ...|error: ...",
  "write_pipeline": "ok|warning|rate_limited|error",
  "vdb_provider": "zvec",
  "vdb_collection": "agent_memories_1024",
  "embed_dims": 1024
}
```

- `ok` — all green
- `warning` — LLM throttled or partial failure; reads still work, new writes may be queued/degraded
- `error` — hard failure (VDB down or embed failure); status code 503

LLM rate limits no longer mark the whole stack `error: 503`. Previously persisted memories remain readable even when extraction is throttled.
