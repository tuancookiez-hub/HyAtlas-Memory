"""Subprocess lifecycle manager for the full HyAtlas-Memory stack.

Spawns Qdrant, the Hy-Memory upstream server, and the dashboard in
background. Designed to be called from ``HyMemoryProvider.initialize()``
and ``sync_turn()`` so the stack starts automatically on first use, like
Hindsight's embedded daemon.

Heavily inspired by ``hindsight_embed.daemon_embed_manager.DaemonEmbedManager``.
"""

from __future__ import annotations

import contextlib
import logging
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import IO

logger = logging.getLogger(__name__)

_DEFAULT_PORTS = {
    "qdrant": 6333,
    "upstream": 19527,
    "dashboard": 8765,
}

_HEALTH_TIMEOUT = 2
_HEALTH_RETRIES = 60
_HEALTH_DELAY = 1.0

_LOCK_DIR = Path(tempfile.gettempdir()) / "hyatlas-memory"


def _port_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(1)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def _pid_on_port(port: int) -> int | None:
    try:
        if sys.platform == "win32":
            r = subprocess.run(
                ["netstat", "-ano", "-p", "TCP"],
                capture_output=True, text=True, timeout=5,
            )
            for line in r.stdout.splitlines():
                if f"127.0.0.1:{port}" in line and "LISTENING" in line:
                    return int(line.strip().split()[-1])
        else:
            r = subprocess.run(
                ["lsof", "-ti", f":{port}", "-sTCP:LISTEN"],
                capture_output=True, text=True, timeout=5,
            )
            if r.returncode == 0 and r.stdout.strip():
                return int(r.stdout.strip().split()[0])
    except (subprocess.TimeoutExpired, ValueError, OSError, FileNotFoundError):
        pass
    return None


def _kill_pid(pid: int) -> bool:
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/F", "/PID", str(pid)], capture_output=True, timeout=10)
        else:
            os.kill(pid, 15)
            for _ in range(50):
                time.sleep(0.1)
                try:
                    os.kill(pid, 0)
                except OSError:
                    return True
    except OSError:
        return True
    except Exception:
        pass
    return False


def _detach_kwargs(log_handle: IO[bytes], *, visible: bool = False) -> dict:
    """Subprocess kwargs for spawning a stack service.

    ``visible=False`` (default) gives the v1.4.0 behavior: completely
    background, no console window, all output to the log file. This
    is what auto-start uses so the user is not spammed with windows
    on every plugin load.

    ``visible=True`` opens a new console window for the subprocess.
    Used by ``hyatlas console`` so the user can see Qdrant / upstream
    / dashboard output in real time. On non-Windows platforms the
    ``start_new_session`` flag is the closest equivalent (a new
    session leader) and is preserved either way.
    """
    if sys.platform == "win32":
        if visible:
            return {
                "creationflags": subprocess.CREATE_NEW_CONSOLE | subprocess.CREATE_NEW_PROCESS_GROUP,
                "stdin": subprocess.DEVNULL,
                "stdout": log_handle,
                "stderr": subprocess.STDOUT,
                "close_fds": True,
            }
        return {
            "creationflags": subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW,
            "stdin": subprocess.DEVNULL,
            "stdout": log_handle,
            "stderr": subprocess.STDOUT,
            "close_fds": True,
        }
    return {
        "start_new_session": True,
        "stdin": subprocess.DEVNULL,
        "stdout": log_handle,
        "stderr": subprocess.STDOUT,
    }


class StackManager:
    """Start/stop the Qdrant + upstream + dashboard stack."""

    def __init__(self, *, project_root: str | Path, hermes_home: str | Path, log_dir: str | Path):
        self._root = Path(project_root)
        self._home = Path(hermes_home)
        self._log_dir = Path(log_dir)
        self._lock = _LOCK_DIR / "stack.lock"
        self._lock_fd: IO | None = None
        self._procs: list[subprocess.Popen] = []
        self._log_path = self._log_dir / "hyatlas-memory.log"

    # ------------------------------------------------------------------
    # Config helpers
    # ------------------------------------------------------------------

    def _read_hy_memory_json(self) -> dict:
        p = self._home / "hy_memory.json"
        if not p.exists():
            return {}
        try:
            import json
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _python(self) -> str:
        return sys.executable

    def _qdrant_paths(self) -> tuple[str | None, str | None]:
        env_bin = os.environ.get("QDRANT_BIN")
        if env_bin and Path(env_bin).is_file():
            return env_bin, os.environ.get("QDRANT_CONFIG", "")
        import shutil
        path_bin = shutil.which("qdrant")
        if path_bin:
            return path_bin, ""
        candidates = [
            Path("C:/qdrant/qdrant.exe"),
            Path(os.environ.get("PROGRAMFILES", "C:/Program Files")) / "qdrant" / "qdrant.exe",
            Path.home() / "qdrant" / "qdrant.exe",
        ]
        for c in candidates:
            if c.is_file():
                cfg = c.parent / "config.yaml"
                return str(c), str(cfg) if cfg.exists() else ""
        return None, None

    def _wait_health(self, port: int, path: str, *, expected_status: int = 200, retries: int = _HEALTH_RETRIES) -> bool:
        for _ in range(retries):
            if _port_open(port):
                try:
                    import urllib.request
                    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", method="GET")
                    with urllib.request.urlopen(req, timeout=_HEALTH_TIMEOUT) as resp:
                        if resp.status == expected_status:
                            return True
                except Exception:
                    pass
            time.sleep(_HEALTH_DELAY)
        return False

    # ------------------------------------------------------------------
    # Locking
    # ------------------------------------------------------------------

    def _acquire_lock(self) -> bool:
        self._lock.parent.mkdir(parents=True, exist_ok=True)
        try:
            import fcntl
            self._lock_fd = open(self._lock, "w")
            fcntl.flock(self._lock_fd, fcntl.LOCK_EX)
            return True
        except Exception:
            pass
        try:
            import msvcrt
            self._lock_fd = open(self._lock, "w")
            msvcrt.locking(self._lock_fd.fileno(), msvcrt.LK_NBLCK, 1)
            return True
        except Exception:
            self._lock_fd = None
            return False

    def _release_lock(self) -> None:
        if self._lock_fd is None:
            return
        with contextlib.suppress(Exception):
            import fcntl
            fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
        with contextlib.suppress(Exception):
            self._lock_fd.close()
        self._lock_fd = None

    # ------------------------------------------------------------------
    # Start / stop
    # ------------------------------------------------------------------

    def start(self) -> bool:
        """Start the full stack. Idempotent."""
        if not self._acquire_lock():
            # Another process is starting; wait for it to finish.
            for _ in range(30):
                if self.is_running():
                    return True
                time.sleep(1)
            return self.is_running()

        try:
            if self.is_running():
                return True
            return self._start_locked()
        finally:
            self._release_lock()

    def start_visible(self) -> bool:
        """Start the stack with visible console windows.

        v1.4.2: convenience wrapper for ``hyatlas console``. Spawns
        Qdrant, upstream, and dashboard with ``CREATE_NEW_CONSOLE``
        so the user can see their output. Behavior is otherwise
        identical to :meth:`start`.
        """
        if not self._acquire_lock():
            for _ in range(30):
                if self.is_running():
                    return True
                time.sleep(1)
            return self.is_running()
        try:
            if self.is_running():
                return True
            return self._start_locked(visible=True)
        finally:
            self._release_lock()

    def _start_locked(self, *, visible: bool = False) -> bool:
        self._log_dir.mkdir(parents=True, exist_ok=True)
        log = open(self._log_path, "ab")

        cfg = self._read_hy_memory_json()
        ports = {
            "qdrant": int(cfg.get("qdrant", {}).get("port", _DEFAULT_PORTS["qdrant"])),
            "upstream": int(cfg.get("server_port", _DEFAULT_PORTS["upstream"])),
            "dashboard": int(cfg.get("dashboard", {}).get("port", _DEFAULT_PORTS["dashboard"])),
        }

        # Services ALWAYS use DETACHED_PROCESS so they survive window closure
        detach = _detach_kwargs(log, visible=False)

        # Qdrant
        qdrant_bin, qdrant_cfg = self._qdrant_paths()
        if not _port_open(ports["qdrant"]):
            if not qdrant_bin:
                logger.error("[hy-memory] Qdrant binary not found")
                return False
            cmd = [qdrant_bin]
            if qdrant_cfg:
                cmd += ["--config-path", qdrant_cfg]
            self._procs.append(subprocess.Popen(cmd, **detach))
            if not self._wait_health(ports["qdrant"], "/healthz"):
                logger.error("[hy-memory] Qdrant failed to start")
                return False
            logger.info("[hy-memory] Qdrant ready on port %d", ports["qdrant"])

        # Upstream server
        if not _port_open(ports["upstream"]):
            env = os.environ.copy()
            env["HERMES_HOME"] = str(self._home)
            cmd = [self._python(), "-m", "hyatlas_memory.server.start_server"]
            self._procs.append(subprocess.Popen(cmd, env=env, cwd=str(self._root), **detach))
            if not self._wait_health(ports["upstream"], "/info"):
                logger.error("[hy-memory] Upstream server failed to start")
                return False
            logger.info("[hy-memory] Upstream server ready on port %d", ports["upstream"])

        # Dashboard
        if not _port_open(ports["dashboard"]):
            env = os.environ.copy()
            env["HERMES_HOME"] = str(self._home)
            cmd = [self._python(), str(self._root / "server" / "dashboard" / "dashboard.py")]
            self._procs.append(subprocess.Popen(cmd, env=env, cwd=str(self._root), **detach))
            if not self._wait_health(ports["dashboard"], "/api/memories?offset=0&limit=1"):
                logger.error("[hy-memory] Dashboard failed to start")
                return False
            logger.info("[hy-memory] Dashboard ready on port %d", ports["dashboard"])

        return True

    def stop(self) -> None:
        for proc in self._procs:
            try:
                if proc.poll() is None:
                    proc.terminate()
                    proc.wait(timeout=5)
            except Exception:
                with contextlib.suppress(Exception):
                    proc.kill()
        self._procs = []

    def is_running(self) -> bool:
        cfg = self._read_hy_memory_json()
        upstream_port = int(cfg.get("server_port", _DEFAULT_PORTS["upstream"]))
        return _port_open(upstream_port)

    def ensure_running(self) -> bool:
        if self.is_running():
            return True
        return self.start()
