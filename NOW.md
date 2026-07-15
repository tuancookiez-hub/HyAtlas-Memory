# HyAtlas-Memory — NOW.md

## Current state (2026-07-16)

**L1_RAW transparency:** `/api/v1/list` now returns raw writes alongside extracted memories. Each entry has an `extracted: true|false` field. Default `include_raw=True`; pass `include_raw=False` to revert to the previous "extracted-only" view. Fixes the "I wrote something but it didn't show up" confusion.

**3-tier server health:** `/api/v1/status` returns `{status: ok|warning|error, vdb, embed, llm, write_pipeline}`. LLM rate limits now surface as `warning` (with `write_pipeline: rate_limited`) instead of marking the whole stack `error: 503`. Persisted memories remain readable even when extraction is throttled.

**Vector store:** zvec only at runtime (1024 dims, `agent_memories_1024` collection). Qdrant fully removed (zip kept at `~/.hyatlas/archive/qdrant_v3_1_0_release.zip`).

**Live numbers (probed 2026-07-16):**
- VDB visible via `/api/v1/list` with `include_raw=True`: **207 memories** (1 l0_basic_info, 9 l2_fact, 14 l1_raw, 181 l5_knowledge, 2 l7_intention)
- Graph: **L5=1807, L6=580, L7=188, relations=8988** (via `/api/graph-counts`)
- zvec collection: **7149 docs** on disk

## What changed recently

- **Memory transparency** — `include_raw` flag in `/api/v1/list`, `extracted` field on each memory entry. (L1_RAW no longer silently hidden when LLM extraction skips noisy input.)
- **Persist failure surfaces** — `writer.write()` now returns `success: False, error_code: 502` when `vector_store.upsert()` fails. Previously swallowed as `success: True`.
- **zvec `update_payload` schema fix** — payload-only updates now fetch existing embedding and serialize `custom` as JSON string to match zvec's expected schema.
- **System 2 digest tuning** — `_S2_MAX_TOKENS=1024`, `_S2_MAX_FACTS_PER_CALL=8`, `_S2_MAX_CLUSTERS_PER_CALL=4`. Single 108-fact cluster now splits into 14 batches instead of one context-busting call. Token-cap-then-retry behavior on `finish_reason=length`.
- **Privacy scrub** — `pyproject.toml` author, Discord snowflake IDs, GitHub handles, and `C:\Users\<user>\` paths replaced with `<placeholder>` markers. Runtime behavior preserved via new env vars: `HYATLAS_DASHBOARD_USER_IDS`, `HYATLAS_DEFAULT_USER_ALIASES`, `MEMORY_L5_USER_IDS`.

## Console status window

`hyatlas --detach` spawns a visible status window from `cmd.exe`. From MSYS bash the spawn path is still flaky (window sometimes fails to render) — known regression tracked separately. **Service stays up regardless of window state.** Reopen with `hyatlas console`.

## Layer model

| Layer | Purpose | Visibility |
|-------|---------|------------|
| L0 basic_info | User identity facts | list (always) |
| L1 raw | Unprocessed user input | list (when `include_raw=True`, default) |
| L2 fact | LLM-extracted facts | list (always) |
| L5 knowledge | Curated knowledge nodes | list + graph |
| L6 schema | Cross-domain schemas | graph only |
| L7 intention | Goals & plans | graph + list |

L4 retired (legacy VDB rows only).

## Operational state

| Area | Status |
|------|--------|
| Capture | Hermes → L1 under `tuna` / `default_agent` |
| Evolution | Weekly digest (System 2 batched); log `ok`; graph L5 **1807**, L6 **580**, L7 **188**, rels **8988** |
| L4 | Retired; legacy VDB rows only; archive in `D:\HyAtlas\.hyatlas\archive\` |
| Vector store | **zvec** only at runtime |
| Runtime home | `HYATLAS_HOME=D:\HyAtlas\.hyatlas` |
| Cron | `smart-memory-prune` (4h, Discord thread); `HyAtlas weekly digest` (7d) |
| Dashboard | http://127.0.0.1:8765 — Quality Metrics, layer-health, L6 schema samples |

## Manual ops

- Digest: `python %LOCALAPPDATA%\hermes\scripts\run_hyatlas_digest.py`
- Status: `curl http://127.0.0.1:19527/api/v1/status`
- List (with raw): `curl -X POST http://127.0.0.1:19527/api/v1/list -H "Content-Type: application/json" -d '{"user_id":"tuna","limit":50}'`
- List (extracted only): same with `"include_raw": false`
- L6 browse: `http://127.0.0.1:8765/api/l6-schemas?n=8` or `GET /api/v1/graph?layer=l6_schema`

## Privacy / placeholders

All real identifiers replaced with `<placeholder>` markers. Override at runtime via env vars:
- `HYATLAS_DASHBOARD_USER_IDS` (comma-separated list)
- `HYATLAS_DEFAULT_USER_ALIASES`
- `MEMORY_L5_USER_IDS`

Default values in source are public-safe (`hermes-user,<discord_user_id>` placeholders).

## Stale doc references

The following docs predate the L1_RAW transparency fix and 3-tier status update. They are correct in broad strokes but may need a refresh pass before publishing:
- `docs/LAYERS.md` (Jul 8)
- `docs/architecture.md` (Jul 8)
- `docs/API.md` (Jul 8)
- `docs/DASHBOARD.md` (Jul 8)
- `docs/SERVER.md` (Jul 5)
- `docs/TROUBLESHOOTING.md` (Jul 8)
- `docs/AUDIT-v3.2.1.md` (Jul 8)

## Next (optional)

- Console window UX fix (MSYS bash spawn path)
- Stale doc refresh (see list above)
- Profile canary (research profile) end-to-end test
- BM25 reader E2E with `HY_MEMORY_READER=hybrid_v2`
