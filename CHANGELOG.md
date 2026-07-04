## [Unreleased]

### Added
- **Runtime layout consolidation (`HYATLAS_HOME`).** All runtime state now resolves under a single `~/.hyatlas` root. New `hyatlas config` subcommands (`show`, `model`, `embedder`, `validate`) and migration helpers (`hyatlas snapshot`, `hyatlas migrate layout --dry-run|--apply|--rollback`). Config precedence: CLI flags > env vars > `~/.hyatlas/config/.env` > `~/.hyatlas/config/hy_memory.json` > legacy `HERMES_HOME/hy_memory.json` > `~/.hy_memory/pkg/.env`.
- **Multi-key LLM resilience.** `llm.api_keys` list is probed at startup; the first key that authenticates wins. Falls back to `llm.api_key` for backward compatibility.
- **Legacy deprecation warnings.** `hyatlas config show` and `hyatlas status` warn when configs or data still live in `~/.hy_memory` or `~/.hermes/hy_memory.json`.
- **Patch 23: Live graph endpoint `GET /api/v1/graph`.** Adds a server endpoint that queries Kuzu directly via the already-open `graph_store` connection. Eliminates the need for the `l5_kuzu_export.json` bridge file (which required stopping the server to regenerate and went stale between exports). Supports `n` (limit), `type` (entity filter), `q` (search), `rels` (include relations) query params.

### Changed
- **Dashboard `/api/l5/graph` and `/api/l5/context`.** Now proxy to the live server endpoint first (`/api/v1/graph`); fall back to `l5_kuzu_export.json` only if the upstream server is unavailable. Response includes `"fallback": true|false` so callers can tell which path was used.
- **S1 extractor `_get_l5_context_for_prompt`.** Same live-first / fallback pattern — fetches entities from the live endpoint and only reads the export file if the server is down. Added a `hasattr` guard so the patch no longer crashes when the SDK version lacks the method (this was preventing `apply_all_patches` from completing and blocking every patch listed after it in the registry).
- **Qdrant and service logs** now default to `~/.hyatlas/qdrant/` and `~/.hyatlas/logs/` on Windows; the binary path is resolved from `QDRANT_BIN` / `PATH` / `~/.hyatlas/qdrant/`.
- **Documentation** rewritten to reference `~/.hyatlas` paths instead of scattered `~/.hy_memory` and `~/.hermes/hy_memory.json` locations.

### Fixed
- **L5 graph was invisible in the dashboard** despite a healthy Kuzu graph. The `l5_kuzu_export.json` file was last regenerated June 30 and used an older schema (`"edges"`/`"from"`/`"to"` keys) incompatible with the dashboard's reader (`"relations"`/`"a"`/`"b"`). The live endpoint now serves fresh data directly from Kuzu — the export file is no longer required for day-to-day viewing.

## [2.0.0] - 2026-06-30 — S-Class memory upgrade (public release)

### Added
- **`l5_inprocess.py`** — L5 knowledge graph writer inside System2 digest (no subprocess batch lock). Gated by `MEMORY_L5_VERSION=2`; watermark in `l5_state.json`; multi-user Qdrant scroll for L2 backfill.
- **`apply_s2_operations_json_patch()`** — Robust parse of System2 digest JSON ops (think blocks / fenced JSON) so `create_schema` reaches Kuzu; fixes perpetual `no L6 basics` when the LLM returns valid JSON inside noise.
- **`apply_l4_identity_patch()`** — L4 pre-write cosine dedup (`MEMORY_L4_DEDUP_*`), `identity_type` (world/experience/opinion), evolution chain enrich on search.
- **Hybrid v2 + rerank** — documented env for `HY_MEMORY_READER=hybrid_v2` and `MEMORY_RERANK_*` (upstream reader; see migration doc).
- **`tests/smoke_test.py`** — 6-test smoke suite: import, provider, server health, dashboard health, graph counts, hardcoded path scan.
- **`tests/test_clean_install.py`** — Wheel build + isolated venv install verification.
- **`ROADMAP_v2_public.md`** — 4-phase release roadmap with acceptance criteria.

### Changed
- **`patches.py` / `client.py`** — aligned with production S-class stack (search timeouts, patch registry).
- **Default upgrade path** — 1024-d embeddings + Kuzu graph dims; see **`docs/MIGRATION_v2_SCLASS.md`**.
- **Default Qdrant collection** — `agent_memories_384` → `agent_memories_1024` across all code.
- **Dashboard `/api/graph-counts`** — limit increased from 200 → 10000 to show real L6/L7 counts.
- **`__init__.py`** — hermes-agent imports (`agent.memory_provider`, `hermes_constants`, `tools.registry`) now optional with graceful fallback stubs. Clean `pip install` no longer crashes.
- **All `bin/` scripts** — hardcoded `C:\Users\tuanc\` paths replaced with `HERMES_HOME` env var + `Path.home()` fallbacks. Cross-platform compatible.
- **Dockerfile** — fixed module path to `hyatlas_memory.server.start_server`, updated COPY paths for v2.0.0 structure.
- **docker-compose.yml** — added `MEMORY_VECTOR_HOST`/`PORT` env, `hermes_config` volume, fixed dashboard command.
- **README.md** — embedder example updated to `bge-large-en-v1.5` (1024-dim), Docker section formatting fixed.

### Breaking Changes
1. **Embedding dimension: 384 → 1024.** Existing 384-d Qdrant collections are incompatible. See **`docs/MIGRATION_v2_SCLASS.md`** for migration steps.
2. **Kuzu graph dims must match.** Old 384-d Kuzu graph DB is incompatible. Reset Kuzu if S2 logs show dimension mismatch.
3. **New env defaults:** `HY_MEMORY_READER=hybrid_v2`, `MEMORY_L5_VERSION=2`, `MEMORY_L4_DEDUP_ENABLED=true`.

### Migration from v1.x
1. `hyatlas stop`
2. `git clone https://github.com/tuancookiez-hub/HyAtlas-Memory.git && cd HyAtlas-Memory && pip install -e .`
3. Re-embed Qdrant collection if on 384-d (or create new `agent_memories_1024` collection)
4. Reset Kuzu if S2 logs show "Expected: 384, Actual: 1024"
5. Clear `__pycache__/patches*.pyc` and `l5_inprocess*.pyc`
6. `hyatlas start` — verify `l5_inprocess: True`, `l4_identity: True` in logs
7. `curl http://127.0.0.1:19527/healthz` → 200

### Known Limitations
- **Kuzu WAL accumulates** during runtime (14MB+ observed). Only flushes on clean `hyatlas stop`. Recommend periodic restarts for long-running deployments.
- **Dashboard `/api/layer-counts`** fetches up to 10,000 memories per refresh. Works fine for <10k memories; optimization deferred for larger deployments.
- **`rerank_stage` patch** silently no-ops against current upstream reader API. Cross-encoder rerank can be re-enabled via `MEMORY_RERANK_ENABLED=true` when upstream reader API stabilizes.

### Notes for consumers
- After upgrade: `hyatlas stop` → install → clear `__pycache__` for `patches`/`l5_inprocess` if needed → single `hyatlas start` → confirm `l5_inprocess` + `l4_identity` in server log.
- Run `python tests/smoke_test.py` to verify installation health.

## [1.5.0] - 2026-06-25

### Fixed
- **Dashboard shows L5/L6/L7 from live Kuzu data, not from a stale JSON export.** v1.4.2 read layer counts from `l5_kuzu_export.json`, which was last refreshed on 2026-06-18. v1.5.0 reads from the upstream's `/api/v1/list` response and merges the `graph.nodes` array (L5/L6/L7) into the dashboard's per-layer counts. **All 8 layers (L0–L7) now appear in the Memory Layers main table, Memory Observatory layer stats, Overview Memory Composition bar, and Recent Ingestions feed.**
- **Dashboard `/api/memories` includes L5/L6/L7 graph nodes** alongside L0–L4 VDB items. The dashboard's `_extract_memories()` now also iterates `payload.graph.nodes` and normalizes each into a memory dict with proper `layer`, `content`, `gmt_created` (Unix seconds), `user_id`, `agent_id`, etc. Click-to-view works on L5/L6/L7 items in the Recent Ingestions feed.
- **Recent Ingestions puts L5/L6/L7 first** so they remain visible regardless of L2 fact volume. The merge logic dedupes by `memory_id`, separates graph vs. VDB items, sorts by `gmt_created` desc, then takes the first 50 graph items + 450 VDB items. The Time Ago label works because `gmt_created` is a Unix timestamp (was previously a string that broke `new Date(m.gmt_created * 1000)`).
- **Memory Observatory 3D graph populates** with real L5/L6/L7 nodes from the same `/api/memories` pipeline, no longer empty. L5 is 0.3%, L6 is 11.0%, L7 is 0.4% of the memory corpus.

### Notes for consumers
- No code changes needed. Existing installations upgrade cleanly to v1.5.0. After upgrade, refresh the browser to see L5/L6/L7 counts.

## [1.4.2] - 2026-06-23

### Added
- **`hyatlas console`** — visible status window that shows live service health (Qdrant · upstream · dashboard) and the most recent memory activity (writes, recalls, errors) with a live activity ticker. Read-only; closing it does NOT stop the stack. Open it any time you want to see what the memory system is doing at a glance. Available as `hyatlas console` (always visible) or `python -m hyatlas_memory.console --no-start` (attach to an already-running stack).
- **Cross-process log tailer** in the console. Writes and recalls from any Python process (Hermes agent, dashboard, MCP, etc.) appear in the console ticker in real time — not just events from the console's own process. The log file `hyatlas-memory.log` is the inter-process pipe; the tailer handles rotation and filters noise (health pings, GET /favicon).
- **In-process log queue handler** (`MemoryQueueHandler`) — optional bridge so in-process log records also show in the console. No-op when no console is listening.

### Fixed
- **`hyatlas start` is now safe to close.** Services used to inherit a parent process group from `start_visible()`. Closing the visible console window would send a kill signal to the entire stack (Qdrant + upstream + dashboard all died together). Services now always spawn with `DETACHED_PROCESS` regardless of console visibility — the visible console is now strictly read-only.
- **Dashboard L5 graph 503** — writer (bin) and reader (dashboard) used different hardcoded paths for the L5 export JSON, and neither used Hermes' canonical home resolver. 4 reader sites in `dashboard.py` and 2 writer sites in `l5_export_json.py` now share a single `_l5_export_path()` helper.

### Notes for consumers
- No code changes needed. `hyatlas console` is opt-in — if you never run it, nothing changes. Existing auto-start flow is unchanged.

## 1.2.1 — 2026-06-21

### Fixes

- **Plug-and-play provider**: `is_available()` now returns `True` whenever the
  class can be loaded, decoupling `installable` from `currently operational`.
  Use `is_healthy()` for runtime reachability. Gating on upstream reachability
  at init time caused silent stuck-agent failures: the consumer rejected the
  provider, the `MemoryManager` ended up `None`, and every subsequent turn
  was a no-op until the consumer restarted.

- **Self-healing `sync_turn`**: if the client is `None` or the upstream is
  briefly down, the provider lazy-initializes the client and waits up to 3s
  for the upstream to come up before dropping. Closes the silent-stuck-agent
  window where a 1s upstream blip would drop every turn.

### Notes for consumers

- No code changes needed in hermes-agent. Install via `pip install hyatlas-memory`
  and configure (or let it auto-configure from `hy_memory.json`), and the
  provider wires itself in and self-heals at sync time.

# Changelog

> All notable changes to HyAtlas-Memory are documented here. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.2.0] - 2026-06-20

### Changed
- **Self-contained PyPI package** — moved `server/` (upstream SDK wrapper + dashboard) into `src/hyatlas_memory/server/` so `pip install hyatlas-memory` makes `hyatlas start` work from **any directory** without `cd` or `HYATLAS_PROJECT_ROOT`. Resolution order: env var → package install dir → legacy CWD walk.
- **`hyatlas` restart flow** — TIME_WAIT PID-0 filter, foreground vs `--detach` modes, sync port-wait, Hermes-style restart watcher, Kuzu lock-free wait. Five commits refactored the entire start/stop/restart lifecycle.

### Fixed
- **Missing `import re` in `l5_relation_prototype.py`** — would have failed at runtime on the LLM-response parsing path. Surfaced when the file got linted for the first time during the move.
- **Redundant `import urllib.request` in `test_l5_trigger.py`** — shadowed the top-level import.
- **Importance patch silently failing** — `patches.patch_importance_for_request` referenced `_LAYER_IMPORTANCE` which was never defined, so the entire fire-and-forget importance PATCH was NameError-swallowed by its try/except. New memories never got `importance` populated, breaking the 0.15 importance term in the upstream 4-factor MemoryScorer. Added the missing layer→score mapping.
- **Importance patch missing reconciled points** — `_maybe_patch_importance` fired once per `add()` but the upstream reconciler promotes `l1_raw` → `l4_identity` asynchronously, often AFTER the patch ran. Reconciled points never got their importance set. Added an 8s delayed retry that re-scans the user/session/time-window.

### Internal
- Added `package_data` to `pyproject.toml` + `MANIFEST.in` so non-Python files (HTML, CSS, JS, PNG, YAML) ship inside the wheel.
- Per-file-ignores added for the moved `server/` files; pre-existing style issues ignored rather than churning the file.
- `test_importance_and_access_count_are_populated` now polls up to 60s for both reconciliation (memory_id populated) and importance patch (fire-and-forget daemon) instead of fixed 15s sleep that wasn't enough on a loaded box with 3k+ existing memories.

## [1.1.0] - 2026-06-20

### Added
- **`hyatlas memory write|recall|list|reflect` CLI** — manual memory operations from any shell, cron job, or another session. Mirrors Hindsight's `retain|recall|reflect` and Memories.sh's `add|search|recall` patterns. Aliases: `add`/`retain` for `write`, `search`/`find` for `recall`, `ls` for `list`. Supports `--limit`, `--layer`, `--user-id` flags.
- **`hyatlas memory write`** goes through the same `sync_turn` → LLM fact-extraction → qdrant indexing pipeline as a Hermes conversation turn. ~8s indexing delay.
- **`hyatlas memory reflect`** outputs the exact `<relevant-memories>` block the agent would inject into the system prompt for the same query — useful for debugging recall quality.

## [1.0.1] - 2026-06-19

### Fixed
- **`hyatlas` CLI from any directory** — the `hyatlas start|--stop|--status` entry point now works regardless of the current working directory. Previously it failed with "start.py not found" when invoked outside the repo root after `pip install hyatlas-memory`. The startup logic was moved from the repo-root `start.py` into `hyatlas_memory._start` (bundled in the package) and resolves the project root via `HYATLAS_PROJECT_ROOT` env var → cwd → editable-install detection.

## [1.1.0] - 2026-06-20

### Added
- **First stable release.** PyPI package `hyatlas-memory` installable via `pip install hyatlas-memory`.
- `__all__` exports in `hyatlas_memory` (`HyMemoryProvider`, `__version__`).
- `MANIFEST.in` ensures LICENSE, README, CHANGELOG, CONTRIBUTING, docs/, and tests ship in the sdist.
- `hyatlas` console_scripts entry point — `pip install -e .` then `hyatlas start|--stop|--status` works from any directory.
- `docker-compose.yml` — one-command full stack via `docker-compose up -d`.
- `Dockerfile` (python:3.11-slim) and `.dockerignore`.
- Token-based dashboard auth when bound to `0.0.0.0` (auto-generated 32-char token, stored at `~/.hy_memory/.dashboard_token`, cookie-based session, `/api/health` exempt).
- `hyatlas_memory.start` module wrapping `start.py` for the entry point.
- **Experimental layer-as-importance scoring** (on by default, set `HYATLAS_MEMORY_IMPORTANCE=0` to disable).
  Upstream `hy-memory` ships a 4-factor `MemoryScorer`
  (semantic 0.50 + recency 0.30 + importance 0.15 + access 0.05). The
  `importance` and `access` inputs were never populated by the SDK, so the
  0.15 and 0.05 terms effectively stayed zero. HyAtlas now writes a layer-derived
  `importance` score (l4_identity=1.0, l2_fact=0.8, l3_summary=0.6,
  l0_basic_info=0.5, l1_raw=0.3) on each new memory, restoring the full scorer.
  No LLM cost.
- **Access-count tracking** (on by default, set `HYATLAS_MEMORY_ACCESS_COUNT=0` to disable).
  Increments `access_count` on every memory returned by a recall operation, so
  the upstream `MemoryScorer` has a live access signal. Runs in a
  fire-and-forget thread so it never blocks recall.
- Integration tests in `tests/test_integration.py` covering the full local stack: Qdrant + upstream hy-memory server + `HyMemoryProvider`. Backfilled by default-on importance/access-count tracking.
- `unit` and `integration` pytest markers so CI can run the fast suite without the live stack.
- Dashboard front-end refactor: split the monolithic `app.js` into `app.js`, `js/l5.js`, and `js/observatory.js` to isolate the L5 knowledge graph and Three.js observatory modules; `server/dashboard/dashboard.py` now serves files under `/js/`.

### Changed
- **Qdrant auto-detection** — removes hardcoded `C:\qdrant\qdrant.exe`; auto-detects via `QDRANT_BIN` env var → `PATH` → common OS locations. Skips launch if already running (Docker).
- `pyproject.toml` — `[project.scripts]`, per-file ruff ignores, `long_description_content_type` is now auto-detected from `README.md`.
- README — Quick Start moved above How It Works; prerequisites table added; `hyatlas` CLI command documented; runtime vs dev install separated.

### Fixed
- `start.py` path resolution broken when invoked via console_scripts (double dirname on `src/` layout).
- README forcing dev/test deps on normal users (cleanly separated runtime vs dev).
- Missing `__main__` guard in entry wrapper.
- Startup script argv not forwarded (documented).

## [0.6.0] - 2026-06-18

### Added
- `start.py` — one-command startup for the full stack (Qdrant → upstream → dashboard)
- Sequential health checks between each service start
- Auto-cleanup of stale processes on occupied ports
- `--stop` and `--status` commands for service management
- Live status terminal pinned to taskbar (Windows `CREATE_NEW_CONSOLE`)
- Emoji status header (🧠 Hy-Memory, 📊 Dashboard, 🗄️ Qdrant)
- Service logs written to `logs/` directory
- Graceful Ctrl+C shutdown (kills all child processes in reverse order)
- Crash recovery — auto-restarts a service if it dies during health-check window
- `CREATE_NO_WINDOW` flag suppresses blank console popups from child processes

## [0.5.0] - 2026-06-18

### Added
- Full documentation suite: `docs/DASHBOARD.md`, `docs/API.md`, `docs/LAYERS.md`, `docs/TROUBLESHOOTING.md`
- `CONTRIBUTING.md` — contributor guidelines
- `CHANGELOG.md` — this file
- Social preview images for the GitHub repo

### Changed
- README updated with documentation links and dashboard quick start

## [0.4.0] - 2026-06-18

### Added
- Memory Observatory: 3D galaxy visualization with 8 memory layers
- Observatory entrance animation (nodes fly from center on first load)
- Observatory scope-change morph animation (cubic ease between zoom levels)
- Cross-layer edge rendering with warm-gold color + higher opacity
- Density-based opacity scaling for crowded layers
- Initial boot screen with HyAtlas logo + progress bar
- Smooth page transitions (fade + slide) on navigation
- Galaxy seed loading animation (covers only main content area)
- HyAtlas branding (proper case "HyAtlas" replaces "HY-MEMORY" / "HYATLAS")

### Changed
- Default Observatory load state: ALL layers + Last 500 scope
- Legend bar moved outside the 3D canvas
- `computeObservatoryEdges` rewritten for guaranteed cross-layer connections
- `computeObservatoryFitZoom` rewritten with proper geometry math

### Fixed
- Camera clipping at far plane (increased from 2000 to 8000)
- Galaxy oversized at large scopes (0.45x scaling for scope > 100)
- Stale dashboard process cleanup on startup

## [0.3.0] - 2026-06-18

### Fixed
- Re-entrant lock deadlock in `sync_turn` (`threading.Lock` → `threading.RLock`)
- 5-bug chain causing silent cross-session memory write failures
- `_persist_buffer_to_disk` re-entering the same lock it was already holding
- `register_memory_tool` crash on `ToolRegistry` with no such method
- Qdrant storage path mismatch after PC restart (`--config-path` requirement)

## [0.2.0] - 2026-06-16

### Added
- System2 writer with scheduled trigger mode
- Kuzu graph store for L5 knowledge, L6 schema, L7 intention layers
- Cross-encoder reranking for recall quality
- L5 pipeline: 7-step graph rebuild batch job

## [0.1.0] - 2026-06-15

### Added
- Initial release
- 7-layer memory model (L0 basic info → L7 intention)
- Hermes Agent plugin via `MemoryProvider` interface
- Local HTTP dashboard on port 8765
- Local upstream server on port 19527
- 9 carried SDK patches (LLMConfig env-loading, cross-encoder rerank, etc.)
- 4-tier context pressure monitor (fastpath → emergency)
- `hermes hy-memory` CLI subcommands: `doctor`, `add`, `search`, `list`, `init`, `install`, `reset`
- Coding memory subsystem (sqlite-backed)
- 12-test pytest suite

### Known limitations
- `dashboard.html` is a single 3,200-line file
- No auth on the dashboard (loopback only by design)
- No docker-compose for the full stack
- L7 intention layer is experimental, not part of the official Hy-Memory spec

---

## Versioning policy

- **Major (X.0.0)** — breaking changes to the public API (plugin interface, dashboard HTTP API, or data formats)
- **Minor (0.X.0)** — new features, non-breaking. Layer additions, new CLI subcommands, new dashboard pages
- **Patch (0.0.X)** — bug fixes, performance improvements, docs

## Release cadence

There is no fixed release schedule. Releases are cut when:
1. A meaningful chunk of work has accumulated (5+ merged PRs, or a major feature)
2. A critical bug fix needs to be pushed
3. The maintainer feels like it

The current maintainer is [@tuancookiez-hub](https://github.com/tuancookiez-hub). If you're a regular contributor and want release-cutter permissions, ask.

## Migration guides

When a release includes breaking changes, a migration guide is added to `docs/MIGRATION.md`. See the [README](../README.md#migration-from-in-fork-plugin) for the most recent migration from the in-fork plugin version.

## Deprecation policy

Features are deprecated through one minor release before removal:
1. Marked deprecated in `CHANGELOG.md` and a `DeprecationWarning` in code
2. Removed in the next major version

The dashboard's HTTP API follows [semver](https://semver.org/) — breaking endpoint changes bump a major version.
