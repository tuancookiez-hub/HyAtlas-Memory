# After install

Install and enable the plugin, then restart the gateway so it loads:

```bash
hermes plugins install tuancookiez-hub/HyAtlas-Memory/plugins/hyatlas
hermes plugins enable hyatlas
hermes memory setup                # choose "hyatlas", then answer its prompts (see below)
```

`hermes plugins install` takes a catalog name, a Git URL, or `owner/repo` with the
plugin's subdirectory. The subdirectory is required, because the repository root
is not the plugin. Equivalent forms: `tuancookiez-hub/HyAtlas-Memory#plugins/hyatlas`
and `https://github.com/tuancookiez-hub/HyAtlas-Memory.git#plugins/hyatlas`. The
GitHub forms install the repository's default branch as it is at that moment. The
bare name `hyatlas` works only once the plugin is in the Hermes catalog. Add
`--enable` to the install command to enable it in the same step.

To install from a local clone, use a `file://` URL with the subdirectory fragment:

```bash
hermes plugins install file:///path/to/HyAtlas-Memory#plugins/hyatlas
```

A plain local path does not work. Hermes reads it as `owner/repo` and tries GitHub.

## What `hermes memory setup` does

1. Shows the memory providers and "Built-in only". Pick `hyatlas`. The plugin
   declares no dependencies beyond Hermes core, so nothing is installed.
2. On a real terminal, prompts for each of the plugin's 14 settings, in this order.
   Enter keeps the default. Without a terminal it asks nothing (see *Without a
   terminal* below). The first five matter: how much HyAtlas should think (a menu:
   `ultra`, `pro`, `lite`), the AI endpoint (Enter = the Nous Portal), the API key
   (masked, written to Hermes' `.env` as `HYATLAS_LLM_KEY`; Enter skips it), the
   model (Enter = a free Nous Portal model), and whether Hermes should start the
   server for you. The rest are marked "Advanced:" and Enter keeps their defaults.
   A server you start yourself receives the key from the plugin over localhost.
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

## Without a terminal

Where `hermes memory setup` has no terminal, it only selects the provider. Set the
values in one of these places instead. A later source wins over an earlier one (see
*Settings*). The keys are the setting names, such as `mode` and `llm_base`.

`$HERMES_HOME/hyatlas.json`:

```json
{ "mode": "pro", "llm_base": "https://your-endpoint.example/v1", "llm_model": "your-model" }
```

`config.yaml`, under the plugin's settings:

```yaml
plugins:
  entries:
    hyatlas:
      settings:
        mode: pro
        llm_base: "https://your-endpoint.example/v1"
        llm_model: "your-model"
```

Or export the matching `HYATLAS_*` variable, such as `HYATLAS_MODE=pro`.

The LLM key is your own, and it is set only through the environment: save it with
`hermes memory setup` (the `llm_key` secret, which Hermes keeps in `.env`), or export
`HYATLAS_LLM_KEY`, or point `HYATLAS_LLM_KEY_FILE` at a plain-text file holding the
token. The plugin does not use a key found in `hyatlas.json` or `config.yaml`. A Nous
Portal API key, or the key of any OpenAI-compatible provider, works. The plugin never
reads Hermes's own login or credential files. Until a key is set, `status` shows
`llm: "awaiting_key"`.

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

`status` prints the server's JSON, including `version`, `mode` and `llm`
(`ok`, `unconfigured` or `unused`). `search` prints the server's `memories` object,
grouped into the `profile`, `proactive` and `normal` channels.

`hermes hyatlas recent` lists the latest memories. In `lite` it includes raw rows by
default, since raw is the only layer that mode stores. In other modes it leaves them
out. `--include-raw` and `--no-include-raw` override the default.

## Start the server

The plugin does not install the server. Either run the binary yourself:

```bash
hyatlas-go          # listens on 127.0.0.1:19528 by default
```

A release binary is an embedded build and already contains the BGE model; it
ignores `HYATLAS_MODEL_DIR`. A source-built binary needs the model folder: it looks
in `HYATLAS_MODEL_DIR` if set (and then nowhere else), otherwise in `<cwd>/models`,
then `<exe dir>/models`, then the installer's default `~/.hyatlas/models` (Windows
`%LOCALAPPDATA%\hyatlas\models`).

Or let the plugin start it:

```bash
hermes hyatlas start    # spawns the binary (see binary_path), logs to ~/.hermes/logs/hyatlas.log
hermes hyatlas stop     # stops a server the plugin started
```

`/hyatlas start` and `/hyatlas stop` do the same, and work while the server is down.

`start` spawns nothing if a server already answers at the configured host and
port. It returns `ok: true` with `already_running: true`. The `pid` is set, with
`pid_known: true`, when the pidfile names a live `hyatlas-go`. It is `pid_known:
false` when the server was started by hand.

`stop` reports `ok: false` when the server answers but the plugin did not start
it, because it cannot safely stop a pid it does not know. Stop that one from the
process that started it. With nothing running, `stop` returns `ok: true` with
`running: false`.

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
answer. If two Hermes sessions start at once, a lock file means only one of them
starts the server. The other finds it running. If the filesystem cannot lock, the
start goes ahead without the lock and a warning is logged. The server keeps running
after Hermes exits; `hermes hyatlas stop` stops it. See the README's Disclosure section
for the full list of what runs.

## Settings

Settings are read in this order, and a later source wins:

1. Built-in defaults.
2. `$HERMES_HOME/hyatlas.json`, which the setup form writes.
3. `plugins.hyatlas` and `plugins.entries.hyatlas.settings` in `config.yaml`.
4. `HYATLAS_*` environment variables.

The keys and their environment variables are in the README's Settings table. The
setup wizard never writes the LLM key to `hyatlas.json` or `config.yaml`, and the
plugin never forwards it from settings to the server. Set it through `hermes memory
setup` (which writes it to `.env`) or export `HYATLAS_LLM_KEY`. If a key does end
up in one of those files by hand, the plugin reads it but does not use it.

Server-side variables such as `HYATLAS_ALLOWED_HOSTS` and `HYATLAS_USER_ALIASES` are not plugin settings. A
server you start yourself reads them from its own environment. A server the plugin
starts receives every `HYATLAS_*` variable exported in the agent's environment.
`HYATLAS_USER_ALIASES` groups user IDs that are one person, for search, for example
`HYATLAS_USER_ALIASES="221727702992945152,default,hermes-memory-archive"` when the same person reaches Hermes under several user IDs.

### Reaching the server by a hostname

If `server_host` is a DNS name (a LAN hostname, or a box that is not this
machine), the server refuses the request with 403 unless that name is in the
server's `HYATLAS_ALLOWED_HOSTS`. That includes `/healthz`, so the plugin reports
the server as unreachable. Set the allowlist on the server, then set `server_host`.
Use an IP literal or `127.0.0.1` otherwise. Entries are hostnames, and a port in an
entry is ignored.

## Extraction mode

`mode` decides what the server does with each write:

- `lite`: no LLM call. Raw text and local embeddings only. This is the only mode
  where no conversation text goes to an LLM.
- `pro`: one LLM call per write. Fills 5 of 7 layers.
- `ultra` (default): `pro`, plus a consolidation pass every 6 hours. It fills L6 only
  when a pass finds recurring patterns, and L5 only when a pass finds relations
  corroborated by at least two turns, so "7 of 7" is the steady state, not the first
  pass. `HYATLAS_CONSOLIDATE_GRAPH=off` on the server stops L5.

`sync` decides whether a write waits for extraction. For a server the plugin
starts, the default is `off` in every mode, so a Hermes turn does not wait on the
LLM. For a server you start yourself, unset means `pro` waits and `ultra` does not.
An exported `HYATLAS_SYNC_EXTRACT` overrides both.

## Updating

```bash
hermes plugins check-updates     # read-only
hermes plugins update hyatlas    # then restart the gateway
```

This updates the plugin only. The `hyatlas-go` binary is separate: update it with
the server's installer or by replacing the binary with a newer release.

### Upgrading to 5.0

- **A replace or remove through Hermes's `memory` tool now forgets the old entry.** The
  server retires the raw rows and what was derived from them, and the admin restore
  can bring them back. Before, only `add` was mirrored.
- **Bulk delete needs `HYATLAS_ADMIN=on`.** `delete_all` by layer, user, agent or all
  is refused (403) without it. Deleting one memory by id is unchanged.
- **Searches carry the session id**, so the current session's intentions rank first,
  and the recall block follows the channel order the server sends.
- **The plugin never sends local calls through a proxy.**
- **`share_llm_key` is removed (Hermes catalog rule 11).** The plugin no longer takes
  Hermes's Nous sign-in token or hands it to the server. Set your own extraction key
  (`llm_key` through `hermes memory setup`, or `HYATLAS_LLM_KEY`, or a plain-text
  `HYATLAS_LLM_KEY_FILE`). A Nous Portal API key, or any OpenAI-compatible provider
  key, works as `llm_key`. A `share_llm_key` left in `hyatlas.json` is ignored.
- **`HYATLAS_LLM_KEY_FILE` takes a plain-text token only.** A JSON file (such as
  `auth.json`) gives no key; the server logs a warning and uses `HYATLAS_LLM_KEY`.
- **Durations read one way**: `15m`, a bare number as seconds, and `0`, `off`, `false`
  or `no` to turn off. `HYATLAS_CONSOLIDATE_EVERY=off` now fails at start in ultra, as
  `0` always did; `HYATLAS_INTENTION_TTL=off` means no limit on intentions.
- **Turns missed by extraction are caught up** (`HYATLAS_CATCHUP_EVERY`, 10 minutes),
  and `/api/v1/status` shows `unextracted`, the number still waiting.
- **Lists for a `user_id` include that person's aliases** (`HYATLAS_USER_ALIASES`), and
  episodes and the Now page are kept under the first ID of the alias group. A Now page
  stored before aliases were set up is still found.

### Upgrading to 4.8 or later

- **The sample launcher no longer reads Hermes' `auth.json`.** If you used it with a
  Nous login, set your own extraction key (see the key section above); otherwise
  extraction waits with `llm: "awaiting_key"`.
- **Per-write summaries stop.** L4 now holds one summary per episode of work
  (`HYATLAS_EPISODE_GAP`, 30 minutes). Older per-write summaries stay as they are.
- **Usage learning moves to `usage_learning.json`.** An old `usage.json` with usage
  entries is read once.
- **Search results gain fields and a channel**: `event_date`, `history`, and the
  `experiences` channel. Older plugins ignore them; update the plugin to show them.

### Upgrading a store written before 4.5.0

A store written before 4.5.0 can hold raw rows with tool output, because older
releases sent it. The two maintenance endpoints remove that output from raw rows and
supersede duplicate facts.
They are refused with 403 unless the server runs with `HYATLAS_ADMIN=on`. Back up the
data directory (`HYATLAS_GO_DATA`, default `./data`) first, because `compact_raw` is
irreversible: the removed text is gone. Then:

```bash
HYATLAS_ADMIN=on hyatlas-go                       # 1. start the server with admin endpoints on
curl -X POST http://127.0.0.1:19528/api/v1/admin/compact_raw       # 2. dry run (no body): reports, changes nothing
curl -X POST http://127.0.0.1:19528/api/v1/admin/dedupe_facts      # 2. dry run (no body)
curl -X POST -H "Content-Type: application/json" -d '{"dry_run": false}' http://127.0.0.1:19528/api/v1/admin/compact_raw   # 3. apply
curl -X POST -H "Content-Type: application/json" -d '{"dry_run": false}' http://127.0.0.1:19528/api/v1/admin/dedupe_facts   # 3. apply
curl -X POST -H "Content-Type: application/json" -d '{"layer": "l5_knowledge"}' http://127.0.0.1:19528/api/v1/admin/dedupe_facts   # 4. dry run for graph relations
curl -X POST -H "Content-Type: application/json" -d '{"layer": "l5_knowledge", "dry_run": false}' http://127.0.0.1:19528/api/v1/admin/dedupe_facts   # 4. apply
```

Check the dry-run responses before step 3. `dedupe_facts` supersedes facts, so it is
reversible; `compact_raw` is not. When done, restart the server without
`HYATLAS_ADMIN`.

## Logs

- Server output, when the plugin starts the server: `$HERMES_HOME/logs/hyatlas.log`.
- Plugin messages go to Hermes' own log output.
