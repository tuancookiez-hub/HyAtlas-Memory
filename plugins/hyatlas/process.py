"""Subprocess lifecycle for the HyAtlas v4 Go binary.

The Go binary (``hyatlas-go`` / ``hyatlas-go.exe``) is the actual memory
server. The plugin can optionally start it as a child process when
``auto_start: true`` is set in config, or when ``hermes hyatlas start`` is run.

The child's stdout and stderr are appended to ``$HERMES_HOME/logs/hyatlas.log``,
its PID is written to ``hyatlas.pid``, and its working directory is the binary's
folder. It is not stopped when the agent exits: the provider's ``shutdown()``
leaves it running, and ``hermes hyatlas stop`` stops it. The model files are
resolved relative to the binary's directory.
"""

from __future__ import annotations

import errno
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
from .client import HyatlasClient

# Advisory file locking for the start lock. fcntl on POSIX, msvcrt on Windows;
# exactly one of the two is None.
if sys.platform == "win32":
    import msvcrt
    fcntl = None  # type: ignore[assignment]
else:
    import fcntl
    msvcrt = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)


# Directory where the running subprocess logs go (matches the v3.5 convention)
LOG_DIR = settings.home() / "logs"
LOG_FILE = LOG_DIR / "hyatlas.log"
PID_FILE = LOG_DIR / "hyatlas.pid"
# Serialises check-and-spawn across processes (two Hermes sessions with auto_start).
START_LOCK_NAME = "hyatlas.start.lock"
# Default bound on waiting for that lock. Never wait forever: an unattended run
# must not hang behind a stuck starter.
DEFAULT_LOCK_WAIT = 45.0
# After health answers, a child must still be alive this long before its pid is
# recorded. A child that binds a busy port dies within moments.
SETTLE_SECONDS = 0.75

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


class ServerAlreadyRunning(RuntimeError):
    """A hyatlas-go server already owns the configured origin, so nothing is spawned.

    ``serving`` is True when the origin answers health; ``pid`` is the pid from the
    pidfile when that pid is a live hyatlas-go (None when unknown, e.g. a server
    started by hand).
    """

    def __init__(self, origin: str, pid: Optional[int], serving: bool) -> None:
        self.origin = origin
        self.pid = pid
        self.serving = serving
        super().__init__(f"a hyatlas-go server is already running at {origin}")


def _origin(config: Optional[Dict[str, Any]]) -> str:
    cfg = config or {}
    host = str(cfg.get("server_host") or "127.0.0.1")
    port = cfg.get("server_port") or 19528
    return f"{host}:{port}"


def _serving(config: Optional[Dict[str, Any]]) -> bool:
    """True iff the configured origin answers /healthz. Short timeout: this is a probe."""
    try:
        return HyatlasClient(base_url=f"http://{_origin(config)}", timeout=2.0).is_reachable()
    except Exception:
        return False


def _read_pid() -> Optional[int]:
    try:
        return int(PID_FILE.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _write_pidfile(pid: int) -> None:
    try:
        PID_FILE.parent.mkdir(parents=True, exist_ok=True)
        PID_FILE.write_text(str(pid), encoding="utf-8")
    except OSError as e:
        logger.debug("could not write pidfile %s: %s", PID_FILE, e)


def _remove_pidfile(only_pid: Optional[int] = None) -> None:
    """Remove the pidfile. With *only_pid*, only when it still names that pid."""
    if only_pid is not None and _read_pid() != only_pid:
        return
    try:
        PID_FILE.unlink()
    except OSError:
        pass


def _wait_for(predicate: Any, timeout: float, interval: float = 0.25) -> bool:
    deadline = time.monotonic() + timeout
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(interval)


class StartLockBusy(RuntimeError):
    """Another start held the start lock for longer than the caller was willing to wait."""


# Windows (msvcrt.locking) reports a held lock as EACCES or EDEADLOCK.
_WINDOWS_BUSY_ERRNOS = {errno.EACCES} | (
    {errno.EDEADLOCK} if hasattr(errno, "EDEADLOCK") else set())


def _acquire_start_lock(wait: float) -> int:
    """Take the exclusive start lock, waiting at most *wait* seconds.

    Returns an open file descriptor; pass it to :func:`_release_start_lock`. The
    lock is advisory and lives in LOG_DIR. The OS drops it when the holder exits,
    so a crashed starter cannot leave it held.

    Only "someone else holds it" is retried: BlockingIOError (EAGAIN/EWOULDBLOCK)
    on POSIX, and EACCES/EDEADLOCK from msvcrt on Windows. Any other lock error
    means the filesystem cannot lock at all (ENOLCK, EOPNOTSUPP, ...). Waiting
    cannot help there, so a warning is logged and the start proceeds unlocked.

    Raises StartLockBusy when the wait runs out, and OSError if the lock file
    cannot be created.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = LOG_DIR / START_LOCK_NAME
    fd = os.open(str(path), os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.monotonic() + max(0.0, float(wait))
    try:
        while True:
            try:
                if fcntl is not None:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                else:
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                return fd
            except BlockingIOError:
                pass  # flock: another starter holds it; retry below
            except OSError as e:
                if fcntl is None and e.errno in _WINDOWS_BUSY_ERRNOS:
                    pass  # msvcrt: held; retry below
                else:
                    logger.warning(
                        "start lock unavailable on %s (%s); starting without the "
                        "lock, so two simultaneous starts are not serialised", path, e)
                    return fd
            if time.monotonic() >= deadline:
                raise StartLockBusy(
                    f"another hyatlas start holds {path}; gave up after "
                    f"{float(wait):g}s. Retry in a moment.")
            time.sleep(0.1)
    except BaseException:
        os.close(fd)
        raise


def _release_start_lock(fd: int) -> None:
    """Release a lock taken by :func:`_acquire_start_lock` and close its descriptor."""
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_UN)
        else:
            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    except OSError:
        pass  # closing the descriptor releases it anyway
    finally:
        os.close(fd)


class HyatlasProcess:
    """Lifecycle manager for the v4 Go binary subprocess."""

    def __init__(self, config: Dict[str, Any]) -> None:
        self._config = config
        self._proc: Optional[subprocess.Popen] = None
        self._log_handle: Optional[Any] = None
        self._lock_fd: Optional[int] = None
        self._mode = ""
        self._sync = ""
        self._pid: Optional[int] = None
        self._exit_code: Optional[int] = None

    @property
    def pid(self) -> Optional[int]:
        """Pid of the child this instance spawned (set by start())."""
        return self._pid

    @property
    def exit_code(self) -> Optional[int]:
        """Return code of the child, once it has exited during startup."""
        return self._exit_code

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
        env.setdefault("HYATLAS_GO_HOST", str(self._config.get("server_host") or "127.0.0.1"))
        env.setdefault("HYATLAS_GO_PORT", str(self._config.get("server_port", 19528)))
        # The client resolves both of those from config, so the spawned server has
        # to bind the same pair or the plugin talks to a port nothing listens on.
        data = str(self._config.get("data_dir") or "").strip()
        if data:
            env.setdefault("HYATLAS_GO_DATA", data)

        # The extraction mode is a privacy boundary, so only a value start() has
        # already validated and normalised is forwarded. Empty means "let the
        # server decide", so nothing is set and the server keeps its own default.
        mode = getattr(self, "_mode", "") or settings.mode(self._config)
        if mode:
            env.setdefault("HYATLAS_MODE", mode)
        # A server this plugin spawns serves Hermes turns, so by default it never
        # makes a turn wait on the LLM: pro still extracts every write, just
        # behind the response. An explicit setting or exported variable wins.
        sync = getattr(self, "_sync", "") or settings.sync(self._config) or "off"
        env.setdefault("HYATLAS_SYNC_EXTRACT", sync)

        # Endpoint and model are non-secret, so they arrive through the settings
        # form into hyatlas.json rather than the environment. Without forwarding
        # them here the server would fall back to its own default and silently
        # ignore what the user just configured. setdefault keeps an explicitly
        # exported variable authoritative, matching mode and sync.
        for key, var in (("llm_base", "HYATLAS_LLM_BASE"),
                         ("llm_model", "HYATLAS_LLM_MODEL")):
            val = str(self._config.get(key) or "").strip()
            if val:
                env.setdefault(var, val)

        # The key is deliberately NOT forwarded from config: it is declared
        # secret, so the setup wizard routes it to Hermes' .env and save_config
        # strips it. It reaches the child through the HYATLAS_* prefix rule above
        # like any other exported variable, and never through this code path.
        return env

    def start(self, lock_wait: float = DEFAULT_LOCK_WAIT) -> None:
        """Spawn the v4 Go binary as a detached subprocess.

        Raises ValueError if a configured extraction mode or sync setting is not
        one the server accepts. The server treats that as fatal, so starting anyway would
        produce a process that dies immediately and reports it only in
        ``hyatlas.log`` — and a user who meant ``lite`` would not learn that
        nothing came up at all.

        Raises ServerAlreadyRunning, spawning nothing, when the configured origin
        already answers or a live hyatlas-go owns the pidfile. A second spawn would
        die on the port and, worse, overwrite the live server's pidfile.

        The check and the spawn run under the start lock, so two concurrent starts
        cannot both spawn. The lock is held until :meth:`wait_started` returns (or
        this raises), so the second starter sees the first server's answer rather
        than racing it. Raises StartLockBusy if the lock is not free within
        *lock_wait* seconds, and OSError if the spawn itself fails.

        The pidfile is written by :meth:`wait_started`, once the child has been seen
        alive, not here.
        """
        if self._proc is not None:
            return

        # Validated through the shared module so the plugin and the server agree
        # on the same three names rather than each keeping its own list. Resolved
        # once here; _env() forwards the result instead of re-reading the raw
        # setting, so an un-normalised value can never reach the child.
        self._mode = settings.mode(self._config)
        self._sync = settings.sync(self._config)

        # Reuse a lock this instance already holds. Taking a second one would
        # overwrite the first descriptor and leak it.
        if self._lock_fd is None:
            self._lock_fd = _acquire_start_lock(lock_wait)
        try:
            existing = _read_pid()
            owner_alive = existing is not None and HyatlasProcess._is_server(existing)
            serving = _serving(self._config)
            if serving or owner_alive:
                raise ServerAlreadyRunning(
                    _origin(self._config), existing if owner_alive else None, serving)

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
            except OSError:
                self._log_handle.close()
                self._log_handle = None
                raise
            self._pid = self._proc.pid
        except BaseException:
            self._release_lock()
            raise

    def _release_lock(self) -> None:
        """Release the start lock if this instance holds it. Safe to call repeatedly."""
        if self._lock_fd is not None:
            fd, self._lock_fd = self._lock_fd, None
            _release_start_lock(fd)

    def wait_started(self, timeout: float = 30.0) -> str:
        """Wait for the spawned child to serve, or to die.

        Returns ``"serving"`` (health answers and the child is still alive after a
        settle), ``"starting"`` (still alive at the deadline, not yet answering),
        ``"already_running"`` (health answers but the child exited within the settle:
        the port belongs to another server, so this child is not recorded) or
        ``"exited"`` (the child died before health answered; nothing is recorded and
        the log handle is closed).

        The pidfile is written only once the child is seen alive, so a child that
        dies on a busy port never leaves a pid behind, and ``stop_running()`` can
        find a server that is alive but slow to answer. The start lock taken by
        :meth:`start` is released before this returns, whatever the outcome.
        """
        try:
            return self._wait_started(timeout)
        finally:
            self._release_lock()

    def _wait_started(self, timeout: float) -> str:
        proc = self._proc
        if proc is None:
            return "exited"
        deadline = time.monotonic() + timeout
        while True:
            if proc.poll() is not None:
                self._exit_code = proc.returncode
                self._cleanup()
                return "exited"
            if _serving(self._config):
                # Health answers, but it may be another server's. A child that lost
                # the bind exits almost at once, so the settle tells the two apart.
                time.sleep(SETTLE_SECONDS)
                if proc.poll() is None:
                    _write_pidfile(proc.pid)
                    return "serving"
                self._exit_code = proc.returncode
                self._cleanup()
                return "already_running"
            if time.monotonic() >= deadline:
                break
            time.sleep(0.25)
        _write_pidfile(proc.pid)
        return "starting"

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
        self._release_lock()
        if self._log_handle is not None:
            try:
                self._log_handle.close()
            except Exception:
                pass
            self._log_handle = None
        # Only this child's pidfile: never delete a pid that now belongs to another
        # live server.
        _remove_pidfile(only_pid=self._pid)
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
            if Path("/proc").is_dir():
                comm = Path(f"/proc/{pid}/comm").read_text(encoding="utf-8", errors="replace")
                return comm.strip().startswith("hyatlas-go")
            # macOS has no /proc: ask ps for the executable's name instead.
            r = subprocess.run(
                ["ps", "-o", "comm=", "-p", str(pid)],
                capture_output=True, text=True, timeout=10,
            )
            return Path((r.stdout or "").strip()).name.startswith("hyatlas-go")
        except (OSError, ValueError, subprocess.SubprocessError):
            return False

    @staticmethod
    def stop_running(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        """Stop the hyatlas-go server this plugin can identify, and say honestly what happened.

        * pidfile names a live hyatlas-go: signal it, then wait for the origin to stop
          answering. ``ok`` is True only if it stopped answering.
        * nothing identifiable, but the origin still answers: ``ok`` False. That server
          was started by something else (or the pidfile is stale), and killing an
          unknown pid is refused.
        * nothing running: ``ok`` True with ``running`` False.
        """
        cfg = config if config is not None else settings.load()
        origin = _origin(cfg)
        pid = _read_pid()

        if pid is not None and HyatlasProcess._is_server(pid):
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                               capture_output=True, timeout=5)
            else:
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError as e:
                    logger.debug("stop_running: %s", e)
            gone = _wait_for(lambda: not _serving(cfg), timeout=10.0)
            _remove_pidfile(only_pid=pid)
            if gone:
                return {"ok": True, "stopped": True, "pid": pid, "origin": origin}
            return {"ok": False, "stopped": False, "pid": pid, "origin": origin,
                    "error": f"sent a stop to pid {pid}, but the server at {origin} "
                             "still answers; check hyatlas.log"}

        if pid is not None:
            # Dead, or recycled into an unrelated process: not ours to signal.
            _remove_pidfile(only_pid=pid)
        if _serving(cfg):
            return {"ok": False, "stopped": False, "running": True, "pid": None,
                    "origin": origin,
                    "error": f"the server at {origin} is running but was not started by "
                             "this plugin (pid unknown); stop it from the process that "
                             "started it"}
        return {"ok": True, "stopped": False, "running": False, "origin": origin,
                "message": f"no hyatlas-go server was running at {origin}"}


def start_server(config: Optional[Dict[str, Any]], timeout: float = 30.0,
                 lock_wait: Optional[float] = None) -> Dict[str, Any]:
    """Start the binary unless a server already owns the origin. Shared by the CLI,
    the ``/hyatlas start`` slash command and ``auto_start``.

    *lock_wait* bounds how long this call waits for another start in progress
    (default: *timeout* plus 15 s, so a starter that is still coming up is waited
    for). It never waits forever.

    Returns a JSON-ready dict with ``ok``, ``started``, ``reachable`` and, where
    relevant, ``already_running``, ``pid``, ``hint`` or ``error``. Never raises for
    an environment problem: a spawn that the OS refuses is ``ok: False``.
    """
    origin = _origin(config)
    if lock_wait is None:
        lock_wait = timeout + 15.0
    proc = HyatlasProcess(config or {})
    try:
        proc.start(lock_wait=lock_wait)
    except ServerAlreadyRunning as e:
        return {
            "ok": True, "started": False, "already_running": True,
            "reachable": e.serving, "origin": origin,
            "pid": e.pid, "pid_known": e.pid is not None,
            "message": ("a server already answers at " + origin) if e.serving else
                       f"a hyatlas-go (pid {e.pid}) is alive but not answering at {origin}",
        }
    except StartLockBusy as e:
        return {"ok": False, "started": False, "reachable": False, "origin": origin,
                "error": str(e)}
    except (FileNotFoundError, ValueError) as e:
        return {"ok": False, "started": False, "reachable": False, "origin": origin,
                "error": str(e)}
    except OSError as e:
        # PermissionError from a non-executable binary, ENOEXEC, a log directory
        # that cannot be created, and so on. Report it; do not traceback the CLI.
        return {"ok": False, "started": False, "reachable": False, "origin": origin,
                "error": f"could not start hyatlas-go: {e}"}

    state = proc.wait_started(timeout=timeout)
    if state == "serving":
        return {"ok": True, "started": True, "reachable": True,
                "pid": proc.pid, "origin": origin}
    if state == "starting":
        return {"ok": True, "started": True, "reachable": False,
                "pid": proc.pid, "origin": origin,
                "hint": f"hyatlas-go is running but not answering at {origin} yet; "
                        f"check {LOG_FILE}"}
    if state == "already_running":
        # Health answered, but the child we spawned exited straight away, so the
        # port is held by a server this call did not start. Nothing is recorded.
        return {"ok": True, "started": False, "already_running": True,
                "reachable": True, "origin": origin, "pid": None, "pid_known": False,
                "message": f"a server already answers at {origin}; the process this "
                           f"call started exited, so it was not recorded"}
    return {"ok": False, "started": False, "reachable": False, "origin": origin,
            "error": f"hyatlas-go exited during startup (exit code {proc.exit_code}). "
                     f"The port may be in use by another process, or the config is wrong. "
                     f"See {LOG_FILE}"}
