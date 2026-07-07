# HyAtlas ↔ Hermes identity contract

Single-user HyAtlas expects **one namespace** for capture + digest + sweeper.

| Field | Value |
|-------|--------|
| `user_id` | `hermes-user` (or `HY_MEMORY_USER_ID`) |
| `agent_id` | **`default`** — Hermes TUI / gateway writes facts here |

Do **not** use `default_agent` for digest unless you intentionally stored memories under that agent (legacy L5 blobs).

## Commands

```bash
# Preflight + digest (correct defaults)
python scripts/run_digest_once.py hermes-user default

# Weekly evolution (see cron or scripts/scheduled_digest.py)
python scripts/scheduled_digest.py
```

## Environment

```bash
HY_MEMORY_USER_ID=hermes-user
HY_MEMORY_AGENT_ID=default
```

## L4

`l4_identity` is **retired**. Identity is in **L2**. Archived rows live under `~/.hyatlas/archive/l4_identity_pre_migrate_*.jsonl`.