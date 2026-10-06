# HyAtlas v4 — NOW

**v4.1.3** | 2026-10-07 | Release: tuancookiez-hub/HyAtlas-Memory

## Running
- hyatlas-go v4.1.3 build listening :19528 (extraction verified; outage backfill complete)
- Plugin: `hy_memory` @ `C:\Users\tuanc\AppData\Local\hermes\plugins\hy_memory\` + Hermes Desktop pane
- Watchdog: hourly extraction-freshness cron (`hyatlas-extraction-watchdog`)

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
- [ ] Backups: `backup_paths` absolute + a daily data snapshot (parked by Tuna 2026-10-06)