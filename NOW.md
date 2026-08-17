## Current repair set (2026-08-17, v3.5.0 floor refresh)

- **LLM primary + fallback chain.** `hy_memory.json` carries `llm.fallback_model` / `fallback_base_url`. Primary switches to fallback on 429/502/503/504/connection/timeout/unavailable. `_on_fallback` guard prevents loops.
- **Shadow `LLMConfig` class aligned.** `core/agent/llm_provider.py:98` had a duplicate `LLMConfig` definition that lacked every new field. Both `LLMConfig` classes (in `core/config.py` and `core/agent/llm_provider.py`) now have the same 4 fallback fields — this is what unblocked the restart cycle on 2026-08-17.
- **AsyncOpenAI client cached.** `LLMProvider` now reuses a single `AsyncOpenAI(...)` per instance; the cached client is reset (`self._client = None`) when the fallback fires so the new credentials/base_url take effect.
- **zvec ordinary `upsert()` triggers `maybe_compact()`** with cooldown/single-flight; `upsert_batch()` behavior preserved. **Reclaimed 18.33 GB / 3,443 shards live on 2026-08-17** (19.75 GB → 1.42 GB across 1–4 shards).
- **Plugin `auto_start` defaults to `False` upstream**; explicit local `true` still honored. Regression test: `tests/test_autostart_default.py`.
- **`hyatlas status`, `config`, `doctor`, `console`** show only `Provider:` + `Model:`.
- **Weekly digest scripts** use canonical `l3_fact` and run via the dedicated HyAtlas venv (`D:\HyAtlas\.hyatlas\venv`).

### Static + Live Gates

- 167 tests collected, **166 passed, 1 skipped**, `ruff check src/ tests/` clean.
- Live server: `127.0.0.1:19527` returns `status=ok`, `vdb_points≈1212`, `write_pipeline=ok`, `LLM=ok`.
- L3 write proven: memory_id `818c39c1-abab-4dfb-9dc0-261f48...`.

### Risks / Open Items

- OpenRouter free quota is 50 calls/day; reset at 2026-08-17T08:00 MYT. After reset, fallback chain kicks in cleanly when quota runs out.
- This is the **stable floor for the upcoming memory-system overhaul**. Anything pushed after this commit should branch from this SHA.

# HyAtlas-Memory — NOW.md

## Current state (2026-08-05, published v3.5.0 floor)

**v3.5.0 is the certified stable floor.** All clean-floor phases and the
post-certification crash/reopen correction are complete. The maintained
`main` branch, `v3.5.0` tag, and GitHub Release are the public distribution
points for this floor. The release includes the stable-floor infographic at
`assets/hyatlas-v3.5.0-stable-floor.png`.

### Final local runtime
- Recommended local embedder: `BAAI/bge-small-en-v1.5` (**384d**).
- Active physical collection: `agent_memories_384`; fresh-memory strategy,
  no full re-index of the retired 1024d collection.
- **Digest recovery completed 2026-08-06:** the synchronous client inherited
  `_LoopThread.run()`'s generic 300s timeout, so a valid multi-batch digest
  returned HTTP 500 while continuing to mutate Kuzu. `digest()` now uses
  `MEMORY_DIGEST_TIMEOUT` (3600s default), timed-out futures are cancelled,
  and HTTP reports `504 digest_timeout`. After restart, the remaining default
  digest completed in 383.2s: L5 +24, L6 +5, relations +104. Canonical digest
  status is `ok`; three isolated L2 facts remain below the useful clustering
  threshold and were intentionally not force-digested.
- **Layer numbering closed 2026-08-08:** the retired `L4 IDENTITY` slot is now
  gone — layers renumbered to a contiguous `L1..L7` (`L0/L1/L2/L3 → L1/L2/L3/L4`).
  362 VDB rows had their `layer` payload rewritten on disk; graph storage
  values for `l5_knowledge / l6_schema / l7_intention` were unchanged.
  Historical archive file `archive/l4_identity_pre_migrate_20260707.jsonl`
  deleted (no rows referenced it any longer).
- **Kuzu dimension lane repaired 2026-08-05:** the preserved graph keeps its
  original 1024d vector properties, while new writes/search use additive
  `embedding_384` / `beh_embedding_384` and `memory_content_idx_384`. No old
  graph node or relation was re-embedded or removed. Health exposes active
  384d plus legacy `[1024]`; repeated live searches produce zero mismatch warnings.
- zvec crash contract corrected: zero-byte `LOCK` files are normal marker
  files. A force-killed isolated owner reopens successfully under zvec 0.6.0
  without deleting them. Recovery rule: `hyatlas stop`, verify the owner is
  gone, then `hyatlas start --detach`.
- Valid code fixes retained: idempotent collection-path resolution and zvec
  C++ open/create off the asyncio event loop.
- Phase 6.3 complete: trading-profile HyAtlas skills synchronized; obsolete
  copies archived, not deleted.

### Phase 7.3 — Release-floor review ✅
- **Tracks A/B/C re-run live** (originally 5 pass / 23 fail on 2026-07-29).
  Browser walkthrough: all 8 dashboard pages render real canonical data with
  0 JS errors (Overview 1,756 VDB pts / 10,672 relations / 3,884 display;
  Observatory Kuzu 2,849/615/188 stored-only; Explore live search 25 results;
  Layers L4 retired; Today/Activity real entries; Settings v3.5.0 + zvec +
  disk usage; Quality N/A-honest + stale-evidence gated; L5 live counts +
  "Loaded at" + relation filters).
- **Final gates:** 138 passed / 1 skipped / 0 deterministic failures
  (`test_prefetch_returns_formatted_block` is a confirmed 15-19s LLM-latency
  flake; passes isolated in 18.7s). Ruff `--no-cache`, compileall, JS syntax:
  all green.
- **Fixed during 7.3:** dashboard boot stuck at "Loading all profiles" —
  `/api/memories` fetched 500 full-content rows per user (98MB / 43s, killed
  by a 15s proxy timeout → false 502). Capped per-user fetch at 100 rows,
  L1 window to the page size, proxy timeout to 120s, and corrected the
  visible stale digest command. Load now completes in ~24s with a truthful
  exact total (247) and a 32KB payload.
- **Kuzu health gap closed earlier in the arc:** `/api/v1/status` +
  `/api/health` now probe the graph store (no false-green); 5 failure-mode
  contract tests cover embedder/VDB/Kuzu-down honestly.
- Commit series (local, one concern each): health/kuzu contracts,
  dashboard truth + resilient load, bounded list/count-only perf,
  lifecycle offline-env + launcher probes, contract tests, docs, chore.

### Deferred by decision (not floor blockers)
- No full re-index of the old 1024d collection. New 384d memory grows
  naturally; old collection remains quarantined locally for rollback only.
- Upstream Hy-Memory 1.2.21 design ideas (`rolling_summary`, `add_extracted`,
  `user_basic_info`) remain future inputs. No v4.0/Go work in this release.

## Previous current state (2026-07-31, clean-floor Phase 7 — lifecycle cert passed)
- Phases 1–6 remain complete and verified.
- Phase 6 external blocker unchanged: profile-local skill duplicates (trading-profile) deferred until explicit hygiene approval.
- **LLM provider swapped to `minimax:MiniMax-M3`** via ai2api proxy. Previous providers: alibaba-token (quota exhausted, resets 08-03), kimi-grey:k3 (429 rate-limited under load). Minimax is stable.
- Stack running: server `:19527` PID 36176, dashboard `:8765` PID 33536. All green: `vdb=ok, embed=ok, llm=ok, write=ok, points=494`.

### Phase 7.1 — End-to-end memory lifecycle ✅
- Write → `POST /api/v1/add` → `success: true`, `extraction_status: success`, memory_ids returned (~10s with minimax).
- Extract → L2 facts created (count went from 55 → 90 after swap + probes).
- Search → `POST /api/v1/search` returns the memory in the `normal` channel. (GET search returns 0 due to response shape mismatch — POST is the correct client API.)
- Delete → `DELETE /api/v1/memories/{id}` and `POST /api/v1/delete_all` both 200. Probes cleaned.
- **Full lifecycle certified.**

### Phase 7.2 — Failure-mode certification
- ✅ **Provider limited/down:** verified with alibaba-token (quota exhausted) and kimi-grey (429 rate limit). Dashboard reported honest degradation; tests failed deterministically, not silently.
- ✅ **Backend offline while dashboard alive:** verified 2026-07-30. Dashboard degraded honestly, recovered after restart.
- ✅ **Service restart with persisted data:** verified 2026-07-30 and again on provider swap. VDB points and Kuzu graph preserved.
- ✅ **Embedder/VDB/Kuzu unavailable:** 2026-08-04 contract tests (no store renames needed).
- ✅ **Empty specialist profile:** honest `{"memories": [], "total": 0}`.
- ✅ **Large graph rendering:** 1,907 nodes / 10,384 relations served in 0.28s; Observatory caps render at 500 with "showing N of M".

### Test suite
- **121 passed, 14 skipped, 0 failed** (Python 3.13 via uv). Previous 4 integration failures (extraction) now pass with minimax provider.
- `hyatlas doctor` — all checks passed, zero warnings.

### Current blockers / caveats
- GET `/api/v1/search` returns 0 hits due to response shape (returns dict with `profile`/`proactive`/`normal` keys, not a flat `memories` list). POST search works correctly. This is a client API inconsistency, not a data loss issue.
- Phase 6.3 (profile-local skill duplicates) remains deferred.

## Previous current state (2026-07-29, clean-floor Phase 5 complete)
- Phases 1–4 remain live: separated datasets, stored-truth Observatory, explicit profile scope, truthful health/failure handling, durable quality evidence, and active-home storage reporting.
- Phase 5 browser verification covered all eight pages against direct APIs: Overview, Observatory, Explore, Layers, Today, Settings, Quality, and L5.
- The walkthrough found and fixed two additional bugs: specialist Overview used global `status.vdb_points` instead of scoped `vdb_total`, and a fixed 30-second interval overlapped slow scoped refreshes and left the header in a false Loading state.
- Optional-domain failure was exercised by injecting a quality HTTP 503: last-known data remained visible and the page showed a stale-data banner.
- Deep-check receipt: `C:\Users\tuanc\AppData\Local\hermes\checklists\default-hyatlas-phase5-20260729.md`.
- Final verification: **102 passed, 14 skipped, 19 deselected** under Python 3.11; Ruff `--no-cache`, compileall, JavaScript syntax, API parity, profile scope, browser interactions, and screenshots pass.
- The existing dashboard on `:8765` still runs the pre-Phase-4/5 process because no restart was approved. No commit, push, tag, or release performed.
- Next: Phase 6 — reconcile current repository docs and active global HyAtlas skills. Profile-local duplicate cleanup remains a separate approval scope.
