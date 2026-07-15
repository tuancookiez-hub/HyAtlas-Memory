<!--
STALE DOC NOTICE (2026-07-16):
This document may be out of date. For current state, see ../NOW.md
or https://github.com/<owner>/HyAtlas-Memory/blob/main/NOW.md
Last meaningful refresh: see the date in this header's filename context.
-->

# Release audit — v3.2.1 (2026-07-08)

Pre-push verification for HyAtlas-Memory documentation + dashboard graph counts.

## Doc sweep (completed)

| Area | Action |
|------|--------|
| `README.md` | Zvec-first, layer table, L4 retired, digest path |
| `docs/API.md` | Rewritten: Zvec, `/api/v1/graph`, `/api/v1/digest`, dashboard endpoints |
| `docs/LAYERS.md` / `architecture.md` | v3.2 canonical |
| `docs/DASHBOARD.md` | layer-health, l6-schemas, graph-counts shape |
| `docs/TROUBLESHOOTING.md` | Zvec section first; Qdrant = legacy |
| `CONTRIBUTING.md` | `hyatlas start` dev path |
| `CHANGELOG.md` | 3.2.1 bullets |

**Intentionally historical:** `docs/archive/*`, `CHANGELOG` pre-3.2 entries, `docs/MIGRATION_v2_SCLASS.md`, `docs/architecture/RUNTIME_LAYOUT_*.md` (layout migration era).

## Live probes (Tuna machine, 2026-07-08)

Run after `hyatlas start` and **dashboard restart** if `dashboard.py` changed:

```bash
curl -s http://127.0.0.1:19527/info
curl -s http://127.0.0.1:19527/api/v1/graph | jq '.layer_counts, .relation_count'
curl -s http://127.0.0.1:8765/api/layer-health | jq '.graph_layer_counts, .digest_log_status, .fresh_l2_for_digest'
curl -s http://127.0.0.1:8765/api/graph-counts
curl -s http://127.0.0.1:8765/api/l6-schemas?n=3 | jq '.graph_l6_total, .count'
```

### Recorded snapshot (audit run)

| Probe | Value |
|-------|--------|
| `layer_counts` | L5=1594, L6=568, L7=188 |
| `relation_count` | 8128 |
| `layer-health` digest | `ok` |
| Namespace | `hermes-user` / `default` |

`/api/graph-counts` verified L6=568 after dashboard restart (2026-07-08 audit).

## Code fix in this audit

- `dashboard.py` `/api/graph-counts` — use `layer_counts` from live graph API.

## Push checklist

- [ ] `pip install -e .` or restart stack so server reports package version if bumped
- [x] Re-run probes above — **passed** (L6=568 on `/api/graph-counts`)
- [ ] `git push origin main` + `git push origin v3.2.1`