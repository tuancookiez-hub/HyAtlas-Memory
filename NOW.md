# HyAtlas v4 — NOW

**v4.2.0** | 2026-10-07 | Release: tuancookiez-hub/HyAtlas-Memory

## Running
- hyatlas-go v4.2.0 build listening :19528 (live-key-file fix in place; extraction healthy)
- Plugin: `hyatlas` @ `C:\Users\tuanc\AppData\Local\hermes\plugins\hyatlas\` + Hermes Desktop pane
- Watchdog: hourly extraction-freshness cron (`hyatlas-extraction-watchdog`)

## Done (pre-catalog hardening — unreleased, committed local)
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
- Mind Palace starmap updates: learning/graph API + `desktop/plugin.js` + dashboard dist assets
- CI: actions bumped to node24 majors; windows embedded artifact fixed

## Next
- [x] **Extraction LLM key 401 — FIXED.** Root cause was NOT rate-limiting (JWT shows rpm 800, paid_access, no cap) — it's JWT expiry: the Nous key is a 1-hour token Hermes rotates in auth.json, but the server froze it at startup. Fix: `HYATLAS_LLM_KEY_FILE` → `resolveKey()` reads live per call; ps1 points at auth.json; 4 Go tests + E2E verified (extraction fired +2 L3 in 10s).
- [ ] Release v4.2.1 (L5 unification + live-key-file + plugin tests + CI job) — bundle with banner/screenshots once Tuna delivers them
- [ ] Catalog submission PR to NousResearch/hermes-agent (`plugin-catalog/hyatlas.yaml`) — HELD per Tuna until banner + screenshots are ready; re-pin sha to the released commit
- [x] Live migration on this machine: plugins/hyatlas + config.yaml (provider/enabled) + desktop-plugins — DONE (provider resolves, validate green, CLI works)
- [ ] Backups: `backup_paths` absolute + a daily data snapshot (parked by Tuna 2026-10-06)