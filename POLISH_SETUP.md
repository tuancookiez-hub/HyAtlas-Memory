# HyAtlas Memory System — Plugin Polish Setup

*Generated 2026-09-05 · discovery only, no code modified. Updated 2026-10-08 for the paths, version and env names that changed since; the backlog line numbers below are from the 2026-09-05 snapshot and were not re-verified.*

## 1. Plugin Overview

**The plugin is `plugins/hyatlas/`** (formerly `plugins/hy_memory/`) — a pure-Python Hermes memory-provider plugin plus desktop pane. Everything else in the repo is the **Go backend** it talks to (one module, `github.com/tuancookiez-hub/hyatlas-v4`, go 1.26.5, deps: `chromem-go v0.7.0`, `yalue/onnxruntime_go v1.32.0`): `server.go`, `store.go`, `llm.go`, `graph/`, `memory/`, `bge/`, `dashboard/`. The plugin wraps that server; it is not Go itself.

| Field | Value |
|---|---|
| Name / version | `hyatlas` / 4.3.3 (`plugin.yaml`, mirrored in `dashboard/manifest.json` and `self._version` in `__init__.py`). The 2026-09-05 snapshot said `hy_memory` / 4.0.1 |
| Author / license | Tuna / Apache-2.0 |
| Backend | HyAtlas v4 Go binary at `127.0.0.1:19528` (wire-compatible with v3.5 on 19527) |
| Hooks | Provider methods `on_session_end`, `on_pre_compress`, `on_memory_write` (in `__init__.py`; `plugin.yaml` no longer lists hooks) |
| Tools | `hyatlas_status`, `hyatlas_search`, `hyatlas_recent`, `hyatlas_add` (schemas in `schemas.py`) |

**What it currently does:**

- **Provider lifecycle & hooks** (`__init__.py`, 684 lines): `HyatlasMemoryProvider(MemoryProvider)` resolves identity (`user_id`/`agent_id`) from env → kwargs → config, exposes a static system-prompt block, runs background prefetch on a daemon thread, persists each turn via `sync_turn`, and implements the `on_pre_compress` / `on_memory_write` / `on_session_end` hooks. `register(ctx)` (`__init__.py:605`) hands the provider to Hermes and registers a `/hyatlas` slash command.
- **HTTP client** (`client.py`, 213 lines): stdlib-urllib client over the server's `/healthz` + `/api/v1/{status,add,search,list,delete_all,graph,metrics,digest,reprocess}` — verified 1:1 against the route table in `server.go:727-739`. Typed errors: `HyatlasUnreachable` ⊂ `HyatlasClientError`.
- **Subprocess lifecycle** (`process.py`, 180 lines): optional `auto_start` spawns the `hyatlas-go` binary (PATH → plugin `bin/` → well-known dirs), logs to `~/.hermes/logs/hyatlas.log`, forwards `HYATLAS_LLM_*` env (`process.py:93-98`).
- **CLI** (`cli.py` + `__main__.py`): `hermes hyatlas status|search|add|recent|start|stop` subcommands printing JSON.
- **Desktop pane** (`dashboard/plugin_api.py`, `desktop/plugin.js`): FastAPI proxy at `/api/plugins/hy_memory/*` + React page at `/hyatlas` with Overview / Memories / Search / Add tabs (repo copy; the deployed copy has more, see backlog #9).

**Inputs:** `$HERMES_HOME/hy_memory.json` + `HYATLAS_*` env vars + Hermes `ctx` kwargs; JSON bodies from the Go server.
**Outputs:** 4 agent tools, prompt block, `<relevant-memories>` prefetch block, CLI JSON on stdout, desktop HTTP routes.

**Current state:** Functional and consistent with the Go server's surface; tests exist in `tests/` (`test_plugin_unit.py` for pytest, which CI runs, and the hand-rolled `test_smoke.py`, which is partly live-server-dependent). Two structural drifts found: repo `desktop/plugin.js` lags the deployed one, and version metadata disagrees across files (backlog below).

## 2. Polish Backlog

All items cite real code. Ordered by severity within each group.

### A. Functional gaps

1. **`hyatlas stop` is a silent no-op.** `HyatlasProcess.start()` (`process.py:73-119`) never writes a PID file, but the only kill path, `stop_running()` (`process.py:161-181`), reads `LOG_DIR/"hyatlas.pid"` — and nothing in the repo ever writes that file (grep: sole reference is the reader at `process.py:164`). `hermes hyatlas stop` / `/hyatlas stop` can never kill the server it started.
2. **Dead variable + no-op ternary in `on_memory_write`** — `__init__.py:409`: `layer = "l1_profile" if target == "user" else "l1_profile"` (both branches identical) and `layer` is never used; the subsequent `client.add()` never receives a layer, so the docstring's "user vs project preserved in metadata" promise is only half-kept.
3. **Unreachable-error parser can crash** — `client.py:57-61`: if the HTTP error body is valid JSON but not an object (array/string), `payload.get('error', payload)` raises `AttributeError` inside the exception handler.
4. **`HyatlasProcess.stop()` (`process.py:121-147`) has no callers** — `shutdown()` deliberately leaves the server running, and the CLI uses `stop_running()` instead. Wire it up or remove it.

### B. Config / consistency

5. **Env-var naming drift** — `dashboard/plugin_api.py:22-23` reads `HYATLAS_HOST` / `HYATLAS_PORT`; the canonical names everywhere else (plugin.yaml `requires_env`, `after-install.md:60-61`, `_load_config` at `__init__.py:93-101`) are `HYATLAS_SERVER_HOST` / `HYATLAS_SERVER_PORT`. Setting the documented var does nothing for the dashboard proxy.
6. **Undeclared dependency** — `plugin.yaml:7` says `pip_dependencies: []`, but `dashboard/plugin_api.py:18` imports `fastapi` (also `uvicorn`-hosted by the desktop backend).
7. **False `requires_env`** — `plugin.yaml:8-10` requires `HYATLAS_SERVER_HOST`/`PORT`, yet both default to `127.0.0.1:19528` (`__init__.py:65-66`); they're optional in practice.
8. **Version drift** — plugin metadata says 4.0.1 (`plugin.yaml`, `manifest.json`, `__init__.py:148`), `NOW.md:3` says v4.1.1, and the CHANGELOG's latest entry is `[4.0.1] — 2026-09-02`.
9. **Repo desktop plugin is stale vs deployed** — repo `desktop/plugin.js` is 429 lines with 4 tabs and an `api` module global (`plugin.js:28`), while the deployed copy at `%LOCALAPPDATA%\hermes\desktop-plugins\hy_memory\plugin.js` is 1503 lines (Graph tab, `rest` closure — per `NOW.md:14`). Reinstalling from the repo per `README.md` would downgrade the pane.
10. **User-Agent version** — `client.py:46` sends `hermes-hy_memory/4.0` vs 4.0.1 elsewhere (cosmetic).

### C. Dead code / idiom

11. **`ALL_SCHEMAS` unused** (`schemas.py:133-138`) — `__init__.py:43-48` imports the four constants individually.
12. **Four client methods unreferenced** — `graph()`, `metrics()`, `digest()`, `reprocess()` (`client.py:193-213`) have no callers in the plugin (the dashboard proxies those endpoints directly); `delete_all()` is used only by smoke-test cleanup (`tests/test_smoke.py:105`). Keep-if-wire-parity, but undocumented.
13. **CLI provider churn + odd imports** — `cli.py:16` does `from . import __init__ as plugin_root` (self-import of the package's own `__init__`); every `_cmd_*` builds a fresh `HyatlasMemoryProvider`, and `_cmd_search` builds two (`cli.py:56` via `_client_from_args`, then `cli.py:81`). `cli.py:139`'s walrus `if client := _client_from_args(args):` is always truthy.
14. **One-element tuple loop** — `__init__.py:80-82` iterates `for json_path in (Path(...),)` — leftover from a multi-path design.
15. **`import time` inside function** — `client.py:104` (inside `wait_until_reachable`).
16. **Docstring drift** — `schemas.py:1-6` claims tools are registered "via `ctx.register_tool` in the plugin's `register()`", but `register()` (`__init__.py:605-627`) only calls `register_memory_provider` + `register_command`; schemas surface through `get_tool_schemas()`.
17. **Unused parameter** — `prefetch(self, query, ...)` (`__init__.py:310-313`) ignores `query` by design (returns the cached result); worth a doc note, not a bug.

### D. Tests / CI

18. **Thin test coverage** — only `tests/test_smoke.py`, a hand-rolled runner (not pytest), where 2 of 3 tests are offline and the round-trip test silently skips without a live server. Pure helpers with real logic — `_build_turn_text`, `_format_prefetch`, `_load_config` priority order (`__init__.py:526-581, 56-115`) — are untested.
19. **CI is Go-only** — `.github/workflows/tests.yml` runs `go vet` / `go test` / CodeQL; the Python plugin has no lint or test job.

## 3. Agent Setup Needs

**Unit of work:** `plugins/hyatlas/` only. Do not edit Go files for plugin polish; do not edit `data/` (live server state).

**Files to edit, by role:**

| Path | Role |
|---|---|
| `plugins/hyatlas/__init__.py` | Provider, hooks, config loading, slash command |
| `plugins/hyatlas/client.py` | HTTP wire contract to the Go server |
| `plugins/hyatlas/process.py` | Binary discovery, spawn/stop, PID file |
| `plugins/hyatlas/cli.py` | `hermes hyatlas` subcommands |
| `plugins/hyatlas/dashboard/plugin_api.py` | FastAPI proxy for the desktop pane |
| `plugins/hyatlas/desktop/plugin.js` | Desktop pane (stale vs deployed — see backlog #9) |
| `plugins/hyatlas/plugin.yaml` | Manifest (deps, env, version) |

**Build / run / verify (Windows, repo root `F:\HyAtlas-Memory-Go`):**

```bash
go build ./...          # compile backend
go vet ./...            # lint (CI gate)
go test ./...           # backend tests
go build -o hyatlas-go.exe .               # plain build (~17 MB, reads ./models/)
go build -tags embedded -o hyatlas-go.exe . # embedded model
./hyatlas-go.exe        # serves 127.0.0.1:19528
```

Plugin tests (CI runs the pytest suite; `test_smoke.py` needs a live server for its round-trip test):

```bash
python -m pytest plugins/hyatlas/tests
python plugins/hyatlas/tests/test_smoke.py
```

**Environment variables:**

- Plugin config (read from the environment, which wins over `hyatlas.json` and `config.yaml`): `HYATLAS_SERVER_HOST` (default 127.0.0.1), `HYATLAS_SERVER_PORT` (19528), `HYATLAS_USER_ID`, `HYATLAS_AGENT_ID`, `HYATLAS_AUTO_START`, `HYATLAS_BINARY_PATH`, `HYATLAS_LAUNCHER_PATH`, `HYATLAS_REQUEST_TIMEOUT`, `HYATLAS_GO_DATA`, `HYATLAS_MODE`, `HYATLAS_SYNC_EXTRACT`, `HYATLAS_LLM_BASE`, `HYATLAS_LLM_MODEL`, plus `HERMES_HOME` (config file lives at `$HERMES_HOME/hy_memory.json` in the 2026-09-05 snapshot; re-check the current path in `settings.py`).
- Forwarded by `process.py` to a spawned server: `HYATLAS_*` from the environment (allowlist rule), plus `HYATLAS_GO_HOST`, `HYATLAS_GO_PORT`, `HYATLAS_GO_DATA`, `HYATLAS_MODE`, `HYATLAS_SYNC_EXTRACT`, `HYATLAS_LLM_BASE` and `HYATLAS_LLM_MODEL` when set in config. The LLM key is never forwarded from config; it reaches the server only if exported as `HYATLAS_LLM_KEY`. The old `AI2API_KEY` mapping is gone.
- Dashboard proxy reads: `HYATLAS_HOST` / `HYATLAS_PORT` (naming drift, backlog #5; still present in `dashboard/plugin_api.py`).

**Most relevant sources to start from:** `plugins/hyatlas/__init__.py` → `plugins/hyatlas/client.py` → `plugins/hyatlas/process.py`. For the wire contract cross-check, use the `mux.HandleFunc` calls in `server.go` `main()`; for intent/roadmap, read `NOW.md` and `plugins/hyatlas/after-install.md`.

**Gotchas for the agent:** the deployed desktop plugin is newer than the repo copy — sync before editing `plugin.js`; Python here is stdlib-first (urllib, no requirements file), with `fastapi` needed only for `dashboard/plugin_api.py`; the repo is a git repo on branch state independent of the parent workspace.
