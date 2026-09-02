"""Subprocess lifecycle for the HyAtlas v4 Go binary.

The Go binary (``hyatlas-go`` / ``hyatlas-go.exe``) is the actual memory
server. The plugin can optionally auto-start it as a subprocess when
``auto_start: true`` is set in config.

This is a thin wrapper — the canonical pattern (Hindsight-style) is
to spawn the binary detached, capture logs to a file, and stop it on
plugin unload. The embedded `models/` are resolved relative to the
binary's CWD.
"""

from __future__ import annotations

import logging
import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


# Directory where the running subprocess logs go (matches the v3.5 convention)
LOG_DIR = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))) / "logs"
LOG_FILE = LOG_DIR / "hyatlas.log"


class HyatlasProcess:
    """Lifecycle manager for the v4 Go binary subprocess."""

    def __init__(self, config: Dict[str, Any]) -> None:
        self._config = config
        self._proc: Optional[subprocess.Popen] = None
        self._log_handle: Optional[Any] = None

    @staticmethod
    def _discover_binary() -> Optional[str]:
        """Find the v4 Go binary in PATH, alongside this plugin, or in well-known paths."""
        # 1. PATH
        for name in ("hyatlas-go", "hyatlas-go.exe"):
            found = shutil.which(name)
            if found:
                return found
        # 2. Alongside the plugin (cargo-dist / release layouts)
        here = Path(__file__).resolve().parent
        for name in ("hyatlas-go", "hyatlas-go.exe"):
            candidate = here / "bin" / name
            if candidate.is_file() and os.access(candidate, os.X_OK):
                return str(candidate)
        # 3. Common install locations
        for path in (
            Path("/usr/local/bin/hyatlas-go"),
            Path("/opt/hyatlas/hyatlas-go"),
            Path.home() / "hyatlas" / "hyatlas-go",
        ):
            if path.is_file() and os.access(path, os.X_OK):
                return str(path)
        # 4. Windows common
        if sys.platform == "win32":
            for path in (
                Path("C:/hyatlas/hyatlas-go.exe"),
                Path("C:/Program Files/hyatlas/hyatlas-go.exe"),
            ):
                if path.is_file():
                    return str(path)
        return None

    def start(self) -> None:
        """Spawn the v4 Go binary as a detached subprocess."""
        if self._proc is not None:
            return

        binary = self._config.get("binary_path") or self._discover_binary()
        if not binary:
            raise FileNotFoundError(
                "hyatlas-go binary not found. Set `binary_path` in config "
                "or add the binary to PATH."
            )
        if not Path(binary).exists():
            raise FileNotFoundError(f"hyatlas-go binary not found at: {binary}")

        LOG_DIR.mkdir(parents=True, exist_ok=True)
        # Open log file with errors='replace' to avoid surrogate crashes
        self._log_handle = open(LOG_FILE, mode="a", encoding="utf-8", errors="replace")

        env = os.environ.copy()
        # Forward LLM env so the v4 server can talk to ai2api
        env.setdefault("HYATLAS_LLM_BASE", "http://127.0.0.1:49200/v1")
        env.setdefault("HYATLAS_LLM_MODEL", "deepseek:deepseek-v4-flash")
        env.setdefault("HYATLAS_LLM_KEY", os.environ.get("AI2API_KEY", ""))
        # Bind to loopback only by default
        env.setdefault("HYATLAS_GO_HOST", "127.0.0.1")
        env.setdefault("HYATLAS_GO_PORT", str(self._config.get("server_port", 19528)))

        # Use CREATE_NEW_PROCESS_GROUP on Windows so we can kill the whole tree
        creationflags = 0
        if sys.platform == "win32":
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

        logger.info("starting hyatlas-go: %s", binary)
        try:
            self._proc = subprocess.Popen(
                [binary],
                stdout=self._log_handle,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                env=env,
                cwd=str(Path(binary).parent),
                creationflags=creationflags,
            )
        except OSError as e:
            self._log_handle.close()
            self._log_handle = None
            raise

    def stop(self) -> None:
        """Terminate the subprocess gracefully."""
        if self._proc is None:
            return
        if self._proc.poll() is not None:
            # Already exited
            self._cleanup()
            return
        try:
            if sys.platform == "win32":
                self._proc.send_signal(signal.CTRL_BREAK_EVENT)
                try:
                    self._proc.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    self._proc.terminate()
                    self._proc.wait(timeout=5.0)
            else:
                self._proc.terminate()
                try:
                    self._proc.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    self._proc.kill()
                    self._proc.wait(timeout=2.0)
        except Exception as e:
            logger.warning("error stopping hyatlas-go: %s", e)
        finally:
            self._cleanup()

    def is_running(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def _cleanup(self) -> None:
        if self._log_handle is not None:
            try:
                self._log_handle.close()
            except Exception:
                pass
            self._log_handle = None
        self._proc = None

    @staticmethod
    def stop_running() -> None:
        """Stop any existing hyatlas-go process by PID file or taskkill."""
        pidfile = LOG_DIR / "hyatlas.pid"
        if pidfile.exists():
            try:
                pid = int(pidfile.read_text().strip())
                if sys.platform == "win32":
                    subprocess.run(
                        ["taskkill", "/F", "/PID", str(pid)],
                        capture_output=True, timeout=5,
                    )
                else:
                    os.kill(pid, signal.SIGTERM)
            except (OSError, ValueError) as e:
                logger.debug("stop_running: %s", e)
            try:
                pidfile.unlink()
            except OSError:
                pass
