"""``hermes hyatlas`` subcommand — health, search, add, recent.

Hermes auto-calls ``register_cli(parser)`` with an already-created
``ArgumentParser`` for the ``hyatlas`` command. We add subcommands
to that parser.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
from typing import Any

from . import __init__ as plugin_root
from .client import HyatlasClient, HyatlasClientError, HyatlasUnreachable

logger = logging.getLogger(__name__)


def register_cli(plugin_parser: argparse.ArgumentParser) -> None:
    """Register ``hermes hyatlas <subcommand>`` subcommands."""
    sub = plugin_parser.add_subparsers(dest="hyatlas_cmd", required=True)

    p_status = sub.add_parser("status", help="Show v4 server health + layer counts")
    p_status.set_defaults(func=_cmd_status)

    p_search = sub.add_parser("search", help="Semantic search the v4 memory store")
    p_search.add_argument("query", help="Search query string")
    p_search.add_argument("--layer", default="", help="Restrict to one layer")
    p_search.add_argument("--limit", type=int, default=10)
    p_search.set_defaults(func=_cmd_search)

    p_add = sub.add_parser("add", help="Add a memory")
    p_add.add_argument("text", help="Memory text to store")
    p_add.add_argument("--user-id", default="")
    p_add.add_argument("--agent-id", default="")
    p_add.set_defaults(func=_cmd_add)

    p_recent = sub.add_parser("recent", help="List recent memories")
    p_recent.add_argument("--layer", default="")
    p_recent.add_argument("--limit", type=int, default=20)
    p_recent.add_argument("--include-raw", action="store_true")
    p_recent.set_defaults(func=_cmd_recent)

    p_start = sub.add_parser("start", help="Start the v4 Go binary as a subprocess")
    p_start.set_defaults(func=_cmd_start)

    p_stop = sub.add_parser("stop", help="Stop the v4 Go binary")
    p_stop.set_defaults(func=_cmd_stop)


def _client_from_args(args: argparse.Namespace) -> HyatlasClient:
    """Build a client from the plugin's loaded config."""
    provider = plugin_root.HyatlasMemoryProvider()
    return provider._ensure_client()


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _cmd_status(args: argparse.Namespace) -> int:
    try:
        client = _client_from_args(args)
        if not client.is_reachable():
            _print({"error": "server unreachable",
                    "hint": "Start it with `hermes hyatlas start` or run `hyatlas-go` directly"})
            return 1
        _print(client.status())
        return 0
    except (HyatlasClientError, HyatlasUnreachable) as e:
        _print({"error": str(e)})
        return 1


def _cmd_search(args: argparse.Namespace) -> int:
    try:
        client = _client_from_args(args)
        provider = plugin_root.HyatlasMemoryProvider()
        results = client.search(
            query=args.query,
            user_id=provider._user_id,
            agent_id=provider._agent_id,
            layer=args.layer,
            limit=args.limit,
        )
        _print(results)
        return 0
    except (HyatlasClientError, HyatlasUnreachable) as e:
        _print({"error": str(e)})
        return 1


def _cmd_add(args: argparse.Namespace) -> int:
    try:
        client = _client_from_args(args)
        provider = plugin_root.HyatlasMemoryProvider()
        resp = client.add(
            text=args.text,
            user_id=args.user_id or provider._user_id,
            agent_id=args.agent_id or provider._agent_id,
        )
        _print(resp)
        return 0
    except (HyatlasClientError, HyatlasUnreachable) as e:
        _print({"error": str(e)})
        return 1


def _cmd_recent(args: argparse.Namespace) -> int:
    try:
        client = _client_from_args(args)
        provider = plugin_root.HyatlasMemoryProvider()
        items = client.list_memories(
            user_id=provider._user_id,
            agent_id=provider._agent_id,
            layer=args.layer,
            limit=args.limit,
            include_raw=args.include_raw,
        )
        _print(items)
        return 0
    except (HyatlasClientError, HyatlasUnreachable) as e:
        _print({"error": str(e)})
        return 1


def _cmd_start(args: argparse.Namespace) -> int:
    from . import process as process_mod
    provider = plugin_root.HyatlasMemoryProvider()
    process = process_mod.HyatlasProcess(provider._config)
    try:
        process.start()
    except FileNotFoundError as e:
        _print({"ok": False, "error": str(e)})
        return 1
    if client := _client_from_args(args):
        if client.wait_until_reachable(timeout=30.0):
            _print({"ok": True, "started": True, "reachable": True})
            return 0
    _print({"ok": True, "started": True, "reachable": False,
            "hint": "binary started but not reachable on the configured port"})
    return 0


def _cmd_stop(args: argparse.Namespace) -> int:
    from . import process as process_mod
    process_mod.HyatlasProcess.stop_running()
    _print({"ok": True, "stopped": True})
    return 0


def _main_standalone(argv: Any = None) -> int:
    """For ``python -m plugins.memory.hy_memory`` standalone usage."""
    parser = argparse.ArgumentParser(prog="hyatlas", description=__doc__)
    register_cli(parser)
    args = parser.parse_args(argv)
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(_main_standalone())
