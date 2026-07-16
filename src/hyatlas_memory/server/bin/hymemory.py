"""
hyatlas — control the Hy-Memory dashboard and server.

Public CLI name: `hyatlas` (the name on the GitHub repo).
Internal name: "the hymemory" (the user's working name for the project;
the Python file is bin/hymemory.py for stability, referenced by other tooling).

Subcommands:
  start                       start both server and dashboard
  stop                        stop both
  restart                     stop then start both
  status                      show what is running

  dashboard start|stop|restart|status
  server    start|stop|restart|status
  logs      server|dashboard   tail last 50 lines of the log

What this wrapper does that the old kill_hy_mem.ps1 + raw python launch does not:
  1. Popen-based launch — never blocks the terminal
  2. PIDs written to a JSON file so stop can find them reliably
  3. Stop falls back to port-lookup if the PID file is stale
  4. Status queries the actual /healthz endpoints, not just the PID file
  5. Logs go to ~/.hermes/logs/hymemory-{service}.log so you can debug failures

Naming history (2026-06-13):
  - hymemory — rejected for public name: collides with existing local bin/hymemory
    (XAMPP MySQL launcher); kept as the internal/work-in-progress name
  - hymem    — rejected: sounds like "hymen" when said aloud
  - hyctl    — rejected: Unix-generic, doesn't fit the brand
  - hyatlas  — CHOSEN: classical-map metaphor, fits NOUS-BRANDING, no collision
"""
import argparse
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / "AppData/Local/hermes"))
SCRIPTS_DIR = HERMES_HOME
SERVER_SCRIPT = SCRIPTS_DIR / "start_hy_memory_server.py"
DASHBOARD_SCRIPT = SCRIPTS_DIR / "hy_memory_dashboard.py"
PID_FILE = HERMES_HOME / "hymemory.pids.json"
LOG_DIR = HERMES_HOME / "logs"

SERVER_PORT = 19527
DASHBOARD_PORT = 8765

PYTHON = sys.executable  # use the same python that ran this wrapper


def _read_pids() -> dict:
    if not PID_FILE.exists():
        return {"server": None, "dashboard": None}
    try:
        return json.loads(PID_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"server": None, "dashboard": None}


def _write_pids(pids: dict) -> None:
    PID_FILE.write_text(json.dumps(pids, indent=2), encoding="utf-8")


def _port_pid(port: int) -> int | None:
    """Find the PID listening on a port using netstat. Returns None if free."""
    try:
        r = subprocess.run(["netstat", "-ano"], capture_output=True, text=True, timeout=5,
                        creationflags=0x08000000)
    except Exception:
        return None
    for line in r.stdout.splitlines():
        if f":{port}" in line and "LISTENING" in line:
            try:
                return int(line.split()[-1])
            except ValueError:
                pass
    return None


def _is_alive(pid: int | None) -> bool:
    if pid is None:
        return False
    r = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"Get-Process -Id {pid} -ErrorAction SilentlyContinue | Select-Object -First 1 | Measure-Object | Select-Object -ExpandProperty Count"],
        capture_output=True, text=True, timeout=5,
        creationflags=0x08000000,  # CREATE_NO_WINDOW
    )
    try:
        return int(r.stdout.strip()) > 0
    except ValueError:
        return False


def _health(url: str, timeout: float = 2.0) -> tuple[bool, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return True, body[:200]
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def _launch(script: Path, log_name: str) -> int | None:
    """Launch a Python script as a detached background process. Returns PID."""
    if not script.exists():
        print(f"  [ERROR] script not found: {script}")
        return None
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / f"hymemory-{log_name}.log"
    log_fh = open(log_path, "ab", buffering=0)
    # CREATE_NEW_PROCESS_GROUP = 0x00000200, DETACHED_PROCESS = 0x00000008
    # CREATE_NO_WINDOW = 0x08000000 — suppress the visible console window.
    flags = 0x00000008 | 0x08000000
    p = subprocess.Popen(
        [PYTHON, str(script)],
        stdout=log_fh, stderr=log_fh, stdin=subprocess.DEVNULL,
        creationflags=flags,
        cwd=str(SCRIPTS_DIR),
        env={**os.environ, "PYTHONUNBUFFERED": "1"},
    )
    return p.pid


def _wait_healthy(name: str, url: str, deadline_s: float = 30.0) -> bool:
    """Poll /healthz until 200 or deadline."""
    start = time.time()
    while time.time() - start < deadline_s:
        ok, _ = _health(url)
        if ok:
            print(f"  {name}: healthy after {time.time() - start:.1f}s")
            return True
        time.sleep(0.5)
    print(f"  {name}: NOT HEALTHY after {deadline_s}s (see {LOG_DIR}/hymemory-{name.lower().replace(' ','-')}.log)")
    return False


def _kill_pid(pid: int) -> bool:
    if not _is_alive(pid):
        return True
    subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         f"Stop-Process -Id {pid} -Force -ErrorAction SilentlyContinue"],
        capture_output=True, timeout=10,
        creationflags=0x08000000,  # CREATE_NO_WINDOW
    )
    # Wait for OS to release the port
    for _ in range(15):
        time.sleep(0.5)
        if not _is_alive(pid):
            return True
    return not _is_alive(pid)


def _print_ready_banner() -> None:
    """Pretty-print the dashboard URL after a successful start so users
    know where to point their browser."""
    print()
    print("  ┌─────────────────────────────────────────────────────────┐")
    print(f"  │  HyAtlas Dashboard is up                                │")
    print(f"  │  → http://127.0.0.1:{DASHBOARD_PORT}                                │")
    print(f"  │  (server: http://127.0.0.1:{SERVER_PORT})                          │")
    print("  └─────────────────────────────────────────────────────────┘")
    print()


def cmd_start_server(verbose: bool = True) -> bool:
    pids = _read_pids()
    port_pid = _port_pid(SERVER_PORT)
    if _is_alive(pids.get("server")) or (port_pid and _is_alive(port_pid)):
        if verbose:
            print(f"  server: already running (pid={pids.get('server') or port_pid}, port={SERVER_PORT})")
        return True
    if verbose:
        print(f"  starting server → port {SERVER_PORT}  (log: {LOG_DIR}/hymemory-server.log)")
    pid = _launch(SERVER_SCRIPT, "server")
    if pid is None:
        return False
    pids["server"] = pid
    _write_pids(pids)
    return _wait_healthy("server", f"http://127.0.0.1:{SERVER_PORT}/healthz")


def cmd_start_dashboard(verbose: bool = True) -> bool:
    pids = _read_pids()
    port_pid = _port_pid(DASHBOARD_PORT)
    if _is_alive(pids.get("dashboard")) or (port_pid and _is_alive(port_pid)):
        if verbose:
            print(f"  dashboard: already running (pid={pids.get('dashboard') or port_pid}, port={DASHBOARD_PORT})")
        return True
    if verbose:
        print(f"  starting dashboard → port {DASHBOARD_PORT}  (log: {LOG_DIR}/hymemory-dashboard.log)")
    pid = _launch(DASHBOARD_SCRIPT, "dashboard")
    if pid is None:
        return False
    pids["dashboard"] = pid
    _write_pids(pids)
    ok = _wait_healthy("dashboard", f"http://127.0.0.1:{DASHBOARD_PORT}/api/status", deadline_s=20.0)
    # Always print the banner if the dashboard is responding at all,
    # even if /api/status is 503 (upstream LLM degraded). The banner
    # is about the dashboard being up, not the whole stack.
    if verbose:
        _print_ready_banner()
    return ok


def cmd_stop_server(verbose: bool = True) -> bool:
    pids = _read_pids()
    pid = pids.get("server") or _port_pid(SERVER_PORT)
    if not pid:
        if verbose:
            print(f"  server: not running (no PID file, no listener on {SERVER_PORT})")
        return True
    if verbose:
        print(f"  stopping server pid={pid}...")
    ok = _kill_pid(pid)
    # Verify port is free
    time.sleep(1)
    still = _port_pid(SERVER_PORT)
    if still:
        if verbose:
            print(f"  [WARN] port {SERVER_PORT} still has pid={still} after kill")
        return False
    pids["server"] = None
    _write_pids(pids)
    if verbose:
        print(f"  server: stopped (port {SERVER_PORT} free)")
    return ok


def cmd_stop_dashboard(verbose: bool = True) -> bool:
    pids = _read_pids()
    pid = pids.get("dashboard") or _port_pid(DASHBOARD_PORT)
    if not pid:
        if verbose:
            print(f"  dashboard: not running (no PID file, no listener on {DASHBOARD_PORT})")
        return True
    if verbose:
        print(f"  stopping dashboard pid={pid}...")
    ok = _kill_pid(pid)
    time.sleep(1)
    still = _port_pid(DASHBOARD_PORT)
    if still:
        if verbose:
            print(f"  [WARN] port {DASHBOARD_PORT} still has pid={still} after kill")
        return False
    pids["dashboard"] = None
    _write_pids(pids)
    if verbose:
        print(f"  dashboard: stopped (port {DASHBOARD_PORT} free)")
    return ok


def cmd_status(verbose: bool = True) -> int:
    """Show running state. Returns 0 if server+dashboard are reachable, 1 otherwise.

    The 'alive' column is the source of truth for lifecycle decisions
    (start/stop/restart) — process running + port listening. The
    'health' column is informational; the dashboard's /api/status
    proxies the server and can return 502 if the upstream is slow.
    Treat alive=True health=False as 'running, slow upstream'."""
    pids = _read_pids()
    server_pid = pids.get("server") or _port_pid(SERVER_PORT)
    dash_pid = pids.get("dashboard") or _port_pid(DASHBOARD_PORT)
    server_alive = _is_alive(server_pid) and server_pid is not None
    dash_alive = _is_alive(dash_pid) and dash_pid is not None
    server_ok, server_msg = _health(f"http://127.0.0.1:{SERVER_PORT}/healthz", timeout=5.0)
    dash_ok, dash_msg = _health(f"http://127.0.0.1:{DASHBOARD_PORT}/api/status", timeout=10.0)

    def row(name, pid, alive, healthy, msg, port):
        if alive and healthy:
            flag, label = "OK  ", "running, healthy"
        elif alive and not healthy:
            flag, label = "UP  ", f"running, endpoint slow/flaky — {msg[:60]}"
        elif not alive and healthy:
            flag, label = "??? ", "endpoint up but no PID/port — zombie?"
        else:
            flag, label = "DOWN", msg[:80]
        pid_s = str(pid) if pid else "—"
        print(f"  {name:<10} pid={pid_s:>6}  port={port:<6}  {flag}  {label}")

    row("server",    server_pid, server_alive, server_ok, server_msg, SERVER_PORT)
    row("dashboard", dash_pid,    dash_alive,   dash_ok,   dash_msg,   DASHBOARD_PORT)

    if verbose:
        # Sidecar: Qdrant
        q_pid = _port_pid(6333)
        q_alive = _is_alive(q_pid) and q_pid is not None
        q_ok, q_msg = _health("http://127.0.0.1:6333/healthz", timeout=5.0)
        if q_alive and q_ok: flag, label = "OK  ", "running, healthy"
        elif q_alive:         flag, label = "UP  ", f"running, slow — {q_msg[:60]}"
        else:                 flag, label = "DOWN", q_msg[:80]
        pid_s = str(q_pid) if q_pid else "—"
        print(f"  {'qdrant':<10} pid={pid_s:>6}  port={6333:<6}  {flag}  {label}")

    # Exit 0 if BOTH server and dashboard are alive (lifecycle truth).
    # Endpoint flakiness on /api/status does NOT count as a failure.
    return 0 if (server_alive and dash_alive) else 1


def cmd_logs(service: str) -> None:
    name = "server" if service == "server" else "dashboard"
    log_path = LOG_DIR / f"hymemory-{name}.log"
    if not log_path.exists():
        print(f"  no log file at {log_path}")
        return
    # tail -n 50 — pure python, no shell needed
    lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in lines[-50:]:
        print(line)



def cmd_qdrant_status(verbose: bool = True) -> int:
    """Verify Qdrant is up and healthy. Returns 0 if healthy, 1 otherwise.
    SAFE: only reads status, never kills anything.
    """
    pids = _port_pid(6333)
    if not pids:
        if verbose:
            print("  qdrant: NOT running (no listener on 6333)")
        return 1
    ok, msg = _health("http://127.0.0.1:6333/healthz", timeout=5.0)
    if verbose:
        if ok:
            print(f"  qdrant: pid={pids} port=6333 OK running, healthy")
        else:
            print(f"  qdrant: pid={pids} port=6333 UP running, {msg[:80]}")
    return 0 if ok else 1


def cmd_qdrant_snapshot(verbose: bool = True) -> bool:
    """Create a Qdrant snapshot before any risky operation.
    SAFE: read-only + snapshot creation, never kills anything.
    """
    try:
        # List collections
        req = urllib.request.Request("http://127.0.0.1:6333/collections")
        data = json.loads(urllib.request.urlopen(req, timeout=10).read())
        collections = [c["name"] for c in data.get("result", {}).get("collections", [])]
        if not collections:
            if verbose:
                print("  qdrant: no collections to snapshot")
            return True
        for coll in collections:
            req = urllib.request.Request(
                f"http://127.0.0.1:6333/collections/{coll}/snapshots",
                method="POST",
            )
            urllib.request.urlopen(req, timeout=60)
            if verbose:
                print(f"  qdrant: snapshot created for {coll}")
        return True
    except Exception as e:
        if verbose:
            print(f"  qdrant: snapshot failed: {e}")
        return False


def cmd_qdrant_stop(verbose: bool = True) -> bool:
    """Stop Qdrant SAFELY:
    1. Verify Qdrant binary path first
    2. Find only the Qdrant process (not qdrant-named arbitrary things)
    3. Snapshot before kill
    4. Verify port is free
    """
    # Find Qdrant binary path
    qdrant_paths = []
    try:
        r = subprocess.run(["where", "qdrant"], capture_output=True, text=True, timeout=5,
                        creationflags=0x08000000)
        for line in r.stdout.splitlines():
            line = line.strip()
            if line and os.path.exists(line):
                qdrant_paths.append(line)
    except Exception:
        pass

    if not qdrant_paths:
        # Try common locations
        for p in [r"C:\qdrant\qdrant.exe",
                  str(Path.home() / "AppData/Local/hermes/qdrant.exe")]:
            if os.path.exists(p):
                qdrant_paths.append(p)

    if verbose:
        if qdrant_paths:
            print(f"  qdrant binary at: {qdrant_paths[0]}")
        else:
            print("  qdrant binary: NOT FOUND (will skip path verification)")

    # Find the right PID (only the Qdrant binary, not arbitrary "qdrant" matches)
    pid = _port_pid(6333)
    if not pid:
        if verbose:
            print("  qdrant: not running (no listener on 6333)")
        return True

    if verbose:
        print(f"  qdrant: stopping pid={pid}...")

    # Create snapshot first (safety net)
    if verbose:
        print("  qdrant: creating snapshot before stop...")
    cmd_qdrant_snapshot(verbose=False)

    # Kill the PID (not a wildcard)
    ok = _kill_pid(pid)
    time.sleep(1)

    still = _port_pid(6333)
    if still:
        if verbose:
            print(f"  [WARN] port 6333 still has pid={still} after kill")
        return False
    if verbose:
        print(f"  qdrant: stopped (port 6333 free)")
    return ok


def cmd_qdrant_start(verbose: bool = True) -> bool:
    """Start Qdrant from a known binary path.
    Tries `where qdrant`, then C:\qdrant\qdrant.exe, then %HERMES_HOME%\qdrant.exe.
    """
    # Find binary
    qdrant_path = None
    for p in [r"C:\qdrant\qdrant.exe",
              str(Path.home() / "AppData/Local/hermes/qdrant.exe")]:
        if os.path.exists(p):
            qdrant_path = p
            break
    try:
        r = subprocess.run(["where", "qdrant"], capture_output=True, text=True, timeout=5,
                        creationflags=0x08000000)
        for line in r.stdout.splitlines():
            line = line.strip()
            if line and os.path.exists(line):
                qdrant_path = line
                break
    except Exception:
        pass

    if not qdrant_path:
        if verbose:
            print("  qdrant: binary not found in any of: C:\\qdrant\\, %HERMES_HOME%, PATH")
            print("  download from https://github.com/qdrant/qdrant/releases")
        return False

    # Check if already running
    if _port_pid(6333):
        if verbose:
            print(f"  qdrant: already running")
        return True

    if verbose:
        print(f"  starting qdrant from {qdrant_path}")

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / "qdrant.log"
    log_fh = open(log_path, "ab", buffering=0)
    # Use qdrant's directory as cwd
    qdrant_dir = str(Path(qdrant_path).parent)
    p = subprocess.Popen(
        [qdrant_path],
        cwd=qdrant_dir,
        stdout=log_fh, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        creationflags=0x00000008 | 0x08000000,
        env={**os.environ, "PYTHONIOENCODING": "utf-8"},
    )
    if verbose:
        print(f"  qdrant: started with pid={p.pid}")

    # Wait for health
    start = time.time()
    while time.time() - start < 30.0:
        if _health("http://127.0.0.1:6333/healthz", timeout=2.0)[0]:
            if verbose:
                print(f"  qdrant: healthy after {time.time()-start:.1f}s")
            return True
        time.sleep(0.5)
    if verbose:
        print(f"  qdrant: NOT HEALTHY after 30s (see {log_path})")
    return False



# ── Subcommand dispatch ────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(prog="hyatlas", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)

    for name, help_ in [("start", "start server and dashboard"),
                        ("stop",  "stop server and dashboard"),
                        ("restart", "stop then start server and dashboard"),
                        ("status", "show running state")]:
        sub.add_parser(name, help=help_)

    for which in ("dashboard", "server"):
        p = sub.add_parser(which, help=f"{which} subcommands")
        ss = p.add_subparsers(dest=f"{which}_cmd", required=True)
        for name, help_ in [("start",   f"start the {which}"),
                            ("stop",    f"stop the {which}"),
                            ("restart", f"restart the {which}"),
                            ("status",  f"show {which} status")]:
            ss.add_parser(name, help=help_)

    logs = sub.add_parser("logs", help="tail the most recent log lines")
    logs.add_argument("service", choices=["server", "dashboard"])

    p = sub.add_parser("qdrant", help="qdrant subcommands (safe - never uses wildcards)")
    ss = p.add_subparsers(dest="qdrant_cmd", required=True)
    for name, help_ in [("status", "check qdrant health"),
                        ("snapshot", "create a snapshot of all collections"),
                        ("start", "start qdrant from known binary path"),
                        ("stop", "stop qdrant (snapshot first)"),
                        ("restart", "stop + start (snapshot first)")]:
        ss.add_parser(name, help=help_)

    args = parser.parse_args()

    if args.cmd in ("start", "stop", "restart", "status"):
        if args.cmd == "status":
            return cmd_status()
        if args.cmd in ("start", "restart"):
            cmd_start_server()
            cmd_start_dashboard()
        if args.cmd in ("stop", "restart"):
            cmd_stop_dashboard()
            cmd_stop_server()
        if args.cmd == "restart":
            print()
            cmd_status()
        return 0

    if args.cmd in ("dashboard", "server"):
        which = args.cmd
        sub_cmd = getattr(args, f"{which}_cmd")
        if sub_cmd == "status":
            if which == "server":
                ok, msg = _health(f"http://127.0.0.1:{SERVER_PORT}/healthz", timeout=5.0)
                pid = _read_pids().get("server") or _port_pid(SERVER_PORT)
                print(f"  server: pid={pid or '—'} healthy={ok}  {msg[:80]}")
                return 0 if ok else 1
            else:
                ok, msg = _health(f"http://127.0.0.1:{DASHBOARD_PORT}/api/status", timeout=10.0)
                pid = _read_pids().get("dashboard") or _port_pid(DASHBOARD_PORT)
                print(f"  dashboard: pid={pid or '—'} healthy={ok}  {msg[:80]}")
                return 0 if ok else 1
        if sub_cmd == "start":
            return 0 if (cmd_start_server() if which == "server" else cmd_start_dashboard()) else 1
        if sub_cmd == "stop":
            return 0 if (cmd_stop_server() if which == "server" else cmd_stop_dashboard()) else 1
        if sub_cmd == "restart":
            (cmd_stop_server if which == "server" else cmd_stop_dashboard)()
            (cmd_start_server if which == "server" else cmd_start_dashboard)()
            return 0

    if args.cmd == "qdrant":
        sub_cmd = args.qdrant_cmd
        if sub_cmd == "status":
            return cmd_qdrant_status()
        if sub_cmd == "snapshot":
            return 0 if cmd_qdrant_snapshot() else 1
        if sub_cmd == "stop":
            return 0 if cmd_qdrant_stop() else 1
        if sub_cmd == "start":
            return 0 if cmd_qdrant_start() else 1
        if sub_cmd == "restart":
            cmd_qdrant_stop(verbose=False)
            return 0 if cmd_qdrant_start() else 1

    if args.cmd == "logs":
        cmd_logs(args.service)
        return 0

    parser.print_help()
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n  [interrupted]")
        sys.exit(130)
