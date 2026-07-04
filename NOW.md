# NOW — HyAtlas-Memory

- Runtime layout consolidation (Patches 24–31) is live on `main` as v2.1.0.
- All runtime state resolves under `~/.hyatlas`; migration helpers are available via `hyatlas migrate layout`.
- Multi-key LLM resilience and legacy deprecation warnings are active.
- Full test suite: 33 passed, 19 skipped.

Next: verify a fresh install from `pip install -e .` works from a clean directory and that `HYATLAS_HOME` defaults correctly on a non-Windows path.

Blocker: none.
