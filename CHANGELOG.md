# Changelog

## [3.3.0] — 2026-07-08

### Quality Metrics (dashboard)

- **New sidebar tab** — **Quality Metrics**: composite/evolution/activity/latency scores, 7-day window.
- **LLM token rollup** — `MetricsCollector.record_llm_tokens()` on System1 extract/reconcile; exposed via `GET /api/v1/metrics` → `llm_tokens`.
- **APIs** — `GET /api/quality-metrics`, `POST /api/quality-baseline` (weekly snapshot at `~/.hyatlas/metrics/quality_baseline.json`).
- **Reference panel** — Tencent Hy-Memory published benchmarks (35% / 25% / 88%) labeled industry reference, not local measurement.

## [3.2.1] — 2026-07-08

### Cleanup & L6 visibility

- **Graph API** — `GET /api/v1/graph?layer=l6_schema|l7_intention` returns schema/intention nodes (not only L5).
- **Dashboard** — `/api/l6-schemas` + Settings → System sample list (568 L6 in Kuzu).
- **Docs** — `NOW.md` refreshed; `docs/CLEANUP.md` for post-zvec disk hygiene; superseded `PLAN.md` → `docs/archive/`.
- **Compose** — `docker-compose.yml` marked legacy (runtime is zvec via `hyatlas start`).
- **README / DASHBOARD** — Zvec-first quick start, Hermes docs links, new API endpoints documented.
- **LAYERS / architecture** — Rewritten for v3.2: Zvec, L4 retired, L5–L7 graph semantics, digest-first evolution.
- **API / TROUBLESHOOTING / CONTRIBUTING** — Full doc audit; Zvec-first troubleshooting.
- **Dashboard** — `/api/graph-counts` uses live `layer_counts` (fixes L6=0 bug).

## [3.2.0] — 2026-07-07

### Hermes single-user second brain (evolution + honesty)

- **Digest namespace** — default `agent_id` is `default` (matches Hermes writer); preflight warns on mismatch.
- **L4 retired** — identity archive script, S2 no longer reads L4, dashboard labels L4 as retired → L2; `/api/layer-health` for digest readiness.
- **Windows digest** — canonical launcher `%LOCALAPPDATA%\hermes\scripts
un_hyatlas_digest.py`; weekly Hermes cron (`no_agent`, script-only); Discord summary on same thread as memory prune.
- **Graph / retrieval** — Kuzu path pinned at server start; L5–L7 layer counts on graph API; BM25 public API restored.
- **CLI** — auto-detach for non-TTY `hyatlas start`; L5 in-process extraction hardening.

### Upgrade notes

- Re-run digest once via `run_hyatlas_digest.py` after upgrade if L6 was stale under `default_agent`.
- See `docs/HYATLAS_HERMES.md` for identity contract and cron.

## [3.1.0] — 2026-07-07

### Zvec as default vector store (Qdrant archived, not required at runtime)

- **`ZvecVectorStore`** — production adapter with refcounted lifecycle, `resolve_zvec_path()`, shared `_FIELD_SCHEMA` with migration.
- **`hyatlas start`** — when `vector_store.provider` is `zvec`, Qdrant is **not** started; status shows Zvec store + server. `hyatlas stop` kills legacy Qdrant on :6333 if still running.
- **Dashboard** — layer counts, L1 raw scroll, and payload enrichment use memory server **`/api/v1/vdb/*`** (works with Zvec; Qdrant HTTP fallback when server down).
- **`hyatlas archive qdrant`** — zip cold backup of HyAtlas Qdrant storage under `~/.hyatlas/archive/` (data left on disk).
- **`hyatlas zvec doctor`** — path lock/reopen checks for cutover rehearsal.
- **Migration** — `scripts/migrate_qdrant_to_zvec.py` with `--apply --verify`; deterministic point IDs.
- **Search completeness** — `_doc_to_node` normalizes migrated epoch-string timestamps → ISO; `vdb_dashboard.payload_by_ids` reads `MemoryNode.importance`/`access_count` (was crashing on `.meta_info`).
- **Consistency (deep review)** — `config_cli validate` enforces `zvec` as the only runtime vector provider; `default_config` uses zvec; `hyatlas doctor` vector-store check is provider-aware; console TUI shows Zvec health row.
- **L1_RAW sweep** — `integrations.start_l1_raw_sweep` is now provider-aware: zvec path reuses the live vector-store handle (no second open / lock collision) and deletes shadowed L1_RAW by filter; added `ZvecVectorStore.delete_by_filter`.
- **Docs** — `pyproject.toml` + `README.md` state Zvec is the default vector store.
- **Runtime cleanup** — removed the remaining Qdrant runtime adapter path and Qdrant sparse-BM25 encoder (`bm25_fastembed.py`). HyAtlas runtime is zvec-only; Qdrant remains only as archived/migration source via `hyatlas archive qdrant` and `scripts/migrate_qdrant_to_zvec.py`. Read keyword channel is Zvec native FTS; write-time dedup still uses store-independent `bm25.py`.

### Upgrade notes

- Set `vector_store.provider` to `zvec` and install `pip install hyatlas-memory[zvec]` (or `zvec>=0.5.1`).
- Run migration from Qdrant while server is stopped, then `hyatlas zvec doctor`, then `hyatlas start`.
- Archive Qdrant with `hyatlas archive qdrant` before decommissioning the sidecar.

## [3.0.0] — 2026-07-06

### Major: Full SDK Fork + Reasoning Model Compatibility + Operational Hardening

The entire hy-memory 1.2.20 SDK (42,668 lines) is now first-party code under
`src/hyatlas_memory/core/`. No external `hy-memory` dependency. Every line is
owned and maintained by HyAtlas. All patches promoted to first-class integrations.
E2E verified with deepseek-v4-flash: write → L2 extraction → S2 digest → L5
knowledge graph → search → graph endpoint.

#### What's New vs 2.1.0

**Full SDK Fork (biggest change)**
- 70 files from hy-memory 1.2.20 forked into `src/hyatlas_memory/core/`
- Zero external `hy-memory` pip dependency — all code is first-party
- Stripped ~8,000 lines of dead backends (Chroma, FAISS, Tencent, Neo4j, MySQL, Redis)
- 23 monkey-patches → 13 first-class integrations in `integrations.py`

**Reasoning Model Compatibility**
- Think-block parsing in all 3 `_parse_json` implementations (extractor, abstractor, emotion analyzer)
- Handles closed `⋖...⋗`, closed `<think>...</think>`, unclosed/truncated think blocks
- `agent_max_tokens` raised 2000 → 8192 (reasoning models need budget for thinking + output)
- Tested with MiniMax-M3 (reasoning) and deepseek-v4-flash (non-reasoning) — both work

**L5 Knowledge Graph (our addition — upstream doesn't have this)**
- In-process entity/relation extraction → Kuzu graph writes
- No batch lock — runs alongside the live server
- Live graph endpoint `/api/v1/graph` (1,444 nodes, 6,374 relations verified)

**Emotion-Aware Memory**
- LLM-based valence/arousal scoring wired into write path
- Arousal-weighted memory strength: emotionally significant memories resist time decay
- `MEMORY_EMOTION_ENABLED=true` to activate

**Kuzu WAL Checkpoint Fix (upstream has the same bug)**
- `close()` now calls `CHECKPOINT` + `db.close()` (was just nulling refs)
- Prevents WAL data loss on crash — verified: 0KB WAL after shutdown

**Operational Hardening**
- VDB circuit breaker (Qdrant resilience — auto-recovers from failures)
- L1_RAW rolling delete + dedup skip
- Multi-key LLM rotation (`llm.api_keys` list)
- Auto-forgetting with recency scoring + expiry sweep

#### Detailed Changes

**SDK Fork (Phase 1)**
- Copied 70 files from hy-memory 1.2.20 wheel into `src/hyatlas_memory/core/`
- Coding judge hardcoded to `return False` — all writes use the normal memory path
- Factory files cleaned: only Qdrant, Kuzu, DisabledCache, SQLite remain
- Zero `from hy_memory` imports in active code paths

**13 First-Class Integrations (Phase 3, replacing 23 patches)**
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
- BM25 hybrid search (dense + keyword fusion at 0.6/0.4 weighting)
- Memory strength scoring: `(1 + log(access_count)) × exp(-idle_days / 180)`
- L7 intentions in Qdrant VDB with lazy expiry to L2_FACT
- Profile evidence reverse lookup
- Token counting via tiktoken
- Audit logging (JSONL rotating log for pipeline events)
- S2 operations JSON robust parsing for reasoning models
- L1_RAW multi-line message parsing fix

**Session Fixes (2026-07-06)**
- Unclosed think-block regex: strips from opening `⋖` to first `{` when response truncates mid-reasoning
- `agent_max_tokens` default raised 2000 → 8192 in `config.py`
- Abstractor `_parse_json` had no think-block stripping — fixed
- Emotion analyzer `_parse_json` had incomplete stripping — fixed
- Kuzu WAL checkpoint: `close()` calls `CHECKPOINT` + `db.close()` (verified 0KB WAL)
- LLM switched from MiniMax-M3 → deepseek-v4-flash (clean JSON, no reasoning overhead)

**Dependencies**
- Removed: `hy-memory` (forked into source)
- Added: `tiktoken>=0.5.0`, `fastembed>=0.2.0`
- Kept: `kuzu`, `qdrant-client`, `sentence-transformers`, `openai`, `pydantic`

#### Safety

- Git tag `v2.1.0-stable` preserves the pre-fork state
- Branch `feat/v3-fork` contains all v3.0.0 work (merged to main)
- Kuzu backup at `kuzu_db_backup_v2` (83 MB)
- Qdrant export at `qdrant_pre_v3.jsonl` (6,135 points, 185 MB)
- Rollback: `git checkout v2.1.0-stable` restores the entire 2.1.0 state

#### Test Results

- 33 passed, 19 skipped (14 graph + 4 integration + 1 dashboard skipped when server not running)
- All critical module imports verified
- Zero `from hy_memory` imports in active code paths
- Zero hardcoded paths in any `.py` file
- Version consistency: 3.0.0 across `pyproject.toml`, `_version.py`, both `plugin.yaml` files
- E2E verified: write → L2 → S2 digest → L5 graph → search → graph endpoint → circuit breaker

#### Upstream Comparison

HyAtlas-Memory v3.0.0 is genuinely ahead of upstream hy-memory 1.2.20 on:
- **L5 knowledge graph** — upstream has no entity/relation extraction (their "L5" is profile summary text)
- **Kuzu WAL checkpoint** — upstream has the same bug (just nulls refs, no checkpoint)
- **Reasoning model support** — upstream `_parse_json` has no think-block handling
- **`agent_max_tokens`** — upstream defaults to 2000 (too low for reasoning models)
- Upstream's `HY_MEMORY_THINKING_MODE=disabled` works for DeepSeek/Qwen/Kimi/Hunyuan but not MiniMax

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
