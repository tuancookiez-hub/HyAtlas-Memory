# HyAtlas-Memory Architecture

> **Scope note:** This document describes the local architecture of this **community implementation** of the [official Hy-Memory framework](https://memory.hunyuan.tencent.com) (by Tencent Hunyuan). The canonical 6-layer model and the three operating modes (Lite / Pro / Ultra) are defined on the official page. This implementation extends the official 6-layer spec with an **experimental 7th layer (L7 = intention)** for proactive intent detection; L7 is **not** part of the official framework and is documented here only for the benefit of contributors working on this code.

This document explains the system design, layer semantics, and the
verified-2026-06-16 implementation choices that aren't visible from
the README.

## Layer mapping: this impl vs. the official spec

| This impl | Official (memory.hunyuan.tencent.com) | Purpose |
|-----------|---------------------------------------|---------|
| L1 raw | **L1 原始痕迹** | Verbatim session entries, time-ordered |
| L2 fact | **L2 原子事实** | Atomic facts extracted by LLM |
| L3 summary | (folded into L2 in official) | Periodic L2 rollups |
| L4 identity | **L3 身份画像** | Long-lived user/agent identity facts |
| L5 pipeline | **L4 心智** | Async ingest into Kuzu graph |
| L6 schema | **L5 模式** | Typed entity/relationship schema |
| L7 intention | **L6 意图** (proactive) | Proactive intent detection, async tasks |
| — | — | **L7 in this impl = experimental extension, not in official spec** |

The official 6-layer spec maps roughly onto our 6 layers L1-L6 with one difference: official L2 (原子事实) includes what we call L3 (summary); we keep them separate for query ergonomics. Our L7 (intention) is the experimental extension.

## The 7 layers in detail (this implementation)

### L1 — Raw
- **What**: Verbatim user/agent message text, time-ordered.
- **Where**: `~/.hy_memory/data/l1_raw.jsonl` (one JSON per line).
- **Why raw**: capture the exact words; downstream layers do interpretation.
- **Dedup**: patch #6 + #7 — exact-duplicate detector uses the embedder
  to compute cosine similarity; if `sim > 0.95` for a recent window of
  N adds, the new entry is skipped (not stored). This is the "live"
  dedup gate that keeps the layer from filling with repeats.

### L2 — Fact
- **What**: Atomic facts extracted from L1 by the LLM, each with
  `(subject, predicate, object, confidence, timestamp)`.
- **Where**: Kuzu graph (nodes are entities, edges are predicates).
- **Schema**: defined in `patches.py` `L2_FACT_SCHEMA` and applied at
  first run if the graph is empty.
- **Mode dependency**: only `pro` and `ultra` modes populate L2.
  `lite` skips LLM extraction.

### L3 — Summary
- **What**: Coherent narrative rollups of recent L2 facts, regenerated
  every 20 L2 adds (configurable via `HY_MEMORY_L3_TRIGGER_EVERY`).
- **Trigger**: patch #4 + #5 (the "dedup gate now reachable" patch) is
  the carrier that makes the trigger reachable from the write path
  without breaking the L2 fast-path.
- **Mode dependency**: only `ultra` mode populates L3.

### L4 — Identity
- **What**: Long-lived user/agent identity facts (name, preferences,
  recurring project names, etc.) promoted from L2 when they survive N
  re-summarizations unchanged.
- **Where**: Kuzu graph, with a special `l4_identity` layer tag on
  edges.
- **Why separated**: identity facts need higher priority in the
  prompt budget than transient context. The `_flatten_memories` order
  (`profile → proactive → normal`) puts L4 facts first.

### L5 — Pipeline
- **What**: Async orchestration of batch ingest, entity resolution,
  relation classification, NER fallback, Kuzu ingest, JSON export,
  digest write.
- **Why async**: the full L5 cycle takes 10+ minutes for 1000 facts;
  running it inline would block every write.
- **Schedule**: manual (via `python -m server.bin.l5_full_pipeline`)
  or `hourly` / `daily` via the config.

### L6 — Schema
- **What**: Typed entity and relationship categories. Read by L5
  during relation classification to constrain the LLM's output.
- **Where**: Kuzu graph schema (read at startup).

### L7 — Intention
- **What**: Proactive intent detection — L5 looks at recent L2 facts
  and surfaces follow-up questions, "did you mean to do X?" prompts,
  and async tasks the agent should consider.
- **Output**: a `proactive` channel in the search response, populated
  with up to N items per query.

## System 1 / System 2 duality

The "dual processing" in the description is operationalized as:

- **System 1 (fast path)**: `add()` returns immediately after
  embedding + L2 fact extraction. The user sees their message
  acknowledged. L1 raw, L2 fact, L4 identity updates happen here.

- **System 2 (slow path)**: L5 runs asynchronously, doing the
  cross-fact reasoning, schema validation, and graph ingest that
  System 1 can't afford.

A user-visible read (`search()`) merges both: System 1 results
(profile/normal channels) come back instantly; the proactive channel
may include System 2 outputs if the L5 cycle has completed recently.

## Why a 7-layer model (and how it relates to the official 6)

Most agent memory systems use 2-3 layers (raw + summary, or facts
only). The [official Hy-Memory framework](https://memory.hunyuan.tencent.com)
defines a 6-layer model; this implementation extends that to 7 with
an experimental proactive-intent layer (L7).

The mapping in the table above is the canonical reference. The 7-layer
model in this implementation maps cleanly to the cognitive-architecture
literature:

| Layer | Cognitive analog |
|-------|-----------------|
| L1 raw | sensory buffer (iconic memory) |
| L2 fact | working memory, post-perceptual |
| L3 summary | episodic memory, recapped |
| L4 identity | semantic memory, self-concept |
| L5 pipeline | consolidation (think "sleep replay") |
| L6 schema | schemas / scripts |
| L7 intention | prospective memory, intentions |

The mapping is intentional but loose — the layers in this implementation
are pragmatic engineering choices that happen to align with the
psychology. Don't read too much into the count being 7.

## Verified-2026-06-16 status (this implementation)

This is the v0.1.0 release of the community implementation. All 7
layers are functional locally; L1-L6 correspond to the [official 6-layer
spec](https://memory.hunyuan.tencent.com) and L7 is experimental:

```text
L1 raw:     ✓ writes, ✓ dedup patch, ✓ time-ordered
L2 fact:    ✓ LLM extraction, ✓ Kuzu ingest, ✓ schema
L3 summary: ✓ rollup every 20 adds, ✓ Kuzu-backed
L4 identity:✓ promoted from L2, ✓ prioritized in prompt
L5 pipeline:✓ 7 steps + orchestrator, ✓ run-id logging, ✓ mutex
L6 schema:  ✓ applied at first run, ✓ read at L5 step 2
L7 intention:✓ proactive channel in search response
```

Tested with: Hermes Agent v0.16.0, Python 3.11.15, Kuzu 0.4.x,
Qdrant 1.7.x, on Windows 10.

## Why this is a separate package

HyAtlas-Memory was originally a plugin inside the `hermes-agent` fork
at `plugins/memory/hy_memory/`. The split into its own package happened
for two reasons:

1. **Survives `hermes update`.** A `git reset --hard origin/main` on
   the fork would have wiped the plugin; a separate pip package
   survives any upstream change.

2. **Testable in isolation.** The package has its own `pyproject.toml`,
   its own test suite, and its own entry point. CI on the public repo
   doesn't need a fork of hermes-agent to run.

The cost: a peer-dependency on `hermes-agent` (declared in
`pyproject.toml`). Users install both packages; the plugin's entry
point is discovered by Hermes at runtime via the
`hermes.memory_provider` group.
