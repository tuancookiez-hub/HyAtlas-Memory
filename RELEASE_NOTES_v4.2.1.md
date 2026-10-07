# HyAtlas v4.2.1 — catalog-submission hardening

> No breaking changes. Drop-in upgrade from 4.2.0.

This release closes the gaps found during the Hermes Plugin Catalog submission audit: a version badge that never changed, an L5 layer count that disagreed between endpoints, and an extraction pipeline that silently died an hour after every restart.

## Fixed

**Real version badge.** The Desktop pane hardcoded the string `v4` in two places (header subtitle and the "Memory saved" toast), so it read v4 forever regardless of what was actually running. `/api/v1/status` now carries a `version` field sourced from one canonical `Version` const; the pane derives it from the API and falls back to `v4` only while connecting. Releasing is now a one-line change.

**L5 count consistency.** L5 knowledge lives in the graph store, never in chromem, so the raw layer count was always 0. The real count was hand-patched into only two of the six endpoints that report it — `/api/v1/list`, `/metrics`, `/layer-health` and `/api/metrics` all said `l5_knowledge: 0` while `/status` said the truth. The override now lives in `store.LayerCounts()` as the single source of truth, and a regression test pins every endpoint to agree.

**Rotating credentials no longer stall extraction.** The server froze `HYATLAS_LLM_KEY` at startup. The Nous Portal key is a 1-hour JWT that Hermes keeps fresh in `auth.json`, so any server up longer than an hour held an expired token and every extraction call failed with a generic HTTP 401 until a manual restart — which is why it looked intermittent. New optional `HYATLAS_LLM_KEY_FILE` makes the client read the key live per call (accepts the `auth.json` shape or a plain-text token). The static `HYATLAS_LLM_KEY` remains the fallback and is still all a normal static-API-key install needs. No timers, no refresh goroutines.

**Plugin config precedence.** `_load_config` picked one config dict wholesale, so a partially-filled Desktop settings form silently shadowed keys the legacy `plugins.hyatlas` block had set. It now layers both per key.

## Changed

- `/api/v1/status` is marshaled from the `Status` struct directly. The handler previously built a struct and then restated all 17 fields by hand into a `map[string]any`, defining the wire shape twice so the struct's json tags were dead code.
- `handleDashLayerCounts` takes one consistent snapshot — it read `Usage()` and `TotalMemories()` twice each, so writes and searches could come from different moments.

## Added

- **24 new tests.** 14 plugin unit tests against a real localhost HTTP server (no mocks): wire round-trip, typed error handling, config precedence, all four tool dispatches, `sync_turn`, `on_memory_write`, and the `delete_all` unscoped-wipe guard. Plus 10 Go regression tests — `l5_counts_test.go` (3, L5 is graph-derived and every endpoint agrees), `llm_keyfile_test.go` (4, live key resolution, fallbacks, and on-the-wire rotation), `version_test.go` (3, status and dash info report the real version).
- **CI `plugin-tests` job** running the plugin suite on a clean Python, using the `MemoryProvider` ABC extracted from the published hermes-agent wheel — no full Hermes install required.

## New environment variable

| Variable | Default | Purpose |
| --- | --- | --- |
| `HYATLAS_LLM_KEY_FILE` | (empty) | Read the LLM key live from this file each call, for rotating credentials. Accepts `providers.nous.agent_key`/`access_token` JSON or a plain-text token. Wins over `HYATLAS_LLM_KEY`, which becomes the fallback. |

## Assets

Single self-contained binary per platform — no Python at runtime, embeddings in-process.

- `hyatlas-go-v4.2.1-linux-amd64`
- `hyatlas-go-v4.2.1-macos-arm64`
- `hyatlas-go-v4.2.1-windows-amd64.exe`

**Full Changelog**: https://github.com/tuancookiez-hub/HyAtlas-Memory/compare/v4.2.0...v4.2.1
