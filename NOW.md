# HyAtlas-Memory — NOW.md

## Current state (2026-07-25, stability freeze)

**Version truth:** hyatlas-memory **3.5.0** (pip + source + live server).  
**Decision:** keep 3.5.0 **stable** — no further dependency upgrades for now.  
**3.5.0 floor:** `[zvec]` extra is **`zvec>=0.6.0`** (LOCK fix). Local stack is the reference stable install.

### Live stack (probed 2026-07-25)
- `/api/v1/status` → ok / vdb ok / embed ok / llm ok / write_pipeline ok
- Server `:19527` + Dashboard `:8765` healthy
- VDB: zvec **0.6.0**, collection `agent_memories_1024`, 1024d, ~2732 points
- Graph: Kuzu intact (reindex source of truth for VDB rebuild)

### Venv pins (freeze baseline)
| Package | Hermes venv | HyAtlas dedicated venv |
|---|---|---|
| hyatlas-memory | 3.5.0 | 3.5.0 |
| zvec | 0.6.0 | 0.6.0 |
| openai | **2.24.0** (hermes-agent pin) | **2.48.0** |
| aiohttp | 3.14.3 | 3.14.3 |
| numpy | 2.4.6 | 2.4.6 |

### Do not touch while freeze holds
- sentence-transformers / transformers / torch major bumps
- openai in Hermes venv (breaks `hermes-agent` pin)
- aggressive ST/transformers rewrites that float `huggingface-hub` to 1.x

### Recovery notes (this session)
- zvec 0.5.1→0.6.0 fixed LOCK open bug
- VDB rebuilt from Kuzu via `scripts/reindex_zvec.py` (2682/2682, 0 errors)
- Stale zvec probe collections archived under `D:/HyAtlas/.hyatlas/archive/zvec-cleanup-20260725/`
- Legacy Qdrant paths renamed to `*.legacy-20260725` (not deleted)
- pip metadata reconciled to 3.5.0 in both venvs; `hyatlas venv setup` embedder OK

## What is solid
- Zvec-only runtime
- Local BGE embedder (1024d) via dedicated venv / in-process wiring
- Write + search proven after reindex
- Canonical ops: `hyatlas start|stop|status|doctor|venv setup`

## Manual ops
- Digest: `python %LOCALAPPDATA%\hermes\scripts\run_hyatlas_digest.py`
- Status: `curl http://127.0.0.1:19527/api/v1/status`
- Restart: `hyatlas stop && hyatlas start --detach`

## Next (optional, non-dep)
- hybrid_v2 live E2E against running server
- Populate empty specialist profiles with real writes
- fastapi bump only if dashboard needs it (not part of freeze)
