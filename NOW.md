# HyAtlas-Memory — NOW.md

## Current State (2026-07-06)
**v3.0.0 released and pushed to GitHub. CI green.**

- Branch: `main` (merged from `feat/v3-fork`)
- Tag: `v3.0.0` — https://github.com/tuancookiez-hub/HyAtlas-Memory/releases/tag/v3.0.0
- CI: All pass (3.10/3.11/3.12 — tests + ruff)
- Model: `deepseek-v4-flash` @ `https://hyper.charm.land/v1` (non-reasoning, clean JSON)
- Graph: 1,444 nodes, 6,374 relations
- Tests: 33 offline pass, 14 server-dependent (47 total), 5 skipped
- Server: Down (stopped for WAL checkpoint verification)

## Completed This Session
1. Diagnosed upstream hy-memory 1.2.20 — confirmed no think-block handling, no L5 graph, same Kuzu WAL bug
2. Switched LLM from MiniMax-M3 → deepseek-v4-flash (zero parse errors)
3. E2E verified: write → L2 → S2 digest → L5 graph (+61 nodes) → search → graph endpoint → circuit breaker
4. Fixed CI: ruff lint (per-file-ignores for forked core/, fixed our code properly)
5. Merged feat/v3-fork → main, tagged v3.0.0, pushed
6. Created GitHub release with infographic, updated notes for full v2→v3 journey
7. Verified no PII in release/infographic/changelog

## Next Moves
1. **Restart server** when ready to use the memory system
2. **MiniMax subscription expiry** — switch to a thinking-disableable model (DeepSeek/Qwen/Kimi/Hunyuan), set `HY_MEMORY_THINKING_MODE=disabled`
3. **Patch 28** (deferred): `hyatlas snapshot`, `hyatlas migrate layout --dry-run/--apply/--rollback`
4. **Patches 29-31** (deferred): docs rewrite, legacy deprecation warnings
5. **BM25 activation**: install `fastembed`, set `HY_MEMORY_READER=hybrid_v2`

## Key Paths
- Repo: `F:\HyAtlas-Memory`
- Config: `C:\Users\tuanc\.hyatlas\config\hy_memory.json`
- Qdrant: `C:\qdrant\qdrant.exe --config-path C:\qdrant\config.yaml`
- Kuzu DB: `C:\Users\tuanc\.hy_memory\data\kuzu_db`
