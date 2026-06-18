# Changelog

> All notable changes to HyAtlas-Memory are documented here. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
