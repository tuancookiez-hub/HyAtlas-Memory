# HyAtlas-Memory — NOW.md

## Current state (2026-07-28 19:55 +08, stability review)

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
