# Day 0 — First proof (new users)

**Goal:** from zero to “Hermes remembered something I said” in about **15 minutes**.

This is the **default profile only** path. Specialist profiles (`research`, `sentinel`, …) are optional later.

---

## Prerequisites

| Need | Why |
|------|-----|
| Python 3.10+ | Package + stack |
| [Hermes Agent](https://hermes-agent.nousresearch.com) installed | HyAtlas is a Hermes **memory provider** |
| An LLM API key (for `pro` / `ultra`) | Fact extraction. Without it, use `mode: lite` (embed-only — weaker “magic”) |
| Disk for local embedder | First start may download `BAAI/bge-large-en-v1.5` |

---

## Install (once)

```bash
git clone https://github.com/tuancookiez-hub/HyAtlas-Memory.git
cd HyAtlas-Memory
pip install -e .
hyatlas setup hermes -y
```

What that does:

1. Installs the `hy_memory` plugin shim under your Hermes home  
2. Sets `memory.provider: hy_memory` in Hermes `config.yaml`  
3. Tries to start the local stack (server **:19527**, dashboard **:8765**)

Restart Hermes (TUI / gateway) so it reloads the memory provider.

---

## Config (once)

Edit `~/.hyatlas/config/hy_memory.json` (Windows often `D:\…\.hyatlas` if you set `HYATLAS_HOME`, else under your user home):

```json
{
  "mode": "ultra",
  "llm": {
    "api_key": "YOUR_KEY",
    "model": "gpt-4o-mini",
    "base_url": "https://api.openai.com/v1"
  },
  "embedder": {
    "model": "BAAI/bge-large-en-v1.5",
    "dims": 1024,
    "provider": "local"
  },
  "vector_store": { "provider": "zvec" }
}
```

Or set `HY_MEMORY_LLM_API_KEY` in the environment.

**Modes:**

| Mode | Behavior |
|------|----------|
| `lite` | No LLM extraction — store + embed only |
| `pro` | LLM extracts facts on write |
| `ultra` | pro + System 2 / graph (default) |

---

## Start + doctor (every boot if needed)

```bash
hyatlas start          # or: hyatlas start --detach
hyatlas doctor         # must finish in a few seconds; exit 0 when healthy
```

| Check | Healthy look |
|-------|----------------|
| Upstream | `✓ Upstream server reachable` on :19527 |
| Deep health | `status=ok`, `vdb=ok`, `embed=ok` |
| Dashboard | `✓ Dashboard reachable` → http://127.0.0.1:8765 |
| Provider | `memory.provider=hy_memory` |

If doctor fails on upstream: run `hyatlas start` again. Logs: `$HYATLAS_HOME/logs/` (default `~/.hyatlas/logs/`).

**Identity contract (default):**

| Field | Value |
|-------|--------|
| `user_id` | `hermes-user` |
| `agent_id` | `default` |

---

## First proof (do this before exploring the whole dashboard)

### A. Manual write + list (proves the stack)

```bash
hyatlas add "Day0 proof: I prefer dark themes and use Bun for frontend builds."
hyatlas list --limit 10
hyatlas search "dark themes Bun" --limit 5
```

Expect: list/search show your sentence (or an extracted L2 fact about dark themes / Bun).

### B. Hermes chat (proves the plugin)

In Hermes (default profile):

> Remember for later: my favorite local port for demos is 8765 and I hate flaky tests.

Then in a **new** message or session:

> What demo port do I like?

Expect: recall mentions 8765 without you repeating it.

### C. Dashboard (proves visibility)

Open http://127.0.0.1:8765

1. **Today / Activity** or **Explore** — recent write  
2. **Settings / System** — layer-health, digest hints  
3. Leave **profile dropdown on `default`** for Day 0  

If a write “didn’t show”: list with raw included (server default) — L1 rows may have `extracted: false` when the LLM skips noisy text. See [TROUBLESHOOTING.md](./TROUBLESHOOTING.md).

---

## Reliability habits (week 1)

1. **Stack is a process.** After reboot: `hyatlas start` then `hyatlas doctor`.  
2. **Doctor must not hang.** If an old build hangs, upgrade; status/deep health use short timeouts.  
3. **LLM 429 / billing** → doctor may show `llm=warning` while VDB still works. Top up or switch model.  
4. **Don’t kill the mid-session flush** for important facts — or use `hyatlas add` for critical notes.  
5. **Default only on Day 0.** Specialist profiles need their own wiring (below).

---

## Multi-profile (optional, after Day 0 works)

Hermes profiles under `$HERMES_HOME/profiles/<name>/` need:

| File | Requirement |
|------|-------------|
| `hy_memory.json` | `"agent_identity": "<same-as-folder-name>"`, `server_port: 19527` |
| `config.yaml` | `memory.provider: hy_memory`, `memory_enabled: true` |

Then real traffic:

```bash
hermes -p research chat -q "…"
```

Empty specialist rows in the dashboard usually mean **no sessions wrote there**, not a broken API. See [PROFILE_MEMORY_ARCHITECTURE.md](./PROFILE_MEMORY_ARCHITECTURE.md).

---

## If something fails

| Symptom | Fix |
|---------|-----|
| doctor: upstream ✗ | `hyatlas start` |
| doctor: provider ≠ hy_memory | `hyatlas setup hermes -y`, restart Hermes |
| no LLM key warning | set key or use `mode: lite` |
| wrote but list empty | `include_raw` / wait for extract; clean factual sentences |
| dashboard blank | hard refresh; confirm :8765 with doctor |
| Windows console missing | `hyatlas console` — stack can still be healthy |

More: [TROUBLESHOOTING.md](./TROUBLESHOOTING.md) · [HYATLAS_HERMES.md](./HYATLAS_HERMES.md)

---

## Success criteria (you’re done with Day 0)

- [ ] `hyatlas doctor` exits 0 (warnings about optional profiles OK)  
- [ ] `hyatlas add` + `hyatlas search` show your proof sentence  
- [ ] Hermes recalls a preference in a later turn  
- [ ] Dashboard loads and shows recent activity under **default**
