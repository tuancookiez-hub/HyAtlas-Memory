# HyAtlas-Memory — NOW.md

## Current State (2026-07-07)
**v3.1.0 ready on `main`. Live vector store: Zvec (`agent_memories_1024`). Qdrant is optional cold backup only — not started when `provider=zvec`.**

- Version: **3.1.0** (Zvec-first stack, archive CLI, VDB dashboard API)
- Config: `~/.hyatlas/config/hy_memory.json` → `vector_store.provider: zvec`
- Zvec path: `~/.hyatlas/zvec/agent_memories_1024`
- Qdrant data (backup): `~/.hyatlas/data/qdrant` — archive via `hyatlas archive qdrant`
- Model: `deepseek-v4-flash` @ Hyper (non-reasoning)
- Tests: full suite green after 3.1.0 changes (see last `pytest -q` in session)

## v3.1.0 release checklist
1. [x] Zvec-only `hyatlas start` / status / stop legacy Qdrant
2. [x] `/api/v1/vdb/layer_count` + `/api/v1/vdb/scroll` on memory server
3. [x] Dashboard reads via VDB API (fallback to Qdrant HTTP)
4. [x] `hyatlas archive qdrant`
5. [x] CHANGELOG + version 3.1.0
6. [x] Live: stop → zvec-only start → archive `qdrant_v3_1_0_release.zip` (119 MiB) → probes OK

## Next moves (post-release)
1. **BM25** — `fastembed` + `HY_MEMORY_READER=hybrid_v2`
2. **Patch 28** — `hyatlas snapshot`, `hyatlas migrate layout` (deferred)
3. **Remove Qdrant binary** from default install docs once archive verified on Tuna's machine

## History (compressed)
- v3.0.0: full SDK fork, L5 graph, think-block parsing, tag `v3.0.0`
- Zvec spike → lifecycle hardening → live cutover 2026-07-07 (rehearsal store promoted)
- Poisoned zvec path archived as `agent_memories_1024_poison_*`