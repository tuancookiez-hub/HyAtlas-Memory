# HyAtlas Profile-Memory Architecture

**Created:** 2026-07-13  
**Status:** Planning — not yet implemented  
**Author:** Hermes (default/orchestrator) for <user>

---

## 1. Background

### Hindsight's bank model (reference)

Hindsight uses `bank_id_template` to create separate memory banks per Hermes profile:

```json
{ "bank_id_template": "hermes-{profile}" }
```

| Profile | Bank ID |
|---------|---------|
| `default` | `hermes-default` |
| `work-coder` | `hermes-work-coder` |
| `sentinel` | `hermes-sentinel` |

Each bank is a fully isolated memory store (PostgreSQL-backed, separate facts, separate knowledge graph).

### Why we don't copy Hindsight's banks

HyAtlas already has a better primitive: **two-level isolation via `user_id` + `agent_id`**.

```text
user_id  = one human identity (hermes-user)
agent_id = one profile / specialist namespace
```

Adding a separate bank system would duplicate what `agent_id` already provides. Instead, we use `agent_id` as the profile isolation key — same concept, fewer moving parts, no second storage system.

---

## 2. Current HyAtlas state (as of 2026-07-13)

### Operational health

| Component | Status |
|-----------|--------|
| Backend (`:19527`) | Up, `status: ok` |
| Dashboard (`:8765`) | Up, `status: ok` |
| VDB (zvec) | `ok`, 5850 total memories |
| Embedder (local BGE) | `ok`, 1024d |
| LLM (OpenRouter `tencent/hy3:free`) | `ok`, reasoning off |
| Write pipeline | `ok` |
| HYATLAS_HOME | `D:\HyAtlas\.hyatlas` |
| Mode | `ultra` |
| Vector store | zvec (Qdrant retired) |

### Graph layers

| Layer | Count |
|-------|-------|
| L0 basic_info | 10 |
| L1 raw | 781 |
| L2 fact | 1691 |
| L3 summary | 309 |
| L4 identity | 665 (retired, legacy) |
| L5 knowledge | 1637 |
| L6 schema | 569 |
| L7 intention | 188 |
| **Total** | **5850** |
| Relations | 8240 |

### Memory namespaces in use today

| agent_id | Memory count |
|----------|-------------|
| `default` | **1745** (main bank — everything goes here) |
| `default_agent` | **106** (legacy namespace from old bug) |
| `research` | 0 |
| `sentinel` | 0 |
| `work-backend` | 0 |
| `work-frontend` | 0 |
| `trading` | 0 |
| `hestia` | 0 |

**Problem:** All Hermes profiles write into `hermes-user / default`. Profile isolation does not exist in practice — only in the API.

### Cron jobs

| Job | Schedule | Status |
|-----|----------|--------|
| `smart-memory-prune` | every 240m | enabled |
| `HyAtlas weekly digest` | every 10080m (7d) | enabled |

### Quality metrics (last 7d window)

| Metric | Value |
|--------|-------|
| Fresh L2 for digest | 125 |
| Sys1 writes (7d) | 3 |
| Sys2 digests (7d) | 0 |
| LLM tokens (7d) | 447,520 |
| Composite score | 50/100 |
| Last digest | ~4.4 days ago (2026-07-08) |

### Known debt / unfinished items

1. **One shared memory soup** — provider hardcodes `agent_id = default`
2. **Digest lag** — 125 fresh L2 facts not yet consolidated; last digest 4.4 days ago
3. **Legacy namespace** — `default_agent` has 106 leftover memories from old agent_id mismatch bug
4. **Housekeeping dirs** — legacy Qdrant data dirs and `~/.hy_memory` still on disk (unused, ~5 GB)
5. **`NOW.md` stale** — still references MiniMax-era numbers and D-migration state
6. **Embedder dependency fragility** — `huggingface-hub` has drifted twice; no pin enforced
7. **No profile memory contract** — no documented agreement on how profiles map to `agent_id`

---

## 3. Proposed architecture: profile-scoped `agent_id` memory

### Core principle

```text
one human:    user_id  = hermes-user
one stack:    HyAtlas on D:\HyAtlas\.hyatlas
one graph:    shared Kuzu + zvec runtime
isolation:    agent_id = <active Hermes profile name>
```

### Profile → agent_id mapping

| Hermes profile | HyAtlas agent_id | Memory scope |
|----------------|-----------------|--------------|
| default | `default` | Own + shared/common (orchestrator sees broad) |
| research | `research` | Strict own only |
| sentinel | `sentinel` | Strict own only |
| work-backend | `work-backend` | Strict own only |
| work-frontend | `work-frontend` | Strict own only |
| trading | `trading` | Strict own only |
| hestia | `hestia` | Strict own only |

### Retrieval policy

| Role | What they can read |
|------|--------------------|
| Specialists (research, sentinel, work-*, trading, hestia) | Only their own `agent_id` memories |
| Default / orchestrator | Own `default` + optional `shared`/`common` agent_id |
| Cross-profile facts (user identity, global prefs) | Written to `default` or a dedicated `shared` namespace |

**Not every profile sees everything.** That is intentional — prevents specialist memory contamination.

### Write path change

Current Hermes provider (`hyatlas_memory/__init__.py`):

```python
self._user_id = "hermes-user"     # hardcoded
self._agent_id = "default"        # hardcoded
```

Proposed:

```python
self._user_id = "hermes-user"              # stays — one human
self._agent_id = active_profile_name       # from Hermes session context
```

The provider should receive the active profile name from Hermes and pass it as `agent_id` on every `add()` and `search()` call.

### Read path change

Current: `search(user_ids=[self._user_id], agent_ids=[self._agent_id])`

Proposed for specialists: same — strict own `agent_id`.

Proposed for default/orchestrator: `agent_ids=[self._agent_id, "shared"]` — own + shared common pool.

### Migration strategy

**Do NOT re-split all historical memories into profiles.**

- Leave the current big bank as `hermes-user / default` (historical shared memory)
- Start clean namespaces for new specialist writes
- Optional later: curated migration of specific topics (frontend resources, research findings) into their profile namespaces
- `default_agent` legacy 106 memories: decide to merge into `default` or leave inert

### Digest changes

- Weekly digest should run per active `agent_id` that has enough fresh L2
- Minimum: digest `default` + any profile with >20 fresh L2 facts
- Digest script (`run_digest_once.py`) already accepts `agent_id` as second arg — no structural change needed, just scheduling

---

## 4. Readiness checklist — do these BEFORE implementing profile memory

These are prerequisites. Do not skip them.

### 4.1 Run one digest now
- Clears the 125 fresh L2 backlog
- Proves `hy3:free` drives System2 after model switch
- Updates graph layers before we start splitting namespaces
- Command: `python %LOCALAPPDATA%\hermes\scripts\run_hyatlas_digest.py`

### 4.2 Freeze the identity contract
- Document and commit to:
  ```text
  user_id  = hermes-user     # one human, all platforms
  agent_id = <active profile>  # per Hermes profile
  ```
- No more `default_agent` namespace — clean up or leave inert
- This contract goes into `NOW.md` and this doc

### 4.3 Decide retrieval policy
- Confirm: specialists strict-own, default = own + shared
- Confirm: no "every profile sees everything"
- Confirm: cross-profile facts (identity, global prefs) live in `default` or `shared`
- Write the decision down (this doc, section 3)

### 4.4 Leave historical bank alone
- Do NOT migrate or re-split old memories yet
- New specialist writes start clean
- Old `default` bank stays as-is (historical shared memory)
- `default_agent` 106 memories: decide later (merge or leave inert)

### 4.5 Fix `NOW.md`
- Update to reflect: OpenRouter hy3:free, reasoning off, current graph numbers, D: home, profile-memory as next step
- Remove stale MiniMax / D-migration references

### 4.6 Pin embedder dependency
- Pin `huggingface-hub>=1.5.0,<2.0` in HyAtlas `pyproject.toml` or `requirements.txt`
- This has drifted twice and broken the embedder both times
- Prevents future silent breakage

### 4.7 (Optional) Clean leftover Qdrant dirs
- legacy Qdrant data dirs (~5 GB total), `~/.hy_memory` (~29 KB), all unused
- These are unused legacy. Safe to delete after confirming no live process reads them.
- Not blocking, but frees ~5 GB on C:

---

## 5. Implementation plan (after readiness pass)

### Phase 1 — Provider write-path change
- Modify `HyMemoryProvider.initialize()` to accept active profile name
- Set `self._agent_id = profile_name` instead of hardcoded `"default"`
- All `add()` calls inherit the active profile's `agent_id`

### Phase 2 — Provider read-path change
- Specialists: `search(agent_ids=[self._agent_id])` — strict own
- Default: `search(agent_ids=[self._agent_id, "shared"])` — own + shared
- Optional: create `shared` agent_id for true cross-profile facts

### Phase 3 — Digest scheduling
- Update weekly digest cron to iterate over active profiles with fresh L2
- Or: run separate digest per profile on a staggered schedule

### Phase 4 — Dashboard update
- Dashboard should show per-agent_id memory counts
- Layer counts filtered by agent_id
- Quality metrics per profile (or at least per default)

### Phase 5 — (Optional) Curated migration
- Selectively move topic-specific memories from `default` to profile namespaces
- e.g., frontend resources → `work-frontend`, research findings → `research`
- LLM-assisted classification, not manual
- Only after write/read paths are proven correct

---

## 6. What NOT to do

- ❌ Clone Hindsight's bank system — `agent_id` already does this
- ❌ Rewrite all historical memories into every profile
- ❌ Multi-home / multi-DB split — one stack, one graph
- ❌ Giant migration before write-path is correct
- ❌ More dashboard polish as a substitute for architecture
- ❌ Start coding before readiness checklist is done
- ❌ Let `huggingface-hub` drift again without a pin

---

## 7. Comparison: Hindsight banks vs HyAtlas agent_id

| Feature | Hindsight banks | HyAtlas agent_id |
|---------|----------------|-----------------|
| Isolation key | `bank_id = hermes-{profile}` | `agent_id = {profile}` |
| Storage | Separate PostgreSQL schemas | Same zvec + Kuzu, filtered by agent_id |
| Per-profile LLM | ✅ per-profile `llm_model` config | ✅ possible via per-profile `hy_memory.json` |
| Knowledge graph | ✅ per-bank | ✅ shared graph, nodes tagged by agent_id |
| Migration complexity | High (separate stores) | Low (same store, filter by field) |
| Cross-profile recall | Hard (separate banks) | Easy (query multiple agent_ids) |
| Shared human identity | Separate per bank | Unified `user_id = hermes-user` |
| Setup complexity | Medium (PostgreSQL) | Low (already running) |

**Verdict:** HyAtlas `agent_id` is the better fit. Same isolation, less infrastructure, unified human identity, easier cross-profile recall when needed.

---

## 8. How memory capture flows from dispatched profiles

### The plumbing already exists in Hermes core

Hermes' `agent_init.py` (line 1416-1420) already resolves the active profile name and passes it to the memory provider:

```python
# agent_init.py — Profile identity for per-profile provider scoping
from hermes_cli.profiles import get_active_profile_name
_profile = get_active_profile_name()
_init_kwargs["agent_identity"] = _profile
```

`get_active_profile_name()` (in `hermes_cli/profiles.py`) infers the profile from `HERMES_HOME`:
- `~/.hermes/` → `"default"`
- `~/.hermes/profiles/research/` → `"research"`
- `~/.hermes/profiles/sentinel/` → `"sentinel"`

### HyAtlas provider already receives it

```python
# hyatlas_memory/__init__.py line 229
self._agent_id = kwargs.get("agent_identity", "") or "default"
```

So the full flow for `hermes -p research -z "..."`:

```text
hermes -p research -z "..."
  → HERMES_HOME = ~/.hermes/profiles/research/
  → get_active_profile_name() = "research"
  → agent_identity = "research"
  → HyMemoryProvider.initialize(agent_identity="research")
  → self._agent_id = "research"
  → all add() calls write to agent_id="research"
  → all search() calls query agent_id="research"
```

**The pipe is wired.** The `agent_id` is already auto-resolved from the profile name.

### What's missing: plugin installation in specialist profiles

None of the specialist profiles have the HyAtlas plugin or config:

| Profile | `plugins/hy_memory/__init__.py` | `hy_memory.json` |
|---------|--------------------------------|-----------------|
| default | ✅ YES | ✅ YES |
| research | ❌ NO | ❌ NO |
| sentinel | ❌ NO | ❌ NO |
| work-backend | ❌ NO | ❌ NO |
| work-frontend | ❌ NO | ❌ NO |
| trading | ❌ NO | ❌ NO |
| hestia | ❌ NO | ❌ NO |

Without the plugin shim, the memory provider never loads in specialist sessions. They have `memories/` (Hermes flat-file) but no HyAtlas connection.

### Fix: install plugin + config into each profile

**The HyAtlas server is shared** — one backend on `:19527`, one zvec store, one Kuzu graph. Profiles just need:

1. `plugins/hy_memory/__init__.py` — the thin shim that imports `HyMemoryProvider`
2. `hy_memory.json` — config pointing to the shared server (same LLM/embedder config, same `D:\HyAtlas\.hyatlas` home)

The `agent_id` is auto-resolved — no per-profile config needed for that.

The `hy_memory.json` in each profile can be identical to the default profile's config, because:
- `user_id` stays `hermes-user` (one human)
- `agent_id` is resolved at runtime from the profile name (not from config)
- LLM/embedder/server settings are shared

So the install is: copy 2 files into each profile's `HERMES_HOME`.

### What about `-z` (non-interactive / quick mode)?

`hermes -p research -z "task"` runs a full agent session under the research profile. The memory provider initializes with `agent_identity = "research"`. Memory capture works the same as interactive mode — `sync_turn` buffers conversation and writes to HyAtlas under `agent_id = "research"`.

The only caveat: if the session is very short (few turns), there may not be enough conversation for the memory provider's turn buffer to flush. The provider has a `sync_turn()` call that batches writes — check that it flushes on session end (it does via `shutdown_memory_provider()`).

---

## 9. Dashboard profile selector

### Concept

Add a dropdown / selector at the top of the HyAtlas dashboard that switches the view between profile memory namespaces.

```text
[Profile: default ▼]  →  [default] [research] [sentinel] [work-backend] [work-frontend] [trading] [hestia]
```

### Backend changes

The dashboard API already has endpoints that accept filtering. We need:

1. **`/api/layer-counts?agent_id=research`** — filter layer counts by agent_id
2. **`/api/memories?agent_id=research&limit=20`** — filter memory list by agent_id
3. **`/api/graph-counts?agent_id=research`** — filter graph counts by agent_id
4. **`/api/quality-metrics?agent_id=research`** — filter quality metrics by agent_id
5. **`/api/profiles`** (new) — list all agent_ids that have memories, with counts

The backend `/api/v1/list` already accepts `agent_id` as a parameter. The dashboard just needs to pass it through.

### Frontend changes

In `app.js`:
- Add a profile selector dropdown in the header
- All fetch calls append `&agent_id=<selected>` when a non-default profile is selected
- "All profiles" option shows aggregate counts (current behavior)
- Layer counts, memory list, graph, quality metrics all re-render on profile switch

In `dashboard.py`:
- Pass `agent_id` parameter from request to upstream `/api/v1/list` and `/api/v1/graph` calls
- Add `/api/profiles` endpoint that queries distinct agent_ids from the VDB

### Implementation order

1. `/api/profiles` endpoint (list agent_ids + counts) — so the dropdown has data
2. Pass `agent_id` filter through existing endpoints
3. Frontend dropdown + re-render on switch
4. Default view = `default` profile (or "all" aggregate)

### Mockup

```text
┌─────────────────────────────────────────────────┐
│  HyAtlas Memory Dashboard    [Profile: default ▼] │
│                                                  │
│  Overview | Memory Layers | Quality | System     │
│                                                  │
│  Total: 5,850  |  Relations: 8,240  |  Layers: 8 │
│  L5: 1637  |  L6: 569  |  L7: 188                │
│  ...                                              │
└──────────────────────────────────────────────────┘
```

When you switch to `research`:
```text
┌─────────────────────────────────────────────────┐
│  HyAtlas Memory Dashboard    [Profile: research ▼]│
│                                                  │
│  Total: 0  |  Relations: 0  |  Layers: 0         │
│  (empty — no memories yet for this profile)      │
└──────────────────────────────────────────────────┘
```

After research profile starts capturing:
```text
┌─────────────────────────────────────────────────┐
│  HyAtlas Memory Dashboard    [Profile: research ▼]│
│                                                  │
│  Total: 42  |  Relations: 5  |  Layers: 3        │
│  L1: 30  |  L2: 12  |  L5: 0  |  L6: 0           │
│  (fresh capture, not yet digested)               │
└──────────────────────────────────────────────────┘
```

---

## 10. Open questions for <user>

1. **`shared` namespace** — do you want a dedicated `shared` agent_id for cross-profile facts (user identity, global preferences), or should those just stay in `default`?
2. **`default_agent` cleanup** — merge the 106 legacy memories into `default`, or leave them inert?
3. **Per-profile LLM** — should each profile be able to use a different LLM for memory extraction (like Hindsight), or is one global LLM fine?
4. **Digest scheduling** — one digest that iterates all profiles, or separate staggered digests per profile?
5. **Dashboard** — do you want per-profile memory counts visible on the dashboard now, or after the write path is proven?
6. **Curated migration** — do you eventually want to move topic-specific memories from `default` to specialist namespaces, or leave history in `default` and only isolate going forward?

---

## 11. Decision log

| Date | Decision | Rationale |
|------|----------|-----------|
| 2026-07-13 | Use `agent_id` for profile isolation, not Hindsight banks | Already exists in HyAtlas, less infrastructure, unified human identity |
| 2026-07-13 | Specialists = strict own memory, default = own + shared | Prevents specialist contamination, orchestrator stays broad |
| 2026-07-13 | Do not re-split historical memories yet | Preserve existing bank, start clean namespaces, migrate curated later |
| 2026-07-13 | Run readiness checklist before implementation | Digest lag + identity contract + dependency pin must be done first |
