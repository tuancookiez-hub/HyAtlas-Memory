# Changelog

## [3.4.0] — 2026-07-16

> **Headline: Profile isolation in the dashboard + L1_RAW transparency.** Pick a profile (default, research, sentinel, work-backend, work-frontend, trading, hestia) and the entire dashboard filters to that scope. New `include_raw` flag on `/api/v1/list` returns original L1_RAW payloads alongside the processed L2 fact. Plus a long list of dashboard truth fixes (L5 timestamps, 3-tier status, authoritative layer counts, console window, zvec schema).

### Headline
- **Profile isolation lands in the dashboard.** The `agent_id` data-layer filter has been in place since the v3.0.0 fork, but the UI surface for it lands in v3.4.0. The dashboard now exposes a profile dropdown (default, research, sentinel, work-backend, work-frontend, trading, hestia) and a `/api/profiles` endpoint, and every tab (Overview, Memory Composition, Today, L5, Settings, Quality) filters to the selected scope via `?agent_id=...`. Switching profiles is sticky in `localStorage`. This is the moment "specialist agents have their own memory" becomes a usable feature.
- **L1_RAW transparency.** `/api/v1/list` accepts `include_raw: true` (default) to return the original raw payload alongside the processed L2 fact, and every memory item now carries an `extracted` boolean so the UI can distinguish "the LLM processed this" from "this is the raw write". Powers the Today / Activity timeline and the VDB scroll path. Previously, raw writes were invisible when LLM extraction failed or skipped noisy input.

### Dashboard
- **L5 Knowledge Graph — EXPORTED AT timestamp fallback.** The `EXPORTED AT` field on the L5 tab no longer reads `unknown` when the upstream `/api/v1/graph` endpoint omits the timestamp. The dashboard proxy now injects `exported_at = server clock (UTC)` when upstream omits it, and the JS has a defensive `new Date().toISOString()` fallback for the same case. Verified live: `EXPORTED AT 2026-07-16 11:35:22`.
- **Settings tab — graph counts now show per-agent AND global.** Previously the Settings tab showed graph counts scoped to the current `agent_id` (e.g. `hermes-user / default`) which under-reported when other agents had data. Now both scopes are visible: `(per agent)` and `(global)` rows. Helps users reconcile discrepancies with the L5 tab and `/api/v1/graph` direct queries.

### Memory pipeline
- **`include_raw` flag.** The `/api/v1/list` endpoint now accepts `include_raw: true` to return the original L1_RAW payload alongside the processed L2 fact. Powers the "Today / Activity" tab's timeline and the VDB scroll path.
- **System2 digest — batched execution + token cap.** `run_system2_agent_batched` now splits large L2-fact sets into clusters of 8 facts / batch and caps the per-call LLM output to 1024 tokens. Mitigates `finish_reason=length` truncation from the `tencent/hy3:free` model when reasoning eats the budget. Cluster splitting added to prevent digest retries.
- **LLM `extra_body` propagation.** `MEMORY_LLM_EXTRA_BODY` env var is now parsed by `config.py` so standalone probe scripts inherit the same `reasoning_effort: none, include_reasoning: false` settings as the server. Fixes digest smoke tests diverging from server behavior.
- **zvec `update_payload` schema fix.** `vector_store_zvec.py` now fetches the embedding before calling `update_payload` to satisfy zvec's schema requirements. Fixes silent `update_payload` failures during digest.
- **Writer — persist failure now surfaces.** `writer.py` no longer marks writes as `success=True` when `vector_store.upsert()` fails. Returns `[PERSIST_FAILED]` error code so callers can detect lost writes.

### Linting
- **Resolved 4 pre-existing ruff errors blocking CI.** Errors were introduced by recent main commits (after the last successful CI on 2026-07-12) and would have failed the next CI run regardless of feature branch. Fixes: `console.py` SIM105 (try/except/pass → `contextlib.suppress`), W292 (trailing newline), `dashboard.py` I001 (import sort), F401 (unused `import pathlib`).

### Launch / process management
- **Console window rewrite.** Old `console.py` cleared the screen every 2s and used `stdout=PIPE` which caused the child Python to exit before the window rendered → "empty PowerShell" flash. Rewrote to incremental in-place updates with no full-screen clear, no pipe redirection, and a `wmic` singleton guard so `hyatlas start` doesn't pile up windows. Auto-launched only on `--detach`.
- **Hyatlas launcher — PID-based directory lock.** `run_hyatlas_digest.py` and related launchers use a PID-based lock with `kernel32.GetExitCodeProcess` liveness check (Windows) so stale locks from crashed processes don't block new runs.

### Repo hygiene
- **Privacy scrub complete.** All `<user>`, `<discord_user_id>`, real name, email, and Windows paths replaced with placeholders across source, docs, and pyproject.toml. Local memory data (L5 graph) still contains historical references; this is expected (data tier is local-only, never pushed).
- **Profile isolation plumbing.** Specialist profile names (`default`, `research`, `sentinel`, `work-backend`, `work-frontend`, `trading`, `hestia`) are recognized by the dashboard dropdown, but most profiles are empty (no data ever written to those agent_ids). Profile isolation itself works (the `agent_id` filter on `/api/v1/list` is enforced); the gap is that data is concentrated in `default` and `trading`.

### Documentation
- Added `docs/DEBRIEF_TUNA_OS_USEFULNESS.md` — debrief of the Tuna Agent OS scaffolding usefulness on the profile-isolation work.
- Added `docs/PROFILE_MEMORY_ARCHITECTURE.md` — design doc for profile-based memory isolation across the HyAtlas stack.

## [3.3.2] — 2026-07-08

### Bug fix + docs

- **Sidebar "Last memory: NaN" fixed.** The L1_RAW memories fetched via the upstream `/api/v1/vdb/scroll` endpoint carried `gmt_created` as a raw ISO **string**; the sidebar did `Date.now()/1000 - "<string>"` → `NaN`. Now normalized to a Unix int at the source (`_fetch_l1_raw_from_vdb`), alongside the existing `_extract_memories` / `_fetch_l1_raw_from_qdrant` paths. Verified live: `gmt_created` returns an int; sidebar shows "Last memory: Xm/h/d ago".
- **README reframed.** No longer described as a "community implementation of the official framework." Now positioned as a personal, local, single-user long-term memory stack — forked from Hy-Memory and refined for one person's daily multi-session use.
- **Package description** (`pyproject.toml`) updated to match.

### Known state (not bugs)

- **DEGRADED** status is expected while the Hyper LLM provider returns `402` (out of credits). `vdb`/`embed` are `ok`; only `llm` is erroring. Top up billing to flip it to OPERATIONAL.
- **Activity = 0 / LLM tokens = —** on Quality Metrics are expected until real writes + digests flow through the instrumented runtime (and the LLM is live). The counters exist; nothing is feeding them yet in the current stack. The dashboard reads them from the upstream `/api/v1/metrics` endpoint.

### Quality Metrics — reactive redesign (dashboard)

- **Dropped the manual "Save baseline" button and the weekly ritual.** Trends are now automatic: every dashboard load appends a snapshot to `~/.hyatlas/metrics/quality_history.json` (legacy `quality_baseline.json` is migrated on first read). Week-over-week deltas compare ~7 days back with no user action.
- **New hero "Vitals" panel** — grade ring (A–D), large **/100** composite score, a plain-language headline, and green check highlights (digest ok, L6 count, relations, writes) so the page reads "my metrics are good" at a glance.
- **Pulse chips** — Overall / Evolution / L6 / Relations with ↑/↓ trend arrows and deltas vs last week (shows "building trend" until enough history exists).
- **Nudges only when needed** — the "What to do next" panel is hidden unless digest is broken or the fresh-L2 queue is backed up, keeping the page calm.
- **Math moved to a collapsed "How scores are calculated" section** so the score composition and glossary no longer dominate the first view.
- `POST /api/quality-baseline` retained as a no-op compatibility shim (writes history, returns a note).

### Quality Metrics (dashboard)

- **New sidebar tab** — **Quality Metrics**: composite/evolution/activity/latency scores, 7-day window.
- **LLM token rollup** — `MetricsCollector.record_llm_tokens()` on System1 extract/reconcile; exposed via `GET /api/v1/metrics` → `llm_tokens`.
- **APIs** — `GET /api/quality-metrics`, `POST /api/quality-baseline` (weekly snapshot at `~/.hyatlas/metrics/quality_baseline.json`).
- **UX (post-release)** — Transparent score breakdown (digest / fresh L2 / L6 components), hero + progress bars, baseline comparison table with plain-language verdicts, actionable tips; removed unrelated industry benchmark panel.
- **Assets** — `assets/hyatlas-v3.3.0-quality-metrics.png` (README hero).

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
