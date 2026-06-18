# Changelog

> All notable changes to HyAtlas-Memory are documented here. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Memory Observatory: 3D galaxy visualization with 8 memory layers
- Observatory entrance animation (nodes fly from center on first load, 900ms ease-out)
- Observatory scope-change morph animation (600ms cubic ease between 25/50/100/500)
- Cross-layer edge rendering with warm-gold color + higher opacity for inter-layer connections
- Density-based opacity scaling for crowded layers (>200 nodes → 70%, >100 → 82%)
- Initial boot screen with HyAtlas logo + progress bar (covers full screen until data loads)
- Smooth page transitions (fade + slide) on navigation
- Galaxy seed loading animation on Observatory page (pulsing golden dot, covers only main content area)
- HyAtlas branding (proper case "HyAtlas" replaces "HY-MEMORY" / "HYATLAS")
- `docs/DASHBOARD.md` — full dashboard reference
- `docs/API.md` — HTTP API reference
- `docs/LAYERS.md` — per-layer deep-dive
- `docs/TROUBLESHOOTING.md` — common issues + fixes
- `CONTRIBUTING.md` — how to contribute
- `CHANGELOG.md` — this file
- `assets/social-preview*.png` — social preview images for the repo

### Changed
- Default Observatory load state: ALL layers + Last 500 scope
- Legend bar moved outside the 3D canvas (no longer overlaps galaxy)
- `computeObservatoryEdges` rewritten to guarantee cross-layer connections
- `computeObservatoryFitZoom` rewritten with proper geometry math

### Fixed
- Camera clipping at far plane (increased from 2000 to 8000)
- Galaxy oversized at large scopes (0.45x scaling for scope > 100)
- Stale dashboard process cleanup

---

## [0.1.0] - 2026-06-15

### Added
- Initial release
- 7-layer memory model (L0 basic info → L7 intention, with L5–L7 in Kuzu graph)
- Hermes Agent plugin via `MemoryProvider` interface
- Local HTTP dashboard on port 8765
- Local upstream server on port 19527
- 9 carried SDK patches (LLMConfig env-loading, cross-encoder rerank, in-process embedding, L3 trigger reachability, L1 dedup gate, etc.)
- 4-tier context pressure monitor (fastpath → emergency)
- L5 pipeline: 7-step graph rebuild batch job
- `hermes hy-memory` CLI subcommands: `doctor`, `add`, `search`, `list`, `init`, `install`, `reset`
- Coding memory subsystem (sqlite-backed, separate from VDB)
- 12-test pytest suite

### Known limitations
- `dashboard.html` is a single 3,200-line file (HTML+CSS+JS)
- No auth on the dashboard (loopback only by design)
- No docker-compose for the full stack
- Documentation is spread between README, `docs/architecture.md`, and inline comments
- L7 intention layer is an experimental extension, not part of the official Hy-Memory spec

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

When a release includes breaking changes, a migration guide is added to `docs/MIGRATION.md` (if it doesn't exist yet, it's created at that time). See the [README](../README.md#migration-from-in-fork-plugin) for the most recent migration from the in-fork plugin version.

## Deprecation policy

Features are deprecated through one minor release before removal:
1. Marked deprecated in `CHANGELOG.md` and a `DeprecationWarning` in code
2. Removed in the next major version

The dashboard's HTTP API follows [semver](https://semver.org/) — breaking endpoint changes bump a major version.
