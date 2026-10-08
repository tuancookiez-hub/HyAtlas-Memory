# hyatlas — HyAtlas v4 memory provider for Hermes Agent

Persistent 7-layer memory for Hermes, backed by **HyAtlas v4**: a pure-Go,
single-binary memory server (Apache-2.0) that runs on your machine. It has an
embedded vector store (chromem-go), in-process BGE-small embeddings (onnxruntime-go,
no Python, no GPU), LLM fact extraction with a selectable mode (`lite`, `pro`,
`ultra`; `lite` makes no LLM call), and a bitemporal knowledge graph with source
citations.

This plugin is the Hermes side: a `MemoryProvider` (a thin HTTP client), four
agent tools, a `/hyatlas` slash command, a `hermes hyatlas` CLI, a Desktop pane
with a graph view, and a web dashboard panel. The server is a separate binary,
`hyatlas-go`, and this plugin does not include it.

## What you get

- **Cross-session memory.** Each completed turn is saved to the server, which
  keeps the raw text (L2) and, in `pro` and `ultra`, extracts profile, fact,
  summary and intention layers (L1, L3, L4, L7), plus knowledge and schema layers
  (L5, L6) in `ultra` (see the mode table below).
- **Recall.** After each turn, Hermes queues the user's message (after stripping
  its own scaffolding and redacting it) as a background search query. The plugin
  asks the server for matching memories and makes them available to the next turn.
- **Agent tools.** `hyatlas_status`, `hyatlas_search` (three channels: profile,
  proactive, normal), `hyatlas_recent`, `hyatlas_add`.
- **Built-in `memory` tool.** An `add` through Hermes' `memory` tool is sent to
  the server as a plain add and stored as a raw memory (L2), tagged with its
  target in the metadata. It is not written to L1 Profile directly. L1 fills
  later, when extraction labels the memory as a user preference (`pro` and
  `ultra`). `replace` and `remove` are not mirrored.
- **Desktop pane.** Overview (health and layer counts), Graph, Memories, Search,
  Add. Its keyboard shortcut is `mod+shift+h`.
- **CLI and slash command.** `hermes hyatlas status|search|add|recent|start|stop`
  and `/hyatlas status|search <q>|add <text>|recent|start|stop`. The `hermes
  hyatlas` command appears only while `memory.provider` is `hyatlas`.

## Install

The plugin needs the **HyAtlas v4 server** (`hyatlas-go`) running on this machine.
Install the server from the project repository first. Its `scripts/install.sh`
(one-line installer, see the repository README) fetches a prebuilt binary and the
embedding model; see [Disclosure](#disclosure-what-this-plugin-does-on-your-machine)
for what that installer downloads. A release binary is an embedded build (about
160 MB) and already contains the model. A source build needs the model folder
(its search order is in the server README's Model assets section). You can also
build the server from source with Go 1.26 or newer.

Then install and enable the plugin:

```bash
# from the catalog, once the entry is merged:
hermes plugins install hyatlas

# or straight from the repository. The subdirectory must be part of the identifier:
hermes plugins install tuancookiez-hub/HyAtlas-Memory/plugins/hyatlas

hermes plugins enable hyatlas
```

Select it as the memory provider, either with `hermes memory setup` or in
`~/.hermes/config.yaml`:

```yaml
memory:
  provider: hyatlas
```

### Setup

`hermes memory setup` picks the provider, then prompts for each field in this
plugin's config schema (Enter keeps the default). It writes `memory.provider` to
`config.yaml`, the non-secret settings to `hyatlas.json`, and the key to `.env`.
It does not start the server. The fields that matter:

1. **Extraction mode**: `lite`, `pro` or `ultra`. The default is `ultra`.
2. **LLM endpoint**: any OpenAI-compatible base URL. Needed for `pro` and `ultra`.
3. **LLM API key**: entered masked and written to Hermes' `.env` (mode 0600) as
   `HYATLAS_LLM_KEY`.

`lite` needs neither 2 nor 3. In `lite` the server makes no LLM call at all.
The plugin never sets a default endpoint, so an unconfigured install has nowhere
to send memory text. The server's own install script suggests the Nous Portal
endpoint (`https://inference-api.nousresearch.com/v1`) as a starting value, and
you can overwrite it.

If you leave the endpoint empty, the server still runs with extraction off. Choose
`lite` in that case, or configure the endpoint later with `hermes memory setup`.
If `pro` or `ultra` has no endpoint, model or key, the server reports
`llm: "unconfigured"` in `/api/v1/status`, and writes return
`extraction_status: "unconfigured"` instead of failing silently. The raw trace is
still stored.

### Settings

| Key | Env var | Default | Purpose |
|---|---|---|---|
| `server_host` | `HYATLAS_SERVER_HOST` | `127.0.0.1` | Server host the plugin connects to |
| `server_port` | `HYATLAS_SERVER_PORT` | `19528` | Server port |
| `user_id` | `HYATLAS_USER_ID` | `default` | Default user scope for memories |
| `agent_id` | `HYATLAS_AGENT_ID` | `default` | Default agent scope (overridden per session) |
| `auto_start` | `HYATLAS_AUTO_START` | `false` | Start the server binary when it is unreachable |
| `binary_path` | `HYATLAS_BINARY_PATH` | *(discover)* | Path to `hyatlas-go` |
| `launcher_path` | `HYATLAS_LAUNCHER_PATH` | *(none)* | Windows only: a `hyatlas-go.ps1` that `hermes hyatlas start\|stop` runs instead of the binary |
| `request_timeout` | `HYATLAS_REQUEST_TIMEOUT` | `15.0` | HTTP timeout for server calls, in seconds |
| `data_dir` | `HYATLAS_GO_DATA` | *(conventional)* | Server data directory. Also used by `hermes backup` |
| `llm_base` | `HYATLAS_LLM_BASE` | *(none)* | Base URL of an OpenAI-compatible extraction endpoint |
| `llm_model` | `HYATLAS_LLM_MODEL` | *(none)* | Model id at that endpoint |
| `llm_key` | `HYATLAS_LLM_KEY` | *(none)* | **Secret.** Stored in `.env` (0600), never in `hyatlas.json` |
| `mode` | `HYATLAS_MODE` | *(server default: ultra)* | Extraction mode: `lite` \| `pro` \| `ultra` |
| `sync` | `HYATLAS_SYNC_EXTRACT` | `off` for a spawned server | Whether a write waits for extraction: `on` \| `off` |

All fourteen are editable in **Desktop → Settings → Plugins → hyatlas**, in
`plugins.entries.hyatlas.settings` in `config.yaml`, or through the environment
variables above (an environment variable wins). Empty or unset values mean the
default. `llm_base` and `llm_model` are forwarded to a server the plugin starts,
and so is `mode`. The plugin does not set `HYATLAS_LLM_KEY`; the server reads
that from its environment (see Disclosure).

The server accepts more `HYATLAS_*` variables than these (for example
`HYATLAS_EMBED_BASE`, `HYATLAS_CONSOLIDATE_EVERY`, `HYATLAS_RAW_RETENTION`). The
plugin passes any of them through to a server it starts, and the server's own
documentation describes them.

### Mode

| `HYATLAS_MODE` | LLM calls | Reasoning scope | Layers filled | Periodic consolidation |
|---|---|---|---|---|
| `lite` | none | none | **1 / 7**: L2 raw | no |
| `pro` | one per write | within that one turn | **5 / 7**: L1, L2, L3, L4, L7 | no |
| `ultra` *(default)* | one per write, plus a periodic batch | across memories and time | **7 / 7** | **yes**, every 6 hours |

L5 (knowledge) and L6 (schema) come only from the consolidation pass, because a
relation needs corroboration from more than one turn and a schema is a pattern
across many turns. So only `ultra` fills them.

Whether a write waits for extraction is a separate setting, `HYATLAS_SYNC_EXTRACT`.
A server the plugin spawns defaults to `off`, so a Hermes turn never waits on
the LLM; `pro` still extracts every write, just behind the response. With
`sync: on`, a write waits; the plugin's request timeout (`request_timeout`,
15 s) still applies on the plugin side. A server you start yourself follows its
own default (`pro` waits, `ultra` does not).

### Updating

The plugin never updates itself. A catalog entry pins one exact commit, and that
pin is the trust model: a self-updater would let an installed copy move to a
commit nobody reviewed. Updates arrive when a re-pin lands in the catalog, and
you apply them deliberately:

```bash
hermes plugins check-updates          # read-only: is anything newer available?
hermes plugins update hyatlas         # apply it
```

Restart the gateway afterwards so the new code loads. The Desktop pane's version
badge reads the version from the running server's `/api/v1/status`, so a stale
server shows up as a stale badge.

Installing straight from the repository works the same way
(`hermes plugins update hyatlas`).

**The server binary is separate.** `hermes plugins update hyatlas` updates the
plugin only. It does not change `hyatlas-go`. Re-run the server's installer, or
replace the binary with a newer release asset, when you want to move the server.

### Requirements

- Hermes Agent `>= 0.21.4`
- The HyAtlas v4 server, prebuilt for Linux, macOS and Windows on amd64 or arm64,
  or built from source with Go 1.26 or newer
- No Python dependencies beyond Hermes core

## Disclosure: what this plugin does on your machine

Every statement below was checked against the plugin source and the server
source at the pinned commit.

### Network

- **Plugin to server.** All plugin traffic goes to the server at
  `server_host:server_port` (default `127.0.0.1:19528`), over plain HTTP. The plugin
  contacts no other host. After each turn, Hermes queues the user's message (stripped
  of scaffolding and redacted) as a background search query to the server. Hermes also calls `sync_turn`
  after each turn, which posts that turn to the server to be stored. Each
  `hyatlas_*` tool call is also a request to the server. The Desktop pane polls
  `/status` every 8 seconds while it is open.
- **Server to an LLM endpoint.** The server calls an LLM only when `HYATLAS_LLM_BASE`,
  `HYATLAS_LLM_MODEL` and `HYATLAS_LLM_KEY` are all set, and only in `pro` or `ultra`.
  In `lite`, no LLM call is made.
  - In `pro` and `ultra`, each write's text is sent to that endpoint for extraction.
    The plugin sends the current turn. If it missed earlier messages in the same
    session, it includes those too.
  - In `ultra` only, by default every 6 hours the server sends one consolidation
    request to the same endpoint. It includes up to 200 stored facts, each cut to 400 characters.
    These are facts already in memory, not just the current turn.
  - There is no default endpoint. The plugin and the server both leave it empty,
    and tests in the plugin check that no endpoint is hard-coded.
- **Server to an embedding endpoint.** By default embeddings are computed locally
  by the bundled BGE-small model. If `HYATLAS_EMBED_BASE` is set to an
  OpenAI-compatible URL, the server sends each memory's text there to embed it.
  This also applies in `lite`. The plugin has no setting for this variable and
  does not set it. It reaches the server only if you export it.
- **Server's web page.** If the server's built-in dashboard is served (at
  `/dashboard/` on the server), its page loads a font from `fonts.googleapis.com`
  and two scripts from `cdn.jsdelivr.net` in your browser when you open it. The
  Desktop pane loads nothing from the network.
- **No telemetry.** The plugin has no analytics or usage reporting. The server's
  only outbound HTTP calls are the LLM and embedding requests listed above.

### Background process

- Only when `auto_start` is `true` (default `false`), or when you run
  `hermes hyatlas start`, the plugin starts the `hyatlas-go` binary as a child
  process.
- Binary lookup, in order: the `binary_path` setting; `hyatlas-go` on `PATH`; a
  `bin/` folder next to the plugin; `/usr/local/bin/hyatlas-go`;
  `/opt/hyatlas/hyatlas-go`; `~/hyatlas/hyatlas-go`; and on Windows
  `C:/hyatlas/hyatlas-go.exe` and `C:/Program Files/hyatlas/hyatlas-go.exe`.
- `start` spawns nothing when a server already answers at `server_host:server_port`.
  The result is `already_running: true`, with the existing pid when `hyatlas.pid`
  names a live `hyatlas-go`. Nothing is written over a live pid.
- The server's working directory is the binary's folder. Its stdin is closed. Its
  stdout and stderr are appended to `$HERMES_HOME/logs/hyatlas.log`
  (`HERMES_HOME` defaults to `~/.hermes`). Its PID is written to
  `$HERMES_HOME/logs/hyatlas.pid` only once the child is seen alive. A child that
  dies during startup (for example because the port is taken) is reported as
  `ok: false` and leaves no pidfile.
- The plugin does not stop the server when Hermes exits. Stop it with
  `hermes hyatlas stop`, which sends SIGTERM to the PID in `hyatlas.pid` (on Windows
  it uses `taskkill`), but only if that PID is still a `hyatlas-go` process. It
  then checks that the server stopped answering. The result is `ok: true` with
  `stopped: true` only when that is so.
- If the server answers but there is no usable pidfile, `stop` returns `ok: false`
  with `running: true`: the server was not started by this plugin, its pid is
  unknown, and the plugin will not guess one. Stop it from the process that started
  it. With nothing running, `stop` returns `ok: true` with `running: false`.
- The auto-start wait is bounded. The plugin waits up to 30 seconds (plus at most one
  request timeout for the last probe), then logs a warning and carries on with the
  server unreachable. Auto-start is skipped in cron and flush contexts.

### Shell commands

The plugin runs no shell commands except one case. On Windows, if `launcher_path`
is set, `hermes hyatlas start` and `hermes hyatlas stop` run that script with
`powershell -NoProfile -ExecutionPolicy Bypass -File <script> start|stop`, and its
output goes to `hyatlas-launcher.out` in the system temp folder. The script is
never discovered automatically. Only set `launcher_path` to a script you trust.
The repository's `hyatlas-go.ps1` reads Hermes' `auth.json` to obtain a Nous
Portal token (see Credentials). Do not point `launcher_path` at it unless you
want that.

### Files

Read:

- `$HERMES_HOME/hyatlas.json`: settings saved by the setup form.
- `$HERMES_HOME/config.yaml`: the `plugins.hyatlas` block and the
  `plugins.entries.hyatlas.settings` block.
- `HYATLAS_*` environment variables.
- The data directory, only to check that it exists, for `hermes backup`.

The plugin does not open `.env`, `auth.json`, any other tool's token store, or a
browser profile.

Written:

- `$HERMES_HOME/hyatlas.json`, mode 0600, when the setup form saves settings.
- `$HERMES_HOME/logs/hyatlas.log` and `hyatlas.pid`, when the plugin starts the server.
- `hyatlas-launcher.out` in the system temp folder, only with a Windows `launcher_path`.

The server writes its memory store, including the vector store and `graph.json`,
to its data directory (`HYATLAS_GO_DATA`). The default is `./data`, relative to
the server's working directory, which is the binary's folder when the plugin
starts it.

### Credentials

- **`llm_key`** is declared secret. The setup wizard writes it to Hermes' `.env`
  as `HYATLAS_LLM_KEY`, with mode 0600. The plugin never reads it from config,
  never writes it to `hyatlas.json` (`save_config` strips it), and never logs it.
- **Environment passed to a started server.** It receives a short list of OS
  variables (such as `PATH`, `HOME`, TLS trust paths), every `HYATLAS_*` variable from
  your environment, and `HYATLAS_LLM_BASE`, `HYATLAS_LLM_MODEL`, `HYATLAS_GO_HOST`,
  `HYATLAS_GO_PORT`, `HYATLAS_GO_DATA`, `HYATLAS_MODE` and `HYATLAS_SYNC_EXTRACT` from
  settings where you have not exported a value yourself. It does not receive other
  tools' keys (for example `OPENAI_API_KEY`). A `HYATLAS_LLM_KEY` exported in your
  environment does reach it.
- **`HYATLAS_LLM_KEY_FILE`** is a server feature. If you export it, the server reads
  the bearer token from that file on every call. The plugin has no setting for it,
  but it passes the variable through. If you point it at another tool's credential
  file, the server will use that tool's login.
- The plugin does not read another tool's login, token store or browser profile.

### Listening address

A started server listens on `HYATLAS_GO_HOST`:`HYATLAS_GO_PORT`. Unless you export
those yourself, the plugin sets them from `server_host` and `server_port`, which
default to `127.0.0.1:19528`. Keep it on loopback unless you have a reason not to.

## Links

- Server and full documentation: https://github.com/tuancookiez-hub/HyAtlas-Memory
- License: Apache-2.0
