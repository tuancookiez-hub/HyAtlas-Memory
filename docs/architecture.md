<!--
STALE DOC NOTICE (2026-07-16):
This document may be out of date. For current state, see ../NOW.md
or https://github.com/<owner>/HyAtlas-Memory/blob/main/NOW.md
Last meaningful refresh: see the date in this header's filename context.
-->

# HyAtlas-Memory Architecture

> **Scope:** Community implementation of the [official Hy-Memory framework](https://memory.hunyuan.tencent.com) (Tencent Hunyuan), extended with an experimental **L7 intention** layer. This document reflects **HyAtlas v3.2.1** unless a section is marked historical.

---

## HyAtlas v3.2 stack (current)

| Component | Choice |
|-----------|--------|
| **Vector DB (L0–L4 VDB)** | **Zvec** in-process (`vector_store.provider: zvec`) — no Qdrant sidecar |
| **Graph (L5–L7)** | **Kuzu** embedded at `~/.hyatlas/data/kuzu_db` |
| **Cache** | DisabledCache / minimal SQLite where needed |
| **Modes** | `lite` · `pro` · `ultra` (default for graph + digest) |
| **Hermes** | `user_id=hermes-user`, `agent_id=**default**` — [HYATLAS_HERMES.md](./HYATLAS_HERMES.md) |
| **L4** | **Retired** — no writer; identity in **L2**; legacy rows archived |
| **Evolution** | `client.digest()` / `POST /api/v1/digest` — System 2 agent + sweeper (not batch `l5_full_pipeline` as primary) |

**Start:** `hyatlas start` (server :19527, dashboard :8765). **Cleanup:** [CLEANUP.md](./CLEANUP.md).

---

## Layer mapping: this impl vs. the official spec

| This impl (v3.2) | Official (memory.hunyuan.tencent.com) | Purpose |
|------------------|---------------------------------------|---------|
| L1 raw | **L1 原始痕迹** | Verbatim / shadow ingest |
| L2 fact | **L2 原子事实** | Atomic facts (Hermes capture) |
| L3 summary | (often folded in official L2) | Rollups / session summaries |
| L4 identity | **L3 身份画像** (legacy) | **Retired in HyAtlas** → use **L2** |
| L5 knowledge | **L4 心智** / graph facts | Kuzu knowledge nodes |
| L6 schema | **L5 模式** | Behavioral schemas in Kuzu |
| L7 intention | **L6 意图** (proactive) | Experimental extension |

Our L7 is **not** in the official 6-layer spec. Official identity (L3) maps to **L2 + L6** in practice after L4 retirement.

---

## System 1 / System 2

- **System 1 (online):** `add()` → embedding + fact extraction → **Zvec** (chiefly **L2**). `search()` / reader injects profile + facts + graph-aware channels.

- **System 2 (digest):** Scheduled or manual **digest** clusters **fresh L2** for `user_id`/`agent_id`, runs **system2_agent** (LLM JSON ops: `create_schema`, `add_evidence`, `add_edge`), writes **L5–L7** in Kuzu; **cross_domain_sweeper** may merge L6 basics into cores.

A user-visible `search()` merges both: fast VDB recall plus graph-backed schema/intention signals when ultra + hybrid reader paths are enabled.

---

## Storage architecture

```text
┌─────────────────────────────────────────┐
│  Hermes Agent / HTTP API (19527)        │
│  add · search · list · digest           │
└───────────────┬─────────────────────────┘
                │
     ┌──────────┴──────────┐
     ▼                     ▼
┌─────────┐         ┌─────────────┐
│  Zvec   │         │   Kuzu      │
│ L0–L4   │         │ L5 L6 L7    │
│ (VDB)   │         │ (graph)     │
└─────────┘         └─────────────┘
```

**Integrations** (`integrations.py`): graph endpoint (`/api/v1/graph` with `layer_counts` + optional `?layer=l6_schema`), VDB dashboard helpers, digest wiring, L1 sweep (Zvec-safe), L5 in-process hooks, etc.

---

## Graph API (dashboard + proof)

- Default `GET /api/v1/graph` returns **L5** nodes + relations for visualization.
- `GET /api/v1/graph?layer=l6_schema&n=10` — browse behavioral schemas.
- Dashboard: `/api/layer-health`, `/api/l6-schemas` — see [DASHBOARD.md](./DASHBOARD.md).

---

## Layer notes (v3.2)

### L2 — Fact

Primary capture layer for Hermes. Namespace must match digest (`default`, not `default_agent`).

### L4 — Identity (retired)

Historical `l4_identity` VDB rows may remain; System 2 **does not** read L4 as an input layer. Archive before deletion.

### L5 — Knowledge

Graph entities and extracted knowledge nodes; grows on digest (+ evidence on facts).

### L6 — Schema

“When [context], the user [pattern]…” — searchable as `l6_schema`. Count in `layer_counts`, not necessarily in per-user VDB layer histogram.

### L7 — Intention

Experimental proactive layer in Kuzu.

---

## Cognitive mapping (loose)

| Layer | Analog |
|-------|--------|
| L2 fact | working / episodic facts |
| L3 summary | episodic rollups |
| L5 knowledge | semantic network |
| L6 schema | scripts / self-model patterns |
| L7 intention | prospective memory |

---

## Verified status (2026-07-08, v3.2.1)

Single-user Hermes path on Windows:

```text
L2 capture:     ✓ hermes-user / default
Digest:         ✓ POST /api/v1/digest, cron launcher
L5 graph:       ✓ Kuzu layer_counts (e.g. 1500+ nodes)
L6 schemas:     ✓ graph + search (500+ typical); digest may add evidence without +count every run
L4:             ✓ retired (legacy rows only)
Vector store:   ✓ Zvec (Qdrant runtime removed)
```

Tested with: Python 3.11, Hermes Agent, Kuzu, **Zvec**, Windows 10/11.

---

## Why a separate package

HyAtlas-Memory ships as **`hyatlas-memory`** (pip) with a Hermes plugin shim so memory survives fork updates and has its own CI. See README “Migration from in-fork plugin”.

---

## Historical sections

Older docs (pre–v3.1) described Qdrant + batch `l5_full_pipeline` as primary; that path is **migration/legacy**. Refer to [CHANGELOG.md](../CHANGELOG.md) for zvec cutover and v3.2 Hermes/digest work.