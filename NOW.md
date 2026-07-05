# NOW.md — HyAtlas-Memory Current State

## v3.0.0 Fork Complete (2026-07-06)

**Branch:** `feat/v3-fork`
**Version:** 3.0.0
**Status:** Ready for release — tests pass, imports clean, version consistent

### What Was Done

The hy-memory SDK (1.2.20, 42,668 lines) was forked into `src/hyatlas_memory/core/`.
23 monkey-patches replaced by 13 first-class integrations in `integrations.py`.
~8,000 lines of dead backends stripped. No external `hy-memory` dependency.

### Commits (feat/v3-fork)

```
8b85efc feat(v3): fix remaining hy_memory imports, bump to 3.0.0
44381ea feat(v3): port 13 patches as first-class integrations, remove hy-memory dep
408e14c feat(cache): re-add DisabledCache as first-class backend
6100cc6 feat(fork): import hy-memory 1.2.20 as first-party core
a9788d3 docs: v3.0.0 fork plan + memory design research
```

### Safety Net

- Tag: `v2.1.0-stable`
- Kuzu backup: `kuzu_db_backup_v2` (83 MB)
- Qdrant export: `qdrant_pre_v3.jsonl` (6,135 points)
- Rollback: `git checkout v2.1.0-stable`

### Next Steps

1. **Push to main** — merge `feat/v3-fork` into `main` (needs Tuna's approval)
2. **Live stack test** — start server with v3.0.0, verify add/search/digest work
3. **Enable hybrid_v2 reader** — set `HY_MEMORY_READER=hybrid_v2` for BM25 hybrid search
4. **Qdrant collection recreation** — create new collection with sparse BM25 vectors (existing collection degrades to dense-only, which is fine)
5. **L7 migration** — if L7 data exists in Kuzu, migrate to Qdrant VDB (currently 0 L7 points, likely no-op)
6. **Delete `patches.py`** — it's legacy and no longer imported, but kept for reference

### Not Yet Done (Post-Release)

- L5 in-process extraction needs live testing (MEMORY_L5_VERSION=2)
- Audit logging is wired but not yet called from extraction pipeline
- Rerank stage is wired but gated behind MEMORY_RERANK_ENABLED=true
- User identity alias expansion gated behind HYATLAS_USER_IDENTITY=1
- LLM fast/smart split requires fast_model config in hy_memory.json
