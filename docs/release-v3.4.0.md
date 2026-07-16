# v3.4.0 — Profile isolation in the dashboard + L1_RAW transparency

**Released:** 2026-07-16
**Tag:** `v3.4.0` (annotated)
**Commits since v3.3.2:** 11
**Files changed:** 53 (+2469 / -1634)
**Breaking changes:** None
**Upgrade from v3.3.2:** Safe. Run `pip install -U hyatlas-memory` and `hyatlas setup hermes`.

---

## TL;DR

Two new things you can use, and a long list of things that now work the way you'd expect.

**The two new things:**

1. **Profile isolation in the dashboard** — the `agent_id` data layer has been in place since the v3.0.0 fork, but the UI surface lands here. Pick a profile from the new dropdown and every dashboard tab filters to that scope.
2. **L1_RAW transparency** — `/api/v1/list` now returns the original raw payload alongside the processed L2 fact. Useful for debugging "I wrote but it didn't show up."

**The "now works" list (selected):**

- L5 tab `EXPORTED AT` is no longer `unknown`
- Status is 3-tier (vdb / embed / llm) instead of one binary ok/degraded
- Settings tab shows both per-agent and global graph counts
- Layer counts are live (no more stale Qdrant reads)
- Console window opens reliably (no more empty-PowerShell flash)
- Weekly digest doesn't loop on `finish_reason=length`
- Writes that fail to persist now return `[PERSIST_FAILED]` instead of lying about success
- 4 pre-existing ruff errors fixed (CI passes again)

---

## What's new

### Headline

#### Profile isolation lands in the dashboard

The `agent_id` data-layer filter (introduced in the v3.0.0 fork) was always supported by the API. What's new in v3.4.0 is the **UI surface**: a profile dropdown in the dashboard, a `/api/profiles` endpoint, and per-tab `?agent_id=...` URL filtering.

| Before v3.4.0 | After v3.4.0 |
|---|---|
| API supported `agent_id` but the dashboard ignored it | Dashboard has a profile dropdown (default, research, sentinel, work-backend, work-frontend, trading, hestia) |
| All tabs showed `default` only | Every tab filters to the selected scope |
| Profile selection not persisted | Sticky via `localStorage` |
| No way to enumerate profiles | `/api/profiles` returns counts per profile |

This is the moment "specialist agents have their own memory" goes from "the data layer supports it" to "I can actually use it." See `docs/PROFILE_MEMORY_ARCHITECTURE.md` for the design rationale.

#### L1_RAW transparency

`/api/v1/list` now accepts `include_raw: true` (default) and returns the original L1_RAW payload alongside the processed L2 fact. Every memory item also carries an `extracted` boolean.

```json
{
  "memory_id": "...",
  "content": "user likes dark mode",
  "layer": "l2_fact",
  "extracted": true,
  "source_raw_memory_id": "..."
}
```

**Why this matters:** before, when LLM extraction failed or skipped noisy input, the raw write was invisible — you'd see "I wrote a memory" succeed but never appear in any list. Now you can see exactly what landed in L1_RAW and what the LLM did (or didn't) extract from it. Powers the Today / Activity timeline and the VDB scroll path.

### Dashboard

- **L5 Knowledge Graph — `EXPORTED AT` timestamp fallback.** Previously the field read `unknown` when the upstream `/api/v1/graph` endpoint omitted the timestamp. The dashboard proxy now injects `exported_at = server clock (UTC)` when upstream omits it, and the JS has a defensive `new Date().toISOString()` fallback for the same case. Verified live: `EXPORTED AT 2026-07-16 11:35:22`.
- **Settings tab — graph counts now show per-agent AND global.** Previously the Settings tab showed graph counts scoped to the current `agent_id` (e.g. `hermes-user / default`) which under-reported when other agents had data. Now both scopes are visible as separate rows: `(per agent)` and `(global)`. Helps reconcile discrepancies with the L5 tab and direct `/api/v1/graph` queries.
- **3-tier status (`vdb` / `embed` / `llm`).** Was binary ok/degraded, now distinguishes which subsystem is broken. `llm` issues (rate limit, missing key) no longer flip the whole system to "degraded" — they're warnings, since the LLM only affects new extraction/digest writes; the already-persisted memory graph remains readable.
- **Authoritative layer counts.** The Memory Composition bar was reading stale Qdrant. Now it reads live data, so the bar matches what `/api/v1/list` actually returns.

### Memory pipeline

- **`include_raw` flag.** The `/api/v1/list` endpoint now accepts `include_raw: true` to return the original L1_RAW payload alongside the processed L2 fact. Powers the "Today / Activity" tab's timeline and the VDB scroll path.
- **System2 digest — batched execution + token cap.** `run_system2_agent_batched` now splits large L2-fact sets into clusters of 8 facts per batch and caps the per-call LLM output to 1024 tokens. Mitigates `finish_reason=length` truncation from the `tencent/hy3:free` model when reasoning eats the budget. Cluster splitting prevents digest retries.
- **LLM `extra_body` propagation.** `MEMORY_LLM_EXTRA_BODY` env var is now parsed by `config.py` so standalone probe scripts inherit the same `reasoning_effort: none, include_reasoning: false` settings as the server. Fixes digest smoke tests diverging from server behavior.
- **zvec `update_payload` schema fix.** `vector_store_zvec.py` now fetches the embedding before calling `update_payload` to satisfy zvec's schema requirements. Fixes silent `update_payload` failures during digest.
- **Writer — persist failure now surfaces.** `writer.py` no longer marks writes as `success=True` when `vector_store.upsert()` fails. Returns `[PERSIST_FAILED]` error code so callers can detect lost writes.

### Linting

- **Resolved 4 pre-existing ruff errors blocking CI.** Errors were introduced by recent main commits (after the last successful CI on 2026-07-12) and would have failed the next CI run regardless of feature branch. Fixes: `console.py` SIM105 (try/except/pass → `contextlib.suppress`), W292 (trailing newline), `dashboard.py` I001 (import sort), F401 (unused `import pathlib`).

### Launch / process management

- **Console window rewrite.** Old `console.py` cleared the screen every 2s and used `stdout=PIPE` which caused the child Python to exit before the window rendered → "empty PowerShell" flash. Rewrote to incremental in-place updates with no full-screen clear, no pipe redirection, and a `wmic` singleton guard so `hyatlas start` doesn't pile up windows. Auto-launched only on `--detach`.
- **Hyatlas launcher — PID-based directory lock.** `run_hyatlas_digest.py` and related launchers use a PID-based lock with `kernel32.GetExitCodeProcess` liveness check (Windows) so stale locks from crashed processes don't block new runs.

### Repo hygiene

- **Privacy scrub complete.** All `<user>`, `<discord_user_id>`, real name, email, and Windows paths replaced with placeholders across source, docs, and `pyproject.toml`. Local memory data (L5 graph) still contains historical references; this is expected (data tier is local-only, never pushed).
- **Profile isolation plumbing.** Specialist profile names (`default`, `research`, `sentinel`, `work-backend`, `work-frontend`, `trading`, `hestia`) are recognized by the dashboard dropdown, but most profiles are empty (no data ever written to those `agent_id`s). Profile isolation itself works (the `agent_id` filter on `/api/v1/list` is enforced); the gap is that data is concentrated in `default` and `trading`.

### Documentation

- Added `docs/DEBRIEF_TUNA_OS_USEFULNESS.md` — debrief of the Tuna Agent OS scaffolding usefulness on the profile-isolation work.
- Added `docs/PROFILE_MEMORY_ARCHITECTURE.md` — design doc for profile-based memory isolation across the HyAtlas stack.

---

## Upgrade notes

**From v3.3.2 → v3.4.0 is a non-event.** No breaking changes, no schema migration, no config changes required.

```bash
pip install -U hyatlas-memory
hyatlas setup hermes    # refreshes the plugin
hyatlas start          # restart the stack
```

**If you're using the dashboard**, refresh your browser tab — the new profile dropdown and 3-tier status appear automatically.

**If you have a stale `build/` or `build.stale_*/` directory**, you can safely delete it. The new `.gitignore` excludes it.

**If you previously hit the "empty PowerShell flash"** when running `hyatlas start`, that should now be gone. If you still see it, file an issue with the output of `hyatlas status --verbose`.

---

## Verification

What I ran before tagging `v3.4.0`:

| Check | Result |
|---|---|
| `ruff check --no-cache src/ tests/` | All checks passed |
| `pytest -v -m "not integration" --ignore=tests/test_integration.py` | 54 passed, 12 deselected |
| `node -c src/hyatlas_memory/server/dashboard/app.js` | exit 0 |
| `python -c "import ast; ast.parse(open('...dashboard.py').read())"` | parse OK |
| `hyatlas status` | server healthy, dashboard healthy, zvec active |
| Live `/api/v1/status` | ok/ok/ok/ok |
| Live `/api/v1/list` | total=207, layers + extracted field working |
| Privacy grep on tracked files | 0 hits |
| All 8 dashboard tabs | verified working in browser |

---

## Files changed (high level)

```
src/hyatlas_memory/core/config.py                          |  +18
src/hyatlas_memory/core/data/vector_store_zvec.py          |  +66 / -...
src/hyatlas_memory/core/pipelines/system2_agent.py         | +126
src/hyatlas_memory/core/pipelines/system2_writer.py        |  +18
src/hyatlas_memory/core/pipelines/writer.py                |   +7
src/hyatlas_memory/core/server.py                          |  +54
src/hyatlas_memory/integrations.py                         |  +47
src/hyatlas_memory/l5_inprocess.py                         |   +6
src/hyatlas_memory/patches.py                              |   +4
src/hyatlas_memory/process.py                              |  +49
src/hyatlas_memory/server/bin/*                            |  various
src/hyatlas_memory/server/dashboard/app.js                 |+198
src/hyatlas_memory/server/dashboard/dashboard.html         |  +21
src/hyatlas_memory/server/dashboard/dashboard.py           |+204
src/hyatlas_memory/server/dashboard/js/l5.js               |   +2
src/hyatlas_memory/server/dashboard/styles.css             |  +49
src/hyatlas_memory/vdb_dashboard.py                        |  +17
docs/DEBRIEF_TUNA_OS_USEFULNESS.md                         |  (new)
docs/PROFILE_MEMORY_ARCHITECTURE.md                        |  (new)
... + ~30 more doc/config/script files
```

---

## What didn't ship

A few things I noticed during the audit that are tracked but **not** part of v3.4.0:

- **Most specialist profiles are empty** (`research`, `sentinel`, `work-backend`, `work-frontend`, `hestia` have 0 memories). The plumbing works; the data isn't there. Future work: route writes from those profiles' agent_ids to populate them.
- **Zvec list enumeration quirk.** `ZvecVectorStore.list_by_user()` uses a zero-vector ANN query for now, which doesn't return diverse embeddings. Writes persist correctly; the list path is best-effort. Future work: switch to a proper query API when zvec ships it.

---

## Where to read more

- `CHANGELOG.md` — same content, in the project's existing changelog format
- `docs/PROFILE_MEMORY_ARCHITECTURE.md` — design rationale for the profile-isolation feature
- `docs/DEBRIEF_TUNA_OS_USEFULNESS.md` — debrief of the scaffolding work
- `docs/LAYERS.md`, `docs/API.md`, `docs/SERVER.md`, `docs/TROUBLESHOOTING.md` — updated with new sections for the `include_raw` flag, `extracted` field, 3-tier status, and "I wrote but it didn't show" troubleshooting

---

**Tagged:** `v3.4.0`  •  **Released:** 2026-07-16  •  **Branch:** `main`

---

![v3.4.0 banner: Profile isolation in the dashboard + L1_RAW transparency](./assets/hyatlas-v3.4.0-banner.jpeg)
