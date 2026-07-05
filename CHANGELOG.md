# Changelog

## [3.0.0] — 2026-07-06

### Major: Full SDK Fork — hy-memory 1.2.20 as First-Party Code

The hy-memory SDK (42,668 lines) has been forked into `src/hyatlas_memory/core/`.
HyAtlas-Memory no longer depends on the external `hy-memory` package. Every line
of code is now owned and maintained by HyAtlas.

#### What Changed

**SDK Fork (Phase 1)**
- Copied 70 files from hy-memory 1.2.20 wheel into `src/hyatlas_memory/core/`
- Stripped ~8,000 lines of dead backends: Chroma, FAISS, Tencent, Neo4j, MySQL, Redis, Memory cache, coding judge module, emotion analyzer, Chinese prompts, unused readers
- Coding judge hardcoded to `return False` — all writes use the normal memory path
- Factory files cleaned: only Qdrant, Kuzu, DisabledCache, SQLite remain
- Zero `from hy_memory` imports in active code paths

**DisabledCache Re-Added (Phase 2)**
- `cache_disabled.py` created with all no-op methods (removed in upstream 1.2.19)
- Registered in cache factory alongside SQLite
- Config validation accepts `"disabled"` backend

**23 Monkey-Patches → 13 First-Class Integrations (Phase 3)**
- 4 patches eliminated (solved upstream in 1.2.19): dedup module, L4 identity elimination, auto-forgetting (strength.py + intention.py), S2 ops logging
- 6 patches eliminated (already in 1.2.20): LLM extra_body env loading, L3 summary default off, in-process embed queue, dedup threshold env var, L1 shadow handling, L1 normal fallback
- 13 patches ported as first-class code in `integrations.py`:
  1. VDB circuit breaker (server resilience)
  2. L1_RAW rolling delete sweep
  3. L1_RAW dedup skip
  4. L5 auto-trigger
  5. L5 in-process extraction
  6. Graph endpoint (`/api/v1/graph`)
  7. L5/L6/L7 counts (raw Kuzu Cypher)
  8. S1 extractor L5 context
  9. User identity (alias expansion)
  10. LLM fast/smart model split
  11. DisabledCache kwargs tolerance
  12. Rerank stage
  13. L1_RAW normal fallback

**New Capabilities from 1.2.19/1.2.20**
- BM25 hybrid search (dense + keyword fusion at 0.6/0.4 weighting) — already integrated in `reader_hybrid_v2.py`, enabled via `HY_MEMORY_READER=hybrid_v2`
- Memory strength scoring (`strength.py`): `(1 + log(access_count)) × exp(-idle_days / 180)`
- L7 intentions in Qdrant VDB with lazy expiry to L2_FACT (moved from Kuzu graph)
- Profile evidence reverse lookup (`profile_evidence.py`)
- Token counting via tiktoken (`utils/token_count.py`)
- Audit logging (`utils/audit_log.py`): JSONL rotating log for pipeline events
- S2 operations JSON robust parsing for reasoning models (think blocks, code fences)
- L1_RAW multi-line message parsing fix

**Dependencies**
- Removed: `hy-memory` (forked into source)
- Added: `tiktoken>=0.5.0`, `fastembed>=0.2.0`
- Kept: `kuzu`, `qdrant-client`, `sentence-transformers`, `openai`, `pydantic`

#### Safety

- Git tag `v2.1.0-stable` preserves the pre-fork state
- Branch `feat/v3-fork` contains all v3.0.0 work
- Kuzu backup at `kuzu_db_backup_v2` (83 MB)
- Qdrant export at `qdrant_pre_v3.jsonl` (6,135 points, 185 MB)
- Rollback: `git checkout v2.1.0-stable` restores the entire 2.1.0 state

#### Test Results

- 38 passed, 14 skipped (matches 2.1.0 baseline)
- All 9 critical module imports verified
- Zero `from hy_memory` imports in active code paths
- Version consistency: 3.0.0 across `pyproject.toml`, `_version.py`, both `plugin.yaml` files

---

## [2.1.0] — 2026-07-05

### Runtime Layout Consolidation

- `HYATLAS_HOME` environment variable and `~/.hyatlas` runtime layout
- New CLI commands: `hyatlas config`, `hyatlas snapshot`, `hyatlas migrate layout`
- Config precedence system: CLI flags > env vars > `~/.hyatlas/config/.env` > `hy_memory.json`
- Multi-key LLM resilience: `llm.api_keys` list with automatic rotation
- Legacy deprecation warnings for old paths
- Data migration with snapshot + rollback contract

## [2.0.0] — 2026-06-21

### S-Class Memory Architecture

- 7-layer cognitive memory: L0 basic, L1 raw, L2 fact, L3 summary, L4 identity, L5 knowledge graph, L6 schema, L7 intention
- Qdrant vector store + Kuzu graph store + DisabledCache
- S1 extractor + S2 System2Writer pipeline
- L5 knowledge graph with entity extraction, resolution, and quality review
- Dashboard with layer counts, graph visualization, and timeline
