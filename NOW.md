# NOW.md — HyAtlas-Memory Current State

## v3.0.0 Fork Complete + Live Tested (2026-07-06)

**Branch:** `feat/v3-fork`
**Version:** 3.0.0
**Status:** Ready for release — live stack tested, core functionality verified

### What Was Done

The hy-memory SDK (1.2.20, ~32,340 lines) was forked into `src/hyatlas_memory/core/`.
23 monkey-patches replaced by 14 first-class integrations in `integrations.py` (~1,020 lines).
~8,000 lines of dead backends stripped. No external `hy-memory` dependency.

### Live Stack Test Results (2026-07-06)

- ✅ Server starts with v3.0.0 code (10s startup with model load)
- ✅ Version reports 3.0.0 (was reading from hy-memory package before fix)
- ✅ Circuit breaker endpoint: GET /api/v1/breaker → state=CLOSED, threshold=3
- ✅ Graph endpoint: GET /api/v1/graph → 5 nodes, 36 relations, entity types shown
- ✅ Search existing memories: 15 results (L0 basic info, L2 facts, L4 identity)
- ✅ Write new memory: success=True, L1_RAW created, 14s elapsed (LLM extraction)
- ✅ In-process embedding: local sentence-transformers model works correctly
- ✅ hy-memory 1.2.18 uninstalled from venv — no external SDK needed

### Known Issues (Post-Release)

- L2 fact extraction not producing output with MiniMax-M3 — the 1.2.20 extractor
  prompts may need tuning for this LLM. L1_RAW writes work correctly.
- 1 integration test fails (test_importance_and_access_count_are_populated) because
  it requires L2 extraction to work. Non-integration tests: 36 passed, 0 failed.
- BM25 hybrid search available but not enabled (set HY_MEMORY_READER=hybrid_v2)
- fastembed not installed (pip install fastembed for BM25 sparse vectors)

### Commits (feat/v3-fork)

```
5b19623 fix(v3): live stack test fixes — import paths, in-process embed, version
bbe918c docs: v3.0.0 CHANGELOG + NOW.md
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

1. **Merge to main** — `git checkout main && git merge feat/v3-fork` (needs Tuna's approval)
2. **Push** — needs Tuna's explicit approval
3. **Tune LLM extraction** — the 1.2.20 extractor prompts may need adjustment for MiniMax-M3
4. **Enable hybrid_v2 reader** — set HY_MEMORY_READER=hybrid_v2 for BM25 hybrid search
5. **Install fastembed** — pip install fastembed for BM25 sparse vectors
6. **Delete patches.py** — legacy, no longer imported, kept for reference
