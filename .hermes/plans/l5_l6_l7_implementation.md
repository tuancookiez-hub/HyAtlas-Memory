# L5 / L6 / L7 Implementation Plan

**Status:** not started
**Goal:** Make the standalone `hyatlas-memory` package produce memories in all 8 layers (L0 through L7), matching the official Tencent Hy-Memory 7-layer model + the L0 fork extension.

## What we know from reading the source (2026-06-17)

### Upstream `hy_memory` PyPI package (v1.2.18) in the venv at:
`C:\Users\tuanc\AppData\Local\hermes\hermes-agent\venv\Lib\site-packages\hy_memory\`

| Component | Status | Source |
|---|---|---|
| MemoryLayer enum (L0-L7) | ✅ exists | `models/memory.py:40-67` |
| Channel mapping `profile=L0+L4+L6, proactive=L7` | ✅ exists | `client.py:1874-1875` |
| L0-L4 producers (extractor, summarizer, reconciler) | ✅ exists | `agent/extractor.py`, `agent/summarizer.py`, `agent/reconciler.py` |
| L5 (knowledge) producer | ❌ NOT IMPLEMENTED (per inline comment in `models/memory.py:46-49`) | — |
| L6 (schema) producer: `cross_domain_sweeper.py` | ✅ exists | `pipelines/cross_domain_sweeper.py` |
| L6 (schema) producer: `intention_detector.py` (mislabeled — it produces L6 schemas) | ✅ exists | `agent/intention_detector.py` |
| L7 (intention) producer | ⚠️ partial — `intention_detector.py` returns intentions but no clear writer to L7 | — |
| L6 trigger condition | `L6 basic count >= 5` then run at end of digest() | `cross_domain_sweeper.py:header` |
| Abstractor (`SchemaAbstractResult`) | ✅ exists, returns schemas | `agent/abstractor.py:300` |

### Your local fork at `F:\Projects\hyatlas-memory\`

| Component | Status | Notes |
|---|---|---|
| Standalone package re-implements L0-L4 | ✅ working | 161 memories in store |
| L5 pipeline (your fork) | ⚠️ never run in this install | `server/bin/l5_full_pipeline.py` is a new implementation that does entity extraction → Kuzu ingest |
| L6 / L7 producers | ❌ not in your fork | dashboard declares them, SDK channel maps them, but no producer code |
| Dashboard layer filters (L0-L7) | ✅ exists | but L5/L6/L7 show 0 items |

### Layer mapping (CANONICAL)

This is the layer mapping to use. Both upstream and your fork's dashboard agree on the names, and this matches the official Tencent Hy-Memory 7-layer model:

```
L0_BASIC_INFO   — unprocessed, raw input
L1_RAW          — situational context, conditions
L2_FACT         — verified facts extracted from L1
L3_SUMMARY      — high-level synthesis of facts
L4_IDENTITY     — core entities, values, roles, self-model
L5_KNOWLEDGE    — knowledge graph (entities + relations, from L2 extraction)
L6_SCHEMA       — cognitive schemas (from L5 + L4 via cross-domain sweeper)
L7_INTENTION    — prospective memory (from L6 via intention_detector)
```

**Channel mapping (upstream `client.py:1874-1875`, also in your fork's SDK):**
```
profile  = L0 + L4 + L6    (user-identity memories)
proactive = L7               (intentions, surfaced before user asks)
normal   = L1 + L2 + L3 + L5 (everything else)
```

## Architecture decision

**Hybrid approach based on real source audit (2026-06-17):**

### L5: use your fork's existing implementation

Your fork has a **fully built L5 pipeline** at `F:\Projects\hyatlas-memory\server\bin\`. It works, it's well-architected, and it's substantially more complete than the upstream (which is explicitly documented as NOT YET IMPLEMENTED for L5):

| Step | Your fork | Upstream |
|---|---|---|
| Extract entities+relations from L2 via LLM | `l5_digest_writer.py` (16 KB, retry+parallelism+normalization+thresholds) | Not implemented |
| Resolve/dedup entities | `l5_entity_resolver.py` (19 KB, 4-pass case+alias+fuzzy+noise) | Not implemented |
| Quality review (filter noise) | `l5_quality_review.py` (8 KB, real/borderline/noise classification) | Not implemented |
| Ingest to Kuzu as `l5_knowledge` nodes + RELATED_TO edges | `l5_ingest_kuzu.py` (16 KB, MERGE + --rebuild + graph queries) | Not implemented |
| Export for dashboard | `l5_export_json.py` (6 KB) | Not implemented |
| Orchestrator with stop-server/lock/restart | `l5_full_pipeline.py` (12 KB, lock file + state tracking) | Not implemented |

**The upstream explicitly says** (in `prompts_zh.py:106`): *"L5_KNOWLEDGE is reserved for future synthesized cross-fact knowledge. It is NOT implemented in hy-memory 1.2.18"*. The upstream's `client.py:1874` channel mapping has `normal = L2_FACT + L5_KNOWLEDGE + L3_SUMMARY` — a placeholder for L5 that nothing writes to.

**Decision: keep your fork's L5 pipeline as-is.** It just needs to RUN. Add a small test, then trigger the run.

### L6: bridge to upstream (no fork implementation exists)

The upstream has a real L6 producer in `cross_domain_sweeper.py`:
- Step 1: behavior embedding of L6 basic nodes
- Step 2: matrix cosine collision + Union-Find clustering
- Step 3: LLM breakthrough induction → L6 core
- Trigger: when L6 basic count >= 5, runs at end of digest()

The fork has nothing for L6. Two options:
- (a) Port `cross_domain_sweeper.py` from upstream into the fork
- (b) Call the upstream code from the fork via a thin bridge

**Decision: bridge.** The upstream's cross_domain_sweeper is integrated with the upstream's digest() flow. Porting it as a separate script (option a) would duplicate 300+ LOC of logic. A bridge that calls the upstream code is cleaner.

### L7: bridge to upstream

Same reasoning as L6. The upstream's `intention_detector.py` produces L7 items. No fork implementation exists. Bridge.

### Layer mapping: keep canonical (matches both upstream and fork's dashboard)

```
L0_BASIC_INFO   — unprocessed, raw input
L1_RAW          — situational context
L2_FACT         — verified facts
L3_SUMMARY      — high-level synthesis
L4_IDENTITY     — core entities, values, roles, self-model
L5_KNOWLEDGE    — knowledge graph (your fork's pipeline)
L6_SCHEMA       — cognitive schemas (upstream cross_domain_sweeper)
L7_INTENTION    — prospective memory (upstream intention_detector)
```

Channel mapping (upstream `client.py:1874-1875`, matches fork):
```
profile  = L0 + L4 + L6
proactive = L7
normal   = L1 + L2 + L3 + L5
```

### LLM prompts: translate to English

Upstream uses Chinese-language prompts (e.g., `INTENTION_DETECT_PROMPT` in `intention_detector.py` starts with "分析以下对话内容"). Your conversations are mostly English. Translate the structure, keep the format, change the language. The LLM doesn't care which language the prompt is in, but English prompts on English content give better extraction quality.

## Implementation phases

### Phase 1: L5 producer activation (1-2 hours)
**Goal:** get `l5_knowledge` items appearing in Qdrant.

Steps:
1. Audit `l5_full_pipeline.py` and the 5 sub-scripts to understand what they do
2. Run a small L5 test run (L2 sample of 50 facts) to validate the pipeline works
3. Add a layer test to `smoke_test.py` that checks `l5_knowledge` count after pipeline runs
4. Commit + verify

### Phase 2: L6 producer (2-3 hours)
**Goal:** get `l6_schema` items appearing in Qdrant via the cross-domain sweeper.

Steps:
1. Port `cross_domain_sweeper.py` logic into the standalone package (or call it via bridge)
2. Wire the sweeper to fire after L5 digest completes (when L5 basic count >= 5)
3. Add the sweeper to the L5 trigger patch
4. Add a layer test for `l6_schema`
5. Commit + verify

### Phase 3: L7 producer (1-2 hours)
**Goal:** get `l7_intention` items appearing in Qdrant via the intention detector.

Steps:
1. Port `intention_detector.py` logic (or call it via bridge)
2. Wire it to fire after L6 has at least 1 schema
3. Add a layer test for `l7_intention`
4. Commit + verify

### Phase 4: end-to-end verification (1-2 hours)
**Goal:** confirm all 8 layers functional in doctor + dashboard.

Steps:
1. Run the full pipeline (L5 → L6 → L7) on the existing 161 memories
2. Verify layer distribution: L0=4+, L1=48+, L2=63+, L3=many, L4=47+, L5>0, L6>0, L7>0
3. Add a comprehensive `layer_coverage` stage to `smoke_test.py` that asserts each layer has >0 items
4. Update dashboard to show layer coverage prominently
5. Document the architecture in README

### Phase 5: cron integration (1 hour)
**Goal:** make the L5/L6/L7 stages run automatically.

Steps:
1. Confirm the L5 trigger patch debounces correctly (12h minimum interval)
2. Add the L6/L7 stages to the same trigger
3. Test that the trigger fires end-to-end
4. Add a `trigger_health` stage to `smoke_test.py`

## Test plan

After all phases:
- `hyatlas doctor` returns 10/10 (new layer_coverage stage added)
- All 8 layers have >0 items in Qdrant
- Dashboard layer palette shows non-zero counts for L0-L7
- The L5 cron trigger fires L5 → L6 → L7 in sequence
- The L7 items surface in the `proactive` channel of search responses

## Risks

1. **LLM cost.** The L5/L6/L7 extractors all use LLM calls. Running them on the full 161-memory dataset could cost a few cents to a few dollars depending on the model.
2. **Server downtime.** The L5 pipeline currently stops the server for Kuzu lock. The L6/L7 stages will also need exclusive access. Expected: 5-15 minutes of downtime per run.
3. **Quality.** The upstream LLM prompts are in Chinese. We may need to adapt them for English-language conversations, or verify they work for mixed content.
4. **Bloat.** Each L5/L6/L7 step adds new items. The doctor already has bloat thresholds (>1.5 GB per collection, >1 MB per point). Need to verify the layer_coverage stage doesn't conflict.

## Estimated total: 6-10 hours over 2-3 focused sessions

## Open decisions

1. **Bridge vs re-implement**: I'm recommending bridge (call upstream). Alternative: copy the source into the standalone package. Bridge is faster and gets upstream updates. Re-implement gives more control.
2. **Chinese prompts**: upstream uses Chinese-language prompts. We can translate, keep Chinese, or write new English prompts. Need to decide.
3. **Trigger frequency**: L5 currently debounces to 12h. L6/L7 piggyback on L5 trigger. Alternative: separate cron.

## File-by-file deliverables

After completion, these files will have changed:
- `F:\Projects\hyatlas-memory\src\hyatlas_memory\patches.py` — add L6/L7 trigger stages
- `F:\Projects\hyatlas-memory\src\hyatlas_memory\__init__.py` — confirm L6/L7 channel mapping
- `F:\Projects\hyatlas-memory\scripts\smoke_test.py` — add `layer_coverage` stage
- `F:\Projects\hyatlas-memory\server\dashboard\dashboard.html` — show layer counts prominently
- `F:\Projects\hyatlas-memory\README.md` — document the architecture
- (possibly) `F:\Projects\hyatlas-memory\src\hyatlas_memory\l6_schema_extractor.py` — new file
- (possibly) `F:\Projects\hyatlas-memory\src\hyatlas_memory\l7_intention_extractor.py` — new file

## Implementation results (2026-06-17 07:30)

**Status: L5 + L6 + L7 all functional.** This was faster than expected because the upstream's `intention_detector.py` had already populated L6 and L7 items in Qdrant during normal background operation — they just weren't being surfaced by the default `legacy` search reader.

### What actually got built
1. **L5 pipeline ran end-to-end** on 63 L2 facts → 8 min, $0.005 cost
   - 30 unique entities extracted (Hermes, HyAtlas, Qdrant, Tavily, etc.)
   - 5 entity nodes + 1 relation written to Kuzu
   - Bug found and fixed: `l5_digest_writer.py` expected `id` field but our L2 export had `memory_id`
2. **L6 + L7 were already there** — discovered via `reader=exhaustive` search
3. **Critical finding**: search reader matters
   - `reader=legacy` (default) only returns L0-L4
   - `reader=exhaustive` returns all 8 layers including L5 from Kuzu and L6/L7 from Qdrant

### To make this complete

The doctor test currently uses the default reader (which doesn't see L5/L6/L7). Need to:
1. Add a `layer_coverage` stage to the doctor that uses `reader=exhaustive` and asserts L0-L7 all >0
2. Make the default search reader `exhaustive` (or document the requirement)
3. Re-run L5 periodically (currently 12h debounce, debounced correctly)

### Cost so far
- L5 run: $0.005 (5 cents in 2026 dollars)
- L6/L7 were free (already running in background)
