# HyAtlas-Memory v3.0.0 — Full Fork & Upgrade Plan

## Decision Summary

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Fork from | hy-memory 1.2.20 (includes all 1.2.19 changes + 1.2.20 fixes) | 1.2.19 has major architecture improvements; 1.2.20 adds token counting, L1_RAW parsing fix, writer ops counts |
| Storage architecture | Qdrant (vectors) + Kuzu (graph) + DisabledCache (none) | 3-engine split — each does what it does best |
| L4 identity | Accept upstream: merged into L2_FACT | Upstream studied this; simpler extractor, no epistemic sub-typing needed |
| L7 intention | Accept upstream: moved from Kuzu to Qdrant | Lazy expiry, simpler graph dependency |
| Cache | Re-add DisabledCache (removed in 1.2.19) | No cache needed at our scale; direct calls are the architecture |
| BM25 | Adopt bm25_fastembed.py with Qdrant sparse vectors | S-class criterion; complementary to dense embeddings |
| Reader | Switch from reader_legacy to reader_hybrid_v2 | Has BM25 keyword channel built in |
| Audit logging | Add lightweight JSON file logging | Not through cache abstraction — direct file writes |
| Coding judge | Force off in source (not patch) | We don't use coding memory path |

## What We Keep vs Strip

### KEEP (62 files, ~29,660 lines)

**Core modules:**
- `client.py`, `config.py`, `server.py`, `inspector.py`, `metrics.py`, `runtime.py`

**Agent (extraction pipeline):**
- `extractor.py`, `llm_provider.py`, `mem_agent.py`, `reconciler.py`, `abstractor.py`, `reflector.py`, `summarizer.py`
- `tools/basic_profile.py`

**Core (scoring/merging/embedding):**
- `embed_service.py`, `merger.py`, `scorer.py`

**Data (storage backends we use):**
- `vector_store_qdrant.py`, `graph_store_kuzu.py`, `graph_store_base.py`, `vector_store_base.py`
- `cache_base.py`, `cache_sqlite.py` (kept for audit logging option)
- `history_store.py`, `kv_store.py`, `rdb.py`

**Models:**
- `memory.py`, `requests.py`

**Pipelines (S1/S2 + readers):**
- `writer.py`, `system2_writer.py`, `system2_agent.py`, `system2_tools.py`
- `reader_hybrid_v2.py` (new primary reader — has BM25)
- `reader_legacy.py` (kept as fallback)
- `cross_domain_sweeper.py`, `base.py`, `registry.py`

**Retrieval modules:**
- `scoring.py`, `evolution.py`, `reconcile_retrieval.py`, `trace.py`, `config.py`
- **NEW from 1.2.19:** `bm25_fastembed.py`, `dedup.py`, `entities.py`, `entity_store.py`, `intention.py`, `profile_evidence.py`, `strength.py`, `lemmatize.py`

**Utils:**
- `token_count.py` (from 1.2.20)

### STRIP (16+ files, ~8,283 lines)

| File | Lines | Why strip |
|------|-------|-----------|
| `data/vector_store_chroma.py` | 782 | We use Qdrant |
| `data/vector_store_faiss.py` | 596 | We use Qdrant |
| `data/vector_store_tencent.py` | 920 | We use Qdrant |
| `data/graph_store_neo4j.py` | 1,248 | We use Kuzu |
| `data/cache_mysql.py` | 513 | We use DisabledCache |
| `data/cache_redis.py` | ~0 | Already removed in 1.2.19 |
| `data/cache_memory.py` | ~0 | Already removed in 1.2.19 |
| `data/storage_interface.py` | ~0 | Already removed in 1.2.19 |
| `agent/emotion_analyzer.py` | 284 | Not imported |
| `agent/prompts_zh.py` | 1,201 | Chinese prompts, we use English |
| `pipelines/reader_exhaustive.py` | ~0 | Already removed in 1.2.19 |
| `pipelines/reader_hybrid.py` | ~0 | Already removed in 1.2.19 |
| `pipelines/reader_hybrid_tag.py` | 714 | Not used |
| `pipelines/reader_mem0.py` | 632 | Not used |
| `pipelines/reader_tencent_hybrid.py` | 413 | Tencent-specific |
| `pipelines/_retrieval/bm25_sparse.py` | 100 | Tencent-specific BM25 |

### Coding module — special handling

The coding module (judge, extractor, reconciler, writer, curator, store, preproc, types) is ~3,808 lines. We don't use the coding memory path. Two options:

**Option A: Strip entirely + hardcode `is_coding = False`** (preferred)
- Delete all coding/ files
- In `client.py`, replace `classify_messages_is_coding` import with `async def classify_messages_is_coding(*a, **kw): return False`
- Remove all `_get_coding_writer` lazy init code
- Saves ~3,808 lines

**Option B: Keep but force off**
- Keep files, just force judge to return False (our current patch approach)
- Less invasive but keeps dead code

We go with Option A. Clean removal.

## Patch Disposition: 23 Patches → First-Class Code

### DELETE (4 patches — solved upstream)

| # | Patch | Replaced by |
|---|-------|------------|
| 6 | `dedup_pre_search` | `pipelines/_retrieval/dedup.py` — first-class module |
| 14 | `l4_identity` | Upstream eliminated L4 identity; extractor merged into `memory` |
| 23 | `auto_forgetting` | `strength.py` (memory decay) + `intention.py` (L7 lazy expiry) |
| 18 | `s2_operations_json` | `cache_sqlite.py` has `store_memory_operation` + `store_pipeline_log` |

### REVIEW & SIMPLIFY (6 patches — partially addressed)

| # | Patch | Status in 1.2.19 | Action |
|---|-------|-----------------|--------|
| 2 | `llm_extra_body` | Env var loading added in config.py (line 263) | Verify; likely delete |
| 3 | `l3_summary` | `enable_summary` defaults to False globally | Verify; may simplify to env var |
| 5 | `inprocess_embed` | `embed_queued` batch queue exists | Verify; likely delete |
| 7 | `dedup_threshold` | `dedup.py` has `get_dedup_threshold()` with env var | Verify; likely delete |
| 10 | `l1_raw_shadow` | Shadow handling in writer.py | Verify; likely keep simplified |
| 17 | `l1_raw_normal_fallback` | L1_RAW excluded from normal path in reader | Already in source; delete |

### PORT AS FIRST-CLASS CODE (13 patches)

| # | Patch | Target file in fork | What to do |
|---|-------|---------------------|------------|
| 1 | `coding_judge` | `client.py` | Hardcode `is_coding = False`, remove coding imports |
| 4 | `rerank` | `pipelines/reader_hybrid_v2.py` | Add cross-encoder rerank stage after fusion |
| 8 | `l1_raw_rolling_delete` | `pipelines/system2_writer.py` | Add periodic shadow sweep daemon thread |
| 9 | `l1_raw_dedup_skip` | `client.py` (add method) | Pre-write similarity check, skip if duplicate |
| 11 | `llm_fast_smart` | `agent/extractor.py` + `pipelines/system2_writer.py` | Fast model for extraction, smart for reconciliation |
| 12 | `l5_auto_trigger` | `pipelines/system2_writer.py` | L5 pipeline trigger after S2 digest |
| 13 | `l5_inprocess` | `pipelines/system2_writer.py` | L5 extraction in S2 process (no server stop) |
| 15 | `vdb_circuit_breaker` | `data/vector_store_qdrant.py` | Circuit breaker for Qdrant failures |
| 16 | `disabled_cache` | `data/cache_disabled.py` (re-add) | Re-add DisabledCache, register in cache.py factory |
| 19 | `user_identity` | `pipelines/reader_hybrid_v2.py` | User identity enrichment in hybrid reader |
| 20 | `graph_endpoint` | `server.py` | `/api/v1/graph` endpoint for L5 dashboard |
| 21 | `l5_l6_l7_counts` | `client.py` | Layer count method for dashboard stats |
| 22 | `s1_extractor_entity_type` | `agent/extractor.py` | L5 context feedback into extractor prompt |

## Phase Breakdown

## Phase 0: Pre-flight & Safety Net (before any code changes)

**Goal:** Create recovery points and verify baseline.

**Steps:**
1. `git tag v2.1.0-stable` — mark the last known-good state
2. `git checkout -b feat/v3-fork` from `main`
3. Backup Kuzu: `cp -r C:/Users/<user>/.hyatlas/data/kuzu_db C:/Users/<user>/.hyatlas/data/kuzu_db_backup_v2`
4. Backup Qdrant config: `cp C:/qdrant/config.yaml C:/qdrant/config.yaml.v2backup`
5. Export all Qdrant points to JSONL: `python scripts/export_qdrant.py --output C:/Users/<user>/.hyatlas/snapshots/qdrant_pre_v3.jsonl`
6. Record point counts by layer (baseline for post-migration verification):
   - L0: 42, L1: 955, L2: 2376, L3: 309, L4: 1421, L5: 1023, L6: 0 (in Kuzu), L7: 0 (in Kuzu)
   - Total Qdrant: 6126 points
   - Total Kuzu L5 graph: 1342 nodes, 5736 relations
7. Run baseline test suite: `python -m pytest -q` — record pass count
8. Document rollback procedure: `git checkout v2.1.0-stable && pip install hy-memory==1.2.20`
9. Commit snapshot manifest as `docs: v3 pre-flight snapshot baseline`

**Verify:** All backups exist, test baseline recorded, rollback procedure documented

### Phase 1: Foundation — Copy & Strip (dispatch: work-backend with -w)

**Goal:** Get 1.2.20 SDK code into `src/hyatlas_memory/core/` with dead backends removed.

**Steps:**
1. Create `src/hyatlas_memory/core/` directory structure mirroring SDK layout
2. Copy all KEEP files from 1.2.20 extracted wheel (includes 1.2.19 architecture changes + 1.2.20 fixes: token_count.py, L1_RAW multi-line parsing fix, writer ops counts, extract_scene parameter)
3. The 1.2.20 wheel is at `C:/tmp/hydiff/120/` — use this as the source for ALL files
4. Strip all STRIP files (don't copy them)
5. Strip coding module entirely; hardcode `is_coding = False` in `client.py`
6. **Import path migration:** The SDK uses 387 relative imports (`from .xxx`) and only 6 absolute imports (`from hy_memory`). Relative imports work automatically after copy. Only 6 absolute imports need updating to `from hyatlas_memory.core`. Run `grep -rn "from hy_memory" src/hyatlas_memory/core/ --include="*.py"` to verify zero remaining after fix.
7. Update factory files to remove stripped backends:
   - `data/vector_store.py`: remove chroma, faiss, tencent branches; keep qdrant only
   - `data/graph_store.py`: remove neo4j, memgraph branches; keep kuzu only
   - `data/cache.py`: add `"disabled"` branch; keep sqlite; remove mysql
   - `config.py`: remove chroma/faiss/tencent/neo4j/mysql config fields and validation
8. Update `pyproject.toml` — remove `hy-memory` dependency, add `fastembed`, `spacy`
9. Verify package installs: `pip install -e .` — must succeed
10. Run type check: `python -m pyright src/hyatlas_memory/core/ 2>/dev/null || python -c "import hyatlas_memory.core.client"` — must import clean
11. Commit as `feat(fork): import hy-memory 1.2.20 as first-party core`

**Verify:** 
- `pip install -e .` succeeds
- `python -c "from hyatlas_memory.core.client import HyMemoryClient"` imports clean
- `grep -rn "from hy_memory" src/hyatlas_memory/core/ --include="*.py"` returns zero results
- `python -m pytest -q` passes (existing tests may need import updates)

### Phase 2: Re-add DisabledCache

**Goal:** Restore DisabledCache as a first-class backend.

**Steps:**
1. Create `data/cache_disabled.py` with the full DisabledCache implementation (~200 lines)
2. Register in `data/cache.py` factory: add `"disabled"` option
3. Set as default in config
4. Commit as `feat(cache): re-add DisabledCache as first-class backend`

**Verify:** `python -c "from hyatlas_memory.core.data.cache_disabled import DisabledCache"`

### Phase 3: Port 13 Patches as First-Class Code

**Goal:** All 13 remaining patches become real code in their target files.

This is the biggest phase. Split into sub-tasks for parallel dispatch:

**3a. Coding judge removal** (client.py)
- Remove `from .coding.judge import classify_messages_is_coding`
- Replace with inline `async def classify_messages_is_coding(*a, **kw): return False`
- Remove `_get_coding_writer` lazy init block
- Remove coding store/writer imports

**3b. L1_RAW management** (system2_writer.py + client.py)
- Port `l1_raw_rolling_delete` — daemon thread sweep, 6h interval, 30-day window
- Port `l1_raw_dedup_skip` — pre-write cosine check, skip if > threshold
- Port `l1_raw_shadow` — shadow status handling (verify if still needed)

**3c. L5 integration** (system2_writer.py + extractor.py + server.py)
- Port `l5_auto_trigger` — trigger L5 pipeline after S2 digest cycle
- Port `l5_inprocess` — L5 entity extraction inside S2 process using server's Kuzu connection
- Port `s1_extractor_entity_type` — L5 graph context fed back into extractor prompt
- Port `graph_endpoint` — `/api/v1/graph` in server.py for dashboard
- Port `l5_l6_l7_counts` — layer count method for dashboard stats

**3d. Resilience** (vector_store_qdrant.py + reader_hybrid_v2.py)
- Port `vdb_circuit_breaker` — circuit breaker class in vector store
- Port `rerank` — cross-encoder rerank stage in hybrid_v2 reader
- Port `user_identity` — identity enrichment in hybrid_v2 reader

**3e. Cost optimization** (extractor.py + system2_writer.py)
- Port `llm_fast_smart` — fast model for S1 extraction, smart model for S2 reconciliation

**Dispatch:** Each sub-task to `hermes -p work-backend -w -z "..."` in parallel (3a-3e are independent)
**Verify:** Full pytest suite after all sub-tasks complete. Each sub-task must also add tests for the specific patches it ports (circuit breaker, rolling delete, fast/smart routing, etc.). Run `python -m pytest -q` after each sub-task commit — must be green before merging.

### Phase 4: BM25 Hybrid Search + Data Migrations

**Goal:** Enable BM25 keyword search, migrate L7 from Kuzu to Qdrant, recreate Qdrant collection with sparse vectors.

**Step 4a: Install dependencies**
1. `pip install fastembed` (adds Qdrant/bm25 ONNX model — ~hundreds MB first download)
2. `pip install spacy && python -m spacy download en_core_web_sm` (English lemmatization)
3. Verify: `python -c "from fastembed import SparseTextEmbedding; e=SparseTextEmbedding('Qdrant/bm25'); print('ok')"`

**Step 4b: L7 Kuzu→Qdrant migration**
- Current state: L7_INTENTION has 0 points in Qdrant, 0 nodes in Kuzu graph endpoint
- L7 was written to Kuzu graph in v2, but the graph endpoint only serves L5
- L6_SCHEMA also has 0 points in Qdrant — both L6 and L7 live in Kuzu only
- **Migration script** `scripts/migrate_l7_kuzu_to_qdrant.py`:
  1. Connect to Kuzu (requires stopping hy-memory server for lock)
  2. Query: `MATCH (m:Memory) WHERE m.layer = 'l7_intention' RETURN m`
  3. For each L7 node: extract content, embedding, metadata → upsert to Qdrant as L7_INTENTION layer
  4. If L7 count is 0 (our current case): log "no L7 data to migrate" and skip
  5. Verify: `curl -X POST http://127.0.0.1:6333/collections/agent_memories_1024/points/scroll -d '{"filter":{"must":[{"key":"layer","match":{"value":"l7_intention"}}]},"limit":10}'` — count must match Kuzu source
  6. Do NOT delete L7 from Kuzu yet — keep as backup until Phase 7 verifies

**Step 4c: Qdrant collection recreation with sparse vectors**
- Current collection `agent_memories_1024`: 6126 points, dense vectors only, no sparse config
- **Migration script** `scripts/recreate_qdrant_collection.py`:
  1. Export ALL points to JSONL: scroll all 6126 points with payloads + vectors → `C:/Users/<user>/.hyatlas/snapshots/qdrant_full_export.jsonl`
  2. Verify export count: `wc -l qdrant_full_export.jsonl` must equal 6126
  3. Create new collection `agent_memories_1024_v3` with:
     - Dense vector: size=1024, distance=cosine (same as before)
     - Sparse vector: named "bm25", SparseVectorParams()
  4. Re-upsert all points in batches of 100:
     - Dense vector: from export
     - Sparse vector: `bm25_fastembed.encode_doc(content)` for each point
     - Skip L1_RAW for sparse encoding (upstream design — raw turns not searched)
     - Log progress every 500 points
     - Retry failed points 3× with exponential backoff
  5. Verify point count: `curl http://127.0.0.1:6333/collections/agent_memories_1024_v3` — must be 6126
  6. Verify sparse vectors: scroll 5 random points, check `vector.bm25` exists
  7. Switch collection alias: `agent_memories_1024` → `agent_memories_1024_v3` (Qdrant supports collection aliases)
  8. Old collection `agent_memories_1024` kept as backup until Phase 7 verifies
  9. If any step fails: restore from JSONL export, delete v3 collection, abort

**Step 4d: Switch reader and enable hybrid search**
1. Switch primary reader from `reader_legacy.py` to `reader_hybrid_v2.py` in client.py — behind config flag `MEMORY_READER=hybrid_v2` (default) or `legacy` (fallback)
2. Verify hybrid search: `curl http://127.0.0.1:19527/api/v1/search?q=Kuzu` — should return hits with both semantic and keyword scores
3. Verify legacy fallback: set `MEMORY_READER=legacy`, restart, verify search still works
4. Test exact entity search: "Kuzu", "Qdrant", "Aegis" — BM25 should boost these
5. Commit as `feat(bm25): enable hybrid dense+BM25 search via Qdrant sparse vectors`

**Dispatch:** `hermes -p work-backend -w -z "..."`
**Verify:** 
- Search for exact entity names ("Kuzu", "Qdrant", "Aegis") — should get precise hits
- Point count post-migration: 6126 (must match baseline)
- L7 count in Qdrant: must match Kuzu source (currently 0)

### Phase 5: L5 Custom Layer Integration

**Goal:** L5 knowledge graph works with the new architecture (L7 in VDB, L4 eliminated).

**Steps:**
1. Update L5 extraction prompt to match new extractor output (no more L4_IDENTITY)
2. Update L5 entity resolver to work with new dedup.py module
3. Update L5 in-process pipeline to use new strength.py for memory scoring
4. Update L5 graph endpoint to serve entity types and mention counts from extra_json
5. Update dashboard to show BM25 search stats
6. Commit as `feat(l5): integrate knowledge graph with v3 architecture`

**Dispatch:** `hermes -p work-backend -w -z "..."`

### Phase 6: Audit Logging

**Goal:** Pipeline logs and write records without the cache abstraction.

**Steps:**
1. Create `utils/audit_log.py` — lightweight JSONL logger
2. Log extraction results (what the LLM produced per add() call)
3. Log reconciliation results (add/supersede/update decisions)
4. Log S2 digest cycles (what was processed, what was written)
5. Log L5 pipeline runs (entities extracted, merged, ingested)
6. Log file rotates at 10MB, keeps 5 archives
7. Commit as `feat(audit): add JSONL pipeline logging`

### Phase 7: Test & Verify

**Goal:** Full test suite passes + live stack verification.

**Steps:**
1. Update all test imports from `hy_memory` to `hyatlas_memory.core`
2. Add tests for DisabledCache
3. Add tests for BM25 hybrid search
4. Add tests for L5 with new architecture
5. Add tests for audit logging
6. Run `python -m pytest -q` — all green
7. Run `uvx ruff check src/` — clean
8. Start live stack: `hyatlas start --detach`
9. Verify health: `curl http://127.0.0.1:19527/healthz`
10. Verify graph: `curl http://127.0.0.1:19527/api/v1/graph?n=10`
11. Verify search: `curl http://127.0.0.1:19527/api/v1/search?q=Kuzu`
12. Verify dashboard loads
13. Send a test message through Hermes, verify it gets ingested

**Dispatch:** `hermes -p work-qa -z "..."` for test writing
**Verify:** Orchestrator probes — real curl, real pytest, real health check

### Phase 8: Documentation

**Goal:** Update all docs to reflect v3.0.0.

**Steps:**
1. Update `README.md` — new architecture diagram, BM25, no more monkey-patches
2. Update `CHANGELOG.md` — v3.0.0 entry
3. Update `NOW.md` — current state
4. Bump version: `pyproject.toml`, `_version.py`, `plugin.yaml`, `hermes_plugin_shim/plugin.yaml`
5. Update `pyproject.toml` dependencies — remove `hy-memory`, add `fastembed`, `spacy`, `kuzu`, `qdrant-client`
6. Commit as `docs: v3.0.0 — forked SDK, BM25 hybrid, first-class patches`

## Dispatch Strategy

| Phase | Profile | Worktree | Parallel? | Est. turns |
|-------|---------|----------|-----------|------------|
| 0 | default | — | No (safety net) | 3-5 |
| 1 | work-backend | -w | No (foundation) | 10-15 |
| 2 | work-backend | -w | No (depends on 1) | 3-5 |
| 3a-3e | work-backend | -w | **Yes (5 parallel)** | 8-12 each |
| 4 | work-backend | -w | No (depends on 3) | 10-15 |
| 5 | work-backend | -w | No (depends on 3+4) | 6-10 |
| 6 | work-backend | -w | Yes (independent of 4-5) | 4-6 |
| 7 | work-qa | -z | No (after all phases) | 8-12 |
| 8 | default | — | No (after verify) | 3-5 |

## Risk Mitigation

| Risk | Mitigation |
|------|-----------|
| Import breakage after copy | Phase 1: only 6 absolute imports to fix; 387 relative imports work automatically. Verify with grep + pip install -e . |
| Patch port introduces bugs | Each sub-task committed separately; pytest after each. Each sub-task adds tests for its specific patches. |
| Qdrant collection recreation loses data | Phase 4c: full JSONL export before any changes; new collection created alongside old; alias switch only after verification; old collection kept as backup |
| BM25 fastembed download fails | Graceful degradation — `_supports_fulltext` falls back to false, system works as dense-only |
| Kuzu schema incompatibility | L5 uses existing schema; no Kuzu schema changes in v3 |
| L7 migration from Kuzu to Qdrant | Phase 4b: dedicated migration script. Current L7 count is 0 in both Kuzu and Qdrant — migration may be a no-op. Script handles both cases. |
| Phase 3 ports into hybrid_v2 but reader not yet switched | Reader switch is behind config flag `MEMORY_READER=hybrid_v2` (default: `legacy`). Phase 3 tests with legacy reader. Phase 4d flips the flag. |
| Fork breaks mid-way | Phase 0: git tag `v2.1.0-stable`, Kuzu backup, Qdrant export. Rollback: `git checkout v2.1.0-stable && pip install hy-memory==1.2.20` |
| Live stack downtime | Stack already stopped; no production downtime risk |
| L6/L7 currently in Kuzu, not Qdrant | L6=0, L7=0 in Qdrant. Both live in Kuzu graph only. Phase 4b migrates L7. L6 stays in Kuzu (graph vector search for schema is the correct path). |

## Pre-flight Checklist

- [ ] HyAtlas stack stopped (`hyatlas stop`)
- [ ] Git tag: `v2.1.0-stable` on current `main`
- [ ] Git branch: `feat/v3-fork` from `main`
- [ ] 1.2.20 wheel extracted at `C:/tmp/hydiff/120/` (source for all files)
- [ ] 1.2.19 wheel extracted at `C:/tmp/hydiff/119/` (reference for diff analysis)
- [ ] `fastembed` installable (`pip install fastembed --dry-run`)
- [ ] `spacy` installable
- [ ] Qdrant binary accessible at `C:\qdrant\qdrant.exe`
- [ ] Kuzu DB backed up at `C:/Users/<user>/.hyatlas/data/kuzu_db_backup_v2`
- [ ] Qdrant full export at `C:/Users/<user>/.hyatlas/snapshots/qdrant_pre_v3.jsonl`
- [ ] Baseline data recorded:
  - Qdrant: 6126 points (L0:42, L1:955, L2:2376, L3:309, L4:1421, L5:1023, L6:0, L7:0)
  - Kuzu L5 graph: 1342 nodes, 5736 relations
  - L6/L7 are in Kuzu only (0 in Qdrant VDB)
  - Qdrant collection has NO sparse vectors configured
- [ ] Full test suite passes on current `main` (baseline)
- [ ] Research profile BM25 analysis complete (done ✓)
- [ ] Sentinel pro review complete (done ✓ — 4 blockers fixed)

## Post-Completion

- [ ] Sentinel review of all changed files
- [ ] Run `uvx ruff check src/` clean
- [ ] Run `python -m pytest -q` all green
- [ ] Update NOW.md
- [ ] Update memory with architectural decisions
- [ ] Push to `main` (after Tuna approval)
- [ ] Celebrate — 23 monkey-patches → 0

## Per-Phase Quality Gates

After EVERY phase commit, run:
1. `python -m pytest -q` — must be green
2. `uvx ruff check src/hyatlas_memory/core/` — must be clean
3. `python -c "from hyatlas_memory.core.client import HyMemoryClient"` — must import
4. Git commit only passes all three
