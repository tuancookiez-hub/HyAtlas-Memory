# After install

Install and enable the plugin, then restart the gateway so it loads:

```bash
hermes plugins install hyatlas     # or: hermes plugins install tuancookiez-hub/HyAtlas-Memory/plugins/hyatlas
hermes plugins enable hyatlas
hermes memory setup                # choose "hyatlas", then answer its prompts (see below)
```

## What `hermes memory setup` does

1. Shows the memory providers; pick `hyatlas`. Hermes installs the plugin's
   dependencies (there are none beyond Hermes core).
2. Prompts for each of the plugin's settings in turn. Press Enter to keep the
   default. The ones that matter are the extraction `mode` (`lite`, `pro` or
   `ultra`), `llm_base` and `llm_model` (any OpenAI-compatible endpoint), and
   `llm_key`. The key is entered masked and written to Hermes' `.env` as
   `HYATLAS_LLM_KEY`. The server host and port, user and agent ids, and the
   auto-start options are also asked; their defaults work.
3. Writes `memory.provider: hyatlas` to `config.yaml`, the non-secret settings to
   `$HERMES_HOME/hyatlas.json`, and the key to `.env`.

It does not start the server and does not call the LLM. Start a new session to
activate.

**Without an LLM endpoint** the server still runs, with extraction off. Writes
store the raw trace and local embeddings, `hermes hyatlas status` reports `llm`
as `unconfigured`, and each write returns `extraction_status: "unconfigured"`.
Either choose `lite` at the mode prompt (no LLM call at all), or enter an endpoint,
model and key. The plugin never invents an endpoint.

The `hermes hyatlas` command appears only while `memory.provider` is `hyatlas`.

Then check it:

```bash
# 1. The provider is selected
hermes memory status

# 2. The server answers (start it first if it does not, see below)
hermes hyatlas status

# 3. A round trip
hermes hyatlas add "I prefer short answers"
hermes hyatlas search "answer style"
```

`status` prints the server's JSON, including `version`, `mode`, and `llm`
(`ok`, `unconfigured` or `unused`). The `search` results are grouped into the
`profile`, `proactive` and `normal` channels.

## Start the server

The plugin does not install the server. Either run the binary yourself:

```bash
hyatlas-go          # listens on 127.0.0.1:19528 by default
```

A release binary is an embedded build and already contains the BGE model. A
source-built binary needs the model folder: it looks in `HYATLAS_MODEL_DIR` if
set, otherwise in `<cwd>/models`, then `<exe dir>/models`, then the installer's
default `~/.hyatlas/models` (Windows `%LOCALAPPDATA%\hyatlas\models`).

or let the plugin start it:

```bash
hermes hyatlas start    # spawns the binary (see binary_path), logs to ~/.hermes/logs/hyatlas.log
hermes hyatlas stop     # stops a server the plugin started
```

`start` spawns nothing if a server already answers at the configured host and
port. It reports `already_running` with the existing pid when the pidfile names
a live `hyatlas-go`, and `pid_known: false` when the server was started by hand.
`stop` reports `ok: false` when the server answers but the plugin did not start
it, because it cannot safely stop a pid it does not know. Stop that one from the
process that started it.

To have the plugin start the server automatically when it is unreachable, set
`auto_start: true`:

```yaml
# ~/.hermes/config.yaml
plugins:
  entries:
    hyatlas:
      settings:
        auto_start: true
        binary_path: "/usr/local/bin/hyatlas-go"   # Windows: "C:/HyAtlas-Memory/hyatlas-go.exe"
```

With `auto_start` the plugin starts the binary during initialization, unless the
run is a cron or flush context. It then waits up to 30 seconds for the server to
answer. The server keeps running after Hermes exits; `hermes hyatlas stop` stops it.
See the README's Disclosure section for the full list of what runs.

## Settings

Settings are read in this order, and a later source wins:

1. Built-in defaults.
2. `$HERMES_HOME/hyatlas.json`, which the setup form writes.
3. `plugins.hyatlas` and `plugins.entries.hyatlas.settings` in `config.yaml`.
4. `HYATLAS_*` environment variables.

The keys and their environment variables are in the README's Settings table. The
LLM key is never read from these files: set it through `hermes memory setup`
(which writes it to `.env`) or export `HYATLAS_LLM_KEY`.

## Extraction mode

`mode` decides what the server does with each write:

- `lite`: no LLM call. Raw text and local embeddings only. This is the only mode
  where no conversation text goes to an LLM.
- `pro`: one LLM call per write. Fills 5 of 7 layers.
- `ultra` (default): `pro`, plus a consolidation pass every 6 hours. Fills all 7 layers.

`sync` decides whether a write waits for extraction. Unset, `pro` waits and `ultra`
does not.

## Updating

```bash
hermes plugins check-updates     # read-only
hermes plugins update hyatlas    # then restart the gateway
```

This updates the plugin only. The `hyatlas-go` binary is separate: update it with
the server's installer or by replacing the binary with a newer release.

## Logs

- Server output, when the plugin starts the server: `$HERMES_HOME/logs/hyatlas.log`.
- Plugin messages go to Hermes' own log output.
