"""HyAtlas status window — flicker-free live health + log tail.

Safe to close (Ctrl+C or X): only this window dies, the server keeps running.
Reopen with `hyatlas console` or by double-clicking `bin/hyatlas-status.bat`.
"""
from __future__ import annotations

import contextlib
import json
import os
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

SERVER = "http://127.0.0.1:19527"
DASH = "http://127.0.0.1:8765"


def _log_path() -> Path:
    """Resolve server log path via layout, not a machine-specific default."""
    try:
        from hyatlas_memory import layout

        return layout.logs() / "hy-memory_server.log"
    except Exception:
        home = Path(os.environ.get("HYATLAS_HOME", "") or (Path.home() / ".hyatlas"))
        return home / "logs" / "hy-memory_server.log"


LOG_PATH = _log_path()
TAIL_LINES = 8
POLL_INTERVAL = 2  # seconds


def _fetch(url: str, timeout: int = 5) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def _health_lines() -> list[str]:
    """Render the static health header + a single status line that we'll
    refresh in place via \\r on each poll. No full-screen clear = no flicker."""
    srv = _fetch(f"{SERVER}/api/v1/status")
    dash = _fetch(f"{DASH}/api/status", timeout=8)
    now = datetime.now().strftime("%H:%M:%S")

    lines = [
        f"\033[36m{'=' * 60}\033[0m",
        f"\033[36m  HyAtlas Memory  \033[0m  \033[2mrefreshed {now}\033[0m",
        f"\033[36m{'=' * 60}\033[0m",
        "",
    ]

    if srv:
        checks = [
            ("Server", True),
            ("VDB", srv.get("vdb") == "ok"),
            ("Embed", srv.get("embed") == "ok"),
            ("LLM", srv.get("llm") == "ok"),
            ("Writer", srv.get("write_pipeline") == "ok"),
        ]
        for label, ok in checks:
            icon = "\033[32mPASS\033[0m" if ok else "\033[31mFAIL\033[0m"
            lines.append(f"  {label:<8} {icon}")
        lines.append(f"  Storage  {srv.get('vdb_provider', '?')} / {srv.get('vdb_collection', '?')}")
    else:
        lines.append("  \033[31mServer not responding on :19527\033[0m")

    dash_icon = "\033[32monline\033[0m" if dash else "\033[31moffline\033[0m"
    lines.append(f"  Dashboard  http://127.0.0.1:8765   [{dash_icon}]")
    lines.append("")
    return lines


def _tail_lines() -> list[str]:
    """Last N lines of the server log, color-coded."""
    try:
        with open(LOG_PATH, "rb") as f:
            data = f.read().decode("utf-8", errors="replace")
        all_lines = [ln for ln in data.strip().split("\n") if ln]
        return all_lines[-TAIL_LINES:]
    except Exception:
        return ["(no log file found)"]


def _colorize(line: str) -> str:
    line = line.strip()
    if "[ERROR]" in line:
        return f"\033[31m{line}\033[0m"
    if "[WARNING]" in line:
        return f"\033[33m{line}\033[0m"
    return f"\033[90m{line}\033[0m"


def main() -> int:
    # Make sure stdout is line-buffered so we don't sit on bytes across polls.
    with contextlib.suppress(Exception):
        sys.stdout.reconfigure(line_buffering=True)

    # Disable cursor so it doesn't blink at the bottom of the screen.
    sys.stdout.write("\033[?25l")
    sys.stdout.flush()

    last_log_mtime: float = 0.0
    last_log_size: int = 0
    log_lines_cache: list[str] = []

    def draw_header() -> None:
        # Print the health header once per cycle. No clear.
        for line in _health_lines():
            sys.stdout.write(line + "\n")
        sys.stdout.write("  " + "\033[2m" + "-" * 56 + "\033[0m" + "\n")
        for line in (_colorize(ln) for ln in log_lines_cache):
            sys.stdout.write(line + "\n")
        sys.stdout.write("\n")
        sys.stdout.write("  \033[2mClose this window anytime -- server keeps running.\033[0m\n")
        sys.stdout.write("  \033[2mReopen: hyatlas console\033[0m\n")
        sys.stdout.flush()

    try:
        # Initial render
        log_lines_cache = _tail_lines()
        draw_header()

        while True:
            time.sleep(POLL_INTERVAL)

            # Read log only if changed (mtime+size guard avoids re-reading).
            try:
                st = LOG_PATH.stat()
                if st.st_mtime != last_log_mtime or st.st_size != last_log_size:
                    last_log_mtime = st.st_mtime
                    last_log_size = st.st_size
                    log_lines_cache = _tail_lines()
                    # Refresh header + tail when log changed.
                    sys.stdout.write("\033[H")  # cursor home
                    sys.stdout.write("\033[J")  # clear below
                    draw_header()
            except FileNotFoundError:
                log_lines_cache = ["(waiting for log file)"]

            # Otherwise: just nudge the refreshed-timestamp without redrawing.
            # Move up to the "refreshed HH:MM:SS" line, overwrite, restore cursor.
            # Header is roughly 5 lines tall. We don't know exact terminal size,
            # so do a tiny in-place update by re-printing the header below the
            # previous one is overkill — instead, just keep silent. The next
            # log change will trigger a full redraw with fresh timestamp.
    except KeyboardInterrupt:
        pass
    finally:
        # Re-enable cursor
        sys.stdout.write("\033[?25h")
        sys.stdout.write("\n\n  Status window closed. HyAtlas server keeps running.\n\n")
        sys.stdout.flush()
    return 0


if __name__ == "__main__":
    sys.exit(main())
