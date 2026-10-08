# After install

Install and enable the plugin, then restart the gateway so it loads:

```bash
hermes plugins install hyatlas     # or: hermes plugins install tuancookiez-hub/HyAtlas-Memory/plugins/hyatlas
hermes plugins enable hyatlas
hermes memory setup                # choose "hyatlas", then mode, LLM endpoint and key
```

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

or let the plugin start it:

```bash
hermes hyatlas start    # spawns the binary (see binary_path), logs to ~/.hermes/logs/hyatlas.log
hermes hyatlas stop     # stops a server the plugin started
```

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
