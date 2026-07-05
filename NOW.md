# NOW.md — HyAtlas-Memory v3.0.0

## Status: READY FOR RELEASE (2026-07-06)

**Branch:** `feat/v3-fork`
**Version:** 3.0.0
**Live tested:** Full pipeline working end-to-end with MiniMax-M3

### Live Test Results

- ✅ Server starts (10s with model load)
- ✅ Version: 3.0.0
- ✅ Circuit breaker: GET /api/v1/breaker → CLOSED, threshold=3
- ✅ Graph endpoint: GET /api/v1/graph → 5 nodes, 36 relations
- ✅ Search existing: 15 results (L0, L2, L4)
- ✅ Write: success=True, 9.65s (LLM extraction + embedding)
- ✅ **L2 fact extraction: WORKING** — "User prefers concise answers" (0.813) + "User working on HyAtlas" (0.457)
- ✅ In-process embedding: local sentence-transformers, no API
- ✅ hy-memory 1.2.18 uninstalled — zero external dependency

### Critical Fixes Applied

1. **bm25.py module** — reconciler imports `from . import bm25` for dedup scoring; module was missing
2. **Think block parsing** — MiniMax-M3 wraps output in `<think>...</think>`; _parse_json now strips these
3. **core/__init__.py version** — was reading from hy-memory package metadata, now reads from hyatlas_memory._version
4. **Relative imports** — 3 imports in integrations.py fixed (from ..core → from hyatlas_memory.core)
5. **In-process embedding** — 14th integration: local sentence-transformers replaces OpenAI API

### Commits

```
a2f3ce2 fix(v3): L2 extraction working — bm25 module + think block parsing
91dea94 docs: update NOW.md with live test results
5b19623 fix(v3): live stack test fixes — import paths, in-process embed, version
bbe918c docs: v3.0.0 CHANGELOG + NOW.md
8b85efc feat(v3): fix remaining hy_memory imports, bump to 3.0.0
44381ea feat(v3): port 13 patches as first-class integrations, remove hy-memory dep
408e14c feat(cache): re-add DisabledCache as first-class backend
6100cc6 feat(fork): import hy-memory 1.2.20 as first-party core
a9788d3 docs: v3.0.0 fork plan + memory design research
```

### Safety

- Tag: `v2.1.0-stable`
- Kuzu backup: `kuzu_db_backup_v2` (83 MB)
- Qdrant export: `qdrant_pre_v3.jsonl` (6,135 points)
- Rollback: `git checkout v2.1.0-stable`

### Next Steps

1. **Merge to main** — needs Tuna's approval
2. **Push** — needs Tuna's explicit approval
3. **Install fastembed** — `pip install fastembed` for BM25 sparse vectors
4. **Enable hybrid_v2** — set `HY_MEMORY_READER=hybrid_v2` for BM25 hybrid search
5. **Delete patches.py** — legacy, kept for reference
