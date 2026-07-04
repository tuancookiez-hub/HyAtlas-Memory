# Runtime Layout Closeout — Patch 24-27

## Completed

- Patch 24: read-only runtime audit written to `docs/architecture/RUNTIME_LAYOUT_AUDIT.md`.
- Patch 25: added `hyatlas_memory.layout` for `HYATLAS_HOME` paths, config precedence, legacy fallback, dotenv loading, and runtime directory creation.
- Patch 26: added `hyatlas config show/model/embedder/validate` and rewired `hyatlas init` to the new config path.
- Patch 27: added status-side Qdrant collection checks for collection name, point count, and vector dimensions.

## Changed

- New runtime default is `~/.hyatlas` via `HYATLAS_HOME`.
- New config path is `~/.hyatlas/config/hy_memory.json`.
- Legacy fallback still reads `HERMES_HOME/hy_memory.json` and legacy env files.
- Logs now prefer `~/.hyatlas/logs` for new stack manager calls.
- `hyatlas config show` redacts API keys.
- `hyatlas config validate` checks config shape and catches LLM/embedder/vector-store mistakes.

## Verification

```text
uvx ruff check ...                                      # passed
python -m compileall -q src/hyatlas_memory tests        # passed
python -m pytest -q                                     # 28 passed, 19 skipped, 3 pre-existing warnings
python -m hyatlas_memory.start config validate          # passed against legacy config
python -m hyatlas_memory.start status                   # all services offline, expected because stack is stopped
```

## Commit

```text
d1da517 Patch 24-27: organize runtime config layout
```

## Next

Patch 28 is data migration. Do not start it without an explicit snapshot/rollback contract and Tuna approval.

Minimum next gates:

1. Create snapshot manifest before copying anything.
2. Dry-run migration plan: source path, target path, byte count, file count, config hash, Qdrant point count, Kuzu graph count.
3. Stop services before copying.
4. Copy, do not delete old dirs.
5. Verify Qdrant points, memory search, graph counts, dashboard.
6. Leave rollback path documented and old dirs untouched.
