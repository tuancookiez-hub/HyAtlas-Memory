# HyAtlas-Memory — NOW.md

## Current state (2026-07-08)
**v3.2.1 on `main` — Hermes single-user stack green; cleanup + L6 visibility.**

| Area | Status |
|------|--------|
| Capture | Hermes → L2 under `hermes-user` / `default` |
| Evolution | Weekly + manual digest; log `ok`; graph L5 **1594**, L6 **568**, rels **8128** |
| L4 | Retired; legacy VDB rows only; archive in `~/.hyatlas/archive/` |
| Vector store | **zvec** only at runtime; Qdrant zip at `~/.hyatlas/archive/qdrant_v3_1_0_release.zip` |
| Cron | `smart-memory-prune` (4h, Discord thread); `HyAtlas weekly digest` (7d, same thread + one-line summary) |
| Dashboard | Settings → System: layer-health, digest command, **L6 schema samples** |

## Manual ops
- Digest: `python %LOCALAPPDATA%\hermes\scripts\run_hyatlas_digest.py`
- L6 browse: `http://127.0.0.1:8765/api/l6-schemas?n=8` or `GET /api/v1/graph?layer=l6_schema&n=10`
- Docs: `docs/HYATLAS_HERMES.md`, `docs/CLEANUP.md`

## Next (optional)
- Remove leftover Qdrant folders on disk after reading `docs/CLEANUP.md`
- BM25 reader E2E with `HY_MEMORY_READER=hybrid_v2`