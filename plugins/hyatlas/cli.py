"""``hermes hyatlas`` subcommand — health, search, add, recent, start, stop.

Hermes wires this file in as the active memory provider's CLI (see
``plugins.memory.discover_plugin_cli_commands``): it imports this module under
a synthetic parent package and calls ``register_cli(parser)``. Because that
parent is synthetic (``__init__.py`` is never executed), the provider module
is loaded from disk explicitly via ``_load_root()`` — a plain
``from . import __init__`` binds a method-wrapper, not the module.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import logging
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from .client import HyatlasClient, HyatlasClientError, HyatlasUnreachable

logger = logging.getLogger(__name__)


def _load_root() -> Any:
    """Provider module (the package ``__init__.py``).

    In a full session the package may already be imported under its real name;
    reuse that. Otherwise load the file explicitly — the CLI's synthetic
    parent package has no executed ``__init__``, so relative access to the
    package itself is unavailable.
    """
    pkg = __package__ or "hyatlas"
    mod = sys.modules.get(pkg)
    if mod is not None and hasattr(mod, "HyatlasMemoryProvider"):
        return mod
    name = f"{pkg}._root"
    mod = sys.modules.get(name)
    if mod is None:
        spec = importlib.util.spec_from_file_location(name, Path(__file__).resolve().parent / "__init__.py")
        if spec is None or spec.loader is None:
            raise RuntimeError("could not load the hyatlas provider module")
        mod = importlib.util.module_from_spec(spec)
        sys.modules[name] = mod
        spec.loader.exec_module(mod)
    return mod


def _provider() -> Any:
    return _load_root().HyatlasMemoryProvider()


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
    # Default (unset): the server's mode decides. Lite includes raw rows, since
    # they are the only rows lite stores. --include-raw / --no-include-raw override.
    p_recent.add_argument("--include-raw", dest="include_raw", action="store_true")
    p_recent.add_argument("--no-include-raw", dest="include_raw", action="store_false")
    p_recent.set_defaults(include_raw=None, func=_cmd_recent)

    p_start = sub.add_parser("start", help="Start the v4 Go server (canonical launcher when present)")
    p_start.set_defaults(func=_cmd_start)

    p_stop = sub.add_parser("stop", help="Stop the v4 Go server")
    p_stop.set_defaults(func=_cmd_stop)


def _client_from_args(args: argparse.Namespace) -> HyatlasClient:
    """Build a client from the plugin's loaded config."""
    return _provider()._ensure_client()


def _identity(provider: Any) -> "tuple[str, str]":
    """Resolved (user_id, agent_id) — same order the provider uses at init."""
    return provider._resolve_user_id({}), provider._resolve_agent_id({})


def _launcher(cfg: dict) -> "Path | None":
    """The user-configured launcher script, when one is set and exists.

    Some installs ship a ``hyatlas-go.ps1`` that owns the full server env (data
    dir, LLM configuration, log redirect). When ``launcher_path`` names one,
    ``hermes hyatlas start|stop`` runs it instead of spawning the binary.

    The script is never discovered implicitly: a script that runs a shell is only
    executed when the user names it in ``launcher_path``. The script itself is the
    user's tooling and is not part of this plugin; it may read other tools'
    credentials (the repository's ``hyatlas-go.ps1`` reads Hermes' ``auth.json``).
    With no ``launcher_path`` the binary is spawned directly.
    """
    if sys.platform != "win32":
        return None
    configured = str(cfg.get("launcher_path") or "").strip()
    if configured and Path(configured).is_file():
        return Path(configured)
    return None


def _run_launcher(ps1: Path, action: str, timeout: int = 90) -> int:
    """Run the canonical launcher, tolerant of its detached child holding handles.

    The server the launcher spawns inherits the launcher's stdio pipe handles,
    so a captured PIPE stays open long after powershell exits — and
    ``subprocess.run(capture_output=True)`` then hangs forever re-waiting on
    it. Redirect to a temp FILE instead and wait on the process itself, with a
    health check as the final arbiter.
    """
    out_path = Path(tempfile.gettempdir()) / "hyatlas-launcher.out"
    with open(out_path, "w", encoding="utf-8", errors="replace") as sink:
        proc = subprocess.Popen(
            ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(ps1), action],
            stdout=sink, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        )
        try:
            rc = proc.wait(timeout=timeout)
            out = out_path.read_text(encoding="utf-8", errors="replace").strip()
            _print({"ok": rc == 0, "via": str(ps1), "output": out})
            return 0 if rc == 0 else 1
        except subprocess.TimeoutExpired:
            proc.kill()
            out = out_path.read_text(encoding="utf-8", errors="replace").strip()
            ok = _provider()._ensure_client().wait_until_reachable(timeout=20.0)
            _print({"ok": ok, "via": str(ps1), "output": out,
                    "note": "launcher did not return; health-checked directly"})
            return 0 if ok else 1


def _print(obj: Any) -> None:
    print(json.dumps(obj, indent=2, default=str))


def _cmd_status(args: argparse.Namespace) -> int:
    try:
        client = _client_from_args(args)
        if not client.is_reachable():
            _print({"error": "server unreachable",
                    "hint": "Start it with `hermes hyatlas start`. The `hermes hyatlas` command "
                            "exists only while memory.provider is hyatlas (`hermes memory setup`)."})
            return 1
        _print(client.status())
        return 0
    except (HyatlasClientError, HyatlasUnreachable) as e:
        _print({"error": str(e)})
        return 1


def _cmd_search(args: argparse.Namespace) -> int:
    try:
        provider = _provider()
        client = provider._ensure_client()
        user_id, agent_id = _identity(provider)
        results = client.search(
            query=args.query,
            user_id=user_id,
            agent_id=agent_id,
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
        provider = _provider()
        client = provider._ensure_client()
        user_id, agent_id = _identity(provider)
        resp = client.add(
            text=args.text,
            user_id=args.user_id or user_id,
            agent_id=args.agent_id or agent_id,
        )
        _print(resp)
        return 0
    except (HyatlasClientError, HyatlasUnreachable) as e:
        _print({"error": str(e)})
        return 1


def _cmd_recent(args: argparse.Namespace) -> int:
    try:
        provider = _provider()
        client = provider._ensure_client()
        user_id, agent_id = _identity(provider)
        items = client.list_memories(
            user_id=user_id,
            agent_id=agent_id,
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
    provider = _provider()
    ps1 = _launcher(provider._config)
    if ps1 is not None:
        return _run_launcher(ps1, "start")
    # No canonical launcher (non-Windows / custom layout): spawn the binary
    # directly. Set HYATLAS_GO_DATA when the binary does not sit next to its
    # data/ dir — the server otherwise creates a fresh store beside itself.
    # start_server refuses to spawn over a server that already answers, reports a
    # child that died during startup as ok:false, and rejects an invalid mode or
    # sync setting before spawning.
    from . import process as process_mod
    result = process_mod.start_server(provider._config, timeout=30.0)
    _print(result)
    return 0 if result.get("ok") else 1


def _cmd_stop(args: argparse.Namespace) -> int:
    provider = _provider()
    ps1 = _launcher(provider._config)
    if ps1 is not None:
        return _run_launcher(ps1, "stop", timeout=60)
    from . import process as process_mod
    result = process_mod.HyatlasProcess.stop_running(provider._config)
    _print(result)
    return 0 if result.get("ok") else 1


def _main_standalone(argv: Any = None) -> int:
    """For ``python -m plugins.memory.hyatlas`` standalone usage."""
    parser = argparse.ArgumentParser(prog="hyatlas", description=__doc__)
    register_cli(parser)
    args = parser.parse_args(argv)
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(_main_standalone())
