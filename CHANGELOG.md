# Changelog

## [3.4.6] — 2026-07-22

### Bug fixes

- **Reconciler `\<think\>` strip**: MiniMax-M3 (and other reasoning models) can emit `\<think\>...\</think\>` blocks into the `content` field, which broke JSON parsing in the reconcile pipeline (3 failed attempts → dropped write). Now stripped in `_strip_code_fence` before parsing. Pairs with `reasoning_split=true` in config, which routes thinking to a separate `reasoning_details` field so content stays clean.
- **`huggingface-hub` pin moved to core dependencies**: was only in the `local-embed` optional extra, so a shared venv could float it to 1.x and silently break the local embedder (transformers 4.46.x requires `<1.0`). Now `huggingface-hub>=0.23.2,<1.0` is a core dep — pip's resolver enforces it on every install.
- **Blank orphan console windows on Windows**: the plugin auto-start (`StackManager`) and CLI launcher spawned services via the venv shim (`python.exe`), which re-execs to a console-subsystem base python. Windows then allocated a COM console (Windows Terminal tab) that lingered as a blank orphan after the launcher exited. Now both paths spawn the base `pythonw.exe` (GUI subsystem — no console is ever allocated) with the venv site-packages + editable source dir on `PYTHONPATH`. The deliberate `hyatlas console` status window is unaffected.

Upgrade: `pip install --upgrade --force-reinstall git+https://github.com/tuancookiez-hub/HyAtlas-Memory.git` then restart.

## [3.4.5] — 2026-07-19

### Bug fixes (log noise)

- **`DisabledCache.cleanup_old_metrics`**: MetricsCollector hourly cleanup called a method that only existed on `SqliteCache`. Added no-op on `DisabledCache` + tolerance lists in `integrations` / `patches` so the hourly loop no longer logs `cleanup error: 'DisabledCache' object has no attribute 'cleanup_old_metrics'`.
- **`vector_store_zvec._safe_topk`**: clamp all `query(..., topk=...)` values to live `coll.stats.doc_count`. Stops zvec 0.5.1 C++ spam `ID is out or range: id[N] count[N]` from `doc_filter.cc` when callers used `topk=100000` on smaller collections.

### Docker

- Image tag in compose bumped to `3.4.5` (same zvec-native stack as 3.4.4 Docker rewrite).

Upgrade: `pip install --upgrade --force-reinstall git+https://github.com/tuancookiez-hub/HyAtlas-Memory.git` then restart.

## [3.4.4] — 2026-07-19

> **Schema fix: `update_payload` was passing `"embedding"` (a VECTOR field) as a scalar field, triggering `schema validate failed: embedding not found in collection schema` on every reconciler UPDATE operation.** v3.4.3 tried to fix this with a fields-only update path, but the real root cause was upstream — the reconciler in `writer.py:586` was passing `"embedding": new_emb` in the `update_payload` dict, and `update_payload` was putting it in the scalar `clean` dict. zvec's `convert_to_cpp_doc` then tried to validate `"embedding"` as a scalar field, which failed because `embedding` is a VECTOR field.

### Bug fixes

- **`vector_store_zvec.py` — `update_payload()`**: detect `"embedding"` in the updates dict and route it to `update_embedding()` (the dedicated vector update method) instead of trying to pass it as a scalar field. Scalar updates are applied via the fields-only path. This fixes the root cause that v3.4.3's fields-only path was trying to work around.
- **`core/server.py` — `_json_response()`**: wrap the response write in a try/except for `ConnectionResetError` / `ConnectionAbortedError` / `BrokenPipeError`. The dashboard polls `/api/v1/status` every 5s with a short timeout; when it abandons a request, the server threw `ConnectionAbortedError` and logged a full stack trace. Now it silently drops the response and exits cleanly.

### Docker (zvec-native)

- **`Dockerfile` / `docker-compose.yml` / `docker/entrypoint.sh`**: replace legacy Qdrant multi-service compose with a single **zvec** stack (API + dashboard). Binds `0.0.0.0` inside the container; data in volume `hyatlas_data` → `/data/hyatlas` (`HYATLAS_HOME`). Optional `--profile local-embed` builds with `[local-embed]`. Default image uses remote OpenAI-compatible embeddings (small).
- **`.env.example` / `hy_memory.json.example`**: aligned to zvec (no Qdrant host/port).
- **README Path A**: Docker docs updated; Qdrant compose path removed.

### Verified

- After restart: 7+ minutes with zero `schema validate failed` errors (was ~10 errors per minute before the fix).
- Health check: `status=ok / vdb=ok / embed=ok / llm=ok / write_pipeline=ok`.
- Unit tests: 62 passed, 0 failed. ruff: clean.

### Why v3.4.3 didn't fix this

v3.4.3 changed `update_payload` to try a fields-only update first (no vectors passed to zvec.Doc). But the error wasn't coming from the vectors loop — it was coming from the **scalar fields loop** in `convert_to_cpp_doc`. The writer was passing `"embedding"` as a key in the `updates` dict, `update_payload` was putting it in the `clean` (scalar fields) dict, and zvec was trying to find a scalar field named `embedding` — which doesn't exist because `embedding` is a VECTOR field. The fix is to split vector updates from scalar updates at the `update_payload` entry point, not to change how vectors are passed to zvec.Doc.

Upgrade from 3.4.3: `pip install --upgrade --force-reinstall git+https://github.com/tuancookiez-hub/HyAtlas-Memory.git` and restart. No data migration.

## [3.4.3] — 2026-07-19

> **Stability fix: server crashed via `forrtl: error (200)` (Intel Fortran runtime abort on console-close) when the launcher exited.** `hyatlas start` printed "ready on port 19527" and exited 0, but the server died within seconds because the Intel Fortran runtime (loaded transitively via `libiomp5md.dll` from ctranslate2 / onnxruntime) received a `CTRL_CLOSE_EVENT` when the parent console window closed and aborted the process. v3.4.2 was marked "Latest" on GitHub Releases but did not stay running unattended.

### Bug fixes

- **`_start.py` — `_child_env()`**: set `FOR_DISABLE_CONSOLE_CLOSE_HANDLER=1` in the env passed to all spawned services. This tells the Intel Fortran runtime to ignore `CTRL_CLOSE_EVENT` instead of aborting. Without this, `hyatlas start` reports "ready" but the server dies the moment the launcher exits and its console handle becomes invalid. Verified: server stays up for >2 minutes after launcher exit (was ~15 seconds before the fix).
- **`vector_store_zvec.py` — `update_payload()`**: try a fields-only update first (no vectors passed). The previous implementation always fetched the existing embedding and re-passed it on every payload update, which triggered `schema validate failed: embedding not found in collection schema` errors inside `convert_to_cpp_doc`'s vectors loop. The fields-only path lets zvec preserve the existing vector on disk untouched. Falls back to the vector-preserving path only if the simple path fails, and only if the schema actually declares a vector named `embedding`.
- **`integrations.py` — `wire_circuit_breaker()`**: detect `ConnectionResetError` / `ConnectionAbortedError` / `BrokenPipeError` specifically. When the client closes the connection mid-write (e.g. Hermes plugin timed out), the handler now logs at DEBUG and exits cleanly — no longer counts the failure against the circuit breaker, and no longer tries to send a 503 response on the dead socket (which produced a second noisy error log).

### Environment fix (not a code change)

- **`huggingface-hub` version in the user's venv**: v3.4.2 relaxed the `pyproject.toml` pin to `huggingface-hub<1.0,>=0.23.2`, but existing installs still had `huggingface-hub==1.2.3` from before the fix. `transformers==4.46.3` requires `huggingface-hub>=0.23.2,<1.0`, so `import sentence_transformers` failed, `wire_inprocess_embed` bailed out, and the embedder fell back to the OpenAI HTTP API with `BAAI/bge-large-en-v1.5` as the model name — surfacing as `embed: error` with `invalid model ID`. Fix: `pip install 'huggingface-hub>=0.23.2,<1.0' --force-reinstall` (→ 0.36.2). New installs via `pip install git+...` get the correct version from the relaxed pin.

### Verified

- Fresh restart: `hyatlas start` → server ready in 4s, dashboard ready in 1s.
- Health check at T+0, T+60s, T+120s: `status=ok / vdb=ok / embed=ok / llm=ok / write_pipeline=ok` — stable.
- Write test: `POST /api/v1/add {"text": "test"}` → `success: true`, `memory_id` returned, `elapsed_ms: 9883` (includes LLM extraction).
- List test: `POST /api/v1/list` → graph nodes returned from L5 knowledge layer.
- Dashboard: `GET /api/layer-counts` → L1=933, L2=1892, L3=309, L4=657, L5=1807, L6=580, L7=295, total=6483.
- Unit tests: `pytest -v -m "not integration"` → 62 passed, 3 skipped, 0 failed.
- Lint: `ruff check src/ tests/` → all checks passed.

### Why this slipped through

The `forrtl: error (200)` crash only manifests when (a) the launcher process exits and (b) a Fortran-linked library is loaded in the child. CI runs the server in foreground mode (no launcher exit) and the CI environment doesn't have ctranslate2 / onnxruntime installed, so the Fortran runtime is never loaded. The bug only appeared in production on the user's Windows machine where the full embedder + LLM stack pulls in `libiomp5md.dll`.

Upgrade from 3.4.2: `pip install --upgrade --force-reinstall git+https://github.com/tuancookiez-hub/HyAtlas-Memory.git` and restart the stack. If `embed: error` persists after upgrade, run `pip install 'huggingface-hub>=0.23.2,<1.0' --force-reinstall` to fix the version in your existing venv. No data migration.

## [3.4.2] — 2026-07-19

> **Patch: broken `local-embed` extras pin (`huggingface-hub>=1.5.0,<2.0` was incompatible with the `transformers==4.46.x` runtime).** Anyone who ran `pip install hyatlas-memory[local-embed]` from 3.4.0 / 3.4.1 got a silently broken in-process embedder — `wire_inprocess_embed` would skip via its `except ImportError: return`, and `_embed_openai` would fall through to a default OpenAI HTTP call with `BAAI/bge-large-en-v1.5` as model name and no API key, surfacing as `embed: error` on `/api/v1/status`.

### Bug fix
- **`pyproject.toml` — `local-embed` extras**: `huggingface-hub>=1.5.0,<2.0` → `huggingface-hub<1.0,>=0.23.2`. Matches the `transformers==4.46.x` dependency that ships with the package. With this pin in place, `pip install hyatlas-memory[local-embed]` resolves a consistent version set and `wire_inprocess_embed` loads `BAAI/bge-large-en-v1.5` (1024 dims, 3.1s on CPU) in-process — no API key, no provider.
- **`sentence-transformers` extras upper bound**: `>=2.0.0` → `>=2.0.0,<4.0` so we don't accidentally pull a future major that changes the `SentenceTransformer(...)` constructor signature.

### Verified
- Fresh venv: `pip install hyatlas-memory[local-embed]==3.4.2` → `SentenceTransformer("BAAI/bge-large-en-v1.5", device="cpu")` loads, `embed("test")` returns 1024-dim float32 vector.
- Server status after restart: `status=ok / vdb=ok / embed=ok / llm=ok / write_pipeline=ok` — `embed_dims: 1024` matches the existing `agent_memories_1024` zvec collection, so no re-ingest is needed for existing graphs.

### Why this slipped through
The `local-embed` extras were declared but never exercised in CI (CI only runs `pip install -e .` with the default deps). The conflict between `huggingface-hub 1.x` and `transformers 4.46.x` only shows up when the user installs the optional extras, so the default install path looked healthy. Test added (3.4.2 follow-up): CI now installs `[local-embed]` on one job and imports `SentenceTransformer` to catch the version drift.

Upgrade from 3.4.0 / 3.4.1: `pip install -U -e ".[local-embed]"` (or reinstall the wheel) and restart the stack. No data migration.

## [3.4.1] — 2026-07-18

> **Patch: Day-0 first-proof path + fail-fast doctor.** Same product as 3.4.0 (profile isolation, L1_RAW transparency). This release makes install and health checks safer for new users and post-reboot ops.

### Docs
- **`docs/DAY0.md`** — 15-minute checklist: install → `hyatlas start` → `hyatlas doctor` → `add`/`search` → Hermes recall → dashboard.
- **README Quick start** — prove memory with doctor + manual add/search before the dashboard tour; reboot habit (stack is local processes, not a service).

### CLI / reliability
- **`hyatlas doctor`** — exit code `1` on failures; fail-fast port checks; short deep-status timeouts; LLM key presence warning for pro/ultra; multi-profile `agent_identity` / `memory.provider` scan; clearer next steps.
- **Status timeouts** — `/api/v1/status` client timeout 5s; `hyatlas status` zvec status fetch 3s (avoid hung shells).
- **Thin client** — `list_memories(..., include_raw=…)` forwards the flag (from post-3.4.0 main).
- **Console spawn** — strip MSYS/Git Bash env keys; layout-based log path; CLI `close_fds=False` (from post-3.4.0 main).
- **Privacy** — remaining `tuanc` path/fixture scrub + detector generalization (from post-3.4.0 main).

### Tests
- `tests/test_doctor_day0.py` — doctor gate unit tests.
- Smoke tests return `None` under pytest (no ReturnNotNone warnings).

Upgrade from 3.4.0: `pip install -U -e .` (or reinstall wheel) and restart the stack / Hermes. No data migration.

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
