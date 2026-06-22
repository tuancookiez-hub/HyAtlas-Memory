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
import sys
from pathlib import Path

from hermes_constants import get_hermes_home

from . import _start, init_wizard
from . import cli as _memory_cli
from .installer import _install_plugin_shim, _update_config
from .process import StackManager


def _run_start(args) -> int:
    """Start the HyAtlas stack.

    If no args and not detached, run in foreground. If --detach, detach
    and exit. Otherwise delegate to the legacy ``hyatlas start`` behavior.
    """
    _start.detach = args.detach
    _start.force_restart = args.restart
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
    manager = StackManager(project_root=root, hermes_home=home, log_dir=home / "logs")
    if manager.ensure_running():
        print("✓ Stack auto-started successfully")
    else:
        print("! Stack auto-start failed — check logs and Qdrant availability")
        return 1

    print("\n[hyatlas] Setup complete. Restart Hermes to load the new plugin.")
    return 0


def main(argv: list[str] | None = None) -> int:
    """Main entry point for the ``hyatlas`` CLI."""
    parser = argparse.ArgumentParser(
        prog="hyatlas",
        description="HyAtlas-Memory command-line tool",
    )
    sub = parser.add_subparsers(dest="cmd", help="subcommand")

    # Legacy start/stop/status
    p_start = sub.add_parser("start", help="Start the HyAtlas stack")
    p_start.add_argument("--detach", action="store_true", help="Run detached")
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
    p_init.set_defaults(func=lambda _: init_wizard.run_interactive())

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

    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        # Bare `hyatlas` with no args — start in foreground (legacy behavior).
        return _run_start(argparse.Namespace(detach=False, restart=False))
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
