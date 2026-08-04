# HyAtlas v3.5 Clean-Floor Roadmap

**Status:** Planning source of truth  
**Created:** 2026-07-29  
**Target:** A genuinely clean, truthful, fully verified Python floor before any v4.0 Go work  
**Audit basis:** `C:/Users/tuanc/AppData/Local/hermes/checklists/default-hyatlas-tracks-abc-20260729.md`

## Goal

Make HyAtlas v3.5 a stable behavioral baseline where:

- every dashboard number has one named source of truth;
- memories, graph nodes, relations, activity events, and coding records are never silently mixed;
- every visible control is wired or removed;
- profile scope behaves consistently on every page;
- health and quality surfaces report uncertainty and degradation honestly;
- runtime paths, docs, and active skills match the live zvec + Kuzu architecture;
- the full write → extract → store → list → search → digest → graph → dashboard path is verified live.

This roadmap deliberately avoids broad Python-core churn. **Go remains reserved for v4.0 and comes after this floor is complete.**

---

## Rules for the roadmap

1. **Truth before polish.** Do not visually refine a page whose data contract is still wrong.
2. **One concern per commit.** Keep each fix reviewable and reversible.
3. **Tests before behavior changes.** Add a regression test that exposes each contract bug before fixing it.
4. **No silent fallback.** If a source is missing, show unavailable/degraded—not a plausible-looking substitute.
5. **No synthetic data presented as stored truth.** Inferred visualization may exist only when explicitly labeled.
6. **No push without review.** Keep commits local until Tuna reviews the completed phase.
7. **No Go research or implementation in v3.5.** The v3.5 work defines the contract that v4.0 may later reproduce.

---

# Phase 0 — Freeze the baseline and contracts

**Purpose:** Prevent moving targets while repairing the dashboard.

## 0.1 Capture the current baseline

- [ ] Save canonical live responses for:
  - `/api/v1/status`
  - `/api/v1/list`
  - `/api/v1/search`
  - `/api/v1/graph`
  - dashboard `/api/memories`
  - dashboard `/api/layer-counts`
  - dashboard `/api/graph-counts`
  - dashboard `/api/layer-health`
  - dashboard `/api/quality-metrics`
- [ ] Record global plus all seven profile scopes.
- [ ] Record test, lint, VDB, graph-node, and relation baselines (baseline snapshots were removed — git history and audit receipts are the durable record).
- [ ] Preserve the current browser screenshots as “before” evidence.

## 0.2 Define the five data classes

Document and enforce these separate concepts:

| Data class | Canonical source | Allowed uses |
|---|---|---|
| VDB memories | zvec | list, recent ingestion, today, search, last write |
| Graph nodes | Kuzu | knowledge graph, layer totals, graph visualization |
| Graph relations | Kuzu | stored connections only |
| Coding memories | current coding SQLite location, if present | coding journal only |
| Operational metrics | durable metrics/log source | health and quality only |

- [ ] Define response schemas for each class.
- [ ] Define global, user, agent, and session scope semantics.
- [ ] Define what “total,” “display total,” “recent,” “today,” and “last memory” mean.

## 0.3 Add contract tests before fixes

**Likely files:**
- `tests/test_dashboard_data_contracts.py` — new
- `tests/test_dashboard_profile_scope.py` — new
- `tests/test_dashboard_status_proxy.py`
- `tests/test_vdb_dashboard.py`

- [ ] Prove `/api/memories.total == actual filtered memory count`.
- [ ] Prove `/api/memories` contains no graph nodes.
- [ ] Prove each profile receives only its own data.
- [ ] Prove canonical layer totals reconcile.
- [ ] Prove unavailable sources remain unavailable instead of becoming zero/healthy.

### Phase 0 gate

- [ ] Contract document exists.
- [ ] Failing tests reproduce the current known defects.
- [ ] Baseline artifacts are saved.
- [ ] No runtime behavior has changed yet.

---

# Phase 1 — Separate the dashboard datasets

**Purpose:** Remove the root cause contaminating multiple pages.

## 1.1 Repair `/api/memories`

**Primary file:** `src/hyatlas_memory/server/dashboard/dashboard.py`

- [ ] Return VDB memory records only.
- [ ] Keep L1_RAW visible without adding Kuzu nodes.
- [ ] Make `total`, `offset`, `limit`, and returned rows obey one pagination contract.
- [ ] Remove graph payload extraction from the memory-list path.
- [ ] Remove stale Qdrant enrichment/fallback comments or isolate migration-only behavior explicitly.
- [ ] Ensure deduplication is scoped and deterministic.

## 1.2 Split frontend state

**Primary file:** `src/hyatlas_memory/server/dashboard/app.js`

Replace the generic `allMemories` model with explicit stores:

- [ ] `vdbMemories`
- [ ] `graphNodes`
- [ ] `graphRelations`
- [ ] `codingMemories`
- [ ] `layerCountsData`
- [ ] operational/quality data

- [ ] Remove graph-node normalization into a fake memory shape.
- [ ] Remove graph-node timestamps derived from `mention_count`.
- [ ] Add helpers that accept only the correct data class.

## 1.3 Repair Overview and Recent Ingestion

- [ ] Recent Ingestion uses actual VDB/coding create or update timestamps only.
- [ ] “All,” “VDB,” “Coding,” and “L1_RAW” tab counts reconcile with their lists.
- [ ] Graph derivations do not appear as ingestion.
- [ ] “Last memory” means latest real persisted memory write.
- [ ] Keep explicit labels for VDB points, graph nodes, graph relations, and display total.

## 1.4 Repair Today / Activity

- [ ] “Last 24 hours” uses real event timestamps only.
- [ ] “VDB Memories” excludes graph nodes by construction, not by fragile filters.
- [ ] “This Week” counts real activity, not graph inventory.
- [ ] Wire Export JSON or remove the button.
- [ ] Show per-filter counts.

### Phase 1 gate

- [ ] `/api/memories` body length and `total` reconcile.
- [ ] Overview has no unexplained 20/44/500/2407 discrepancies.
- [ ] Today and weekly activity contain no Kuzu-derived synthetic timestamps.
- [ ] Browser and API checks pass for global and every profile.

---

# Phase 2 — Make Memory Observatory canonical

**Purpose:** Turn the Observatory from a synthetic demo into a truthful memory visualization.

## 2.1 Choose and enforce the visualization contract

**Decision for v3.5:** stored truth is the default.

- [x] VDB memory nodes come from the VDB memory dataset.
- [x] L5–L7 nodes come from Kuzu.
- [x] Stored graph edges come from Kuzu relations.
- [x] Cross-store associations appear only if backed by a real persisted reference.
- [x] No inferred edge mode remains in the v3.5 stored-truth view.

## 2.2 Remove invented canonical relationships

**Primary file:** `src/hyatlas_memory/server/dashboard/js/observatory.js`

- [x] Remove keyword-generated edges from the default view.
- [x] Remove forced neighboring-layer structural edges from the default view.
- [x] Stop deriving counts from sampled node arrays.
- [x] Use `/api/layer-counts` for rail/legend counts.
- [x] Use Kuzu relation data for connection metrics and linked layers.

## 2.3 Bound rendering cost

- [x] Avoid O(n²) pairwise relationship generation.
- [x] Add deterministic sampling for large node sets.
- [x] Keep canonical totals visible even when only a subset is rendered.
- [x] Add clear “showing N of M” copy.
- [x] Verify performance at current graph size and at a larger generated fixture.

Verification receipt: deterministic selection of 500 records from a generated 20,000-record fixture completed in 5.6–8.1 ms; a live 500-node Observatory rebuild completed in 130.5 ms.

## 2.4 Repair Field Note semantics

- [x] Connections = full stored Kuzu connections for the selected node.
- [x] Linked layers = actual persisted cross-layer relationships available in the loaded canonical graph payload.
- [x] Timestamp = real stored timestamp or “Not available.”
- [x] Confidence/retrieval labels name the real source field.
- [x] Placeholder nodes remain explicitly visual-only and excluded from counts.

### Phase 2 gate

- [x] Observatory L5/L6/L7 counts equal `/api/layer-counts`.
- [x] Displayed edges equal a defined subset of Kuzu relations.
- [x] No synthetic relationship is presented as stored fact.
- [x] Current graph loads without browser errors or severe interaction lag.

---

# Phase 3 — Complete profile scope and user-facing controls

**Purpose:** Make every visible control real and predictable.

## 3.1 Centralize scope construction

**Files:**
- `src/hyatlas_memory/server/dashboard/app.js`
- `src/hyatlas_memory/server/dashboard/js/l5.js`
- dashboard route handlers in `dashboard.py`

- [x] Use one scoped request helper for every page.
- [x] Changing profile invalidates and refreshes page-specific caches.
- [x] Define “All profiles” as an explicit aggregate with `agent_id=all` / empty search agent filter.
- [x] Show current scope beside every scoped metric through the shared scope header/status.

## 3.2 Wire Explore Memory controls

- [x] Semantic/keyword/hybrid mode reaches the backend (`legacy`, `hybrid_tag`, `hybrid_v2`).
- [x] Layer filter is applied transparently after retrieval with populated layer choices.
- [x] Time filter forwards `created_after` and re-filters returned results.
- [x] Relevance/recent sort works.
- [x] Selected profile sends the correct `agent_ids`; all-profile search sends an empty list for all stored namespaces.
- [x] Search loading, no-result, and error states are visible.
- [x] Score title explains semantic/keyword/hybrid source.

## 3.3 Repair L5 Knowledge Graph scope and filters

**Primary file:** `src/hyatlas_memory/server/dashboard/js/l5.js`

- [x] Fetch scoped `/api/l5/graph`.
- [x] Clear/reload `l5State` on profile change.
- [x] Generate entity-type chips from the actual payload taxonomy.
- [x] Add relation-type filtering from `relation_type_distribution`.
- [x] Rename “Exported at” to “Loaded at.”
- [x] Replace the obsolete export-script failure message.
- [x] Show stored source and creation timestamp where available.

## 3.4 Repair profile summary counts

- [x] `/api/profiles` reuses the same canonical VDB + graph contract as `/api/layer-counts`.
- [x] Profile total is explicitly named `display_total`.
- [x] Default and specialist totals reconcile with direct scoped endpoints.

### Phase 3 gate

- [x] Every visible Explore/L5 control changes its result or request.
- [x] Every page updates correctly when switching among all seven profiles and global scope.
- [x] No stale global graph remains after selecting a specialist profile.
- [x] Automated profile-scope tests pass.

Verification receipt: API reconciliation passed for all seven profiles. Browser state-based matrix returned global **2766**, default **2383**, research **18**, sentinel **11**, work-backend **31**, work-frontend **4**, trading **63**, and hestia **15**, with specialist graph payloads containing only the selected agent ID. L5 `built_by` filtering changed visible relations from **3496** to **350**. Explore mode probes sent `legacy`, `hybrid_tag`, and `hybrid_v2`; all-profile search sent `agent_ids=[]`; recent sort returned newest-first.

---

# Phase 4 — Truthful health, quality, and failure behavior

**Purpose:** Stop reporting certainty that the system does not have.

## 4.1 Make health contracts explicit

- [x] Define dashboard-process liveness separately from memory-backend readiness.
- [x] `/api/health` probes required upstream readiness; `/api/live` is dashboard-process liveness.
- [x] Preserve `/api/status` warning/error payloads unchanged.
- [x] Test provider-limited, broken core component, backend-offline, and dashboard-only states.

## 4.2 Fix `fetchJSON()` and page resilience

**Primary file:** `app.js`

- [x] Reject non-2xx responses with structured errors.
- [x] Replace the monolithic all-or-nothing loader with core, operations, graph, and quality request groups.
- [x] Optional endpoint failure does not blank unrelated pages.
- [x] Preserve last known data only with a visible stale/degraded marker.
- [x] Search has explicit errors; domain-dependent pages show a visible last-known-data banner.

## 4.3 Redesign Quality Metrics evidence rules

**Primary file:** `dashboard.py::_build_quality_metrics`

- [x] “No samples” is `N/A`, never a perfect 100.
- [x] Composite score excludes unavailable dimensions and reports coverage.
- [x] Activity uses durable memory timestamps plus digest-log mtimes instead of restart-local counters.
- [x] Add digest-log age and a seven-day staleness threshold.
- [x] Label global infrastructure evidence separately from profile-scoped memory evidence.
- [x] Show capture time and named evidence sources.
- [x] Do not generate positive coaching copy when evidence is missing or stale.

## 4.4 Correct runtime storage reporting

- [x] Replace `~/.hy_memory/data` with `hy_home()`-resolved paths.
- [x] Locate the configured/current-home coding database or explicitly report “not configured.”
- [x] Report disk usage from the active HyAtlas home.
- [x] Remove hardcoded platform/base/port display values where runtime values exist.

### Phase 4 gate

- [x] Backend-offline cannot produce a green readiness result.
- [x] Missing metrics display as unavailable, not perfect or zero without qualification.
- [x] Disk usage reflects `D:/HyAtlas/.hyatlas`.
- [x] Quality score is reproducible from timestamped, named evidence.

Verification receipt: **38 passed, 3 skipped** for dashboard contracts; **100 passed, 14 skipped, 19 deselected** for the Python 3.11 non-integration suite. Ruff, Python compilation, and JavaScript syntax pass. A temporary dashboard on `:8766` live-verified `/api/live`, readiness, active-home storage, explicit coding `not_configured`, durable activity evidence, a **371.7-hour stale digest**, and `N/A` coaching suppression. Browser load completed with no JavaScript errors. The existing dashboard on `:8765` was intentionally not restarted without Tuna approval.

---

# Phase 5 — Tests and live dashboard verification

**Purpose:** Convert the repaired contracts into a durable floor.

## 5.1 Expand automated coverage

- [x] Dashboard route contract tests.
- [x] Global/profile scope matrix tests.
- [x] Memory-vs-graph separation tests.
- [x] Health degradation tests.
- [x] Quality missing/stale evidence tests.
- [x] Search-control request-shape tests.
- [x] Observatory canonical count/relation tests.
- [x] Legacy path regression tests.

## 5.2 Add browser-level smoke verification

For all eight pages:

- [x] Page opens.
- [x] No uncaught JavaScript errors on the completed shadow walkthrough.
- [x] Loading resolves without overlapping refreshes.
- [x] Counts match direct API probes.
- [x] Empty state is truthful.
- [x] Error state is visible.
- [x] Profile scope works.
- [x] Core interaction works.

## 5.3 Run the full project gates

- [x] `ruff check --no-cache src/ tests/`
- [x] targeted dashboard tests
- [x] full non-integration test suite
- [x] `python -m compileall`
- [x] JavaScript syntax check
- [x] live backend and shadow-dashboard probes
- [x] real browser verification

Do not sweep repository-wide formatting debt into a functional fix unless intentionally scheduled as its own isolated task.

### Phase 5 gate

- [x] Every previously failed Track A/B item has a regression test or live verification receipt.
- [x] Full suite is green.
- [x] Browser evidence exists for all eight pages.
- [x] Same-class profile-count and refresh-overlap discrepancies found during the walkthrough are fixed and regression-covered.

Verification receipt: deep-check checklist at `C:/Users/tuanc/AppData/Local/hermes/checklists/default-hyatlas-phase5-20260729.md`. Browser walkthrough covered all eight pages, a live query and no-result state, stored-relation filtering, research scope, and an injected optional-domain HTTP 503. The walkthrough found and fixed two additional bugs: specialist Overview cards using global `status.vdb_points`, and overlapping 30-second refreshes leaving the scope indicator in a false Loading state. Final Python 3.11 gate: **102 passed, 14 skipped, 19 deselected**. Ruff `--no-cache`, compileall, and JavaScript syntax pass. Canonical `:8765` remains untouched pending explicit restart approval.

---

# Phase 6 — Documentation and skill truth cleanup

**Purpose:** Make the operational knowledge match the repaired product.

## 6.1 Update current repository documentation

Current docs to reconcile:

- [x] `README.md`
- [x] `docs/API.md`
- [x] `docs/DASHBOARD.md`
- [x] `docs/architecture.md`
- [x] `docs/LAYERS.md`
- [x] `docs/SKILL.md`
- [x] `docs/TROUBLESHOOTING.md`
- [x] `NOW.md`
- [x] `handoff.md` (created for cold-start continuity)

Required corrections:

- [x] v3.5.x is the current Python floor (headers + SKILL version bumped; `requires-python>=3.10`).
- [x] zvec is the active VDB; Qdrant fenced as migration/archive history only.
- [x] L4 is retired; L5–L7 canonical in Kuzu (SKILL layer table rewritten; L7-in-Kuzu corrected).
- [x] Current runtime home and paths are correct (`D:\HyAtlas\.hyatlas`, dedicated venv).
- [x] Current digest command is correct (`run_digest_once.py hermes-user default` / launcher).
- [x] Provider/model examples labeled current vs historical (OpenRouter free-tier example flagged).
- [x] Mutable counts/test totals timestamped as snapshots (102 passed/14 skipped, 2026-07-29).

Do not rewrite historical changelogs, release notes, or archived migration documents as though they were current.

## 6.2 Clean active HyAtlas skills

Primary global skills:

- [x] `hyatlas-memory` (disabled, v1.3.0-era — staleness banner added; reconcile-or-delete flagged)
- [x] `hy-memory-layer-enablement` (frontmatter repaired + zvec store note; was showing `description: |`)
- [x] `hy-memory-stack-recovery` (audited — leads with v3.5.0 procedures, Qdrant fenced; no change)
- [x] `hyatlas-health-verification` (already current from Phase 5)
- [x] `hy-memory-layer-debugging` (audited — LEGACY banner present, diagnostics valid; no change)
- [x] `software-development/hy-memory` (L7-in-Kuzu corrected; was claiming L7 moved to Qdrant/VDB)
- [x] `prune-memory-to-hyatlas` (zvec collection corrected)

- [x] Remove active-looking Qdrant requirements (fenced as legacy across the fleet).
- [x] Fix legacy paths and obsolete commands (patches.py→integrations.py, L4-promote, l3_context).
- [x] Separate historical incident notes from current procedures (banners + store notes).
- [x] Run skill lint and repair broken references relevant to these skills (frontmatter YAML fixed).

## 6.3 Reconcile profile-local duplicates

**Requires explicit profile-hygiene scope before editing another profile.**

- [ ] Compare trading-profile copies against global skills.
- [ ] Decide update, redirect, archive, or remove per duplicate.
- [ ] Ensure no profile receives contradictory HyAtlas procedures.
- [ ] Record a Hestia/fleet receipt when this separate sweep is approved.

### Phase 6 gate

- [x] Current docs pass stale-version/model/path/command scans (zero active-sense stale claims; remaining v3.4.0 mentions are feature-introduction attributions).
- [x] Active global skills describe the actual v3.5 runtime (zvec + Kuzu, L4 retired, L7 in Kuzu).
- [x] Historical material remains clearly historical (banners, LEGACY tags, snapshot dates).
- [ ] Profile duplicate cleanup is completed or explicitly listed as an external blocker. → **External blocker:** Phase 6.3 (trading-profile copies) deferred pending explicit profile-hygiene approval.

Verification receipt (Phase 6): repo docs reconciled to v3.5.0 across README/DASHBOARD/API/LAYERS/SKILL/architecture/TROUBLESHOOTING; `handoff.md` created. Six global HyAtlas skills audited — three patched (`hy-memory` L7 fix, `prune-memory-to-hyatlas` zvec fix, `hy-memory-layer-enablement` frontmatter+store), one bannered (`hyatlas-memory` disabled/v1.3.0), two confirmed current (`stack-recovery`, `layer-debugging`). Stale-scan clean. No code changed, so the Phase 5 test gate (102 passed) still holds; no commit/push performed.

---

# Phase 7 — Final clean-floor certification

**Purpose:** Prove the entire product, not just isolated patches.

## 7.1 End-to-end memory lifecycle

Using tagged disposable records:

- [ ] Add raw memory.
- [ ] Verify L1 persistence.
- [ ] Verify extraction outcome and L2 persistence.
- [ ] List through API and dashboard.
- [ ] Semantic search and read-back.
- [ ] Run or wait for digest under a healthy provider.
- [ ] Verify graph effect where expected.
- [ ] Verify profile isolation.
- [ ] Delete all probe records.
- [ ] Confirm counts return exactly to baseline.

## 7.2 Failure-mode certification

- [ ] Provider limited/down.
- [ ] Embedder unavailable.
- [ ] VDB unavailable/locked.
- [ ] Kuzu unavailable.
- [ ] Backend offline while dashboard remains alive.
- [ ] Empty specialist profile.
- [ ] Large graph rendering.
- [ ] Service restart with persisted data and durable metrics.

## 7.3 Release-floor review

- [ ] Re-run Tracks A, B, and C from the original audit.
- [ ] Require 28/28 resolved, intentionally accepted, or removed from product scope.
- [ ] Review actual git diff and commit series.
- [ ] Update release notes and version only after deciding whether accumulated fixes warrant the next v3.5.x release.
- [ ] Keep changes local for Tuna’s review.
- [ ] Push/tag only with explicit approval.

### Final certification definition

The v3.5 clean floor is complete only when:

1. All eight dashboard pages use named canonical sources.
2. Every visible control works.
3. Global and seven profile scopes reconcile.
4. No graph node is mislabeled as an ingestion event or memory write.
5. No inferred relation is presented as stored truth.
6. Missing/stale evidence cannot produce false-green health or inflated quality.
7. Active paths, docs, skills, and commands match the live runtime.
8. Full automated and live verification passes.
9. Probe records are cleaned and counts return to baseline.
10. Tuna reviews the local commits before any push or release action.

---

# Recommended implementation batches

These batches are deliberately small enough for review but large enough to close one concern each.

| Batch | Scope | Depends on | Done when |
|---|---|---|---|
| A | Baseline + contracts + failing tests | — | Current defects reproduced deterministically |
| B | `/api/memories` contract | A | Memory endpoint contains memories only; totals reconcile |
| C | Frontend dataset separation | B | No generic mixed `allMemories` state remains |
| D | Overview + Today truth | C | Activity surfaces contain real events only |
| E | Observatory canonical graph | C | Counts and edges match canonical APIs |
| F | Profile and Explore wiring | B/C | All controls and scopes work |
| G | L5 page scope/taxonomy | F | Profile reload and real type filters work |
| H | Health + error resilience | A | No false-green or all-or-nothing refresh |
| I | Quality + durable evidence | H | No-sample/stale evidence handled honestly |
| J | Active runtime paths | B | Disk/coding reports use current home or explicit unavailable state |
| K | Full QA/browser matrix | B–J | Regression and live gates pass |
| L | Docs + global skills | K | Current operational knowledge matches product |
| M | Profile duplicate sweep | L + explicit approval | No profile-local instruction conflicts |
| N | Final certification | K–M | Clean-floor definition passes end to end |

---

# Immediate next todo list

The next execution session should start with **Batch A only**:

- [ ] Create the canonical data-contract document.
- [ ] Save current API/browser baseline artifacts.
- [ ] Add failing `/api/memories` contract tests.
- [ ] Add failing profile-scope tests for Explore and L5.
- [ ] Add failing false-green health test.
- [ ] Add failing no-sample quality-score test.
- [ ] Present the failing-test receipt and exact Batch B patch plan before implementation continues.

**Out of scope for the immediate next batch:** dashboard redesign, Go, formatting sweep, profile edits, provider changes, push, tag, or release.
