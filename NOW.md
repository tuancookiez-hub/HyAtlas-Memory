# HyAtlas-Memory — NOW.md

## Current state (2026-07-29, clean-floor verification)
- Clean-floor fix is committed locally in the current HEAD; it is not pushed.
- Dashboard L1_RAW reads use a complete zvec filter, newest-first sorting, and all-user localhost scope unless `HYATLAS_DASHBOARD_USER_IDS` is explicitly set.
- Dashboard `/api/status` now preserves the backend's live warning/error state instead of presenting stale green health.
- Runtime LLM moved from capacity-limited `poolside/laguna-s-2.1:free` to local AI2API `alibaba-token:qwen3.8-max-preview`; the previous config was backed up under `D:\HyAtlas\.hyatlas\config`.
- Live add → L2 fact → semantic read-back succeeded at score **0.9176**; all temporary verification records were deleted and the VDB returned to **64** points.
- Backend and dashboard both report `status=ok`, `llm=ok`, `write_pipeline=ok`; browser shows `OPERATIONAL`, `L1_RAW 19`, no error overlay, and no JavaScript errors.
- Verification: **78 passed, 19 deselected**; `ruff check src/ tests/` passes. Repository-wide `ruff format --check` remains pre-existing baseline debt affecting 115 files and is intentionally not swept into this fix.

## Previous state (2026-07-29, dashboard L1 freshness)
- HEAD is `02971b3`; dashboard fix remains local and uncommitted.
- L1_RAW dashboard reads now use a complete zvec filter and newest-first sorting; default localhost view no longer hides legacy user scopes.
- Regression gate: **76 passed, 19 deselected** under Python 3.11 + zvec 0.6.0; Ruff and AST checks pass.
- User-approved restart succeeded: backend PID 33048 on `:19527`, dashboard PID 45116 on `:8765`.
- Live API shows **19 L1_RAW** rows, newest `2026-07-29T12:47:46Z`; dashboard `/api/memories` includes the same 19 rows.
- Browser shows `OPERATIONAL`, composition `L1 19`, `L1_RAW 19`, and populated recent ingestion with no JavaScript errors.
- Newest L2 fact is `2026-07-29T08:41:18Z`; provider health recovered after restart, but repeated historical 503/429 reliability remains a separate pending task.

## Previous state (2026-07-28 19:55 +08, stability review)

**Version truth:** hyatlas-memory **3.5.0**. Local HEAD is **4eb0990** on `main`; the local commits are not pushed.
**Verdict:** the process/HTTP/dashboard layer is stable; full memory-pipeline stability is **not signed off** until durable fact extraction and recall are proven.

### Live stack (probed 2026-07-28)
- `hyatlas start --detach` brought up server `:19527` and dashboard `:8765`; both remained listening during the review.
- `/api/v1/status` → HTTP 200: `status=ok`, `vdb=ok`, `embed=ok`, `llm=ok`, `write_pipeline=ok`, zvec `1024d`.
- Dashboard `/api/health`, `/api/layer-counts`, and `/api/graph-counts` → HTTP 200.
- Display counts: VDB L0–L4 = **3** global rows; Kuzu L5–L7 = **2682**; display total = **2685**. Under `hermes-user/default`, VDB rows are currently 0 and the graph is the populated store.

### Verification results
- Full suite: **63 passed, 14 skipped**.
- Ruff, Python compilation, and dashboard JavaScript syntax checks: pass.
- `hyatlas doctor` and `hyatlas status`: pass.
- One tagged `add` returned a memory ID and exit 0; exact `delete` returned exit 0 and restored the VDB count.
- Static standalone fallback fix is committed in `4eb0990`; no push performed.

## Known blockers / caveats
- A new write is persisted as `l1_raw`, but the legacy reader deliberately excludes `l1_raw` from semantic recall. The live default profile has no L2 facts, so `write_pipeline=ok` does not prove durable extraction or recall. An exact L1 probe was not returned by search; this is a functional gap to resolve or explicitly document.
- The current digest log is historical (last recorded run July 14); `digest_log_status=ok` is not evidence of a current digest run. `fresh_l2_for_digest=0`.
- README documents `hyatlas memory ...` aliases, but the installed CLI exposes top-level `add/search/list/delete`; documentation drift remains.

## Solid
- Dedicated HyAtlas venv and local BGE embedder (1024d).
- Zvec 0.6+ runtime opens and stays alive after detached launch.
- Kuzu graph/dashboard split count path is responding and internally consistent.
- Launcher import fallback works without `hermes_constants` (covered by a standalone test).

## Next
- Decide whether L1 fallback recall is intentional; if not, fix reader routing or prove L2 extraction with a real fact and a read-back test.
- Run a current digest only after the extraction path is fixed/confirmed.
- Re-run the exact add → durable layer → search → delete probe before any stable-release claim.
