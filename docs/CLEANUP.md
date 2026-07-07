# Runtime cleanup (post–v3.1.0 zvec cutover)

HyAtlas **does not** run Qdrant. Safe cleanup after you have a cold backup.

## Already archived
- `~/.hyatlas/archive/qdrant_v3_1_0_release.zip` — full HyAtlas Qdrant snapshot (keep until you are confident in zvec).

## Optional to delete (after backup exists)
| Path | Notes |
|------|--------|
| `C:\Users\tuanc\.hy_memory\` | Legacy layout (~trivial size if empty); not used when `HYATLAS_HOME=~/.hyatlas` |
| `C:\qdrant-data\` | Old sidecar data dir if you ran Qdrant manually |
| `~/.hyatlas/qdrant/` | Only if present and server uses zvec (check `hyatlas status`) |

## Commands
```bash
hyatlas archive qdrant    # zip again if you changed Qdrant data since last archive
hyatlas zvec doctor
hyatlas status
```

## Repo hygiene
- Superseded planning docs live under `docs/archive/` (not deleted — historical).
- `docker-compose.yml` is **legacy Qdrant compose**; local dev uses `hyatlas start` (zvec).