# HyAtlas-Memory Architecture

> **Scope:** Personal/local long-term memory stack for Hermes Agent, evolved from the [Hy-Memory](https://memory.hunyuan.tencent.com) (Tencent Hunyuan) framework and extended with an experimental **L7 intention** layer, **profile isolation** (`agent_id`), and L2_RAW transparency. This document reflects **HyAtlas v3.5.0** unless a section is marked historical.

---

## HyAtlas v3.5 stack (current)

| Component | Choice |
|-----------|--------|
| **Vector DB (L0–L4 VDB)** | **Zvec** in-process (`vector_store.provider: zvec`) — no Qdrant sidecar |
| **Graph (L5–L7)** | **Kuzu** embedded at `~/.hyatlas/data/kuzu_db` |
| **Cache** | DisabledCache / minimal SQLite where needed |
| **Modes** | `lite` · `pro` · `ultra` (default for graph + digest) |
| **Hermes** | `user_id=hermes-user`, `agent_id=**default**` (plus specialist profiles) — [HYATLAS_HERMES.md](./HYATLAS_HERMES.md) |
| **L1 list** | **Transparent** — `include_raw=True` default; each item has `extracted` |
| **Status** | **3-tier** — `ok` / `warning` / `error` per vdb · embed · llm · write_pipeline |
| **Profiles** | Dashboard dropdown + `GET /api/profiles`; filter by `agent_id` |
| **L4** | **Retired** — no writer; identity in **L2**; legacy rows archived |
| **Evolution** | `client.digest()` / `POST /api/v1/digest` — System 2 agent + sweeper (not batch `l5_full_pipeline` as primary) |

**Start:** `hyatlas start` / `hyatlas --detach` (server :19527, dashboard :8765). **Cleanup:** [CLEANUP.md](./CLEANUP.md).

---

## Layer mapping: this impl vs. the official spec

| This impl (v3.5) | Official (memory.hunyuan.tencent.com) | Purpose |
|------------------|---------------------------------------|---------|
| L1 profile | (profile basics) | Stable user attributes |
| L2 raw | **L1 原始痕迹** | Verbatim / shadow ingest |
| L3 fact | **L2 原子事实** | Atomic facts (Hermes capture; identity too) |
| L4 summary | (often folded in official L2) | Rollups / session summaries |
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

## Layer notes (v3.5)

The layer model is a **contiguous 7-layer design** — `L1 Profile / L2 Raw / L3 Fact / L4 Summary / L5 Knowledge / L6 Schema / L7 Intention`. The former `L4 IDENTITY` slot was retired in v3.2 and the layer indices renumbered so there is no gap (`L0-L3/L5-L7` → `L1-L7`). Identity content lives in **L3 Fact**.

L1-L4 are stored in the zvec VDB; L5-L7 live in the Kuzu graph.

### L1 — Profile

User attributes (name, age, occupation, etc.). The lightest layer.

### L2 — Raw (list-visible)

Always written on `add()`. Listed by default with `extracted: false` until System 1 extraction produces L3. Pass `include_raw=false` for the old extracted-only view.

### L3 — Fact

Primary capture layer for Hermes. Atomic facts extracted from conversation; identity content also lives here. Namespace must match digest (`default`, not only `default_agent` aliases).

### L4 — Summary

Session-level syntheses / summaries.

### L5 — Knowledge

Graph entities and extracted knowledge nodes; grows on digest (+ evidence on facts).

### L6 — Schema

“When [context], the user [pattern]…” — searchable as `l6_schema`. Count in `layer_counts`, not necessarily in per-user VDB layer histogram.

### L7 — Intention

Experimental proactive layer in Kuzu.

### Profile isolation

`agent_id` scopes VDB + graph. Dashboard profiles: `default`, `research`, `sentinel`, `work-backend`, `work-frontend`, `trading`, `hestia`. Plumbing works even when some specialist profiles still have little/no data.

---

## Cognitive mapping (loose)

| Layer | Analog |
|-------|--------|
| L1 profile | stable identity / self-model |
| L2 raw | sensory trace / buffer |
| L3 fact | working / episodic facts |
| L4 summary | episodic rollups |
| L5 knowledge | semantic network |
| L6 schema | scripts / self-model patterns |
| L7 intention | prospective memory |

---

## Verified status

> **Historical snapshot (2026-07-16, v3.4.0).** The layer model, store choices, and profile isolation below still hold in v3.5.0. What changed in v3.5.0 is operational: a dedicated venv (`hyatlas venv setup`), `zvec>=0.6.0` (Windows LOCK reopen fix), reconciler trailing-comma JSON repair, and `<think>` stripping for reasoning models. The dashboard also gained `/api/live` (process liveness independent of backend health) and completion-scheduled refreshes. Counts shown are from that date and will have grown.

Single-user Hermes path on Windows (live probe):

```text
L1 list:        ✓ include_raw + extracted field
L2 capture:     ✓ hermes-user / default (+ other agent_ids)
Digest:         ✓ POST /api/v1/digest, cron launcher
L5 graph:       ✓ Kuzu layer_counts (1800+ nodes typical)
L6 schemas:     ✓ graph + /api/l6-schemas; digest may add evidence without +count every run
L4:             ✓ retired (legacy rows only)
Vector store:   ✓ Zvec (Qdrant runtime removed)
Status:         ✓ 3-tier ok/warning/error
Profiles:       ✓ /api/profiles + dashboard dropdown
```

Tested with: Python 3.10–3.12, Hermes Agent, Kuzu, **Zvec**, Windows 10/11.

---

## Why a separate package

HyAtlas-Memory ships as **`hyatlas-memory`** (pip) with a Hermes plugin shim so memory survives fork updates and has its own CI. See README “Migration from in-fork plugin”.

---

## Historical sections

Older docs (pre–v3.1) described Qdrant + batch `l5_full_pipeline` as primary; that path is **migration/legacy**. Refer to [CHANGELOG.md](../CHANGELOG.md) for zvec cutover, v3.2 Hermes/digest work, and v3.4 profile isolation.