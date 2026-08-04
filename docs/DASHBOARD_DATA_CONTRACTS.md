# HyAtlas v3.6 Dashboard Data Contracts

**Status:** v3.6 implementation contract  
**Baseline:** the pre-fix contract is documented in the Track A–C audit
(`C:/Users/tuanc/AppData/Local/hermes/checklists/default-hyatlas-tracks-abc-20260729.md`).  
**Audit:** `C:/Users/tuanc/AppData/Local/hermes/checklists/default-hyatlas-tracks-abc-20260729.md`

## Purpose

The dashboard currently combines several distinct kinds of records into one array. That makes plausible-looking numbers disagree and lets graph derivations appear as memory writes or activity. v3.6 fixes this by defining one contract per data class and requiring every dashboard surface to name which contract it uses.

## Data classes

### 1. VDB memory

Canonical source: zvec through the memory server.

Required fields:

```json
{
  "memory_id": "string",
  "user_id": "string",
  "agent_id": "string",
  "session_id": "string|null",
  "layer": "l0_basic_info|l1_raw|l2_fact|l3_summary|l4_identity",
  "content": "string",
  "status": "string",
  "gmt_created": 0,
  "gmt_updated": 0,
  "tags": []
}
```

Rules:

- A VDB memory is a persisted memory record, not a graph node.
- `gmt_created` and `gmt_updated` are real persisted timestamps. They are never synthesized from importance, mention count, or current time.
- L1_RAW remains visible.
- L4 is legacy/archive-only and receives no new writes.
- The dashboard memory-list endpoint returns only this data class.

Allowed surfaces:

- Recent Ingestion
- Today / Activity
- Last Memory
- Explore Memory
- VDB sections of Overview and Memory Layers

### 2. Graph node

Canonical source: Kuzu through `/api/v1/graph`.

Required fields:

```json
{
  "node_id": "string",
  "agent_id": "string",
  "layer": "l5_knowledge|l6_schema|l7_intention",
  "name": "string",
  "entity_type": "string",
  "confidence": 0.0,
  "mention_count": 0,
  "created_at": "string|null"
}
```

Rules:

- A graph node is derived knowledge, not an ingestion event.
- It does not appear in `/api/memories`.
- It does not count toward Today, This Week, Recent Ingestion, or Last Memory.
- Missing timestamps remain unavailable; the frontend must not invent them.

Allowed surfaces:

- L5 Knowledge Graph
- Memory Observatory
- L5–L7 sections of Overview and Memory Layers
- Quality metrics when explicitly labeled as graph inventory

### 3. Graph relation

Canonical source: Kuzu through `/api/v1/graph`.

Required fields:

```json
{
  "a": "string",
  "b": "string",
  "relation_type": "string",
  "confidence": 0.0
}
```

Rules:

- Default Observatory connections come only from this contract.
- Keyword similarity, timestamp proximity, and forced adjacent-layer links are not stored relations.
- Optional inferred relationships must use a separate explicitly labeled view and cannot affect stored connection counts.

### 4. Coding memory

Canonical source: the active coding-memory database resolved under the current HyAtlas runtime home, when configured.

Rules:

- Missing database means `available: false`, not a silent empty working database.
- Coding memories are a separate journal and never affect durable layer totals.
- Coding activity may appear in Today only under a clearly named Coding filter or combined activity view.

### 5. Operational evidence

Canonical sources:

- live status endpoints;
- durable request/activity metrics;
- timestamped digest run evidence;
- active runtime paths.

Rules:

- Liveness and readiness are separate.
- Missing telemetry is `unavailable`, not zero and not perfect.
- Stale telemetry is labeled stale and cannot produce a fresh-green claim.
- Global infrastructure evidence and profile-scoped memory evidence remain separate.

## Scope contract

### Global scope

`agent_id=all` means an explicit aggregate of the seven supported profiles:

- default
- research
- sentinel
- work-backend
- work-frontend
- trading
- hestia

Global scope must not depend on an accidentally omitted query parameter.

### Profile scope

A selected profile must scope:

- memory list;
- search;
- graph nodes and relations;
- layer counts;
- layer health;
- quality memory evidence;
- Observatory;
- L5 Knowledge Graph.

Changing profile invalidates cached page-specific data.

### User scope

Dashboard user scope is resolved in one place. Explicit `HYATLAS_DASHBOARD_USER_IDS` is honored. Local all-user mode must be represented explicitly in responses and tests.

## Count definitions

| Name | Definition |
|---|---|
| VDB points | Active zvec records in the requested scope |
| Graph nodes | Kuzu L5 + L6 + L7 nodes in the requested scope |
| Graph relations | Kuzu relations in the requested scope |
| Display total | VDB L0–L4 + graph L5–L7 |
| Memory-list total | Number of VDB memory records matching the list filters |
| Recent ingestion | Real VDB/coding create or update events in the requested time window |
| Today | Real activity during the previous 24 hours |
| This week | Real activity during the previous seven days |
| Last memory | Latest real persisted VDB memory creation/update, excluding graph derivations |

For every paginated endpoint:

```text
total = total records matching the filters before pagination
len(items) <= limit
items contains only the endpoint's declared data class
```

## Dashboard endpoint contracts

### `GET /api/memories`

Returns VDB memories only.

```json
{
  "memories": [],
  "total": 0,
  "offset": 0,
  "limit": 25,
  "agent_id": "all"
}
```

Must not include graph nodes or coding records.

### `GET /api/l5/graph`

Returns graph nodes and graph relations. It must honor `agent_id` and identify the active scope. Request time must not be labeled as export time.

### `GET /api/health`

Must declare which contract it represents:

- dashboard liveness only; or
- dashboard readiness including required upstream services.

v3.6 target: return readiness. Upstream transport failure cannot return HTTP 200 with `status=ok`.

### `GET /api/status`

Preserves the upstream status payload and status class without manufacturing green health.

### `GET /api/quality-metrics`

Every score dimension includes:

- value or `null`;
- evidence availability;
- evidence timestamp;
- scope;
- source.

Unavailable dimensions are excluded from the weighted denominator. They do not receive 0 or 100 silently.

## Frontend behavior contracts

### Explore Memory

Every visible control must affect the request or result:

- search mode;
- layer;
- time window;
- sort;
- profile scope.

Unsupported controls are removed rather than rendered as decoration.

### Memory Observatory

- Canonical rail/legend counts come from `/api/layer-counts`.
- Graph edges come from stored Kuzu relations.
- Rendered subset and canonical total are shown separately.
- Placeholder nodes are visual-only and excluded from counts.

### Today / Activity

- Uses VDB and coding activity only.
- Never uses graph inventory or synthetic graph timestamps.
- Export control must work or be absent.

### L5 Knowledge Graph

- Fetches the selected profile scope.
- Generates type filters from the actual response taxonomy.
- Reloads when profile changes.
- Failure copy refers to the live Kuzu path, not a removed export script.

## Failure behavior

- Non-2xx responses are errors, not normal JSON data.
- Failure of one optional dataset does not blank unrelated pages.
- Stale retained data is visibly marked stale.
- Search failures are shown in the page.
- Empty, unavailable, and error are distinct states.

## v3.6 clean-floor acceptance

The contract is satisfied when:

1. `/api/memories` returns memories only and its pagination metadata reconciles.
2. No graph node appears as ingestion, Today, This Week, or Last Memory.
3. Observatory counts and connections match canonical sources.
4. Every visible control is functional or removed.
5. All eight dashboard pages honor profile scope.
6. Missing/stale evidence cannot produce false-green health or inflated quality.
7. Runtime storage paths resolve under the active HyAtlas home.
8. Automated and live browser verification pass for global and all seven profiles.
