# HyAtlas-Memory Project — Session Handoff

**Date:** 2026-06-17
**Session duration:** ~6 hours
**Plan file:** `.hermes/plans/l5_l6_l7_implementation.md`
**Last state file:** `~/AppData/Local/hermes/state/l5_l6_l7_progress.md`

## TL;DR — where we are

The HyAtlas-Memory system is **structurally complete with all 8 layers populated**, but the user wants to push it to "best thing ever." We've made all the easy improvements. The next work needs fresh context, real usage feedback, and a clear scope per session.

## What got done this session

1. **Verified cron `memory-pruner` works live** at 01:30, 01:39, 02:00 etc. (status: ok)
2. **Fixed cron prompt's broken Python snippet** — was calling `HyMemoryProvider.add()` (doesn't exist), now correctly calls `HyMemoryClient.add()` with `user_id`, `agent_id`, `session_id`, `metadata`
3. **Cleaned up 4.5 GB of Qdrant bloat** — deleted orphan 1536 collection (757 MB / 0 points), bloated coding_keys_384 collection (949 MB / 89 points), and 4 stale snapshots
4. **Added `qdrant_health` doctor stage** — catches orphan collections, bloat, snapshot pile-up
5. **Added `qdrant-snapshot-rotate` cron** — daily 03:00, keeps ≤2 snapshots per collection
6. **Reset hermes-agent fork to upstream HEAD** — dropped 3 local commits (mimo context guards, modelark provider, etc.) that user no longer wanted
7. **Ran `hermes update`** — clean, no regressions
8. **Fixed PowerShell `hyatlas.bat` shim** — was opening VS Code instead of running python; added `python.exe` explicit invocation
9. **Diagnosed HyMemory mode confusion** — `mode: ultra` in config but the mode only changes the system-prompt text, not L6/L7 enablement
10. **Audited upstream `hy_memory` PyPI package** — found L6/L7 producer code (`intention_detector.py`, `cross_domain_sweeper.py`) that we initially missed
11. **Ran L5 pipeline on 63 L2 facts** — 5 entities (HyAtlas, Hermes, HyMemory, Qdrant, Sentinel) + 1 relation written to Kuzu
12. **Discovered L6/L7 already populated** by upstream's background intention detector
13. **Added `layer_coverage` doctor stage** — verifies all 8 layers queryable via `reader=exhaustive`

## Commits to public repo (F:/Projects/hyatlas-memory)

```
fcfd022  feat(doctor): layer_coverage stage verifies L0-L7 via exhaustive search
0ceb52c  docs(plan): update L5/L6/L7 plan with actual implementation results
b72a023  docs(plan): L5/L6/L7 implementation plan based on real source audit
522351b  feat(doctor): cron_health stage + dynamic stage count
3de4738  feat(doctor): qdrant_health stage + lock dependencies
3c93670  fix(start_server): apply user-space patches from the standalone hyatlas_memory package
```

## Current system state (2026-06-17 07:30)

### Doctor: 9/10 green in 4.5s
- ✅ install, entry_point, server_health, qdrant, qdrant_health, kuzu, plugin_prefetch, roundtrip, cron_health
- ❌ layer_coverage (3 layers missing from search-visible counts: L1, L3, L5 — but all 3 exist in data)

### Layer counts in Qdrant
| Layer | Count | Search-visible? |
|---|---:|---|
| L0 basic_info | 8 | ✅ default |
| L1 raw | 60 | ⚠️ exists, test queries don't surface |
| L2 fact | 132 | ✅ default |
| L3 summary | 3 (1 original + 2 new) | ⚠️ exists, test queries don't surface |
| L4 identity | 56 | ✅ default |
| L5 knowledge | 5 in Kuzu | ⚠️ NOT in Qdrant (only Kuzu) |
| L6 schema | 48 | ✅ reader=exhaustive |
| L7 intention | 12 | ✅ reader=exhaustive |

### Kuzu L5
- 5 entities: HyAtlas, Hermes, HyMemory, Qdrant, Sentinel
- 1 relation: HyMemory --uses--> Qdrant
- Plus 133 inherited RELATED_TO edges from prior runs

### Disk
- Qdrant: 6.8 GB (was 1.3 GB after cleanup; grew back to 6.8 GB after L5 run + upstream items added in parallel)
- HyMemory: 562 MB (Kuzu graph is 67 MB, was 62 MB)
- C: drive: ~98 GB free (was 101 GB before this session)

### Crons
- `memory-pruner` (6f0ce68182e0): every 30 min, last status: ok
- `qdrant-snapshot-rotate` (ff8e4b822f4b): daily 03:00, last status: ok

## Known gaps (the "improve more more" wishlist)

Listed by impact. User wants to push HyAtlas to "best thing ever to come to AI" but we agreed to NOT start more work this session.

### Tier 1: daily experience (1-2 hours each)
1. **Surface L5 in search** (~30 lines in `reader_exhaustive.py` line 175) — L5 entities currently only in Kuzu
2. **L3 summary synthesis on all 132 L2 facts** — produce ~10-15 meta-summaries
3. **Calibrate layer_coverage test queries** — better queries → all 8 ✅

### Tier 2: how it feels (2-4 hours each)
4. **Reasoning flag in doctor** — explain WHY each stage failed
5. **Memory quality scorer** in prefetch — show why each memory matched
6. **`/memories` CLI subcommand** — `git log --oneline` for memory writes

### Tier 3: what's possible (1-2 days each)
7. **Cross-session memory continuity** — session-end summary captures what was done, decided, open. **The single biggest behavioral change.**
8. **Active memory rebalancing** — auto-merge duplicates, archive old L7, prevent bloat over time
9. **Real LLM-based fact extraction at write-time** — higher quality L2 facts
10. **Semantic search with graph expansion** — vector + L5 graph + L6 schema edges. **This is what makes cognitive architecture earn its keep.**

### Tier 4: nobody will notice
11. More LLM prompt engineering
12. More SDK channels
13. Better error messages
14. L5 auto-rebalance cron

## Recommendation for next session

**Don't start Tier 1 immediately.** Use the system in normal mode for a few days first. Find friction. Then pick the tier item that addresses the actual friction you hit.

If forced to pick the highest-impact item right now, **Tier 3 #7 (cross-session memory continuity)** is the one that would make the most behavioral difference. Sessions currently have no narrative continuity — you start a new session and the system has no idea what you were doing yesterday.

## Important context for the next session

### What the user cares about
- "stay with me to keep learning along with me" (long-term companion)
- "FULL ALL LAYER hymemory" (all 8 layers)
- "best thing ever to come to AI" (the wishlist above)
- Honest answers, no overselling

### What the user is sensitive about
- Don't make big assumptions about what they want — ask
- Don't break the working state
- The system must remain operational, not just impressive
- "Data loss OK in testing if documented" per memory.md, but real memories are not testing data

### Decisions made this session (user explicit)
- L5: use fork's existing pipeline (`server/bin/l5_*.py`)
- L6/L7: bridge to upstream `intention_detector.py` and `cross_domain_sweeper.py`
- LLM prompts: English (not Chinese, which is what upstream uses)
- Branch: default is `dev` in hermes-agent fork, but HyAtlas repo uses `main`
- Fork's old L5 pipeline: keep, don't delete (it's the actual L5 implementation)

### Decisions to make in next session
- Which tier to start with (depends on what friction user hits)
- Whether to change the doctor to use `reader=exhaustive` by default (currently legacy)
- Whether to disable the bloat warning (549 MB for 163 points is leftover HNSW bloat, will self-heal)
- Whether to commit the L2 export as a proper script in the repo (currently one-off)

## Files / paths to know

### Project (F:/Projects/hyatlas-memory)
- `src/hyatlas_memory/__init__.py` — main package (38 KB)
- `src/hyatlas_memory/client.py` — `HyMemoryClient` (8 KB)
- `src/hyatlas_memory/patches.py` — 16 patches including L5 auto-trigger (75 KB)
- `server/start_server.py` — server entry
- `server/bin/l5_*.py` — L5 pipeline (10 files, ~120 KB)
- `server/dashboard/` — Flask + HTML dashboard
- `scripts/smoke_test.py` — doctor with 10 stages (33 KB)
- `.hermes/plans/l5_l6_l7_implementation.md` — the L5/L6/L7 plan

### Logs / data (C:/Users/tuanc/AppData/Local/hermes)
- `hy_memory.json` — config (mode: ultra, LLM via tokenrouter, embedder local BGE-small 384-dim)
- `bin/hymemory.py` — CLI wrapper (25 KB)
- `bin/qdrant_snapshot_rotate.py` — rotation script
- `bin/l5_*.py` — L5 pipeline orchestrator
- `cron/jobs.json` — cron registry
- `cron/memory-pruner-prompt.txt` — pruner prompt
- `cron/output/6f0ce68182e0/` — pruner fire outputs
- `logs/` — agent.log, errors.log, l5_*.log/json, etc.
- `state/l5_l6_l7_progress.md` — this session's progress file

### Hermes-agent fork (C:/Users/tuanc/AppData/Local/hermes/hermes-agent)
- Branch: main (synced with upstream NousResearch/hermes-agent, no local commits)
- HyAtlas shim: `venv/Scripts/hyatlas` and `venv/Scripts/hyatlas.bat`
- 19 commits ahead of fork/main (pushed to fork/main only if user requests)

### Qdrant + Kuzu
- Qdrant: `C:/qdrant/` — collection `agent_memories_384` (161 indexed, 7983 doc count, 645 MB) and `agent_memories_384_tag_index`
- Kuzu: `C:/Users/tuanc/.hy_memory/data/kuzu_db` (67 MB)
- Both have 5 snapshots each (from the 5 `start` invocations during this session)

## Lessons learned this session (save for future me)

1. **Read the source, not just the local fork.** I claimed L5/L6/L7 were "not implemented" when they were in the upstream PyPI package. Cost 30 min of confusion.
2. **The default `legacy` search reader only surfaces L0-L4.** Use `reader=exhaustive` to see all 8 layers. Document this in the wrapper.
3. **`hyatlas.bat` needs `python` prefix in PowerShell.** Otherwise Windows file association opens the file in VS Code.
4. **L5 lives in Kuzu (graph), not Qdrant (vector).** Search readers query Qdrant for L5, which is empty. Architectural gap.
5. **The L5 pipeline's input file expects `id` key, not `memory_id`.** When generating L2 exports, use `id` not `memory_id`.
6. **The server's add pipeline reclassifies layer and drops metadata.** Direct writes via `HyMemoryClient.add()` get the full upstream treatment, not the raw payload you sent. Use this for production, expect the layer to be reclassified.
7. **Don't promise to build things in one session that are multi-day projects.** "Full all layers" looked like a 5-min task from the outside and was 5 hours.
8. **"Make it the best" is not a code task — it's a feedback loop task.** Use the system, find friction, fix friction. The system is already at parity with or beyond the alternatives.
