# Patch 29 — Write/recall repair

Date: 2026-07-04

## Problem

After Patch 28, HyAtlas booted from `HYATLAS_HOME`, but write/recall was not verified:

- Qdrant was healthy but had only a tiny point count.
- `tests/test_integration.py` failed 3 write/recall assertions.
- Direct `/api/v1/add` returned success with `error_code=502` and `LLM_ERROR`.
- Server logs showed MiniMax auth failures: stale `HY_MEMORY_LLM_API_KEY` env overrode the active JSON config key.

## Root cause

`server/start_server.py` said JSON won, but `_resolve()` actually preferred `HY_MEMORY_LLM_*` env values when present and different from JSON. Tuna's shell still had an older `HY_MEMORY_LLM_API_KEY`, so the server launched with the wrong key even though `~/.hyatlas/config/hy_memory.json` held the working native MiniMax config.

Secondary issue: the default upstream hybrid reader did not return freshly written L1/L4 records in these local tests, while the legacy reader did. The provider wrapper now defaults search calls to `reader="legacy"` unless explicitly overridden.

## Changed

- `src/hyatlas_memory/server/start_server.py`
  - Active JSON config now wins over legacy `HY_MEMORY_LLM_*` env vars.
- `src/hyatlas_memory/client.py`
  - `HyMemoryClient.search()` defaults to `reader="legacy"`.
- `tests/test_client_defaults.py`
  - Regression tests for legacy reader default + override.
- `tests/test_integration.py`
  - Forces immediate flush for integration lifecycle tests.

## Verification

Commands:

```bash
uvx ruff check src/hyatlas_memory/client.py src/hyatlas_memory/server/start_server.py tests/test_client_defaults.py tests/test_integration.py
python -m compileall -q src/hyatlas_memory
python -m pytest tests/test_client_defaults.py tests/test_integration.py -q
python -m pytest -q
python -m hyatlas_memory.start status
curl -s http://127.0.0.1:8765/api/graph-counts
```

Results:

- Ruff: passed.
- Compileall: passed.
- Targeted tests: `6 passed`.
- Full tests: `49 passed, 6 warnings`.
- Stack healthy:
  - Qdrant `6333`: healthy, `agent_memories_1024`, 1024 dims.
  - Hy-Memory Server `19527`: healthy.
  - Dashboard `8765`: healthy.
- Graph counts stayed intact: `l5_knowledge=1208`, `l6_schema=460`, `l7_intention=146`, `total=1814`.
- Direct MiniMax digestion probe returned `error_code=null` and produced an L4 identity memory retrievable through legacy reader.

## Commit

`9da41fc Patch 29: restore write recall path`

## Next

Patch 30 should make `hyatlas status` detect half-green states explicitly:

- service healthy but collection suspiciously tiny
- graph counts healthy but vector memory nearly empty
- active config key source is JSON vs env fallback
- default reader choice and last write/recall smoke verdict
