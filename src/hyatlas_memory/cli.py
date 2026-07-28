"""
HY Memory CLI for Hermes — `hermes hy-memory <subcommand>`

Adapted from the canonical upstream plugin (plugins/native/hermes/cli.py)
to use our local HTTP-sidecar client instead of the in-process SDK.

Subcommands:
  doctor          connectivity + config health check (read-only, no writes)
  add <text>      manually add a memory
  search <query>  manually search
  list            list recent N memories
  init            interactive setup wizard (writes ~/.hermes/.env)
  install         activate the plugin in Hermes (idempotent)
  setup hermes    one-line install: plugin shim + config + auto-start
  reset           erase all memories for a user (DESTRUCTIVE)

Hermes calls register_cli(subparser) at plugin-load time to attach these
subcommands to the `hermes hy-memory` subparser. Only active when the
plugin is the active memory provider (HY_MEMORY_USER_ID set, or
hermes config has memory.provider: hy_memory).
"""

from __future__ import annotations

import argparse
import os
import socket
import subprocess
import sys
from pathlib import Path

try:
    from hermes_constants import get_hermes_home
except ImportError:
    def get_hermes_home() -> Path:
        return Path(os.environ.get("HERMES_HOME", Path.home() / "AppData" / "Local" / "hermes"))

from . import layout


def _add_subcommands(sub: argparse._SubParsersAction) -> None:
    """Attach init / install / doctor / add / search / list / reset."""
    p_init = sub.add_parser(
        "init", help="Interactive setup wizard (writes ~/.hermes/.env)"
    )
    p_init.set_defaults(func=_cmd_init)

    p_install = sub.add_parser(
        "install",
        help="Verify plugin is activated in Hermes (symlink + SDK already in venv)",
    )
    p_install.add_argument(
        "--hermes-python",
        help="Path to the Python that Hermes runs (auto-detected if omitted)",
    )
    p_install.add_argument(
        "--copy", action="store_true",
        help="[no-op for local fork] (kept for API compat with canonical)",
    )
    p_install.add_argument(
        "--no-sdk", action="store_true",
        help="[no-op for local fork] (kept for API compat with canonical)",
    )
    p_install.add_argument(
        "-U", "--upgrade", action="store_true",
        help="Re-verify (no-op for local fork)",
    )
    p_install.set_defaults(func=_cmd_install)

    p_setup = sub.add_parser(
        "setup-hermes",
        help="One-line install: plugin shim, config, and auto-start"
    )
    p_setup.add_argument(
        "--hermes-home",
        help="Path to Hermes home directory (auto-detected if omitted)",
    )
    p_setup.add_argument(
        "--yes", "-y", action="store_true",
        help="Skip confirmation prompts",
    )
    p_setup.set_defaults(func=_cmd_setup_hermes)

    p_doctor = sub.add_parser("doctor", help="Health check (read-only diagnostic)")
    p_doctor.set_defaults(func=_cmd_doctor)

    p_console = sub.add_parser(
        "console",
        help="Show status window (health + live log tail). Safe to close.",
    )
    p_console.add_argument(
        "--no-start",
        action="store_true",
        help="Do not auto-start the stack; just attach to whatever is already running",
    )
    p_console.set_defaults(func=_cmd_console)

    p_add = sub.add_parser("add", help="Manually add a memory")
    p_add.add_argument("text", help="Memory content")
    p_add.add_argument("--user-id", help="Override HY_MEMORY_USER_ID")
    p_add.add_argument("--agent-id", help="Override HY_MEMORY_AGENT_ID")
    p_add.set_defaults(func=_cmd_add)

    p_search = sub.add_parser("search", help="Search memories")
    p_search.add_argument("query", help="Search query")
    p_search.add_argument("--limit", type=int, default=10)
    p_search.add_argument("--user-id", help="Override HY_MEMORY_USER_ID")
    p_search.add_argument("--agent-id", help="Override HY_MEMORY_AGENT_ID")
    p_search.set_defaults(func=_cmd_search)

    p_list = sub.add_parser("list", help="List recent memories")
    p_list.add_argument("--limit", type=int, default=20)
    p_list.add_argument("--user-id", help="Override HY_MEMORY_USER_ID")
    p_list.add_argument("--agent-id", help="Override HY_MEMORY_AGENT_ID")
    p_list.set_defaults(func=_cmd_list)

    p_delete = sub.add_parser(
        "delete",
        help="Delete a specific memory by ID",
    )
    p_delete.add_argument("memory_id", help="Memory ID to delete (from `list` or `search`)")
    p_delete.add_argument("--user-id", help="Override HY_MEMORY_USER_ID")
    p_delete.add_argument("--agent-id", help="Override HY_MEMORY_AGENT_ID")
    p_delete.add_argument(
        "--yes", "-y", action="store_true",
        help="Skip the confirmation prompt",
    )
    p_delete.set_defaults(func=_cmd_delete)

    p_reset = sub.add_parser(
        "reset",
        help="Erase all memories for a user (DESTRUCTIVE)",
    )
    p_reset.add_argument("--user-id", help="Override HY_MEMORY_USER_ID")
    p_reset.add_argument("--agent-id", help="Override HY_MEMORY_AGENT_ID")
    p_reset.add_argument(
        "--all-agents", action="store_true",
        help="Delete across ALL agents for this user (default: only the current agent)",
    )
    p_reset.add_argument(
        "--yes", "-y", action="store_true",
        help="Skip the confirmation prompt",
    )
    p_reset.set_defaults(func=_cmd_reset)


def register_cli(plugin_parser: argparse.ArgumentParser) -> None:
    """Hermes plugin CLI registration entry point.

    Called by hermes main CLI at plugin load:
        hermes hy_memory {init|install|doctor|add|search|list|reset}

    Contract (verified against google_meet, honcho, photon, teams_pipeline plugins):
        ``plugin_parser`` is the already-created ArgumentParser for this
        command. We attach a subparsers action to it and register our
        7 subcommands on that. We do NOT add a new top-level parser
        (the discovery code at hermes_cli/main.py:11004 already did that
        with the plugin's name, help, and description).
    """
    sub = plugin_parser.add_subparsers(dest="hy_memory_cmd", required=True)
    _add_subcommands(sub)


# ---------------------------------------------------------------------------
# Client accessor
# ---------------------------------------------------------------------------

def _get_client(user_id: str | None = None, agent_id: str | None = None):
    """Build a HyMemoryClient from current env vars + optional overrides."""
    # Lazy import — keeps the CLI importable even if hy_memory SDK is broken
    from .client import HyMemoryClient

    base_url = os.environ.get(
        "HY_MEMORY_BASE_URL",
        f"http://{os.environ.get('HY_MEMORY_HOST', '127.0.0.1')}:"
        f"{os.environ.get('HY_MEMORY_PORT', '19527')}",
    )
    return HyMemoryClient(base_url=base_url)


def _get_user_id(args, env_var: str = "HY_MEMORY_USER_ID", default: str = "hermes-user") -> str:
    return (getattr(args, "user_id", None) or os.environ.get(env_var, "") or default).strip() or default


def _get_agent_id(args, env_var: str = "HY_MEMORY_AGENT_ID", default: str = "default") -> str:
    return (getattr(args, "agent_id", None) or os.environ.get(env_var, "") or default).strip() or default


# ---------------------------------------------------------------------------
# Subcommand handlers
# ---------------------------------------------------------------------------

def _port_open(host: str, port: int, timeout: float = 0.5) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, port)) == 0


def _has_llm_key(hy_cfg: dict) -> bool:
    """True if an LLM key is configured (env or hy_memory.json). Never prints it."""
    for key in ("HY_MEMORY_LLM_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY"):
        if (os.environ.get(key) or "").strip():
            return True
    llm = hy_cfg.get("llm") or {}
    if (llm.get("api_key") or "").strip():
        return True
    keys = llm.get("api_keys") or []
    return any(str(k).strip() for k in keys)


def _cmd_doctor(args) -> int:
    """Run comprehensive health checks for the embedded stack (fail-fast)."""
    print("[hy-memory] doctor — running health checks\n")
    fails = 0
    warns = 0

    def ok(msg: str) -> None:
        print(f"  ✓ {msg}")

    def bad(msg: str) -> None:
        nonlocal fails
        fails += 1
        print(f"  ✗ {msg}")

    def warn(msg: str) -> None:
        nonlocal warns
        warns += 1
        print(f"  ! {msg}")

    def info(msg: str) -> None:
        print(f"  • {msg}")

    try:
        home = get_hermes_home()
    except Exception as e:
        bad(f"Cannot determine Hermes home: {e}")
        print("\n[hy-memory] doctor — failed")
        return 1

    # 1. Package version
    from hyatlas_memory._version import __version__
    info(f"hyatlas-memory version: {__version__}")
    info(f"Hermes home: {home}")
    info(f"HYATLAS_HOME: {layout.home()}")

    # 2. Hermes config.yaml (default profile)
    cfg_path = home / "config.yaml"
    if cfg_path.exists():
        try:
            import yaml
            data = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
            mem = data.get("memory", {}) or {}
            provider = mem.get("provider", "(not set)")
            enabled = mem.get("memory_enabled", "(not set)")
            info(f"Hermes config: memory.provider={provider}, memory_enabled={enabled}")
            if provider != "hy_memory":
                bad("memory.provider is not hy_memory — run `hyatlas setup hermes -y`")
        except Exception as e:
            warn(f"Could not parse config.yaml: {e}")
    else:
        warn(f"No config.yaml at {cfg_path}")

    # 3. Plugin directory
    plugin_dir = home / "plugins" / "hy_memory"
    if (plugin_dir / "__init__.py").exists() and (plugin_dir / "plugin.yaml").exists():
        ok(f"Plugin shim installed at {plugin_dir}")
    else:
        bad(f"Plugin shim missing at {plugin_dir} — run `hyatlas setup hermes -y`")

    # 4. Runtime config + LLM key (Day-0 gate)
    from .process import StackManager
    manager = StackManager(
        project_root=Path(__file__).parent, hermes_home=home, log_dir=layout.logs()
    )
    hy_cfg = manager._read_hy_memory_json()
    mode = hy_cfg.get("mode", "ultra")
    info(f"hy_memory mode: {mode}")
    if _has_llm_key(hy_cfg):
        ok("LLM API key present (env or hy_memory.json)")
    elif str(mode).lower() in ("pro", "ultra", ""):
        warn(
            "No LLM API key found — fact extraction needs one for pro/ultra. "
            "Set llm.api_key in ~/.hyatlas/config/hy_memory.json or HY_MEMORY_LLM_API_KEY. "
            "Use mode=lite for embed-only."
        )
    else:
        info("mode=lite — LLM key not required")

    # 5. Vector store
    vec_provider = (hy_cfg.get("vector_store") or {}).get("provider", "zvec")
    if vec_provider == "qdrant":
        # v3.4+: Qdrant is no longer a supported runtime. Detect the
        # legacy config and steer the user toward migration.
        bad(
            "vector_store.provider='qdrant' is no longer supported in v3.4+ "
            "(qdrant-client removed). Run `scripts/migrate_qdrant_to_zvec.py` "
            "to migrate, then set provider='local'."
        )
    else:
        zvec_path = layout.home() / "zvec"
        if zvec_path.exists():
            ok(f"Zvec store present at {zvec_path}")
        else:
            warn(
                f"Zvec store not created yet at {zvec_path} "
                "(normal before first `hyatlas start`)"
            )

    # 6. Upstream + deep health (short timeouts — never hang the shell)
    client = _get_client()
    upstream_up = False
    if _port_open("127.0.0.1", 19527) and client.is_reachable():
        ok(f"Upstream server reachable at {client.base_url}")
        upstream_up = True
        try:
            status = client.status()
            s = status.get("status", "unknown")
            vdb = status.get("vdb", "unknown")
            emb = status.get("embed", status.get("embedder", "unknown"))
            llm = status.get("llm", "unknown")
            wp = status.get("write_pipeline", "?")
            cnt = status.get(
                "vdb_points", status.get("points_count", status.get("vdb_count", "?"))
            )
            line = f"Deep health: {s} (vdb={vdb}, embed={emb}, llm={llm}, write={wp}, points={cnt})"
            if str(s) == "error" or str(vdb) != "ok":
                bad(line)
            elif str(s) == "warning" or str(llm).startswith(("warning", "rate_limited", "error")):
                warn(line + " — reads may work; extraction/writes can be degraded")
            else:
                ok(line)
        except Exception as e:
            bad(f"Deep status failed: {e}")
    else:
        bad(
            f"Upstream not reachable at {client.base_url} — run `hyatlas start` "
            "(or `hyatlas start --detach`)"
        )

    # 7. Dashboard
    dash_port = int((hy_cfg.get("dashboard") or {}).get("port", 8765))
    if _port_open("127.0.0.1", dash_port):
        ok(f"Dashboard reachable on port {dash_port} → http://127.0.0.1:{dash_port}")
    else:
        bad(f"Dashboard not reachable on port {dash_port} — run `hyatlas start`")

    # 8. Specialist profiles (optional Day-0 note — not a hard fail)
    profiles_dir = home / "profiles"
    if profiles_dir.is_dir():
        missing_provider = []
        missing_identity = []
        try:
            import yaml
        except Exception:
            yaml = None  # type: ignore
        for pdir in sorted(profiles_dir.iterdir()):
            if not pdir.is_dir():
                continue
            name = pdir.name
            hy_p = pdir / "hy_memory.json"
            if hy_p.exists():
                try:
                    import json as _json
                    aid = (_json.loads(hy_p.read_text(encoding="utf-8")) or {}).get(
                        "agent_identity"
                    )
                    if aid != name:
                        missing_identity.append(name)
                except Exception:
                    missing_identity.append(name)
            else:
                missing_identity.append(name)
            cfg_p = pdir / "config.yaml"
            if cfg_p.exists() and yaml is not None:
                try:
                    pdata = yaml.safe_load(cfg_p.read_text(encoding="utf-8")) or {}
                    prov = ((pdata.get("memory") or {}).get("provider") or "").strip()
                    if prov != "hy_memory":
                        missing_provider.append(name)
                except Exception:
                    missing_provider.append(name)
        if missing_provider or missing_identity:
            warn(
                "Some Hermes specialist profiles are not wired for HyAtlas "
                f"(provider gaps: {missing_provider or '—'}; "
                f"agent_identity gaps: {missing_identity or '—'}). "
                "Default profile still works. See docs/DAY0.md § multi-profile."
            )
        else:
            ok("Specialist profiles have hy_memory provider + matching agent_identity")
    else:
        info("No Hermes profiles/ dir — single-profile install (fine for Day 0)")

    # 9. Stale tui_gateway processes
    stale = 0
    try:
        if sys.platform == "win32":
            r = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq tui_gateway.exe", "/FO", "CSV"],
                capture_output=True, text=True, timeout=5,
            )
            stale = r.stdout.count("tui_gateway")
        else:
            r = subprocess.run(
                ["pgrep", "-c", "tui_gateway"],
                capture_output=True, text=True, timeout=5,
            )
            stale = int(r.stdout.strip()) if r.returncode == 0 else 0
    except Exception:
        pass
    if stale:
        warn(f"{stale} stale tui_gateway process(es) — restart Hermes TUI if UI is stuck")
    else:
        ok("No stale tui_gateway processes")

    # Day-0 next steps
    print()
    if fails:
        print(f"[hy-memory] doctor — {fails} failure(s), {warns} warning(s)")
        if not upstream_up:
            print("  Next: hyatlas start")
            print("        hyatlas doctor")
            print("        See docs/DAY0.md for first-proof steps")
        else:
            print("  Next: fix ✗ items above, then see docs/DAY0.md")
        return 1
    if warns:
        print(f"[hy-memory] doctor — ok with {warns} warning(s)")
    else:
        print("[hy-memory] doctor — all checks passed")
    print("  First proof: docs/DAY0.md  |  Dashboard: http://127.0.0.1:8765")
    return 0


def _cmd_add(args) -> int:
    text = args.text
    if not text or not text.strip():
        print("Error: text is required", file=sys.stderr)
        return 1
    user_id = _get_user_id(args)
    agent_id = _get_agent_id(args)
    client = _get_client()
    if not client.is_reachable():
        print(f"Error: server not reachable at {client.base_url}", file=sys.stderr)
        return 1
    try:
        result = client.add(
            text,
            user_id=user_id,
            agent_id=agent_id,
            session_id="cli-add",
        )
        if result.get("success"):
            print(f"✓ Added memory {result.get('memory_id')} in {result.get('elapsed_ms', 0):.0f}ms")
            return 0
        print(f"✗ Add failed: {result.get('error', 'unknown')}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"✗ Add error: {e}", file=sys.stderr)
        return 1


def _cmd_search(args) -> int:
    query = args.query
    if not query or not query.strip():
        print("Error: query is required", file=sys.stderr)
        return 1
    user_id = _get_user_id(args)
    agent_id = _get_agent_id(args)
    client = _get_client()
    if not client.is_reachable():
        print(f"Error: server not reachable at {client.base_url}", file=sys.stderr)
        return 1
    try:
        result = client.search(
            query,
            user_id=user_id,
            agent_ids=[agent_id] if agent_id else None,
            limit=args.limit,
        )
        mems = result.get("memories", [])
        # v1.2+ server returns layered shape
        if isinstance(mems, dict):
            flat = []
            for layer_name, items in mems.items():
                if not items:
                    continue
                for m in items:
                    if not m.get("layer"):
                        m = {**m, "layer": layer_name}
                    flat.append(m)
            mems = flat
        print(f"Found {len(mems)} result(s) for '{query}':\n")
        for i, m in enumerate(mems[:args.limit], 1):
            layer = m.get("layer", "?")
            score = m.get("score", 0)
            content = (m.get("content", "") or "")[:200]
            print(f"  [{i}] {layer} (score={score:.2f})")
            print(f"      {content}")
            print()
        return 0
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def _cmd_list(args) -> int:
    user_id = _get_user_id(args)
    agent_id = _get_agent_id(args)
    client = _get_client()
    if not client.is_reachable():
        print(f"Error: server not reachable at {client.base_url}", file=sys.stderr)
        return 1
    try:
        result = client.list_memories(
            user_id=user_id, agent_id=agent_id, limit=args.limit,
        )
        vdb = result.get("vdb", {}) or {}
        items = vdb.get("memories", [])
        total = vdb.get("total", "?")
        print(f"Listing {len(items)} of {total} memories (user={user_id}, agent={agent_id}):\n")
        for i, m in enumerate(items, 1):
            layer = m.get("layer", "?")
            content = (m.get("content", "") or "")[:150]
            mid = m.get("memory_id", "")[:8]
            print(f"  [{i}] {layer} ({mid}...) {content}")
        return 0
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def _cmd_delete(args) -> int:
    """Delete a specific memory by ID."""
    memory_id = args.memory_id
    if not memory_id:
        print("Error: memory_id is required", file=sys.stderr)
        return 1
    user_id = _get_user_id(args)
    agent_id = _get_agent_id(args)
    client = _get_client()
    if not client.is_reachable():
        print(f"Error: server not reachable at {client.base_url}", file=sys.stderr)
        return 1

    if not args.yes:
        confirm = input(
            f"This will DELETE memory {memory_id!r} for user='{user_id}', agent='{agent_id}'.\n"
            f"Type 'yes' to continue: "
        )
        if confirm.strip().lower() != "yes":
            print("Aborted.")
            return 1

    try:
        result = client.delete(memory_id)
        if result.get("error") and not result.get("success"):
            print(f"✗ Delete failed: {result.get('error')}")
            return 1
        print(f"✓ Deleted memory {memory_id}")
        return 0
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


def _cmd_reset(args) -> int:
    user_id = _get_user_id(args)
    agent_id = _get_agent_id(args)
    client = _get_client()
    if not client.is_reachable():
        print(f"Error: server not reachable at {client.base_url}", file=sys.stderr)
        return 1

    if not args.yes:
        scope = "all agents" if args.all_agents else f"agent {agent_id}"
        confirm = input(
            f"This will DELETE ALL memories for user='{user_id}', {scope}.\n"
            f"Type 'yes' to continue: "
        )
        if confirm.strip().lower() != "yes":
            print("Aborted.")
            return 1

    try:
        if args.all_agents:
            result = client.delete_all(user_id=user_id, agent_ids=None)
        else:
            result = client.delete_all(user_id=user_id, agent_ids=[agent_id])
        print(f"✓ Reset result: {result}")
        return 0
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1


# ---------------------------------------------------------------------------
# Console — visible status window with live activity ticker
# ---------------------------------------------------------------------------

def _console_already_running() -> bool:
    """Return True if a hyatlas_memory.console window is already open.

    Prevents the spawn-on-every-start pile-up. Scans wmic for any
    python.exe whose commandline contains hyatlas_memory.console.
    """
    try:
        out = subprocess.run(
            ["wmic", "process", "where", "name='python.exe'", "get", "commandline"],
            capture_output=True, text=True, timeout=4,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
        ).stdout
    except Exception:
        return False
    return "hyatlas_memory.console" in out


def _cmd_console(args) -> int:
    """Open the HyAtlas status window — health header + live server log tail.

    Safe to close: only the window dies, the server keeps running.
    If the stack isn't running yet, starts it first (detached).
    """
    if not args.no_start:
        from .process import StackManager
        try:
            home = get_hermes_home()
        except Exception:
            home = None
        if home is None:
            home = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "hermes"
        manager = StackManager(
            project_root=Path(__file__).parent,
            hermes_home=home,
            log_dir=layout.logs(),
        )
        manager.start()

    py = getattr(sys, "_base_executable", None) or sys.executable
    inner = [py, "-m", "hyatlas_memory.console"]
    if getattr(args, "no_start", False):
        inner.append("--no-start")

    flags = subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NEW_PROCESS_GROUP
    if _console_already_running():
        print(_msg_dim("HyAtlas status window is already open."))
        return 0
    # Build PYTHONPATH so the base python finds the venv site-packages.
    # Spawning the base python directly (vs the venv shim) avoids the
    # re-exec that causes console-window flicker.
    spawn_env = os.environ.copy()
    # MSYS/Git Bash env vars confuse CREATE_NEW_CONSOLE children.
    for key in (
        "MSYSTEM",
        "MSYSTEM_CARCH",
        "MSYSTEM_CHOST",
        "MSYSTEM_PREFIX",
        "MINGW_PREFIX",
        "MINGW_CHOST",
        "MINGW_PACKAGE_PREFIX",
        "CYGWIN",
        "TERM_PROGRAM",
        "ORIGINAL_PATH",
    ):
        spawn_env.pop(key, None)
    paths = []
    sp = os.path.join(sys.prefix, "Lib", "site-packages")
    if os.path.isdir(sp):
        paths.append(sp)
    # Editable install: cli.py lives at <root>/hyatlas_memory/cli.py.
    # Package root is the PARENT of this file.
    package_root = Path(__file__).resolve().parent.parent
    paths.append(str(package_root))
    # REPLACE inherited PYTHONPATH (hermes-agent shell pollution).
    spawn_env["PYTHONPATH"] = os.pathsep.join(paths)
    try:
        subprocess.Popen(
            inner,
            creationflags=flags,
            close_fds=False,
            cwd=str(Path(__file__).parent),
            env=spawn_env,
        )
        print(_msg_green("HyAtlas status window launched."))
        print(_msg_dim("Close it anytime — the server keeps running."))
        print(_msg_dim("Reopen: hyatlas console  or  bin/hyatlas-status.bat"))
    except Exception as e:
        print(_msg_yellow(f"Failed to launch console: {e}"))
        print(_msg_dim("You can run it directly:"))
        print(f"  {py} -m hyatlas_memory.console")
        return 1
    return 0


# init / install delegate to wizard / installer modules (separate files)
def _cmd_init(args) -> int:
    from . import init_wizard
    return init_wizard.run_interactive()


def _cmd_install(args) -> int:
    from . import installer
    return installer.run_install(
        hermes_python=getattr(args, "hermes_python", None),
    )


def _cmd_setup_hermes(args) -> int:
    """Install the Hermes plugin shim, set config, and test auto-start."""
    from .installer import _install_plugin_shim, _update_config
    from .process import StackManager

    home = Path(args.hermes_home) if args.hermes_home else _find_hermes_home()
    print(f"[hy-memory] setup-hermes using Hermes home: {home}")

    if not args.yes:
        confirm = input("This will install the hy_memory plugin shim and set memory.provider. Continue? [y/N]: ")
        if confirm.strip().lower() != "y":
            print("Aborted.")
            return 1

    # Install shim
    if not _install_plugin_shim(home):
        print("✗ Plugin shim installation failed")
        return 1
    print("✓ Plugin shim installed")

    # Set active provider
    if not _update_config(home, "hy_memory"):
        print("✗ Config update failed")
        return 1
    print("✓ Hermes config memory.provider set to hy_memory")

    # Optional: test auto-start
    print("[hy-memory] Verifying auto-start...")
    root = Path(__file__).parent
    manager = StackManager(project_root=root, hermes_home=home, log_dir=layout.logs())
    if manager.ensure_running():
        print("✓ Stack auto-started successfully")
    else:
        print("! Stack auto-start failed — run `hyatlas start` and check logs under HYATLAS_HOME/logs")
        return 1

    print("\n[hy-memory] Setup complete. Restart Hermes TUI/CLI to load the new plugin.")
    return 0


def _ansi(code: str) -> str:
    return f"\x1b[{code}m"


def _msg_green(text: str) -> str:
    return f"  \x1b[32m{text}\x1b[0m"


def _msg_yellow(text: str) -> str:
    return f"  \x1b[33m{text}\x1b[0m"


def _msg_dim(text: str) -> str:
    return f"  \x1b[90m{text}\x1b[0m"


def _find_hermes_home() -> Path:
    """Return the Hermes home directory using the same logic as Hermes itself."""
    try:
        return get_hermes_home()
    except Exception:
        return Path.home() / "AppData" / "Local" / "hermes"


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------

def _main_standalone(argv: list[str] | None = None) -> int:
    """Run as `hermes-hy-memory <cmd>` (without the parent `hermes` CLI)."""
    parser = argparse.ArgumentParser(
        prog="hermes-hy-memory",
        description="HY Memory plugin CLI (standalone mode)",
    )
    sub = parser.add_subparsers(dest="hy_memory_cmd", required=True)
    _add_subcommands(sub)
    args = parser.parse_args(argv)
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(_main_standalone())
