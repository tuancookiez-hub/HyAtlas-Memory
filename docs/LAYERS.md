<!--
STALE DOC NOTICE (2026-07-16):
This document may be out of date. For current state, see ../NOW.md
or https://github.com/<owner>/HyAtlas-Memory/blob/main/NOW.md
Last meaningful refresh: see the date in this header's filename context.
-->

# Memory Layers

> **HyAtlas v3.2.1 (2026-07)** — This doc describes **this repo’s** layer model. Runtime vector store is **Zvec** (in-process). **L4 is retired** for new writes; identity lives in **L2**. Graph layers **L5–L7** live in **Kuzu**.

For Hermes single-user setup (digest, cron, L6 proof), see [HYATLAS_HERMES.md](./HYATLAS_HERMES.md). For disk cleanup after the zvec cutover, see [CLEANUP.md](./CLEANUP.md).

High-level design: [architecture.md](./architecture.md). API: [API.md](./API.md).

---

## Layer at a glance (HyAtlas v3.2)

| Layer | Key | Purpose | Canonical store | Hermes / digest notes |
|-------|-----|---------|-----------------|------------------------|
| L0 | `l0_basic_info` | Stable profile snippets (name, locale, etc.) | **Zvec** VDB | Small count; recall “profile” channel |
| L1 | `l1_raw` | Raw message shadow / ingest buffer | **Zvec** VDB | Often **0** under `hermes-user`/`default` — shadowed after L2 extract |
| L2 | `l2_fact` | Atomic facts from chat (System 1) | **Zvec** VDB | **Primary capture layer** for Hermes; “fresh L2” feeds digest |
| L3 | `l3_summary` | Session / rollup summaries | **Zvec** VDB | Medium volume |
| L4 | `l4_identity` | **Retired** | **Zvec** legacy rows only | **No writer**; archived to `~/.hyatlas/archive/l4_identity_pre_migrate_*.jsonl`; identity → **L2** |
| L5 | `l5_knowledge` | Knowledge graph entities / facts | **Kuzu** graph | Count via `GET /api/v1/graph` → `layer_counts` |
| L6 | `l6_schema` | Behavioral schemas (“When X, user Y…”) | **Kuzu** graph | System 2 `create_schema` / `add_evidence`; VDB count may be **0** — graph is canonical |
| L7 | `l7_intention` | Proactive intentions (experimental) | **Kuzu** graph | Grows with System 2 / sweeper when patterns warrant |

**Evolution:** `POST /api/v1/digest` (ultra mode) runs System 2 on fresh L2 clusters → L5/L6/L7 updates. Weekly Hermes cron runs `run_hyatlas_digest.py` (see HYATLAS_HERMES).

---

## System 1 vs System 2

| Path | When | What |
|------|------|------|
| **System 1** | Every `add` / chat turn | Embed, extract **L2** (pro/ultra), inject recall on `search` |
| **System 2** | Manual or scheduled **digest** | Cluster fresh L2 → LLM ops (`create_schema`, `add_evidence`, edges) → graph **L5–L7**; cross-domain sweeper may promote L6 cores |

Do **not** rely on `l5_full_pipeline.py` as the primary evolution path — upstream-style **`digest()`** is the intended mechanism in HyAtlas.

---

## L0 — Basic info

Short-lived or stable profile fields extracted or seeded for recall. Stored in Zvec under `l0_basic_info`.

---

## L1 — Raw (`l1_raw`)

Upstream “raw trace” layer. Under the Hermes fast path, content is often **promoted/shadowed** into L2; dashboard may show **0** for your isolation key while L2 is healthy. L1 rolling delete/dedup is **provider-aware** (Zvec uses in-process store + filters).

---

## L2 — Fact (`l2_fact`)

**Main durable fact layer** for conversation capture. Hermes provider writes here under `user_id` + `agent_id` (use **`default`** for TUI — see HYATLAS_HERMES).

Facts with low `s2_evidence_count` count as **fresh** and feed the next digest.

---

## L3 — Summary (`l3_summary`)

Rollups and session-level summaries over L2. Stored in Zvec.

---

## L4 — Identity (**retired in HyAtlas**)

Upstream once promoted identity into `l4_identity`. HyAtlas **does not write** new L4 rows. Legacy points may remain searchable; export before any purge: `scripts/archive_l4_identity.py`.

**Identity and preferences** for evolution and recall are modeled in **L2** (+ graph schemas in **L6**).

---

## L5 — Knowledge (`l5_knowledge`)

Entity- and fact-shaped **graph nodes** in Kuzu (products, concepts, people, etc.). The public graph API returns L5 nodes by default; use `?layer=l5_knowledge` explicitly.

Dashboard **Graph** tab is L5-centric; totals for all graph layers are in `layer_counts`.

---

## L6 — Schema (`l6_schema`)

**Behavioral patterns** — full-sentence schemas with evidence links to facts. Created/updated by System 2 during digest.

**Proof:** `GET /api/v1/graph?layer=l6_schema&n=10`, dashboard `/api/l6-schemas`, or Settings → System sample list. Semantic search returns `layer: l6_schema` in hybrid recall.

Digest may **add evidence** to existing schemas without increasing the L6 **count** every run.

---

## L7 — Intention (`l7_intention`)

Experimental proactive layer (extension beyond official 6-layer spec). Stored in Kuzu; surfaced when configured in ultra / reader paths.

---

## Storage summary (v3.2)

| Layer | Store | Location |
|-------|--------|----------|
| L0–L4 (VDB) | **Zvec** | `~/.hyatlas/zvec/` (collection e.g. `agent_memories_1024`) |
| L5–L7 (graph) | **Kuzu** | `~/.hyatlas/data/kuzu_db` |

Qdrant is **not** used at runtime in v3.1+; see migration archive in [CLEANUP.md](./CLEANUP.md).

---

## Flow (simplified)

```text
Chat (Hermes) → L2 facts (Zvec)
                    ↓ digest (System 2)
              L5 knowledge + L6 schemas + L7 intentions (Kuzu)
                    ↓ search / hy_memory_search
              Injected context on next turn
```

---

## Official spec mapping

Tencent’s [Hy-Memory](https://memory.hunyuan.tencent.com) 6-layer spec is the architectural reference. HyAtlas renumbers/extends for historical fork reasons; see [architecture.md](./architecture.md#layer-mapping-this-impl-vs-the-official-spec) for the mapping table and **L4 retirement** note.

## L1_RAW visibility (added 2026-07-16)

L1 (raw user input) is now visible in `/api/v1/list` by default. Each memory entry has an `extracted: true|false` field. Pass `include_raw=False` to revert to extracted-only.

This fixes the design gap where unprocessed L1 writes were silently hidden from the user-facing list. New behavior:

- Write → L1_RAW always persisted to zvec
- LLM extraction → creates sibling L2_FACT memory (when extraction succeeds)
- `/api/v1/list` with default `include_raw=True` → returns L1_RAW + L2_FACT + L5_KNOWLEDGE etc.
- `extracted: false` on L1_RAW entries marks them as "raw, not yet processed"

If you see `extracted: false` items older than 24h in your list, the LLM extractor rejected that input as not worth extracting — the raw text is preserved and searchable via direct zvec query.
