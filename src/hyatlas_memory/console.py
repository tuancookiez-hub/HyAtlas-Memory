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
    line = f"{ts}  {level:<8} {name:<20} {msg}"
    return line


# ---------------------------------------------------------------------------
# Log-file tailer — the cross-process bridge
# ---------------------------------------------------------------------------
#
# v1.4.2: the in-process queue handler only sees events logged by THIS
# Python process. Writes from a separate Hermes session (or from the
# dashboard, the MCP, the agent, etc.) all write to
# %LOCALAPPDATA%\hermes\logs\hyatlas-memory.log. Tail that file in a
# background thread and feed formatted lines into the same output
# buffer. This is the only practical way to give the user a single
# "what is the memory system doing right now" surface that covers
# all three services AND all client processes.
#
# Why a tail and not a listener: Windows does not have inotify
# equivalents as portable as Linux. A 200ms read-and-seek loop is
# the lowest-common-denominator that works on every supported
# platform and every log rotation scheme.
# ---------------------------------------------------------------------------

def _parse_log_line(line: str) -> str | None:
    """Convert a raw log line to the same visual format as the
    in-process queue handler produces, or return None to skip.

    Three line families are observed in the v1.4.x stack:

    1. Upstream Python logs:
         2026-06-22 23:40:24 [INFO] [trace-id] module.path: message
    2. Qdrant (Rust) logs:
         2026-06-22T15:40:18.818627Z  INFO actix_web::middleware::logger: ...
    3. Dashboard (Tornado) logs:
         [dash] 127.0.0.1 - "GET / HTTP/1.1" 200 -

    Each is normalized to the same shape: time, level, source, message.
    The visual filter that follows is the "interesting event"
    predicate — we skip pure health pings (the user's ticker should
    not be drowned in ``[dash] GET /`` lines) but keep writes,
    recalls, extractions, L5 loads, and errors.
    """
    import re

    line = line.rstrip()
    if not line:
        return None

    # Family 1 — upstream Python
    m = re.match(
        r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) \[(\w+)\] "
        r"(?:\[([a-f0-9-]+)\] )?([\w.]+): (.*)$",
        line,
    )
    if m:
        ts, level, _trace, source, msg = m.groups()
        ts_short = ts.split(" ")[1][:8]  # HH:MM:SS
        source = source.replace("hy_memory.", "").replace("hyatlas_memory.", "")
        return f"{ts_short}  {level:<8} {source:<24} {msg}"

    # Family 2 — Qdrant
    m = re.match(
        r"^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.\d+Z)?\s+"
        r"(\w+)\s+([\w:]+): (.*)$",
        line,
    )
    if m:
        ts, level, source, msg = m.groups()
        ts_short = ts.split("T")[1][:8]
        # Drop pure actix middleware chatter; keep everything else.
        if "actix_web::middleware::logger" in source and "GET /healthz" in msg:
            return None
        if "actix_web::middleware::logger" in source and 'GET / HTTP' in msg:
            return None  # dashboard polling pings — noise
        if "actix_web::middleware::logger" in source and "POST" not in msg:
            return None
        return f"{ts_short}  {level:<8} {source.split('::')[-1]:<24} {msg}"

    # Family 3 — dashboard access log
    if line.startswith("[dash] "):
        # Skip routine dashboard pings; keep writes.
        rest = line[len("[dash] "):]
        if '"GET / HTTP' in rest or '"GET /favicon' in rest:
            return None
        ts_short = datetime.now().strftime("%H:%M:%S")
        return f"{ts_short}  INFO    dashboard                  {rest}"

    return None


_INTERESTING_RE = (
    # Anything that represents a real user-visible event.
    r"extraction|recall|"
    r"sync_turn|sync-turn|"
    r"TRACE_PERF|"
    r"pipeline|pipelines\.writer|"
    r"L5.*load|L5.*export|"
    r"vector-store.*(add|delete|update|query)|"
    r"mem_agent|"
    r"reconcil|"
    r"ERROR|WARN|"
    r"stack|started|stopped|"
    r"went DOWN|came UP|"
    r"\[trace\]"
)


def _tail_log_file(
    log_path: Path,
    state: _State,
    stop_event: threading.Event,
) -> None:
    """Background thread: tail ``log_path`` and update ``state.current``.

    Bounded seek-and-read loop. The file may be rotated by
    Python's ``RotatingFileHandler`` (size-based); we handle that
    by re-opening on size-shrink. New lines are detected by
    recording the file size and reading forward.
    """
    if not log_path.exists():
        return
    try:
        f = open(log_path, "r", encoding="utf-8", errors="replace")
    except OSError:
        return
    # Seek to end so we don't replay the entire log on launch.
    f.seek(0, 2)
    last_size = f.tell()
    import re as _re
    interesting = _re.compile(_INTERESTING_RE, _re.IGNORECASE)

    while not stop_event.is_set():
        line = f.readline()
        if line:
            last_size = f.tell()
            if not interesting.search(line):
                continue
            formatted = _parse_log_line(line)
            if formatted is None:
                continue
            with state.lock:
                state.current = formatted
                state.current_version += 1
                state.last_event_at = time.monotonic()
                state.recent.append(formatted)
                if len(state.recent) > 8:
                    del state.recent[: len(state.recent) - 8]
            continue
        # No line available. Sleep briefly, then re-check.
        time.sleep(0.2)
        # Rotation: if the file shrank, reopen and seek to start.
        try:
            cur = log_path.stat().st_size
        except OSError:
            cur = last_size
        if cur < last_size:
            f.close()
            try:
                f = open(log_path, "r", encoding="utf-8", errors="replace")
            except OSError:
                return
            last_size = 0
    try:
        f.close()
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Layout — fixed line positions, no scrolling
# ---------------------------------------------------------------------------
#
# The screen is laid out as a fixed set of lines, each with a
# known row number. The render loop moves the cursor to each row
# with \x1b[<n>;1H and overwrites the line in place — no clearing
# the screen, no reprinting the whole layout, no flash.
#
# Line numbers are 1-indexed. Rows below 12 are reserved for
# the recent-events tail (8 lines, max). The terminal needs to be
# at least ~20 rows tall; on a smaller window the bottom lines
# may be clipped, which is acceptable.
# ---------------------------------------------------------------------------

# Each entry: (row, label, format_fn(state) -> str)
# The render loop iterates this list and writes each row in place.
_LAYOUT_TEMPLATE: list[tuple[str, str]] = [
    # (kind, source) — kind is "static" or "health" or "current" or "recent"
    ("bar",         ""),                                       # row 1
    ("title",       ""),                                       # row 2
    ("bar",         ""),                                       # row 3
    ("blank",       ""),                                       # row 4
    ("health",      "qdrant"),                                 # row 5
    ("health",      "upstream"),                               # row 6
    ("health",      "dashboard"),                              # row 7
    ("blank",       ""),                                       # row 8
    ("section",     "Currently doing"),                        # row 9
    ("current",     ""),                                       # row 10
    ("section",     "Last events"),                            # row 11
    # rows 12-19: 8 recent-event slots (rendered by _render_recent)
]


_SERVICE_BY_KEY = {name.lower(): (name, desc, port) for name, desc, port in _SERVICES}


def _ansi_move(row: int, col: int = 1) -> str:
    """ANSI cursor-position: \x1b[<row>;<col>H (1-indexed)."""
    return f"\x1b[{row};{col}H"


def _ansi_clear_eol() -> str:
    """ANSI: erase from cursor to end of line."""
    return "\x1b[K"


def _truncate(text: str, width: int) -> str:
    """Trim ``text`` to ``width`` columns, adding an ellipsis if cut."""
    if len(text) <= width:
        return text
    return text[: max(0, width - 1)] + "…"


# Pre-compute width budget for the activity line. The terminal
# default is 80 cols; on wider terminals the budget grows. Cap at
# 120 so the layout stays visually balanced.
_WIDTH = 78


def _row_static_bar(_state: _State) -> str:
    return _color("cyan", "═" * _WIDTH)


def _row_static_title(_state: _State) -> str:
    return (
        _color("bold", f"  ✦ HyAtlas-Memory ")
        + _color("bright_white", f"v{__version__}")
        + "  "
        + _color("dim", "— live status window")
    )


def _row_static_section(label: str) -> str:
    return _color("dim", f"  ── {label} " + "─" * max(0, _WIDTH - len(label) - 6))


def _row_health(key: str, state: _State) -> str:
    name, desc, port = _SERVICE_BY_KEY[key]
    up = state.health.get(port, False)
    if up:
        status = _color("green", "● healthy")
    else:
        status = _color("red", "○ down")
    return (
        f"  {_color('cyan', name):<12} {desc:<22} :{port:<5}  {status}"
    )


def _row_current(state: _State) -> str:
    if not state.current:
        return f"  {_color('dim', '— idle —')}"
    return _truncate(f"  {state.current}", _WIDTH)


def _row_recent(idx: int, state: _State) -> str:
    """Row 12+ : 8 slots for the most recent events (newest at idx 0)."""
    if idx >= len(state.recent):
        return ""
    line = state.recent[-(idx + 1)]
    return _truncate(f"  {_color('gray', '·')} {line}", _WIDTH)


_ROW_FNS = {
    "bar":     lambda state, kind: _row_static_bar(state),
    "title":   lambda state, kind: _row_static_title(state),
    "section": lambda state, kind: _row_static_section(kind),
    "health":  lambda state, kind: _row_health(kind, state),
    "current": lambda state, kind: _row_current(state),
    "recent":  lambda state, kind: _row_recent(int(kind), state),
    "blank":   lambda state, kind: "",
}


# Each rendered line is (row_number, text). The render loop writes
# each row at its position. _RECENT_ROWS is filled at startup time
# because it depends on the layout length.
def _build_line_plan() -> list[tuple[int, str]]:
    """Return [(row, text-factory-kind, kind-arg), ...] in row order."""
    plan: list[tuple[int, str, str]] = []
    row = 1
    for kind, arg in _LAYOUT_TEMPLATE:
        plan.append((row, kind, arg))
        row += 1
    # 8 recent-event rows
    for i in range(8):
        plan.append((row, "recent", str(i)))
        row += 1
    return plan


_LINE_PLAN = _build_line_plan()
_N_ROWS = _LINE_PLAN[-1][0]


def _render_once(state: _State) -> None:
    """Emit one full layout to the terminal, in place.

    Uses absolute cursor positioning so this function can be called
    any number of times. On the first call it draws the static
    frame; on subsequent calls it just overwrites the live cells
    with their current values.
    """
    out = []
    for row, kind, arg in _LINE_PLAN:
        fn = _ROW_FNS[kind]
        out.append(_ansi_move(row) + fn(state, arg) + _ansi_clear_eol())
    sys.stdout.write("".join(out))
    sys.stdout.flush()


def _update_lines(state: _State, last_health_version: int, last_current_version: int) -> tuple[int, int]:
    """Refresh only the rows that have changed since last render.

    Returns the new versions. Cheap to call: 3 health rows + 1 current
    row + 8 recent rows = at most 12 short writes per tick.
    """
    writes: list[str] = []
    # Health rows: only re-emit if the health snapshot changed
    if state.health_version != last_health_version:
        for row, kind, arg in _LINE_PLAN:
            if kind == "health":
                writes.append(_ansi_move(row) + _row_health(arg, state) + _ansi_clear_eol())
    # Current + recent: only re-emit if the current event changed
    if state.current_version != last_current_version:
        for row, kind, arg in _LINE_PLAN:
            if kind == "current":
                writes.append(_ansi_move(row) + _row_current(state) + _ansi_clear_eol())
            elif kind == "recent":
                writes.append(_ansi_move(row) + _row_recent(int(arg), state) + _ansi_clear_eol())
    if writes:
        sys.stdout.write("".join(writes))
        sys.stdout.flush()
    return state.health_version, state.current_version


# ---------------------------------------------------------------------------
# Console state — one struct, no buffer, no scrolling
# ---------------------------------------------------------------------------
#
# v1.4.2 redesign: the previous version cleared the whole screen and
# reprinted the buffer every 1 s, which produced a visible flash and
# a scrolling tail the user did not want. The fix is in-place
# overwrite using ANSI cursor positioning: print the static layout
# once, then update a small set of fixed lines by jumping the cursor
# to each line, writing the new content, and \x1b[K-ing the rest.
#
# All workers write to ``State`` (a thread-safe struct) instead of
# appending to a list. The render loop reads ``State`` and emits
# only the deltas, never the whole screen.
# ---------------------------------------------------------------------------

class _State:
    """Thread-shared snapshot of everything the console renders."""

    __slots__ = (
        "lock",
        "health",          # {port: bool}
        "health_version",  # bumped each successful probe; lets the
                           # render loop skip no-op updates
        "current",         # str — the "Currently:" line content
        "current_version", # bumped on each new event
        "last_event_at",   # float — monotonic timestamp of last event
        "recent",          # list[str] — last few events for the
                           # "Last events:" tail (small bounded ring)
    )

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.health: dict[int, bool] = {}
        self.health_version: int = 0
        self.current: str = "— idle —"
        self.current_version: int = 0
        self.last_event_at: float = 0.0
        self.recent: list[str] = []


def _consume_log_queue(
    q: "queue.Queue[logging.LogRecord]",
    state: _State,
    stop_event: threading.Event,
) -> None:
    """Drain the in-process log queue and update ``state.current``."""
    while not stop_event.is_set():
        try:
            record = q.get(timeout=0.5)
        except queue.Empty:
            continue
        try:
            line = _format_record(record)
        except Exception:
            continue
        with state.lock:
            state.current = line
            state.current_version += 1
            state.last_event_at = time.monotonic()
            state.recent.append(line)
            if len(state.recent) > 8:
                del state.recent[: len(state.recent) - 8]


def _health_poll_loop(
    state: _State,
    stop_event: threading.Event,
) -> None:
    """Refresh the health indicator every 2 s and update ``state.health``.

    A health change is also surfaced as an activity event so the
    "Currently:" line records transitions (Service went DOWN, came UP).
    """
    while not stop_event.is_set():
        time.sleep(2.0)
        if stop_event.is_set():
            break
        new_health = {port: _probe_port(port) for _, _, port in _SERVICES}
        with state.lock:
            old = state.health
            for port, up in new_health.items():
                was = old.get(port)
                if was is None or was == up:
                    continue
                name = next(n for n, _, p in _SERVICES if p == port)
                if was and not up:
                    msg = f"{name} :{port} went DOWN"
                else:
                    msg = f"{name} :{port} came UP"
                state.current = msg
                state.current_version += 1
                state.last_event_at = time.monotonic()
                state.recent.append(msg)
                if len(state.recent) > 8:
                    del state.recent[: len(state.recent) - 8]
            state.health = new_health
            state.health_version += 1


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

    state = _State()
    q = subscribe()

    # Prime the state with a current health snapshot so the first
    # render is accurate, not "all down".
    with state.lock:
        state.health = {port: _probe_port(port) for _, _, port in _SERVICES}
        state.health_version = 1

    consumer = threading.Thread(
        target=_consume_log_queue,
        args=(q, state, stop_event),
        daemon=True,
        name="hyatlas-log-consumer",
    )
    consumer.start()

    poller = threading.Thread(
        target=_health_poll_loop,
        args=(state, stop_event),
        daemon=True,
        name="hyatlas-health-poll",
    )
    poller.start()

    # Cross-process bridge: tail %LOCALAPPDATA%\hermes\logs\hyatlas-memory.log
    # and feed interesting lines into state.current. This is the only
    # way the user sees writes that come from a different Python process.
    try:
        from hermes_constants import get_hermes_home as _ghh
        _hermes_home = Path(_ghh())
    except Exception:
        _hermes_home = Path(
            os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local"))
        ) / "hermes"
    log_path = _hermes_home / "logs" / "hyatlas-memory.log"
    tailer = threading.Thread(
        target=_tail_log_file,
        args=(log_path, state, stop_event),
        daemon=True,
        name="hyatlas-log-tailer",
    )
    tailer.start()

    # Hide the cursor, clear the screen, draw the initial frame.
    try:
        sys.stdout.write("\x1b[?25l")  # hide cursor
        sys.stdout.write("\x1b[2J\x1b[H")
        sys.stdout.flush()
    except Exception:
        pass

    last_health_version = -1
    last_current_version = -1
    last_health_tick = 0.0
    last_paint = 0.0

    try:
        # First full render
        _render_once(state)
        last_health_version = state.health_version
        last_current_version = state.current_version
        last_health_tick = time.monotonic()
        last_paint = time.monotonic()

        while not stop_event.is_set():
            time.sleep(0.1)
            now = time.monotonic()

            # Health poll drives the poller thread; the thread bumps
            # state.health_version. We only need to call _render_once
            # when the health snapshot or the current event changed.
            if now - last_paint >= 0.25:  # 4 fps cap — fast enough for
                                          # perceived instant update, slow
                                          # enough that the user never
                                          # sees a flash
                last_paint = now
                new_h, new_c = _update_lines(
                    state, last_health_version, last_current_version
                )
                last_health_version = new_h
                last_current_version = new_c
    except KeyboardInterrupt:
        stop_event.set()
    finally:
        unsubscribe(q)
        stop_event.set()
        consumer.join(timeout=2.0)
        poller.join(timeout=2.0)
        tailer.join(timeout=2.0)
        # Restore cursor, clear screen, leave a clean goodbye.
        try:
            sys.stdout.write("\x1b[?25h\x1b[2J\x1b[H")
            sys.stdout.flush()
        except Exception:
            pass

    _stop_stack()
    print(_color("cyan", "HyAtlas-Memory console exited cleanly."), flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
