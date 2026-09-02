# After install

After copying this directory to `~/.hermes/plugins/hy_memory/` and
restarting the gateway, verify the plugin loaded:

```bash
# 1. Check the plugin is discoverable
hermes memory status
# Should show: provider: hy_memory

# 2. Verify the v4 server is reachable
hermes hyatlas status
# Should print: {"embed":"ok","llm":"ok",...}

# 3. If the server isn't running, start it
hermes hyatlas start
# OR run the binary directly:
hyatlas-go

# 4. Try a search to confirm wire-compat
hermes hyatlas search "anything"
# Should return JSON with profile/proactive/normal channels
```

## Auto-start the v4 server from Hermes

If you want Hermes to spawn the Go binary automatically when it
starts (instead of you running `hyatlas-go` in another terminal):

```yaml
# ~/.hermes/config.yaml
plugins:
  hy_memory:
    server_host: 127.0.0.1
    server_port: 19528
    auto_start: true
    binary_path: "C:/HyAtlas-Memory/hyatlas-go.exe"  # Windows
    # binary_path: "/usr/local/bin/hyatlas-go"      # Linux/macOS
```

The plugin will spawn the binary on the first `initialize()` call if
the port isn't already bound.

## Required environment variables

The plugin reads (in priority order):

1. Per-profile JSON at `~/.hermes/hy_memory.json`:
   ```json
   {
     "server_host": "127.0.0.1",
     "server_port": 19528,
     "user_id": "default",
     "agent_id": "default",
     "auto_start": false
   }
   ```

2. Env vars (canonical 12-factor):
   - `HYATLAS_SERVER_HOST` (default `127.0.0.1`)
   - `HYATLAS_SERVER_PORT` (default `19528`)
   - `HYATLAS_USER_ID`
   - `HYATLAS_AGENT_ID`
   - `HYATLAS_AUTO_START` (`1`/`true` to enable)
   - `HYATLAS_BINARY_PATH` (path to the Go binary if not in PATH)

3. The Go binary itself reads (separately, from the binary's process env):
   - `HYATLAS_LLM_BASE` — OpenAI-compatible endpoint
   - `HYATLAS_LLM_MODEL` — model name (e.g. `poolside/laguna-s-2.1:free`)
   - `HYATLAS_LLM_KEY` — bearer token for the LLM endpoint

## Switching between v3.5 and v4

The plugin's HTTP wire contract is identical between v3.5 (port 19527)
and v4 (port 19528). To switch:

```yaml
# Use v4 (current)
plugins:
  hy_memory:
    server_port: 19528

# Use v3.5 (legacy)
plugins:
  hy_memory:
    server_port: 19527
```

Restart the gateway after changing the port.

## Verifying the plugin

In a fresh session, ask the agent about something you've talked
about before. If the agent finds it, the plugin is working. The
prefetched context should appear in the agent's first turn.

## Logs

- v4 server logs: `~/.hermes/logs/hyatlas.log` (when auto-started)
- Plugin errors: see `~/.hermes/logs/errors.log` filtered for `hy_memory`
