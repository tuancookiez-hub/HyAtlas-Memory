# HyAtlas v4 — Native Hermes memory plugin (user install)

This is the user-side plugin directory. Copy the entire `hy_memory/`
folder into `~/.hermes/plugins/hy_memory/` to install:

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
```

This plugin talks to the **HyAtlas v4.0** Go backend
(`hyatlas-go.exe` or `hyatlas-go`) at `127.0.0.1:19528`. It is the
canonical user-facing integration; the v3.5 Python floor's `pip
install hyatlas-memory` path is no longer maintained.

See `plugins/hy_memory/README.md` for full setup details.

## Wire-compat with v3.5

The plugin works against any HyAtlas v4 server regardless of how it
was started. The port change (`19527` → `19528`) is the only config
edit needed. The `HyMemoryClient`-shaped wire contract is identical
between v3.5 and v4, so the plugin's HTTP path is byte-for-byte the
same against both backends (with the port pointing at whichever you
run).
