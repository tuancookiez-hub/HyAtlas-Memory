"""Restart watcher: poll a parent PID, then re-launch the stack detached.

Adopted from Hermes' gateway restart pattern (gateway/run.py lines 4690-4789)
to make `hyatlas` restarts robust against parent-process deaths.

The watcher is spawned as a SIBLING process (not a child of `hyatlas`) using
`CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS | CREATE_NO_WINDOW` on Windows
(or `start_new_session=True` on POSIX). It polls the original `hyatlas`
PID; once that PID exits, it re-launches the HyAtlas stack detached. The
user's terminal gets control back immediately; the watcher handles the
relaunch out-of-band.

Why a watcher (instead of letting the parent re-launch directly)?

- If the user Ctrl+C's or closes the terminal during `stop_all()`, the
  parent `hyatlas` process dies. Children spawned by the parent may
  inherit a torn-down process group and die too. A watcher running as
  a detached sibling is independent of the parent's lifecycle.
- The watcher can do the polling synchronously without complicating
  `start_all`'s flow. It runs in its own process and only takes action
  once the parent has fully exited.
- Matches Hermes' pattern exactly, so anyone familiar with
  `hermes gateway restart` will recognize the flow.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Sequence
from contextlib import suppress


def _alive(pid: int) -> bool:
    """Return True if `pid` is still a live process (cross-platform).

    On Windows, ``os.kill(pid, 0)`` is NOT a no-op — it maps to
    ``GenerateConsoleCtrlEvent(0, pid)`` (bpo-14484), which can confuse
    the parent. Use the Win32 handle-based existence check instead.
    """
    if os.name == "nt":
        try:
            import ctypes  # noqa: PLC0415  (Windows-only import)

            k32 = ctypes.windll.kernel32
            # PROCESS_QUERY_LIMITED_INFORMATION (0x1000) | PROCESS_QUERY_INFORMATION (0x100000)
            handle = k32.OpenProcess(0x1000 | 0x100000, False, int(pid))
            if not handle:
                # ERROR_INVALID_PARAMETER (87) means the PID is gone.
                err = k32.GetLastError()
                return err != 87
            try:
                # WAIT_OBJECT_0 (0x0) = signaled (process exited)
                # WAIT_TIMEOUT  (0x102) = still running
                still = k32.WaitForSingleObject(handle, 0) == 0x102
            finally:
                k32.CloseHandle(handle)
            return still
        except Exception:
            return False
    try:
        os.kill(int(pid), 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _detach_kwargs() -> dict:
    """Cross-platform Popen kwargs for spawning a fully-detached child.

    On Windows: ``CREATE_NEW_PROCESS_GROUP | DETACHED_PROCESS | CREATE_NO_WINDOW``
    On POSIX: ``start_new_session=True`` (``setsid``)
    """
    if os.name == "nt":
        create_new_process_group = 0x00000200
        detached_process = 0x00000008
        create_no_window = 0x08000000
        return {"creationflags": create_new_process_group | detached_process | create_no_window}
    return {"start_new_session": True}


def wait_and_relaunch(
    parent_pid: int,
    relaunch_argv: Sequence[str],
    deadline_seconds: float = 120.0,
    poll_interval: float = 0.3,
) -> None:
    """Poll ``parent_pid`` until it exits, then re-launch ``relaunch_argv``.

    Args:
        parent_pid: The PID of the `hyatlas` process that initiated the restart.
            We wait for this PID to exit before re-launching.
        relaunch_argv: The command line to invoke for the fresh launch
            (typically ``[sys.executable, "-m", "hyatlas_memory._start", "--detach"]``).
        deadline_seconds: Maximum time to wait for the parent. After this we
            re-launch anyway (the parent may have crashed and never exited cleanly).
        poll_interval: Seconds between parent-alive checks.
    """
    deadline = time.monotonic() + deadline_seconds
    while time.monotonic() < deadline:
        if not _alive(parent_pid):
            break
        time.sleep(poll_interval)

    # Re-launch the stack detached. The user already has control of their
    # terminal back; the new children live independently.
    try:
        subprocess.Popen(
            list(relaunch_argv),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
            **_detach_kwargs(),
        )
    except Exception as e:
        # Best-effort: print to whatever stderr the watcher has (often
        # DEVNULL when spawned detached, but harmless).
        with suppress(Exception):
            print(f"[hyatlas restart watcher] re-launch failed: {e}", file=sys.stderr)


def main() -> int:
    """CLI entry point for the watcher.

    Usage:
        python -m hyatlas_memory._restart_watcher <parent_pid> <relaunch_argv...>

    Typically invoked internally by `hyatlas` itself; not user-facing.
    """
    if len(sys.argv) < 3:
        print(
            "Usage: python -m hyatlas_memory._restart_watcher <parent_pid> <relaunch_argv...>",
            file=sys.stderr,
        )
        return 1

    try:
        parent_pid = int(sys.argv[1])
    except ValueError:
        print(f"[hyatlas restart watcher] invalid parent_pid: {sys.argv[1]!r}", file=sys.stderr)
        return 1

    relaunch_argv = sys.argv[2:]
    wait_and_relaunch(parent_pid, relaunch_argv)
    return 0


if __name__ == "__main__":
    sys.exit(main())
