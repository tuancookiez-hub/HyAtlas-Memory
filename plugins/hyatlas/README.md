# hyatlas — HyAtlas v4 memory provider for Hermes Agent

Persistent 7-layer memory for Hermes backed by **HyAtlas v4**: a pure-Go,
single-binary, local-first memory server (Apache-2.0). Chromem-go embedded
vector store, in-process BGE-small embeddings via onnxruntime-go (no Python,
no GPU, no external embedding service), async LLM fact extraction, and a
bitemporal knowledge graph with source citations.

This plugin is the Hermes-side integration: a `MemoryProvider` implementation
(thin HTTP client), four agent tools, a `hermes hyatlas` CLI, a Desktop pane
with a starmap graph view, and a web dashboard.

## What you get

- **Cross-session memory** — conversation turns are persisted and
  LLM-extracted into facts/summaries/knowledge/intentions (7 layers, L1–L7)
- **Agent tools** — `hyatlas_status`, `hyatlas_search` (3-channel semantic),
  `hyatlas_recent`, `hyatlas_add`
- **Recall injection** — relevant memories are prefetched into context
  automatically each turn
- **Desktop pane** — Overview (health + layer bars), Memories, Search, Add,
  and a Graph tab with a starmap visualization of the knowledge layers
- **CLI** — `hermes hyatlas status|search|add|recent|start|stop`

## Install

The plugin requires the **HyAtlas v4 Go server** (`hyatlas-go`) running
locally. The server is *not* bundled and *not* downloaded by this plugin —
install it once from the project repository (see its README for the
one-line installer, or grab a release binary from
[Releases](https://github.com/tuancookiez-hub/HyAtlas-Memory/releases)):

```bash
hyatlas start        # starts hyatlas-go on 127.0.0.1:19528
```

Then install and enable the plugin:

```bash
hermes plugins install tuancookiez-hub/HyAtlas-Memory   # or from the catalog by name
hermes plugins enable hyatlas
```

And select it as the memory provider (`hermes memory setup` or in
`~/.hermes/config.yaml`):

```yaml
memory:
  provider: hyatlas
```

Settings (server host/port, user/agent id, auto-start, binary path, launcher
path, timeout) are editable in **Desktop → Settings → Plugins → hyatlas**, or
via `plugins.entries.hyatlas.settings` in `config.yaml`, or the
`HYATLAS_SERVER_HOST` / `HYATLAS_SERVER_PORT` / `HYATLAS_USER_ID` /
`HYATLAS_AGENT_ID` / `HYATLAS_AUTO_START` / `HYATLAS_BINARY_PATH` /
`HYATLAS_LAUNCHER_PATH` env vars (env wins). Defaults work out of the box for a
local server on port 19528. `launcher_path` is optional: point it at a
`hyatlas-go.ps1` if your install ships one beside the binary; otherwise
`start`/`stop` spawn the binary directly. The plugin does not configure the
server's LLM — `HYATLAS_LLM_*` are read from your environment or the server's
own config, never set by the plugin.

The Desktop pane loads automatically from this plugin's `desktop/plugin.js`
(the unified-package door) — no separate install step.

## Disclosure (what this plugin does at runtime)

- **Network calls:** all traffic goes to the HyAtlas server you configure
  (default `127.0.0.1:19528`). The plugin itself contacts nothing else. The
  *server* makes LLM API calls for fact extraction to the endpoint configured
  on the server side (`HYATLAS_LLM_*` env vars) — no LLM credentials live in
  or flow through this plugin.
- **Subprocess spawning:** only when you enable `auto_start` (default
  **off**); it then spawns the `hyatlas-go` binary you point it at.
- **Data written:** conversation turns and memories are stored by the server
  in its own data directory (default alongside the binary). Nothing is sent
  to the plugin author; there is no telemetry.
- **Reads outside its own data:** none.

## Requirements

- Hermes Agent >= 0.21.4
- HyAtlas v4 Go server (see Install above); Linux, macOS (arm64), Windows (amd64)
- No Python dependencies beyond Hermes core

## Links

- Server + full docs: https://github.com/tuancookiez-hub/HyAtlas-Memory
- License: Apache-2.0
