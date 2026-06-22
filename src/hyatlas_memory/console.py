"""HyAtlas-Memory visible status window.

v1.4.2: opens a Windows console that shows the live state of the
Qdrant + upstream + dashboard stack. The console is the user's
"what is the memory system doing right now" surface — matches the
style of ``Hermes_Gateway.cmd``.

Behavior:
  - Renders a header with service name, version, ports, and a per-
    service health indicator that refreshes every 2 seconds.
  - Subscribes to the in-process ``hyatlas_memory`` logger via
    :mod:`hyatlas_memory._console_handler`. Writes, recalls, L5
    loads, reconnects, and reconciler events appear as a scrolling
    activity ticker below the header.
  - **Ctrl+C** stops the entire stack (Qdrant + upstream + dashboard)
    via :class:`StackManager.stop` and exits with code 0. This is
    the user's single-key way to tear down the memory system without
    killing the parent Hermes gateway.

This module never auto-imports. It is launched explicitly by:

  1. The CLI subcommand ``hyatlas console`` (always visible window).
  2. The opt-in auto-launch hook in
     :func:`hyatlas_memory.HyMemoryProvider.initialize` when
     ``HERMES_HYATLAS_CONSOLE=1`` is set in the environment.

If the console is run when the stack is already up, it just attaches
to the existing logger and renders the header — it does NOT
re-spawn a second stack. This means you can open the console as
many times as you like without breaking anything.

ANSI color codes are used for the header (green = healthy, red =
unhealthy, yellow = starting). On Windows the codes work in any
modern console host (Windows Terminal, ConEmu, the new Windows
Console) without configuration. If colors are unwanted, set
``NO_COLOR=1`` in the environment.
"""

from __future__ import annotations

import logging
import os
import queue
import signal
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from ._console_handler import subscribe, unsubscribe
from ._version import __version__

logger = logging.getLogger("hyatlas_memory.console")

# Ports from process.py: kept in sync intentionally (single source of
# truth would be a future refactor; for v1.4.2 the constants are small
# enough to live here too).
_DEFAULT_PORTS = {
    "qdrant": 6333,
    "upstream": 19527,
    "dashboard": 8765,
}

# Service labels (left column of the header)
_SERVICES: list[tuple[str, str, int]] = [
    ("Qdrant", "Vector store", _DEFAULT_PORTS["qdrant"]),
    ("Upstream", "Hy-Memory server", _DEFAULT_PORTS["upstream"]),
    ("Dashboard", "Web UI", _DEFAULT_PORTS["dashboard"]),
]

_ANSI = {
    "reset": "\x1b[0m",
    "bold": "\x1b[1m",
    "dim": "\x1b[2m",
    "green": "\x1b[32m",
    "red": "\x1b[31m",
    "yellow": "\x1b[33m",
    "cyan": "\x1b[36m",
    "magenta": "\x1b[35m",
    "gray": "\x1b[90m",
    "bright_white": "\x1b[97m",
}


def _color(code: str, text: str) -> str:
    if os.environ.get("NO_COLOR"):
        return text
    return f"{_ANSI[code]}{text}{_ANSI['reset']}"


def _probe_port(port: int, path: str = "/", timeout: float = 0.6) -> bool:
    """Return True if the service at ``port`` answers on ``path``."""
    import socket
    import urllib.request
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            pass
    except OSError:
        return False
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return 200 <= resp.status < 500
    except Exception:
        return True  # port is open; service didn't answer / but is up
    return True


def _service_status(port: int) -> tuple[str, str]:
    """Return (color_code, label) for a service."""
    if _probe_port(port):
        return "green", "● healthy"
    return "red", "○ down"


def _health_table() -> list[str]:
    lines: list[str] = []
    for name, desc, port in _SERVICES:
        color, status = _service_status(port)
        status_text = _color(color, status)
        lines.append(
            f"  {_color('cyan', name):<12} {desc:<22} :{port:<5}  {status_text}"
        )
    return lines


def _format_record(record: logging.LogRecord) -> str:
    ts = datetime.fromtimestamp(record.created).strftime("%H:%M:%S")
    level = record.levelname
    name = record.name.replace("hyatlas_memory.", "")
    msg = record.getMessage()
    line = f"  {_color('gray', ts)}  {_color('yellow', level):<8} {_color('magenta', name):<20} {msg}"
    if record.exc_info:
        line += f"\n{_color('red', self.format(record))}" if False else ""  # noqa: E501
    return line


def _render_header() -> list[str]:
    bar = _color("cyan", "═" * 78)
    lines: list[str] = [
        bar,
        _color(
            "bold",
            f"  ✦ HyAtlas-Memory {_color('bright_white', f'v{__version__}')}  "
            f"{_color('dim', '— live status window')}",
        ),
        bar,
        "",
    ]
    lines.extend(_health_table())
    lines.append("")
    lines.append(
        _color("dim", "  " + "─" * 76)
    )
    lines.append(
        _color(
            "dim",
            f"  Activity  "
            f"({_color('yellow', 'Ctrl+C')} to stop the entire memory system)",
        )
    )
    lines.append(
        _color("dim", "  " + "─" * 76)
    )
    return lines


def _consume_log_queue(
    q: "queue.Queue[logging.LogRecord]",
    out_lines: list[str],
    out_lock: threading.Lock,
    stop_event: threading.Event,
) -> None:
    """Background thread: drain the in-process log queue and append to
    the visible console output buffer."""
    while not stop_event.is_set():
        try:
            record = q.get(timeout=0.5)
        except queue.Empty:
            continue
        try:
            line = _format_record(record)
        except Exception:
            continue
        with out_lock:
            out_lines.append(line)
            # Bound the visible buffer so a long-running session does
            # not exhaust memory. 500 lines is enough to read a few
            # minutes of activity without scrolling.
            if len(out_lines) > 500:
                del out_lines[: len(out_lines) - 500]


def _health_poll_loop(
    out_lines: list[str],
    out_lock: threading.Lock,
    stop_event: threading.Event,
) -> None:
    """Background thread: refresh the health indicator every 2 s.

    Re-renders the full screen so the user sees the latest state even
    if they aren't typing. This is the equivalent of the Hermes
    Gateway's status heartbeat.
    """
    last_status: dict[int, bool] = {}
    while not stop_event.is_set():
        time.sleep(2.0)
        if stop_event.is_set():
            break
        current = {port: _probe_port(port) for _, _, port in _SERVICES}
        if current != last_status:
            transitions: list[str] = []
            for port, up in current.items():
                was = last_status.get(port)
                name = next(n for n, _, p in _SERVICES if p == port)
                if was is None:
                    continue
                if was and not up:
                    transitions.append(
                        f"  {_color('gray', datetime.now().strftime('%H:%M:%S'))}  "
                        f"{_color('red', '!!')}  {name} :{port} went DOWN"
                    )
                elif not was and up:
                    transitions.append(
                        f"  {_color('gray', datetime.now().strftime('%H:%M:%S'))}  "
                        f"{_color('green', 'OK')}  {name} :{port} came UP"
                    )
            if transitions:
                with out_lock:
                    out_lines.extend(transitions)
            last_status = current


def _install_signal_handler(stop_event: threading.Event) -> None:
    def _handler(signum: int, frame: Any) -> None:
        stop_event.set()
    if sys.platform == "win32":
        try:
            signal.signal(signal.SIGINT, _handler)
        except (ValueError, OSError):
            pass
    else:
        signal.signal(signal.SIGINT, _handler)
        signal.signal(signal.SIGTERM, _handler)


def _stop_stack() -> None:
    """Best-effort stop of the running stack via the package's
    :class:`StackManager`. Imported lazily so the console can be
    launched even when the package import path is unusual.
    """
    try:
        from .process import StackManager
        try:
            from hermes_constants import get_hermes_home
            home = Path(get_hermes_home())
        except Exception:
            home = Path.home() / ".hermes" if sys.platform != "win32" else Path(
                os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
            ) / "hermes"
        manager = StackManager(
            project_root=Path(__file__).parent,
            hermes_home=home,
            log_dir=home / "logs",
        )
        manager.stop()
        print(_color("green", "  ✓ Stack stopped."), flush=True)
    except Exception as e:
        print(_color("yellow", f"  ! Stack stop best-effort: {e}"), flush=True)


def main() -> int:
    """Console entry point. Returns 0 on clean Ctrl+C shutdown."""
    stop_event = threading.Event()
    _install_signal_handler(stop_event)

    q = subscribe()
    out_lines: list[str] = []
    out_lock = threading.Lock()

    consumer = threading.Thread(
        target=_consume_log_queue,
        args=(q, out_lines, out_lock, stop_event),
        daemon=True,
        name="hyatlas-log-consumer",
    )
    consumer.start()

    poller = threading.Thread(
        target=_health_poll_loop,
        args=(out_lines, out_lock, stop_event),
        daemon=True,
        name="hyatlas-health-poll",
    )
    poller.start()

    last_render = 0.0
    try:
        while not stop_event.is_set():
            now = time.monotonic()
            if now - last_render >= 1.0:
                last_render = now
                _redraw(out_lines, out_lock)
            time.sleep(0.1)
    except KeyboardInterrupt:
        stop_event.set()
    finally:
        unsubscribe(q)
        stop_event.set()
        consumer.join(timeout=2.0)
        poller.join(timeout=2.0)

    _clear_screen()
    _stop_stack()
    print(_color("cyan", "HyAtlas-Memory console exited cleanly."), flush=True)
    return 0


def _clear_screen() -> None:
    try:
        sys.stdout.write("\x1b[2J\x1b[H")
        sys.stdout.flush()
    except Exception:
        pass


def _redraw(out_lines: list[str], out_lock: threading.Lock) -> None:
    _clear_screen()
    try:
        for line in _render_header():
            print(line, flush=False)
        with out_lock:
            tail = list(out_lines[-200:])
        for line in tail:
            print(line, flush=False)
        sys.stdout.flush()
    except Exception:
        pass


if __name__ == "__main__":
    sys.exit(main())
