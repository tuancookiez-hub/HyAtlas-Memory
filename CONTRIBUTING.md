# Contributing to HyAtlas-Memory

> Thanks for your interest in contributing! This is a community implementation of the official [Hy-Memory framework](https://memory.hunyuan.tencent.com) by Tencent Hunyuan. All contributions are welcome — bug reports, docs, code, tests, and design feedback.

> **Local dev (2026+):** use `hyatlas start` with **zvec** (`vector_store.provider: zvec`). Qdrant steps below are **legacy/migration** only. See `docs/CLEANUP.md`.

## Code of conduct

Be respectful. We're all here to build something useful. Disagreements are fine; personal attacks are not. Assume good faith. When in doubt, follow the [Contributor Covenant](https://www.contributor-covenant.org/).

## What to work on

The easiest ways to help, ordered roughly by impact:

1. **Use it and report bugs** — Install, run, hit an edge case, open an issue with reproduction steps
2. **Improve docs** — If something's unclear, fix it. PRs to `/docs` and `/README.md` are always welcome
3. **Add tests** — The `tests/` directory could use more coverage
4. **Add a new memory layer operation** — See "Adding a new feature" below
5. **Improve the dashboard** — `server/dashboard/dashboard.html` is a single file; refactors welcome
6. **Port a patch from upstream** — If a newer Hy-Memory SDK fixes something we patch around, port the patch

## Reporting bugs

Use [GitHub Issues](https://github.com/tuancookiez-hub/HyAtlas-Memory/issues). Include:

- **What you did** (exact commands, in order)
- **What you expected**
- **What happened** (full error message, screenshot if visual)
- **Environment:**
  - OS + version
  - Python version (`python --version`)
  - Hermes Agent version (`hermes --version`)
  - Package version (`pip show hyatlas-memory`)

For dashboard bugs, also include the browser + version.

## Suggesting features

Open an issue with the `enhancement` label. Describe:
- The use case (what you're trying to do)
- The proposed solution (how it should work)
- Alternatives you considered

If it's a large change (new layer, new store, breaking change), start a discussion first.

## Development setup

### Prerequisites

- Python 3.10+ (3.11+ recommended)
- [uv](https://github.com/astral-sh/uv) (fast pip alternative) or pip
- [Qdrant](https://qdrant.tech/) running locally (Docker, native, or via the project's scripts)
- [Hermes Agent](https://github.com/NousResearch/hermes-agent) installed and on PATH
- Git

### Clone and install

```bash
git clone https://github.com/tuancookiez-hub/HyAtlas-Memory.git
cd HyAtlas-Memory

# Editable install with dev + test extras
uv pip install -e ".[dev,test]"
# or:
pip install -e ".[dev,test]"
```

### Start Qdrant

```bash
# Docker (easiest)
docker run -d -p 6333:6333 -p 6334:6334 \
  -v $(pwd)/qdrant_data:/qdrant/storage \
  --name hyatlas-qdrant \
  qdrant/qdrant

# Or use the project script (if present)
python scripts/start_qdrant.py
```

Verify:
```bash
curl http://127.0.0.1:6333/collections
# Should return: {"result":{"collections":[]}}
```

### Run tests

```bash
# All tests
pytest

# With coverage
pytest --cov=hyatlas_memory --cov-report=term-missing

# Integration tests only (need a running server)
pytest -m integration

# Specific test
pytest tests/test_hy_memory_search.py::test_search_basic -v
```

### Run the linter

```bash
ruff check src/ tests/
ruff format src/ tests/   # auto-format
```

### Run the dashboard

```bash
# In one terminal
python -m server.start_server

# In another
python server/dashboard/dashboard.py
# Open http://127.0.0.1:8765
```

### Run the L5 pipeline

```bash
# This rebuilds the Kuzu graph from scratch — takes minutes
python server/bin/l5_full_pipeline.py
```

## Project structure

```
HyAtlas-Memory/
├── src/hyatlas_memory/    # the plugin (Python package)
│   ├── __init__.py        # HyMemoryProvider — entry point
│   ├── client.py          # HTTP client to upstream
│   ├── patches.py         # 9 SDK patches (applied at import)
│   ├── context_pressure.py # 4-tier token budget monitor
│   ├── process.py         # subprocess lifecycle
│   ├── embed_server.py    # local embedder
│   ├── init_wizard.py     # first-run setup
│   ├── installer.py       # pip-deps installer
│   ├── cli.py             # `hermes hy-memory ...` subcommands
│   └── plugin.yaml        # legacy manifest
│
├── server/                 # standalone server
│   ├── start_server.py    # uvicorn launcher
│   ├── bin/               # L5 pipeline scripts
│   └── dashboard/         # web UI (port 8765)
│
├── tests/                  # pytest suite
├── docs/                   # architecture + API + troubleshooting
├── assets/                 # screenshots, infographics
├── examples/               # usage examples
└── scripts/                # dev scripts (smoke_test, etc.)
```

## Coding style

### Python

- **Type hints** on all public functions
- **Docstrings** on all public modules, classes, functions
- **No `any`** in type hints — use `Any` if truly needed, but prefer specific types
- **Imports**: stdlib first, then third-party, then local; alphabetized within each group
- **Line length**: 100 chars (enforced by ruff)
- **Quotes**: double quotes for strings, single for short dict keys
- **Naming**: snake_case for functions/vars, PascalCase for classes, UPPER_SNAKE for constants

### JavaScript (dashboard)

- **No frameworks** — vanilla JS + Three.js, keep it that way unless absolutely necessary
- **No build step** — the HTML is served as-is, you must be able to edit and refresh
- **ES2020+** syntax is fine (the dashboard runs in modern browsers only)
- **DOM access**: cache `getElementById` results, don't query in hot loops
- **Event handlers**: clean up on `pagehide` / `beforeunload` to avoid memory leaks

### Markdown

- **One sentence per line** in source (wraps better in diffs)
- **Code blocks** with language tags (`bash`, `python`, `json`, etc.)
- **Links** relative within the repo (`./docs/...`), absolute for external (`https://...`)
- **Headings**: title case, no trailing period

## Testing

### When to add a test

- New public function in `src/hyatlas_memory/` → add a unit test in `tests/`
- New API endpoint in `server/dashboard/dashboard.py` → add an integration test
- Bug fix → add a regression test that fails before your fix
- New CLI subcommand → add a test for the subcommand

### Test conventions

- **Test names**: `test_<unit_being_tested>_<scenario>` — e.g., `test_search_with_empty_query_returns_empty`
- **Use fixtures** for shared setup, not module-level globals
- **Mock external calls** (Qdrant, upstream server) — tests should run without those running
- **One assertion per test** when possible (multiple asserts are OK if testing one behavior)
- **Parametrize** for similar tests with different inputs

Example:
```python
import pytest
from hyatlas_memory import search

def test_search_returns_results_above_threshold():
    # ...
    results = search("TypeScript", min_score=0.5)
    assert all(r.score >= 0.5 for r in results)

@pytest.mark.parametrize("query,expected_empty", [
    ("", True),
    ("   ", True),
    ("!@#$", False),  # gibberish might match noise
])
def test_search_empty_query(query, expected_empty):
    results = search(query)
    assert (len(results) == 0) == expected_empty
```

## Adding a new feature

### Adding a new memory layer operation

1. Add the function in `src/hyatlas_memory/__init__.py` (or a new module)
2. Add the corresponding HTTP endpoint in `server/dashboard/dashboard.py` if it needs dashboard support
3. Add a test in `tests/`
4. Update `docs/LAYERS.md` if it changes layer semantics
5. Update the dashboard HTML if it needs UI representation

### Adding a new API endpoint

1. Add a new `if path == "/api/your-endpoint":` branch in `do_GET` / `do_POST`
2. Use `self._json(status, payload)` to send responses
3. Document it in `docs/API.md`
4. Add an integration test in `tests/test_dashboard_api.py` (create if missing)

### Adding a new dashboard page

1. Add a `<div class="page-section" id="page-yourpage">...</div>` to the HTML
2. Add a nav item in the sidebar
3. Add a `renderYourPage()` function called from `renderAll()`
4. Document it in `docs/DASHBOARD.md`

### Adding a new patch

Patches fix upstream SDK issues. If you're patching around something in the SDK:

1. Add the patch to `src/hyatlas_memory/patches.py`
2. Make it idempotent (safe to apply multiple times)
3. Add a comment explaining **what** it fixes and **why** (with a link to the issue if there is one)
4. Register it in the `patches = [...]` list at the bottom of the file
5. Add a test that verifies the patch is applied (or at least doesn't error)

## Pull request process

1. **Fork the repo** and create a branch:
   ```bash
   git checkout -b fix/issue-123-search-typo
   # or
   git checkout -b feature/new-layer
   ```

2. **Make your changes** in small, focused commits:
   ```bash
   git add -p   # stage hunks, not whole files
   git commit -m "fix(search): handle empty query without crashing

   Empty queries were throwing AttributeError on .split() because the
   upstream SDK doesn't guard against them. Added a check.

   Closes #123"
   ```

3. **Run tests + linter locally:**
   ```bash
   pytest
   ruff check src/ tests/
   ```

4. **Push and open a PR:**
   ```bash
   git push origin your-branch
   gh pr create --fill   # or use the GitHub web UI
   ```

5. **PR description should include:**
   - What changed and why
   - How to test
   - Screenshots/recordings for UI changes
   - Linked issues (`Closes #123`)

6. **Wait for review.** The maintainers will respond within a few days. Be patient — this is a hobby project.

### Commit message style

Use [Conventional Commits](https://www.conventionalcommits.org/):
- `feat:` — new feature
- `fix:` — bug fix
- `docs:` — docs only
- `refactor:` — code change that doesn't add a feature or fix a bug
- `test:` — add or fix tests
- `chore:` — maintenance (deps, CI, etc.)

Scope is optional but helpful: `fix(dashboard): handle empty layer counts`

### After your PR is merged

- Your contribution will be in the next release's [CHANGELOG.md](CHANGELOG.md)
- You'll be added to the contributors list (or the GitHub auto-generated one)

## Release process

The maintainer (currently [@tuancookiez-hub](https://github.com/tuancookiez-hub)) cuts releases:

1. Bump version in `pyproject.toml` and `src/hyatlas_memory/_version.py`
2. Update `CHANGELOG.md` with the release notes
3. Tag the commit: `git tag -a v0.X.0 -m "Release 0.X.0"`
4. Push the tag: `git push origin v0.X.0`
5. Build and publish to PyPI: `uv build && uv publish`

## License

By contributing, you agree that your contributions will be licensed under the [Apache License, Version 2.0](../LICENSE).
