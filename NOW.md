# HyAtlas v4 — NOW

**v4.4.0** | 2026-10-08 | Release: tuancookiez-hub/HyAtlas-Memory

## Running
- Release is 4.4.0: `Version` const in server.go, `plugins/hyatlas/plugin.yaml`, and the install.sh default `HYATLAS_VERSION=v4.4.0` all say so. The latest tag in this checkout is v4.3.3; there is no v4.4.0 tag or release asset yet, so the installer builds from source until there is one.
- Plugin: `hyatlas` @ `C:\Users\tuanc\AppData\Local\hermes\plugins\hyatlas\` + Hermes Desktop pane
- Watchdog: hourly extraction-freshness cron (`hyatlas-extraction-watchdog`)

## Unreleased (branch claude/busy-faraday-3tvefr)
- **Plugin start/stop are truthful:** `hermes hyatlas start` does not spawn over a server that already answers (reports `already_running` and the pid when known); the pidfile is written only once the child is seen alive; `stop` returns `ok:false` for a server it did not start.
- **Installer:** a failed BGE model download no longer aborts the install; release binaries (embedded) skip the download entirely.
- **Plugin:** `on_memory_write` is documented as a raw (L2) write, not an L1 mirror; the `llm_key` settings link is removed.
- **Host guard:** a DNS-name `Host` gets 403 on every route, `/healthz` included, unless it is in `HYATLAS_ALLOWED_HOSTS` (hostnames only; a port in an entry is ignored). Plugin `server_host` set to a DNS name needs that entry on the server.
- **Owner-less graph rows** (written before owners existed) stay visible under every user filter.
- **Consolidation windows:** an owner with more facts than `HYATLAS_CONSOLIDATE_BATCH` is consolidated in successive windows across passes. After a change, passes alternate newest window and walk; a walk window failing 3 passes in a row is skipped and reported. Soft write failures (arc, schema, L5 edge, L1 mirror) are reported but not retried; LLM, merge and supersede failures are retried. The fingerprint format changed, so each owner runs once after upgrading.
- **Reprocess** walks unextracted raw rows oldest first; extracted rows are filtered before `max`.
- **Security:** dashboard values are escaped before `innerHTML` (XSS fix).
- **Dashboard:** owner selector (pairs from the most recent 1000 memories), Explore search wired to `/api/v1/search` (the semantic/keyword/hybrid tabs are not applied server-side yet), L5 page fixed. Served at `/dashboard/`.
- **Installer:** questions on `/dev/tty` under `curl | bash`; with no terminal, exported `HYATLAS_*` values go to `$HERMES_HOME/.env` (0600). Four verification outcomes with exit codes; source builds clone the `HYATLAS_VERSION` tag.
- **Plugin:** `recent` includes raw rows in lite by default; start lock falls back to unlocked with a warning on filesystems without locking.
- **Installer:** source builds fetch the onnxruntime package for the CPU (Linux x64/aarch64, macOS arm64, Windows x64/arm64; Intel macOS stops with a clear error).
- **Plugin auto-start lock:** two Hermes sessions auto-starting at once start one server.
- Status note: the `/api/v1/status` `graph_nodes` / `graph_edges`, and the `/api/graph-counts` `l5_knowledge` / `relation_count`, count all owners. Per-owner counts are in `/api/v1/graph` (`node_count` / `edge_count`).
- See CHANGELOG.md `[Unreleased]` for the full list.

## Done (v4.3.x — slow path)
- **Consolidation pass detached from the request (v4.3.2):** `POST /api/v1/digest` runs on its own context, still bounded by `consolidateTimeout` (10 min), so a client that stops waiting no longer cancels a pass. Passes are single-flight.
- **LLM client has no global timeout (v4.3.3):** a hidden 180s cap on the shared HTTP client was overriding the 600s consolidation bound. Each call path now sets its own deadline on the context.
- **Reasoning-only replies accepted (v4.3.3):** when `content` is empty, `chat()` falls back to `reasoning_content`.

## Known gaps
- Slow path is timer-driven (`HYATLAS_CONSOLIDATE_EVERY`, default 6h), not run at startup; `POST /api/v1/digest` runs one on demand.
- `/api/quality-metrics` returns `{available: false}` (v3.5-only feature, not ported).
- Upscaling and codemode are not implemented (v3.5 features).
- The coding layer is not in v4; `/api/coding-*` return empty results.

## Done (v4.2.1 — pre-catalog hardening)
- **Real version badge:** `/api/v1/status` now carries `version` from one canonical `Version` const in server.go; `handleDashInfo` reads the same const (was its own duplicate "4.2.0" literal). Desktop pane derives `const ver` from `status.version` and uses it in both the header subtitle and the save toast — no more frozen hardcoded "v4". Verified live: both endpoints report 4.2.1.
- **status wire shape defined once:** the handler built a `Status` then restated every field in a `map[string]any`; now it marshals the struct, so its json tags are the real contract. `handleDashLayerCounts` reads `Usage()`/`TotalMemories()` once each (was 2x each — inconsistent snapshots); `UsageForJSON` became dead and was deleted.
- **Version tests:** `version_test.go` (3) pins status + dash info to the canonical const and asserts the full status wire contract.
- **Catalog art:** `docs/images/banner.png` (1200x600, the exact documented `image` size; variant A "orrery rings" chosen by Tuna from 8 generated, glyph-artifact-checked clean) + the two live Desktop pane screenshots for `screenshots:`. Retina 2400x1200 variant deleted as unused — docs say follow 1200x600.
- **Compliance checked before cutting:** rule 11 — the pinned `plugins/hyatlas/` subdir never reads `auth.json`; the live-key-file feature is server-side only (`llm.go`/`server.go`/`hyatlas-go.ps1`), outside the reviewed subtree. Rule 14 — `requires_hermes: ">=0.21.4"` is not newer than the current release (v0.21.5), so the loader won't skip it.
- **L5 count unified:** `store.LayerCounts()` now always reports `l5_knowledge` = graph node count; deleted the hand-applied per-handler overrides so `/api/v1/list`, `/metrics`, `/layer-health` no longer report l5:0 while `/status` reports the real count. Go regression test pins all endpoints agree.
- **Plugin config precedence fixed:** `_load_config` layers legacy `plugins.hyatlas` + `plugins.entries.hyatlas.settings` per key (was wholesale pick).
- **Plugin unit tests:** 14 new tests vs a real localhost HTTP server (no mocks) — wire round-trip, typed errors, config precedence, tool dispatch, sync_turn, on_memory_write, delete_all guard. Full plugin suite 17 passed.
- **CI `plugin-tests` job:** pytest + smoke runner on clean Python using `agent.memory_provider` ABC from the published wheel; verified in a scrubbed venv (17 passed).

## Done (v4.2.0 — rename)
- Plugin renamed `hy_memory` → `hyatlas` everywhere: manifest, provider name, `hermes hyatlas` CLI, dashboard, desktop pane id, docs, installer snippet
- Catalog entry drafted as `plugin-catalog/hyatlas.yaml` (passes scripts/validate_plugin_catalog.py)

## Done (v4.1.4)
- Plugin catalog readiness: manifest v2 + config_schema (Desktop settings form), requires_env removed (was install-blocking), provides_tools/hooks misdeclarations removed
- `_load_config` reads `plugins.entries.hyatlas.settings` (settings-form writer) — env still wins
- Plugin README rewritten with rule-13 disclosures (network, subprocess, data, no telemetry)
- `hermes plugins validate --install-deps`: ALL GREEN incl. security scan + desktop surface
- Catalog name check: `hyatlas` free; category=memory; 49 memory entries, no lineage collision

## Done (v4.1.3)
- delete_all data-safety fix: body+query scoping, confirm=wipe-all guard (plugin body scoping was silently ignored → full-store wipe)
- doc_index.json loaded at startup (size-checked): extracted flags survive restarts
- lastExtractErr RWMutex-guarded (race fixed, -race clean)
- 7 new tests pinning retry + reprocess-by-ids + the fixes above

## Done (v4.1.2)
- Extraction outage fixed — root cause: Nous Portal WAF 403s Go's default User-Agent; fix: honest UA + reinforced retry for conversational replies (`llm.go`)
- Outage-window backfill (2026-09-05 → 10-06): 60/63 rows re-extracted, +371 L3 facts, all 7 layers live
- `/api/v1/reprocess` accepts explicit `ids`; list raw-filter applied before pagination
- Plugin CLI fixed (import boot, launcher hang, start/stop delegation)
- Mind Palace starmap updates: learning/graph API + the Desktop-side `plugin.js` (in the Hermes Desktop app, not in this repo) + dashboard dist assets
- CI: actions bumped to node24 majors; windows embedded artifact fixed

## Next
- [x] **Extraction LLM key 401 — FIXED.** Root cause was NOT rate-limiting (JWT shows rpm 800, paid_access, no cap) — it's JWT expiry: the Nous key is a 1-hour token Hermes rotates in auth.json, but the server froze it at startup. Fix: `HYATLAS_LLM_KEY_FILE` → `resolveKey()` reads live per call; ps1 points at auth.json; 4 Go tests + E2E verified (extraction fired +2 L3 in 10s).
- [x] Release v4.2.1 (L5 unification + live-key-file + plugin tests + CI job) — shipped; v4.2.5 through v4.3.3 tags exist since
- [ ] Catalog submission PR to NousResearch/hermes-agent (`plugin-catalog/hyatlas.yaml`) — HELD per Tuna until banner + screenshots are ready; re-pin sha to the released commit
- [x] Live migration on this machine: plugins/hyatlas + config.yaml (provider/enabled) + desktop-plugins — DONE (provider resolves, validate green, CLI works)
- [ ] Backups: `backup_paths` absolute + a daily data snapshot (parked by Tuna 2026-10-06)