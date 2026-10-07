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

from . import settings

logger = logging.getLogger(__name__)


# Directory where the running subprocess logs go (matches the v3.5 convention)
LOG_DIR = Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))) / "logs"
LOG_FILE = LOG_DIR / "hyatlas.log"
PID_FILE = LOG_DIR / "hyatlas.pid"

# Environment passed to the spawned server. Everything else in the agent's
# environment stays behind, so provider tokens and API keys never reach a child
# process that talks to a network endpoint. The server reads only HYATLAS_*
# (verified against server.go), so that prefix is matched separately.
_ENV_ALLOW = {
    # process execution
    "PATH", "HOME", "USERPROFILE", "SHELL", "LANG", "LC_ALL", "TERM",
    # Windows: without these the Go runtime cannot resolve DNS or complete a
    # TLS handshake, so extraction would fail in a way that looks like an
    # endpoint problem rather than a stripped environment.
    "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "TEMP", "TMP", "COMSPEC",
    "PATHEXT", "APPDATA", "LOCALAPPDATA", "PROGRAMFILES", "COMMONPROGRAMFILES",
    # TLS trust anchors Go's x509 loader consults — paths, not secrets.
    "SSL_CERT_FILE", "SSL_CERT_DIR", "SSLKEYLOGFILE",
    # keep the child out of a stale proxy the parent inherited, rather than
    # silently routing its LLM calls through it
    "NO_PROXY", "no_proxy",
}

if sys.platform == "win32":
    # Windows environment variables are case-insensitive but Go reads them
    # case-sensitively, so match the parent's spelling rather than guessing.
    _ENV_ALLOW = {k.upper() for k in _ENV_ALLOW} | {"NO_PROXY", "no_proxy"}


class HyatlasProcess:
    """Lifecycle manager for the v4 Go binary subprocess."""

    def __init__(self, config: Dict[str, Any]) -> None:
        self._config = config
        self._proc: Optional[subprocess.Popen] = None
        self._log_handle: Optional[Any] = None
        self._mode = ""
        self._sync = ""

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

    def _env(self) -> Dict[str, str]:
        """Minimal environment for the server subprocess.

        Built from an explicit allowlist rather than ``os.environ.copy()``. The
        agent process holds API keys, provider tokens and shell history-shaped
        variables that a spawned server has no business reading, and handing the
        whole environment over leaks all of them into a child that then talks to
        a network endpoint. Only three groups are passed on:

        * the variables the OS needs for a process to run at all — without
          ``SYSTEMROOT`` on Windows the Go runtime's DNS and TLS calls fail, so
          dropping it would silently break LLM extraction;
        * ``HYATLAS_*``, which is the only prefix the server reads (verified:
          ``server.go`` consults no other variable and never calls
          ``os.Environ()``);
        * the TLS trust anchors Go's x509 loader consults, which are paths and
          not secrets.

        Nothing here invents an endpoint, a model or a credential.
        ``HYATLAS_LLM_*`` arrive only if the user exported them, so a fresh
        install cannot start a server pointed at somebody else's proxy, and no
        secret is copied from one variable into another.
        """
        allow = _ENV_ALLOW
        env: Dict[str, str] = {
            k: v for k, v in os.environ.items()
            if k in allow or k.startswith("HYATLAS_")
        }
        env.setdefault("HYATLAS_GO_HOST", "127.0.0.1")
        env.setdefault("HYATLAS_GO_PORT", str(self._config.get("server_port", 19528)))

        # The extraction mode is a privacy boundary, so only a value start() has
        # already validated and normalised is forwarded. Empty means "let the
        # server decide", so nothing is set and the server keeps its own default.
        mode = getattr(self, "_mode", "") or settings.mode(self._config)
        if mode:
            env.setdefault("HYATLAS_MODE", mode)
        sync = getattr(self, "_sync", "") or settings.sync(self._config)
        if sync:
            env.setdefault("HYATLAS_SYNC_EXTRACT", sync)
        return env

    def start(self) -> None:
        """Spawn the v4 Go binary as a detached subprocess.

        Raises ValueError if a configured extraction mode or sync setting is not
        one the server accepts. The server treats that as fatal, so starting anyway would
        produce a process that dies immediately and reports it only in
        ``hyatlas.log`` — and a user who meant ``lite`` would not learn that
        nothing came up at all.
        """
        if self._proc is not None:
            return

        # Validated through the shared module so the plugin and the server agree
        # on the same three names rather than each keeping its own list. Resolved
        # once here; _env() forwards the result instead of re-reading the raw
        # setting, so an un-normalised value can never reach the child.
        self._mode = settings.mode(self._config)
        self._sync = settings.sync(self._config)

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

        env = self._env()

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

        # stop_running() looks for this file to reap a server it did not spawn
        # (one started in a previous process, or left behind by a crash). Without
        # the write that lookup can never match, and `hermes hyatlas stop` after a
        # gateway restart leaves the old server holding the port.
        try:
            PID_FILE.write_text(str(self._proc.pid), encoding="utf-8")
        except OSError as e:
            logger.debug("could not write pidfile %s: %s", PID_FILE, e)

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
        # Only stale after a hard crash now, which is what makes the pid check in
        # stop_running() worth its cost.
        try:
            PID_FILE.unlink()
        except OSError:
            pass
        self._proc = None

    @staticmethod
    def _is_server(pid: int) -> bool:
        """True iff *pid* is still a hyatlas-go process.

        Writing the pidfile made ``stop_running()`` live, and a stale pid from a
        crashed server can be recycled by the OS into an unrelated process.
        Confirming the name before a force-kill is what keeps that from taking
        down whatever now owns the pid.
        """
        if pid <= 0:
            return False
        try:
            if sys.platform == "win32":
                r = subprocess.run(
                    ["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                    capture_output=True, text=True, timeout=10,
                )
                return "hyatlas-go" in (r.stdout or "").lower()
            cmdline = Path(f"/proc/{pid}/comm").read_text(encoding="utf-8", errors="replace")
            return cmdline.strip().startswith("hyatlas-go")
        except (OSError, ValueError, subprocess.SubprocessError):
            return False

    @staticmethod
    def stop_running() -> None:
        """Stop any existing hyatlas-go process by PID file or taskkill."""
        pidfile = PID_FILE
        if pidfile.exists():
            try:
                pid = int(pidfile.read_text().strip())
                if not HyatlasProcess._is_server(pid):
                    logger.info("stop_running: pid %s is not hyatlas-go; leaving it alone", pid)
                    pidfile.unlink(missing_ok=True)
                    return
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
