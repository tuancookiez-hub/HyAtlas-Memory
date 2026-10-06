# HyAtlas v4 — NOW

**v4.1.2** | 2026-10-06 | Release: tuancookiez-hub/HyAtlas-Memory (tag pending push)

## Running
- hyatlas-go v4.1.2 build listening :19528 (extraction verified; outage backfill complete)
- Plugin: `hy_memory` @ `C:\Users\tuanc\AppData\Local\hermes\plugins\hy_memory\` + Hermes Desktop pane
- Watchdog: hourly extraction-freshness cron (`hyatlas-extraction-watchdog`)

## Done (v4.1.2)
- Extraction outage fixed — root cause: Nous Portal WAF 403s Go's default User-Agent; fix: honest UA + reinforced retry for conversational replies (`llm.go`)
- Outage-window backfill (2026-09-05 → 10-06): 60/63 rows re-extracted, +371 L3 facts, all 7 layers live
- `/api/v1/reprocess` accepts explicit `ids`; list raw-filter applied before pagination
- Plugin CLI fixed (import boot, launcher hang, start/stop delegation)
- Mind Palace starmap updates: learning/graph API + `desktop/plugin.js` + dashboard dist assets

## Next
- [ ] Tuna review → push v4.1.2 (tag + GitHub release)
- [ ] `SetExtracted` meta persistence fix (flags reset on restart)
- [ ] Review follow-ups: `backup_paths` absolute + real backups; `delete_all` guard; doc-rot sweep