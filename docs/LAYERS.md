# Memory Layers

> The 7-layer memory model implemented by HyAtlas-Memory. Each layer has a specific purpose, lifecycle, and storage location.

This is a deep-dive reference. For a high-level overview, see the [Architecture doc](./architecture.md) and the [main README](../README.md).

## Layer at a glance

| # | Name | Purpose | Storage | When it fires | Volume (typical) |
|---|---|---|---|---|---|
| L0 | Basic Info | Verbatim user/agent message text, time-ordered | Qdrant + `l1_raw.jsonl` | Every message | ~50-200 |
| L1 | Raw | Atomic facts extracted by LLM | Qdrant (L2 vector) | Every message | ~100-500 |
| L2 | Fact | Periodic L2 rollups (every 20 facts) | Qdrant (L3 vector) | After 20 new L2 | ~50-200 |
| L3 | Summary | Long-lived user/agent identity facts | Qdrant (L4 vector) | After 20 new L3 | ~20-100 |
| L4 | Identity | Async ingest into Kuzu graph | Kuzu graph nodes | Hourly / on-demand | ~5-50 |
| L5 | Pipeline | Typed entity/relationship schema | Kuzu graph nodes | After L5 rebuild | ~100-500 |
| L6 | Schema | Proactive intent detection, async tasks | Kuzu graph nodes | Continuous | ~100-500 |
| L7 | Intention | (Extension) Experiment-specific memory | Kuzu + Qdrant | Manual / experimental | varies |

> **Volume** is rough orders of magnitude for a single user after a few months of regular use. Will vary significantly by use case.

---

## L0 — Basic Info

**What:** The rawest layer. Verbatim text of every user/agent message, time-ordered. This is the "I literally said this" layer — no interpretation.

**Where it lives:**
- `~/.hyatlas/data/l1_raw.jsonl` — one JSON per line (legacy flat-file log)
- Qdrant collection `l0_basic_info` — vector-indexed for similarity search

**Why it exists:** Provides an immutable audit trail. Downstream layers (L1+) are interpretations that can be wrong; L0 is the ground truth. Also powers the dashboard's "Recent Ingestion" feed.

**When it fires:** On every single message the agent receives or sends. No LLM call — just JSONL append + vector upsert.

**Dedup:** Carries patch #6 + #7 from the fork — exact-duplicate detector uses the embedder to skip near-identical consecutive messages.

**Read in dashboard:** Memory Observatory (L0 ring at the innermost position).

---

## L1 — Raw (legacy name: `l1_raw`)

**What:** Despite the name "L1 Raw" being the same as L0, in this implementation `l1_raw` refers to **atomic facts extracted by LLM** in real-time as messages come in. This is the fast-path layer that powers immediate context recall.

**Where it lives:**
- Qdrant collection `l1_raw` — vector-indexed, 768-dim embeddings
- Indexed by user_id + timestamp

**Why it exists:** When you start a new message, the agent needs to recall relevant context in milliseconds. L1 facts are pre-extracted and pre-embedded, so the recall query is just a vector similarity search — no LLM call needed for the recall step.

**When it fires:** 
- On every new message: extract atomic facts via LLM, upsert to Qdrant
- On every new message: query top-K similar facts from L1, inject into context

**LLM cost:** High — one extraction call per message. This is the primary cost driver.

**Read in dashboard:** Memory Observatory (L1 ring). Also surfaced in "Recent Ingestion" feed.

---

## L2 — Fact

**What:** A higher-level rollup layer. After 20 new L1 facts accumulate for a user, the system triggers a background job that summarizes them into a single L2 fact. L2 facts are more durable and abstract than L1.

**Where it lives:**
- Qdrant collection `l2_fact` — vector-indexed
- Triggered by the L3 rollup job (every 20 L1 → 1 L2)

**Why it exists:** As raw facts accumulate, recall becomes noisy. L2 rollups act as "chapter summaries" — when the agent needs broad context ("what has this user been working on lately?"), L2 recall gives better signal-to-noise than L1.

**When it fires:**
- Background job checks: if user has ≥20 unprocessed L1 facts since last rollup → trigger
- Runs asynchronously, doesn't block the fast path

**Read in dashboard:** Memory Observatory (L2 ring — typically the densest layer).

---

## L3 — Summary

**What:** Long-term identity facts. "The user prefers TypeScript", "The user works on Windows", "The user has a project called X". These don't change message-to-message — they persist for weeks/months.

**Where it lives:**
- Qdrant collection `l3_summary` — vector-indexed
- Higher confidence threshold (0.8+) than L1/L2

**Why it exists:** Identity facts need special treatment — they should be durable, high-signal, and only updated when something contradicts them. L3 has its own conflict-resolution logic (see `patches.py` in source).

**When it fires:**
- On every new L2 rollup, the L3 updater checks if the L2 contains identity-relevant info
- If yes, it either creates a new L3 or updates an existing L3 (with conflict detection)

**Read in dashboard:** Memory Observatory (L3 ring). Also in the "Explore Memory" page under the L3 filter.

---

## L4 — Identity

**What:** The cumulative identity profile. Less about specific facts, more about patterns. "This user is a hands-on engineer who prefers concise responses." Built from L3 over time.

**Where it lives:**
- Qdrant collection `l4_identity` — vector-indexed
- Limited to ~30-100 entries per user (oldest get pruned)

**Why it exists:** When the agent starts a new conversation, L4 is the first thing injected into context. It sets the tone and approach. It's the "who is this person" memory.

**When it fires:**
- On every L3 update, the L4 synthesizer decides if the change is identity-level
- Also triggered manually via `hermes hy-memory add "..."` with a high-importance tag

**Read in dashboard:** Memory Observatory (L4 ring — nodes are larger than other layers to indicate importance).

---

## L5 — Pipeline (Knowledge Graph)

**What:** The bridge from vector memory to graph memory. L5 is where atomic facts become **entities** in a typed relationship graph. "User" → "works on" → "Project X" → "uses" → "React".

**Where it lives:**
- Kuzu graph database at `~/.hyatlas/data/kuzu_db`
- Node types: `person`, `project`, `technology`, `concept`, `task`
- Edge types: `works_on`, `uses`, `knows`, `prefers`, `related_to`

**Why it exists:** Vector search finds "similar" things. Graph traversal finds "related" things. L5 gives the agent the ability to answer questions like "what projects use React?" or "who else prefers TypeScript?" — things vector search can't do.

**When it fires:**
- Background batch job (`server/bin/l5_full_pipeline.py`) runs periodically
- 7-step pipeline: stop server → extract facts → resolve entities → quality review → rebuild graph → export JSON → restart server
- Takes minutes for thousands of facts

**Read in dashboard:** Memory Observatory (L5 ring — typically only a few nodes since it's the curated layer). Also the L5 Knowledge Graph page in the dashboard.

---

## L6 — Schema

**What:** Typed domain schemas. When the L5 pipeline finds recurring patterns, it promotes them to formal schemas. E.g., "every project has a name, a tech stack, and a status" becomes a `Project` schema with those properties.

**Where it lives:**
- Kuzu graph nodes with `node_type = "schema"`
- Connected to L5 entities via `instance_of` edges

**Why it exists:** Schemas let the agent reason about categories, not just instances. "Tell me about other projects like this one" is a schema-level query.

**When it fires:**
- After L5 rebuild, the schema synthesizer (`server/bin/l5_*.py`) checks for repeated entity shapes
- Promotes recurring shapes to schema nodes

**Read in dashboard:** Memory Observatory (L6 ring — often the largest layer by count, since it's a rollup of patterns).

---

## L7 — Intention (experimental extension)

**What:** Proactive intent detection. Based on the conversation history and identity profile, L7 contains "the user is likely about to ask about X" or "this user typically wants Y next".

**Where it lives:**
- Kuzu graph nodes with `node_type = "intention"`
- Refreshed continuously based on recent activity

**Why it exists:** This is an **experimental extension** of the official 6-layer spec. The goal is to make the agent feel anticipatory rather than just reactive. L7 is **not** part of the official Hy-Memory framework — see the [Architecture doc](./architecture.md#layer-mapping-this-impl-vs-the-official-spec) for the mapping.

**When it fires:**
- After every message, the intention detector runs (lightweight LLM call)
- Output is upserted to Kuzu as L7 nodes
- On new conversation, L7 nodes are queried alongside L4 to set initial context

**Read in dashboard:** Memory Observatory (L7 ring — the outermost band, largest radius).

---

## How layers relate

```text
  L0 ── raw text
   ↓ (every message)
  L1 ── atomic facts
   ↓ (every 20 L1)
  L2 ── rollups
   ↓ (on identity-relevant change)
  L3 ── identity facts
   ↓ (on pattern emergence)
  L4 ── identity profile
   ↓ (batch pipeline)
  L5 ── graph entities
   ↓ (schema synthesizer)
  L6 ── typed schemas
   ↓ (continuous)
  L7 ── proactive intentions
```

**Read direction:** L0 is the ground truth. L7 is the highest-level abstraction. The system is **append-only at L0** (never delete raw text) but **mutable at higher layers** (L3 can be updated when identity changes).

**Write frequency:**
- L0, L1: every message (high write rate)
- L2, L3: every ~20 messages (medium)
- L4: daily-ish (low)
- L5, L6: batch job (very low)
- L7: every message (medium — lightweight LLM call)

**Read priority** (when injecting context into a new message):
1. L4 (identity) — always
2. L7 (intentions) — always
3. L1 (recent facts) — top-K by similarity
4. L2 (rollups) — top-K by recency
5. L3 (identity facts) — only if L4 doesn't cover

See `src/hyatlas_memory/client.py` for the actual recall query logic.

---

## Storage summary

| Layer | Store | File / Collection |
|---|---|---|
| L0 | Flat file + Qdrant | `~/.hyatlas/data/l1_raw.jsonl` + `l0_basic_info` |
| L1 | Qdrant | `l1_raw` collection (yes, same name as old L1) |
| L2 | Qdrant | `l2_fact` collection |
| L3 | Qdrant | `l3_summary` collection |
| L4 | Qdrant | `l4_identity` collection |
| L5 | Kuzu | `~/.hyatlas/data/kuzu_db` (graph) |
| L6 | Kuzu | same graph, `node_type = "schema"` |
| L7 | Kuzu | same graph, `node_type = "intention"` |

The two-store split is intentional:
- **Qdrant** (vector store) — for similarity search on L0–L4
- **Kuzu** (graph store) — for relationship queries on L5–L7

See `src/hyatlas_memory/__init__.py` for the layer → collection mapping constants.
