"""Unified CLI entry point for `hyatlas`.

This module implements the ``hyatlas`` command-line tool. It keeps the
original ``start``/``stop``/``status`` semantics for backwards compat and
adds a subcommand parser for the new v1.4 operations:

  hyatlas start           (default) — start the stack in the foreground
  hyatlas stop            — stop the stack
  hyatlas status          — show stack status
  hyatlas setup hermes    — install the Hermes plugin shim + set config
  hyatlas doctor          — run full health checks
  hyatlas init            — interactive setup wizard
  hyatlas add <text>      — manually add a memory
  hyatlas search <query>  — manually search
  hyatlas list            — list recent memories
  hyatlas delete <id>     — delete a memory
  hyatlas reset           — erase all memories

The module is importable as ``hyatlas_memory._cli`` and is also invoked
from ``hyatlas_memory.start`` for the ``python -m hyatlas_memory.start``
entry path.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

try:
    from hermes_constants import get_hermes_home
except Exception:
    def get_hermes_home():
        return Path.home() / "AppData" / "Local" / "hermes"

from . import _start, archive_cli, config_cli, layout, migrate_cli, zvec_cli
from . import cli as _memory_cli
from .installer import _install_plugin_shim, _update_config
from .process import StackManager


def _should_auto_detach() -> bool:
    """Detach when the launcher is not an interactive foreground terminal session."""
    if os.environ.get("HYATLAS_FOREGROUND", "").strip().lower() in ("1", "true", "yes", "on"):
        return False
    if os.environ.get("HYATLAS_START_DETACHED", "").strip().lower() in ("1", "true", "yes", "on"):
        return True
    # Background jobs often redirect stdout only; either non-TTY is enough to detach.
    return not sys.stdin.isatty() or not sys.stdout.isatty()


def _run_start(args) -> int:
    """Start the HyAtlas stack.

    Foreground: blocks until Ctrl+C (services share the launching console).
    Detached: children survive terminal close; use ``hyatlas start --detach``.
    Auto-detach when stdin/stdout is not a TTY (background jobs, log redirection).
    """
    detach = bool(getattr(args, "detach", False))
    if getattr(args, "foreground", False):
        detach = False
    elif not detach and _should_auto_detach():
        detach = True
    _start.detach = detach
    _start.force_restart = bool(getattr(args, "restart", False))
    if _start.detach:
        _start._start_detached_and_exit()
        return 0
    _start.signal.signal(_start.signal.SIGINT, lambda *_: (print(), _start.shutdown(), sys.exit(0)))
    _start.start_all()
    return 0


def _run_stop(args) -> int:
    _start.stop_all()
    return 0


def _run_status(args) -> int:
    _start.show_status()
    return 0


def _run_setup_hermes(args) -> int:
    """Install the Hermes plugin shim, set config, and optionally test auto-start."""
    try:
        home = get_hermes_home()
    except Exception:
        home = Path.home() / "AppData" / "Local" / "hermes"

    print(f"[hyatlas] setup hermes — Hermes home: {home}")
    if not args.yes:
        confirm = input("Install the hy_memory plugin shim and set memory.provider? [y/N]: ")
        if confirm.strip().lower() != "y":
            print("Aborted.")
            return 1

    if not _install_plugin_shim(home):
        print("✗ Plugin shim installation failed")
        return 1
    print("✓ Plugin shim installed")

    if not _update_config(home, "hy_memory"):
        print("✗ Config update failed")
        return 1
    print("✓ Hermes config: memory.provider = hy_memory")

    if args.no_start:
        print("\n[hyatlas] Setup complete. Restart Hermes to load the new plugin.")
        return 0

    print("[hyatlas] Verifying auto-start...")
    root = Path(__file__).parent
    manager = StackManager(project_root=root, hermes_home=home, log_dir=layout.logs())
    if manager.ensure_running():
        print("✓ Stack auto-started successfully")
    else:
        print("! Stack auto-start failed — run `hyatlas start` and check logs under HYATLAS_HOME/logs")
        return 1

    print("\n[hyatlas] Setup complete. Restart Hermes to load the new plugin.")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Main entry point for the ``hyatlas`` CLI."""
    raw = list(argv) if argv is not None else sys.argv[1:]
    global_detach = "--detach" in raw
    global_restart = "--restart" in raw
    filtered = [a for a in raw if a not in ("--detach", "--restart")]

    parser = argparse.ArgumentParser(
        prog="hyatlas",
        description="HyAtlas-Memory command-line tool",
    )
    sub = parser.add_subparsers(dest="cmd", help="subcommand")

    # Legacy start/stop/status
    p_start = sub.add_parser("start", help="Start the HyAtlas stack")
    p_start.add_argument(
        "--foreground",
        action="store_true",
        help="Stay attached (default when run in an interactive terminal)",
    )
    p_start.add_argument(
        "--detach",
        action="store_true",
        help="Detach services (survive terminal close). Also: hyatlas --detach start",
    )
    p_start.add_argument("--restart", action="store_true", help="Force restart if running")
    p_start.set_defaults(func=_run_start)

    p_stop = sub.add_parser("stop", help="Stop the HyAtlas stack")
    p_stop.set_defaults(func=_run_stop)

    p_status = sub.add_parser("status", help="Show stack status")
    p_status.set_defaults(func=_run_status)

    # New v1.4 subcommands (delegated to the existing CLI modules)
    p_setup = sub.add_parser("setup", help="Install plugin into Hermes")
    setup_sub = p_setup.add_subparsers(dest="setup_cmd", required=True)
    p_setup_hermes = setup_sub.add_parser("hermes", help="Install the Hermes plugin shim")
    p_setup_hermes.add_argument("--hermes-home", help="Path to Hermes home (auto-detected if omitted)")
    p_setup_hermes.add_argument("--yes", "-y", action="store_true", help="Skip confirmation")
    p_setup_hermes.add_argument("--no-start", action="store_true", help="Skip auto-start test")
    p_setup_hermes.set_defaults(func=_run_setup_hermes)

    p_init = sub.add_parser("init", help="Interactive setup wizard")
    p_init.set_defaults(func=config_cli.init)

    config_cli.register(sub)
    migrate_cli.register(sub)
    zvec_cli.register(sub)
    archive_cli.register(sub)

    p_doctor = sub.add_parser("doctor", help="Run health checks")
    p_doctor.set_defaults(func=lambda _: _memory_cli._main_standalone(["doctor"]))

    p_add = sub.add_parser("add", help="Manually add a memory")
    p_add.add_argument("text", help="Memory content")
    p_add.add_argument("--user-id", help="Override HY_MEMORY_USER_ID")
    p_add.add_argument("--agent-id", help="Override HY_MEMORY_AGENT_ID")
    p_add.set_defaults(func=lambda args: _memory_cli._main_standalone(
        ["add", args.text, "--user-id", args.user_id or "hermes-user", "--agent-id", args.agent_id or "default"]
    ))

    p_search = sub.add_parser("search", help="Search memories")
    p_search.add_argument("query", help="Search query")
    p_search.add_argument("--limit", type=int, default=10)
    p_search.add_argument("--user-id", help="Override HY_MEMORY_USER_ID")
    p_search.add_argument("--agent-id", help="Override HY_MEMORY_AGENT_ID")
    p_search.set_defaults(func=lambda args: _memory_cli._main_standalone(
        ["search", args.query, "--limit", str(args.limit), "--user-id", args.user_id or "hermes-user", "--agent-id", args.agent_id or "default"]
    ))

    p_list = sub.add_parser("list", help="List recent memories")
    p_list.add_argument("--limit", type=int, default=20)
    p_list.add_argument("--user-id", help="Override HY_MEMORY_USER_ID")
    p_list.add_argument("--agent-id", help="Override HY_MEMORY_AGENT_ID")
    p_list.set_defaults(func=lambda args: _memory_cli._main_standalone(
        ["list", "--limit", str(args.limit), "--user-id", args.user_id or "hermes-user", "--agent-id", args.agent_id or "default"]
    ))

    p_delete = sub.add_parser("delete", help="Delete a memory by ID")
    p_delete.add_argument("memory_id", help="Memory ID")
    p_delete.add_argument("--user-id", help="Override HY_MEMORY_USER_ID")
    p_delete.add_argument("--agent-id", help="Override HY_MEMORY_AGENT_ID")
    p_delete.add_argument("--yes", "-y", action="store_true", help="Skip confirmation")
    p_delete.set_defaults(func=lambda args: _memory_cli._main_standalone(
        ["delete", args.memory_id, "--user-id", args.user_id or "hermes-user", "--agent-id", args.agent_id or "default"]
        + (["--yes"] if args.yes else [])
    ))

    p_reset = sub.add_parser("reset", help="Erase all memories (DESTRUCTIVE)")
    p_reset.add_argument("--user-id", help="Override HY_MEMORY_USER_ID")
    p_reset.add_argument("--agent-id", help="Override HY_MEMORY_AGENT_ID")
    p_reset.add_argument("--all-agents", action="store_true", help="Delete across all agents")
    p_reset.add_argument("--yes", "-y", action="store_true", help="Skip confirmation")
    p_reset.set_defaults(func=lambda args: _memory_cli._main_standalone(
        ["reset", "--user-id", args.user_id or "hermes-user", "--agent-id", args.agent_id or "default"]
        + (["--all-agents"] if args.all_agents else [])
        + (["--yes"] if args.yes else [])
    ))

    p_console = sub.add_parser("console", help="Show live status console (Ctrl+C to exit)")
    p_console.set_defaults(func=lambda _: _memory_cli._cmd_console(argparse.Namespace(no_start=False)))

    p_venv = sub.add_parser("venv", help="Manage the dedicated HyAtlas venv (dependency isolation)")
    venv_sub = p_venv.add_subparsers(dest="venv_cmd", required=True)
    p_venv_setup = venv_sub.add_parser(
        "setup",
        help="Create $HYATLAS_HOME/venv with isolated deps (fixes embedder conflicts + orphan windows)",
    )
    p_venv_setup.set_defaults(func=lambda _: _start._venv_cli(["setup"]))

    args = parser.parse_args(filtered)
    if getattr(args, "func", None) is _run_start:
        if not getattr(args, "detach", False):
            args.detach = global_detach
        if not getattr(args, "restart", False):
            args.restart = global_restart
    if not hasattr(args, "func"):
        # Bare `hyatlas` with no args — start (foreground if TTY, else detached).
        return _run_start(
            argparse.Namespace(detach=global_detach, restart=global_restart)
        )
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
