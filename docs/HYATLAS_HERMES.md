# HyAtlas ↔ Hermes identity contract

Single-user HyAtlas expects **one namespace** for capture + digest + sweeper.

| Field | Value |
|-------|--------|
| `user_id` | `hermes-user` (or `HY_MEMORY_USER_ID`) |
| `agent_id` | **`default`** — Hermes TUI / gateway writes facts here |

Do **not** use `default_agent` for digest unless you intentionally stored memories under that agent (legacy L5 blobs).

## Weekly digest (Hermes cron)

A **script-only** cron job is already scheduled:

- **Name:** `HyAtlas weekly digest`
- **Schedule:** every **7 days** (`every 168h` / `10080m`)
- **Script:** `%LOCALAPPDATA%\hermes\scripts\run_hyatlas_digest.py`
- **Deliver:** `local` (log only; read `~/.hyatlas/logs/digest_run_latest.log`)

List jobs: Hermes `cronjob` list / TUI cron tab.

## Manual digest (always use this on Windows)

Do **not** run `python /f/HyAtlas-Memory/...` from git-bash in background — MSYS can mangle paths to `F:\f\...`.

```text
python %LOCALAPPDATA%\hermes\scripts\run_hyatlas_digest.py
```

Or from repo (foreground):

```bash
python scripts/run_digest_once.py hermes-user default
```

## Dashboard proof

Settings → **System** tab shows `/api/layer-health`: digest namespace, fresh L2, graph L5/L6, digest log status (`ok` / `stale`), and the Windows manual digest command.

## Environment

```bash
HY_MEMORY_USER_ID=hermes-user
HY_MEMORY_AGENT_ID=default
```

## L4

`l4_identity` is **retired**. Identity is in **L2**. Archived rows live under `~/.hyatlas/archive/l4_identity_pre_migrate_*.jsonl`.