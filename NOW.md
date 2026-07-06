# HyAtlas-Memory — NOW.md

## Current State (2026-07-07)
**v3.0.0 on `main`. Live stack cut over to Zvec (`agent_memories_1024`). Qdrant still auto-started by `hyatlas start` as sidecar; vector reads/writes go through Zvec.**

- Branch: `main` (merged from `feat/v3-fork`)
- Tag: `v3.0.0` — https://github.com/tuancookiez-hub/HyAtlas-Memory/releases/tag/v3.0.0
- CI: All pass (3.10/3.11/3.12 — tests + ruff)
- Model: `deepseek-v4-flash` @ `https://hyper.charm.land/v1` (non-reasoning, clean JSON)
- Graph: 1,444 nodes, 6,374 relations
- Tests: 33 offline pass, 14 server-dependent (47 total), 5 skipped
- Server: Running on Zvec (`/api/v1/status` ok, `vdb_provider=zvec`, collection=`agent_memories_1024`)

## Zvec Spike Closeout (2026-07-06)
- Verdict: Zvec performance spike was legitimately good, but live integration is not production-ready yet.
- Worked: Qdrant → Zvec migration exported/imported 6,433 docs; count verification passed; fresh temp Zvec collections reopen correctly.
- Failed: live Zvec server repeatedly failed with `RuntimeError: Can't open lock file ... LOCK` after open/reopen cycles; failure is specific to production-path collection lifecycle, not Qdrant data loss.
- Fixed while debugging: `start_server.py` now gives config priority over stale `MEMORY_VECTOR_STORE`, and passes `MEMORY_COLLECTION_NAME`, `MEMORY_VECTOR_HOST`, `MEMORY_VECTOR_PORT` from `hy_memory.json`.
- Reverted runtime config to Qdrant for stability. Keep Zvec work as v3.1 spike until adapter has a reliable Windows close/reopen contract and startup tests.
- Tuna decision: once Zvec is implemented properly and passes lifecycle/E2E gates, remove Qdrant instead of keeping dual backends long-term.
- Zvec hardening pass added temp-store lifecycle tests, removed normal-startup LOCK deletion, unified runtime/migration path resolution, and fixed migration point-id/schema coercion. Full pytest: 52 passed, 5 skipped. Live runtime remains Qdrant (`agent_memories_1024`, 6,443 points).
- Added `hyatlas zvec doctor`, fixed status collection suffix handling, and proved a temp Zvec config can boot the real server on an isolated port. Full pytest: 57 passed, 5 skipped.
- **Live Zvec cutover (2026-07-07):** Archived poisoned `~/.hyatlas/zvec/agent_memories_1024` → `agent_memories_1024_poison_20260706_221610`. Promoted verified rehearsal store (copy) to canonical `agent_memories_1024`. Config `vector_store.provider=zvec`. `hyatlas start --detach` healthy; `/api/v1/status` reports `zvec`. `hyatlas zvec doctor` exits 1 while server holds collection lock (expected); subprocess reopen ok after stop.
- **Qdrant data note:** Standalone `qdrant.exe --config-path C:/qdrant/config.yaml` saw only 374 points in `C:/qdrant-data`; HyAtlas-managed Qdrant still reports ~6,465 — investigate storage path before relying on Qdrant for backup migration.

## Completed This Session
1. Diagnosed upstream hy-memory 1.2.20 — confirmed no think-block handling, no L5 graph, same Kuzu WAL bug
2. Switched LLM from MiniMax-M3 → deepseek-v4-flash (zero parse errors)
3. E2E verified: write → L2 → S2 digest → L5 graph (+61 nodes) → search → graph endpoint → circuit breaker
4. Fixed CI: ruff lint (per-file-ignores for forked core/, fixed our code properly)
5. Merged feat/v3-fork → main, tagged v3.0.0, pushed
6. Created GitHub release with infographic, updated notes for full v2→v3 journey
7. Verified no PII in release/infographic/changelog

## Next Moves
1. **Skip Qdrant on zvec-only start** (or document sidecar) — then remove Qdrant binary dep when comfortable
2. **Reconcile Qdrant storage paths** (`C:/qdrant-data` vs HyAtlas-managed data) before any backup migration
3. **MiniMax subscription expiry** — thinking-disableable model already on deepseek-v4-flash
4. **Patch 28** (deferred): `hyatlas snapshot`, `hyatlas migrate layout`
5. **BM25 activation**: install `fastembed`, set `HY_MEMORY_READER=hybrid_v2`

## Key Paths
- Repo: `F:\HyAtlas-Memory`
- Config: `C:\Users\tuanc\.hyatlas\config\hy_memory.json`
- Qdrant: `C:\qdrant\qdrant.exe --config-path C:\qdrant\config.yaml`
- Kuzu DB: `C:\Users\tuanc\.hy_memory\data\kuzu_db`
