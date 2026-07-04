# NOW — HyAtlas-Memory

- Runtime layout consolidation (Patches 24–31) is live on `main` as v2.1.0.
- All runtime state resolves under `~/.hyatlas`; migration helpers are available via `hyatlas migrate layout`.
- Active LLM is MiniMax-M3 via the official MiniMax API (`https://api.minimax.io/v1`).
- Multi-key LLM resilience is wired in `start_server.py`; currently falls back to `llm.api_key` (string) because `llm.api_keys` (list) is not yet populated in `hy_memory.json`.
- Maintainer cleanup done: `custom-provider-setup` missing reference fixed, duplicate `hermes-soul-authoring` skill archived.
- Legacy deprecation warnings are active in `hyatlas config show` and `hyatlas status`.
- Full test suite: 33 passed, 19 skipped.

Next:
1. Verify a fresh install from `pip install -e .` works from a clean directory and that `HYATLAS_HOME` defaults correctly on a non-Windows path.
2. Consider migrating `qdrant_bin` from `C:\qdrant\qdrant.exe` to `~/.hyatlas/qdrant/qdrant.exe` if the migrated binary exists.
3. Populate `llm.api_keys` if you want to exercise multi-key rotation.

Blocker: none.
