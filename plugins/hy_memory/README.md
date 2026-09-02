# HyAtlas v4 — Native Hermes memory plugin (user install)

Copy the entire `hy_memory/` folder into `~/.hermes/plugins/hy_memory/` to install:

```bash
cp -r plugins/hy_memory ~/.hermes/plugins/
hermes gateway restart
```

Then in `~/.hermes/config.yaml`:

```yaml
memory:
  enabled: true
  provider: hy_memory
  providers:
    hy_memory:
      provider: hy_memory
      server_host: 127.0.0.1
      server_port: 19528
      auto_start: false

plugins:
  enabled:
    - hy_memory   # required for the desktop pane backend
```

This plugin talks to the **HyAtlas v4.0** Go backend
(`hyatlas-go.exe` or `hyatlas-go`) at `127.0.0.1:19528`. It is the
canonical user-facing integration; the v3.5 Python floor's `pip
install hyatlas-memory` path is no longer maintained.

## Desktop pane

The plugin ships a Hermes Desktop page at `/hyatlas`:

- Sidebar nav row: **HyAtlas Memory** (database icon), same cluster as Turbofit
- Palette: **Open HyAtlas Memory**
- Shortcut: `Mod+Shift+H` (rebindable in Settings → Keybinds)

Install the desktop door as well:

```bash
mkdir -p ~/.hermes/desktop-plugins/hy_memory
cp plugins/hy_memory/desktop/plugin.js ~/.hermes/desktop-plugins/hy_memory/plugin.js
```

The pane talks to `/api/plugins/hy_memory/*`, which is mounted from
`dashboard/plugin_api.py` **only if** `hy_memory` is in `plugins.enabled`.
After changing that list, restart the Desktop backend (⌘K → Restart backend).
Hot-reloading `plugin.js` is not enough — Python routes mount at backend start.

Tabs: Overview (health + 7-layer bars + write/search usage counters),
Memories (layer filter), Search (3-channel semantic hits), Add (write +
async extract). The 3D Observatory lives on the Go dashboard (`/dashboard/`),
not in this pane.

## Wire-compat with v3.5

The plugin works against any HyAtlas v4 server regardless of how it
was started. The port change (`19527` → `19528`) is the only config
edit needed. The `HyMemoryClient`-shaped wire contract is identical
between v3.5 and v4, so the plugin's HTTP path is byte-for-byte the
same against both backends (with the port pointing at whichever you
run).
