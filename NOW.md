# HyAtlas-Memory — NOW.md

## Current state (2026-07-16, post v3.4.0 hygiene)

**Released:** v3.4.0 on `main` — profile isolation in the dashboard, L1_RAW transparency, 3-tier status, console rewrite, privacy scrub follow-ups.

**Live stack (probed 2026-07-16):**
- `/api/v1/status` → `ok` / vdb ok / embed ok / llm ok / write_pipeline ok / zvec
- Dashboard `:8765` healthy; `/api/profiles` returns 7 agent_ids
- Graph: L5 **1807**, L6 **580**, L7 **188**, relations **8988**
- Layer-health (hermes-user/default): L1_raw **278**, L2 **839**, L3 **251**, graph L5 **1561** / L6 **577** / L7 **187**

**Profiles (live `/api/profiles`):**
| agent_id | memory_count | notes |
|----------|--------------|-------|
| default | ~2645 | primary |
| trading | ~185 | has data |
| research | 1 | canary-scale |
| sentinel / work-* / hestia | 0 | plumbing only |

## What is solid

- Zvec-only runtime (Qdrant archived)
- L1_RAW visible via `include_raw` (default true) + `extracted` field
- 3-tier status (LLM rate limit → warning, not full stack error)
- Dashboard profile dropdown + `/api/profiles`
- Privacy scrub: no `tuanc` username leaks outside intentional detector patterns; public handle `tuancookiez-hub` kept in URLs
- Docs refreshed to v3.4.0 (LAYERS / API / architecture / DASHBOARD / SERVER / TROUBLESHOOTING)

## Console status window

`hyatlas --detach` / `hyatlas console` spawn a visible status window via `CREATE_NEW_CONSOLE` + base Python (skip venv shim flicker). Spawn env now strips MSYS/Git Bash keys (`MSYSTEM`, `MINGW_*`, …) and uses `close_fds=False` on the CLI path so the window is more reliable from MSYS. **Service stays up regardless of window state.** Reopen with `hyatlas console` or `python -m hyatlas_memory.console`.

`console.py` log path uses `layout.logs()` / `HYATLAS_HOME` (no hardcoded `D:/HyAtlas/...`).

## Layer model

| Layer | Purpose | Visibility |
|-------|---------|------------|
| L0 basic_info | User identity facts | list (always) |
| L1 raw | Unprocessed user input | list (when `include_raw=True`, default) |
| L2 fact | LLM-extracted facts | list (always) |
| L5 knowledge | Curated knowledge nodes | list + graph |
| L6 schema | Cross-domain schemas | graph only |
| L7 intention | Goals & plans | graph + list |

L4 retired (legacy VDB rows only).

## Operational state

| Area | Status |
|------|--------|
| Capture | Hermes → L1 under hermes-user / default (+ specialists) |
| Evolution | Weekly digest (System 2 batched); graph L5/L6/L7 populated |
| Vector store | **zvec** only |
| Runtime home | `HYATLAS_HOME` (this machine: `D:\HyAtlas\.hyatlas`) |
| Dashboard | http://127.0.0.1:8765 — profiles, Quality Metrics, layer-health, L6 samples |
| Memory server | http://127.0.0.1:19527 |

## Manual ops

- Digest: `python %LOCALAPPDATA%\hermes\scripts\run_hyatlas_digest.py`
- Status: `curl http://127.0.0.1:19527/api/v1/status`
- List (with raw): `curl -X POST http://127.0.0.1:19527/api/v1/list -H "Content-Type: application/json" -d "{\"user_id\":\"hermes-user\",\"limit\":50}"`
- List (extracted only): same with `"include_raw": false`
- Profiles: `curl http://127.0.0.1:8765/api/profiles`
- L6 browse: `http://127.0.0.1:8765/api/l6-schemas?n=8`

## Privacy / placeholders

Runtime identifiers override via env vars:
- `HYATLAS_DASHBOARD_USER_IDS`
- `HYATLAS_DEFAULT_USER_ALIASES`
- `MEMORY_L5_USER_IDS`

Public repo keeps GitHub handle `tuancookiez-hub` in functional URLs. Author fields use `<Maintainer>` placeholders unless branding is restored on purpose.

## Next (optional / v3.5)

- Populate empty specialist profiles (route real writes from research/sentinel/work-*/hestia)
- BM25 / hybrid_v2 live E2E against running server (`HY_MEMORY_READER=hybrid_v2`) — unit coverage exists for client + resolve
- Console UX polish if MSYS still flakes on some hosts
- Delete any local-only scratch (`.tmp-release-notes-*`) if still present
