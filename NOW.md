# HyAtlas-Memory — NOW.md

## Current State (2026-07-07)
**v3.2.0 on `main` — Hermes-aligned digest, L4 retired, weekly evolution cron, graph L5/L6 verified.**

- Version: **3.2.0**
- Config: `vector_store.provider: zvec`, collection `agent_memories_1024`
- Zvec path: `~/.hyatlas/zvec/agent_memories_1024` (~6.5k docs)
- Qdrant: archived at `~/.hyatlas/archive/qdrant_v3_1_0_release.zip` (119 MiB); not started
- Model: `deepseek-v4-flash` @ Hyper | Embedder: local `BAAI/bge-large-en-v1.5`
- Tests: **65 passed, 4 skipped** | Ruff: clean

## Deep review (this session) — issues found & fixed
1. **Search empty on migrated data** — `_doc_to_node` choked on epoch-string timestamps; `payload_by_ids` crashed on `.meta_info`. Both fixed + tested.
2. **`config_cli validate` rejected `provider: zvec`** — now accepts `zvec|qdrant`.
3. **`default_config` hardcoded qdrant** — now zvec (matches shipped stack).
4. **`hyatlas doctor` reported Qdrant ✗ on zvec-only** — now provider-aware (Zvec store presence).
5. **Console TUI showed false Qdrant "down"** — health row now Zvec when provider=zvec.
6. **L1_RAW sweep hardcoded to Qdrant** — silently no-op'd on zvec (storage bloat). Now provider-aware; zvec path reuses live handle + `delete_by_filter`.
7. **Docs** — pyproject + README state Zvec is default.
8. **Runtime cleanup** — removed Qdrant runtime adapter path + `bm25_fastembed.py`. Qdrant is archive/migration-only; zvec is the sole runtime backend. `bm25.py` (in-memory write-time dedup) retained. Read keyword channel = Zvec native FTS.

## Live verification
- `search` (user `221727702992945152`) → 13 hits, real content
- Layer counts: l0=46, l2_fact=2519, l5_knowledge=1126
- `hyatlas status` → all healthy, Zvec active
- L1 sweep → no errors in log

## Verdict
Zvec **fully replaces Qdrant** for HyAtlas-Memory: no sidecar, in-process reads, native BM25, same recall, complete ops surface. Qdrant retained only as archived rollback. System is more complete than v3.0.0.

## Next moves (post-release)
1. **BM25 reader** — `HY_MEMORY_READER=hybrid_v2` + verify zvec FTS path
2. **Patch 28** — `hyatlas snapshot`, `hyatlas migrate layout`
3. **Remove Qdrant binary** from default install docs
