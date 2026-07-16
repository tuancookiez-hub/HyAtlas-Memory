"""Self-contained startup script for the HyAtlas-Memory stack.

Bundled inside the package so `hyatlas start` works from any directory
after `pip install hyatlas-memory`. The project root (containing the
``server/`` directory) is resolved via, in order:

  1. ``HYATLAS_PROJECT_ROOT`` env var (explicit)
  2. Current working directory (works when run from a cloned repo)

When the project root can't be resolved the script exits with a clear
message instead of failing mysteriously.

This module exposes the same ``main()`` as the legacy repo-root ``start.py``:
``python -m hyatlas_memory._start [start|--stop|--status|--help]``.
The CLI entry point ``hyatlas`` calls into here via
``hyatlas_memory.start``.
"""

from __future__ import annotations

import json
import os
import platform
import signal
import subprocess
import sys
import time
import urllib.error  # noqa: I001  (urllib.error + urllib.request must be together)
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from . import layout

# ── Project root resolution ─────────────────────────────────────────────


def _resolve_project_root() -> str | None:
    """Find the directory containing the bundled ``server/`` module.

    Resolution order:

    1. ``HYATLAS_PROJECT_ROOT`` env var (explicit override, legacy)
    2. The package install directory itself — where ``server/`` is bundled.
       Works for both ``pip install hyatlas-memory`` (PyPI wheel) and
       ``pip install -e .`` (editable), because in both cases
       ``server/`` sits next to ``_start.py`` inside the package.
    3. Legacy: walk up from CWD looking for a ``server/`` dir, for
       old repo layouts where ``server/`` was at the project root.
    """
    # 1. Explicit env var
    env_root = os.environ.get("HYATLAS_PROJECT_ROOT")
    if env_root and os.path.isdir(os.path.join(env_root, "server")):
        return os.path.abspath(env_root)

    # 2. Package install dir (works for both PyPI and editable installs).
    #    __file__ = <pkg_root>/hyatlas_memory/_start.py
    #    dirname(__file__) = <pkg_root>/hyatlas_memory/  ← this is the dir
    #                                                 with server/ bundled inside
    here = os.path.dirname(os.path.abspath(__file__))
    if os.path.isdir(os.path.join(here, "server")):
        return here

    # 3. Legacy: CWD + a few parents (old repo layout).
    cwd = os.getcwd()
    for _ in range(4):
        if os.path.isdir(os.path.join(cwd, "server")):
            return cwd
        parent = os.path.dirname(cwd)
        if parent == cwd:
            break
        cwd = parent

    return None


# ── Config ──────────────────────────────────────────────────────────────

QDRANT_PORT = int(os.environ.get("QDRANT_PORT", 6333))
UPSTREAM_PORT = int(os.environ.get("UPSTREAM_PORT", 19527))
DASHBOARD_PORT = int(os.environ.get("DASHBOARD_PORT", 8765))

HEALTH_TIMEOUT = 2          # seconds per health-check attempt
HEALTH_RETRIES = 20         # max attempts per service (upstream needs ~15s)
HEALTH_DELAY = 1            # seconds between retries


def _find_qdrant():
    """Locate the Qdrant binary. Returns (path, config_path) or (None, None).

    Search order:
      1. HYATLAS_QDRANT_BIN env var (explicit new override)
      2. Migrated binary under HYATLAS_HOME/vector/qdrant
      3. QDRANT_BIN env var (legacy override)
      4. `qdrant` on PATH
      5. Common locations per OS

    The migrated HYATLAS_HOME config is always used unless the user
    explicitly sets HYATLAS_QDRANT_CONFIG / QDRANT_CONFIG.
    """
    def _cfg():
        env_cfg = os.environ.get("HYATLAS_QDRANT_CONFIG") or os.environ.get("QDRANT_CONFIG")
        return str(env_cfg) if env_cfg else str(layout.qcfg())

    env_new = os.environ.get("HYATLAS_QDRANT_BIN")
    if env_new and os.path.isfile(env_new):
        return env_new, _cfg()

    if layout.qbin().is_file():
        return str(layout.qbin()), _cfg()

    env_legacy = os.environ.get("QDRANT_BIN")
    if env_legacy and os.path.isfile(env_legacy):
        return env_legacy, _cfg()

    import shutil
    path_bin = shutil.which("qdrant")
    if path_bin:
        return path_bin, _cfg()

    if platform.system() == "Windows":
        candidates = [
            r"C:\qdrant\qdrant.exe",
            os.path.expandvars(r"%PROGRAMFILES%\qdrant\qdrant.exe"),
            os.path.expanduser(r"~\qdrant\qdrant.exe"),
        ]
        for c in candidates:
            if os.path.isfile(c):
                return c, _cfg()
    else:
        candidates = [
            "/usr/local/bin/qdrant",
            "/usr/bin/qdrant",
            "/opt/qdrant/qdrant",
            os.path.expanduser("~/qdrant/qdrant"),
        ]
        for c in candidates:
            if os.path.isfile(c):
                return c, _cfg()

    return None, None


def _build_qdrant_cmd():
    """Build the Qdrant start command, or None if Qdrant should be skipped."""
    if is_port_listening(QDRANT_PORT):
        return None

    qbin, qcfg = _find_qdrant()
    if not qbin:
        return None

    cmd = [qbin]
    if qcfg:
        cmd += ["--config-path", qcfg]
    return cmd


# Service table — rebuilt per call from config (zvec skips Qdrant sidecar)


def _qdrant_service() -> dict:
    return {
        "name": "Qdrant",
        "port": QDRANT_PORT,
        "url": f"http://127.0.0.1:{QDRANT_PORT}/collections",
        "expect": "collections",
        "cmd": None,
        "cwd": None,
        "external": False,
    }


def _services(project_root: str) -> list[dict]:
    services: list[dict] = []
    services.extend([
        {
            "name": "Hy-Memory Server",
            "port": UPSTREAM_PORT,
            "url": f"http://127.0.0.1:{UPSTREAM_PORT}/info",
            "expect": "hy-memory-server",
            "cmd": [sys.executable, "-m", "server.start_server"],
            "cwd": project_root,
            "external": False,
        },
        {
            "name": "Dashboard",
            "port": DASHBOARD_PORT,
            "url": f"http://127.0.0.1:{DASHBOARD_PORT}/api/health",
            "expect": "ok",
            "cmd": [sys.executable, "server/dashboard/dashboard.py"],
            "cwd": project_root,
            "external": False,
        },
    ])
    return services


def _child_env() -> dict[str, str]:
    env = os.environ.copy()
    env.pop("PYTHONHOME", None)
    # Enable the in-process L5 knowledge-graph extraction (reads L2 facts from
    # the live zvec store and writes entities/relations to Kuzu during digest).
    # This replaces the legacy stop-server batch pipeline (MEMORY_L5_VERSION=1),
    # which could not stop the server managed by `hyatlas start`.
    env.setdefault("MEMORY_L5_VERSION", "2")
    # Disable the broken stop-server L5 auto-trigger: in-process extraction
    # already runs L5 inside digest, so spawning the batch pipeline is redundant
    # and always fails to stop the server.
    env["MEMORY_L5_AUTO"] = "false"
    return env


# ── Config display ──────────────────────────────────────────────────────


def _provider_from_url(base_url: str) -> str:
    """Derive a friendly provider name from an OpenAI-compatible base_url."""
    if not base_url:
        return "?"
    try:
        host = urlparse(base_url).hostname or ""
    except Exception:
        host = ""
    mapping = {
        "api.openai.com": "OpenAI",
        "api.deepseek.com": "DeepSeek",
        "openrouter.ai": "OpenRouter",
        "api.together.xyz": "Together",
        "api.groq.com": "Groq",
        "api.anthropic.com": "Anthropic",
        "api.mistral.ai": "Mistral",
        "api.perplexity.ai": "Perplexity",
        "api.fireworks.ai": "Fireworks",
        "api.x.ai": "xAI",
        "api.tokenrouter.com": "TokenRouter",
    }
    if host in mapping:
        return mapping[host]
    return host or "custom"


def _read_config() -> dict | None:
    """Read the active HyAtlas config if it exists."""
    cfg = layout.read_config()
    return cfg or None


def _vector_provider(cfg: dict | None = None) -> str:
    raw = cfg if cfg is not None else _read_config()
    if not raw:
        return os.environ.get("MEMORY_VECTOR_STORE", "zvec").lower()
    return str((raw.get("vector_store") or {}).get("provider") or "zvec").lower()


def _zvec_status_line(cfg: dict | None) -> str:
    from .core.config import MemoryConfig
    from .core.data.vector_store_zvec import resolve_zvec_path

    mem = MemoryConfig()
    vec = (cfg or {}).get("vector_store") or {}
    emb = (cfg or {}).get("embedder") or {}
    mem.vector_store.provider = "zvec"
    mem.vector_store.collection_name = str(vec.get("collection") or "agent_memories")
    mem.vector_store.embedding_dims = int(emb.get("dims") or 1024)
    path = resolve_zvec_path(mem)
    if not path.exists():
        return f"zvec collection missing at {path}"
    try:
        import zvec

        coll = zvec.open(str(path))
        n = coll.stats.doc_count
        coll = None
        return f"zvec {path.name}: {n} docs"
    except Exception as e:
        return f"zvec at {path}: {e}"


def _collection(cfg: dict | None) -> str:
    def norm(name: str, dims: int) -> str:
        suffix = f"_{dims}"
        if name.endswith(suffix):
            return name
        return f"{name}{suffix}"

    if not cfg:
        return os.environ.get("MEMORY_VECTOR_COLLECTION", "agent_memories_1024")
    vec = cfg.get("vector_store") or {}
    dims = int((cfg.get("embedder") or {}).get("dims", 1024))
    if vec.get("collection"):
        return norm(str(vec["collection"]), dims)
    env = os.environ.get("MEMORY_VECTOR_COLLECTION")
    if env:
        return env
    return f"agent_memories_{dims}"


def _qdrant_collection_status(port: int, cfg: dict | None) -> tuple[str, int | None, int | None]:
    name = _collection(cfg)
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/collections/{name}", timeout=3
        ) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="replace"))
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return f"collection {name} missing", None, None
        return f"collection {name} check failed: HTTP {e.code}", None, None
    except Exception as e:
        return f"collection {name} check failed: {e}", None, None
    res = data.get("result") or {}
    points = res.get("points_count")
    size = ((res.get("config") or {}).get("params") or {}).get("vectors", {}).get("size")
    expected = (cfg.get("embedder") or {}).get("dims") if cfg else None
    if expected and size and int(size) != int(expected):
        return f"collection {name} dimension mismatch: {size} != {expected}", points, size
    if points == 0 and layout.active_config_path() in layout.legacy_cfgs():
        return f"collection {name} is empty; possible wrong storage for existing legacy install", points, size
    return f"collection {name}: {points} points, {size} dims", points, size


def _print_config_summary() -> None:
    """Print the LLM model + provider, embedder, mode, vector store from config."""
    cfg = _read_config()
    if not cfg:
        return

    llm = cfg.get("llm") or {}
    embed = cfg.get("embedder") or {}
    vs = cfg.get("vector_store") or {}

    llm_model = llm.get("model") or "?"
    llm_provider = _provider_from_url(llm.get("base_url", ""))
    embed_model = embed.get("model") or "?"
    embed_provider = embed.get("provider") or "?"
    mode = cfg.get("mode") or "?"
    vs_provider = vs.get("provider") or "?"

    print()
    print(f"  {CYAN}🧠 LLM{RESET}         {llm_model}  {DIM}via {llm_provider}{RESET}")
    print(f"  {CYAN}📐 Embedder{RESET}    {embed_model}  {DIM}({embed_provider}){RESET}")
    print(f"  {CYAN}🎯 Mode{RESET}        {mode}  {DIM}· vector store: {vs_provider}{RESET}")
    print()


# ── ANSI helpers ────────────────────────────────────────────────────────

BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
RESET = "\033[0m"


def ok(msg):   return f"{GREEN}✔{RESET} {msg}"
def fail(msg): return f"{RED}✘{RESET} {msg}"
def warn(msg): return f"{YELLOW}⚠{RESET} {msg}"
def info(msg): return f"{CYAN}→{RESET} {msg}"
def dim(msg):  return f"{DIM}{msg}{RESET}"


# ── Process management ──────────────────────────────────────────────────

children: list[subprocess.Popen] = []
# Module-level flag so start_service() can branch on detached vs foreground
# child creation flags without threading the argument through every caller.
detach: bool = False
# Module-level flag so start_all() can bypass the restart prompt when
# the user passes --restart on the command line.
force_restart: bool = False


def kill_on_port(port: int) -> int:
    """Kill any process listening on `port`. Returns number killed."""
    if platform.system() == "Windows":
        out = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True, text=True, timeout=5,
        ).stdout
        pids = set()
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 5 and f":{port}" in parts[1] and "LISTENING" in parts[3]:
                try:
                    pids.add(int(parts[4]))
                except ValueError:
                    pass
        for pid in pids:
            if pid == os.getpid():
                continue
            try:
                subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                               capture_output=True, timeout=5)
            except Exception:
                pass
        return len(pids)
    else:
        try:
            out = subprocess.run(
                ["lsof", "-ti", f":{port}"],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            if out:
                for pid in out.splitlines():
                    subprocess.run(["kill", "-9", pid], capture_output=True, timeout=5)
                return len(out.splitlines())
        except FileNotFoundError:
            pass
        return 0


def is_port_listening(port: int) -> bool:
    """Check if a process is listening on `port`.

    Only returns True for an actual LISTENING socket owned by a real PID.
    Filters out TIME_WAIT / FIN_WAIT entries (which have PID 0 and no live
    process) so the restart flow doesn't get confused by recently-killed
    processes whose TCP connections haven't fully torn down yet.
    """
    if platform.system() == "Windows":
        out = subprocess.run(
            ["netstat", "-ano"],
            capture_output=True, text=True, timeout=5,
        ).stdout
        for line in out.splitlines():
            parts = line.split()
            # format: Proto  LocalAddr  ForeignAddr  State  PID
            if (
                len(parts) >= 5
                and f":{port}" in parts[1]
                and "LISTENING" in parts[3]
                and parts[4] != "0"  # PID 0 = TIME_WAIT, no real process
            ):
                return True
        return False
    else:
        try:
            out = subprocess.run(
                ["lsof", "-ti", f":{port}"],
                capture_output=True, text=True, timeout=5,
            ).stdout.strip()
            return bool(out)
        except FileNotFoundError:
            return False


def health_check(url: str, expect: str, timeout: float = HEALTH_TIMEOUT) -> bool:
    """Return True if `url` returns a response containing `expect`."""
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return expect in body
    except Exception:
        return False


# ── Startup ─────────────────────────────────────────────────────────────

def banner():
    print()
    print(f"  {BOLD}╔══════════════════════════════════════╗{RESET}")
    print(f"  {BOLD}║{RESET}          {BOLD}HyAtlas Memory{RESET}            {BOLD}║{RESET}")
    print(f"  {BOLD}║{RESET}       AI Memory Atlas v1.0         {BOLD}║{RESET}")
    print(f"  {BOLD}╚══════════════════════════════════════╝{RESET}")
    print()


def _print_service_urls():
    """Print post-start URLs (Qdrant line omitted when vector provider is zvec)."""
    dash_bind = os.environ.get("HY_DASH_BIND", "127.0.0.1")
    dash_url = f"http://127.0.0.1:{DASHBOARD_PORT}"
    if dash_bind not in ("127.0.0.1", "localhost", "::1"):
        token_file = os.path.join(os.path.expanduser("~"), ".hy_memory", ".dashboard_token")
        try:
            with open(token_file) as f:
                token = f.read().strip()
            dash_url = f"{dash_url}/?token={token}"
        except Exception:
            pass
    print(f"  {BOLD}Dashboard:{RESET}  {dash_url}")
    print(f"  {BOLD}Upstream:{RESET}   http://127.0.0.1:{UPSTREAM_PORT}")
    if _vector_provider() != "zvec":
        print(f"  {BOLD}Qdrant:{RESET}     http://127.0.0.1:{QDRANT_PORT}")
    else:
        print(dim("  Vector store: zvec (no Qdrant sidecar)"))
    print()


def start_service(svc: dict) -> bool:
    """Start a single service. Returns True if healthy.

    Honors the module-level `detach` flag:
    - detach=False (default): children get CREATE_NEW_PROCESS_GROUP so Ctrl+C
      in the launching terminal kills them. Standard "stop with the terminal"
      behavior.
    - detach=True: children get DETACHED_PROCESS so they survive the launching
      terminal closing. Use `hyatlas stop` to kill them.
    """
    name = svc["name"]
    port = svc["port"]

    # Resolve Qdrant command lazily
    if name == "Qdrant" and svc["cmd"] is None:
        if is_port_listening(port):
            svc["external"] = True
        else:
            qbin, qcfg = _find_qdrant()
            if qbin:
                cmd = [qbin]
                if qcfg:
                    cmd += ["--config-path", qcfg]
                svc["cmd"] = cmd
            else:
                svc["external"] = True

    # Already running?
    if is_port_listening(port):
        if health_check(svc["url"], svc["expect"]):
            print(ok(f"{name} already running on port {port}"))
            return True
        elif svc.get("external"):
            print(fail(f"{name} on port {port} but unhealthy (external — check your Docker/container)"))
            return False
        else:
            print(warn(f"Port {port} occupied but health check failed — killing stale process"))
            kill_on_port(port)
            time.sleep(1)
    elif svc.get("external"):
        if name == "Qdrant":
            print(warn(f"{name} not found on port {port} and no local binary detected."))
            print(dim("  Start Qdrant separately (e.g. 'docker run -p 6333:6333 qdrant/qdrant')"))
            print(dim("  or install it and ensure it's on your PATH."))
            print(dim("  Continuing without Qdrant — vector search will not work."))
            return False
        return False

    # Kill stale processes
    killed = kill_on_port(port)
    if killed:
        print(dim(f"  killed {killed} stale process(es) on port {port}"))
        time.sleep(1)

    # Start
    print(info(f"Starting {name} on port {port}..."))
    log_dir = str(layout.logs())
    os.makedirs(log_dir, exist_ok=True)
    log_path = os.path.join(log_dir, f"{name.lower().replace(' ', '_')}.log")
    try:
        env = _child_env()
        log_file = open(log_path, "w")
        flags = 0
        no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        # DETACHED_PROCESS (0x08) makes the child independent of the
        # launching console — survives terminal close. Used for
        # `hyatlas --detach` so users can close the launcher
        # without killing the stack.
        #
        # CREATE_NEW_PROCESS_GROUP (0x200) keeps the child tied to
        # the launching console's process group so Ctrl+C kills it.
        # Used for foreground `hyatlas start` so closing the
        # terminal cleanly stops everything.
        flags = (
            0x00000008 | no_window
            if detach
            else subprocess.CREATE_NEW_PROCESS_GROUP | no_window
        )
        proc = subprocess.Popen(
            svc["cmd"],
            stdout=log_file,
            stderr=subprocess.STDOUT,
            cwd=svc["cwd"],
            env=env,
            creationflags=flags,
        )
        children.append(proc)
        print(dim(f"  log: {log_path}"))
    except FileNotFoundError as e:
        print(fail(f"Cannot start {name}: {e}"))
        return False

    # Health check with retries
    for attempt in range(1, HEALTH_RETRIES + 1):
        if health_check(svc["url"], svc["expect"]):
            print(ok(f"{name} ready on port {port}  {dim(f'({attempt}s)')}"))
            if name == "Qdrant":
                time.sleep(2)
            return True

        if proc.poll() is not None and attempt <= 3:
            print(warn(f"{name} crashed, restarting..."))
            try:
                env = _child_env()
                log_file = open(log_path, "a")
                flags = 0
                if platform.system() == "Windows":
                    flags = subprocess.CREATE_NEW_PROCESS_GROUP | getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
                proc = subprocess.Popen(
                    svc["cmd"],
                    stdout=log_file,
                    stderr=subprocess.STDOUT,
                    cwd=svc["cwd"],
                    env=env,
                    creationflags=flags,
                )
                children[-1] = proc
            except Exception:
                pass

        time.sleep(HEALTH_DELAY)

    print(fail(f"{name} failed to start after {HEALTH_RETRIES}s"))
    if os.path.exists(log_path):
        with open(log_path) as f:
            tail = f.read()[-500:]
        print(dim(f"  last log lines: {tail.strip()[:200]}"))
    return False


def start_all(detach_requested: bool = False) -> None:
    project_root = _resolve_project_root()
    if not project_root:
        print(fail("Could not find the HyAtlas project root (the directory with `server/`)."))
        print()
        print("  Fix one of these:")
        print("    1. Set env var:        set HYATLAS_PROJECT_ROOT=F:\\Projects\\hyatlas-memory")
        print("    2. cd into the project:cd F:\\Projects\\hyatlas-memory")
        print("    3. pip install -e .     (editable install picks it up automatically)")
        sys.exit(1)

    services = _services(project_root)

    # If every service is already running and healthy, ask the user whether
    # they want to restart. Prevents the "I ran `hyatlas` and nothing happened"
    # surprise — the user gets a clear choice between "keep what's running"
    # and "bounce everything". The --restart flag bypasses the prompt.
    if all(
        is_port_listening(svc["port"]) and health_check(svc["url"], svc["expect"])
        for svc in services
    ):
        if not force_restart:
            try:
                answer = input(
                    "\n  All services already running. Restart all? [y/N]: "
                ).strip().lower()
            except (EOFError, KeyboardInterrupt):
                answer = "n"
        else:
            answer = "y"
        if answer in ("y", "yes"):
            # Restart path: stop the running services, then spawn a
            # watcher subprocess that waits for THIS process to exit
            # and re-launches the stack detached. The watcher is a
            # SIBLING of `hyatlas`, not a child, so it survives even
            # if this process is killed (Ctrl+C, terminal close, system
            # suspend) mid-shutdown. Once spawned, we return control
            # to the user's terminal immediately.
            #
            # Pattern adopted from Hermes' gateway/restart flow
            # (gateway/run.py lines 4690-4789).
            stop_all()
            print()
            print(info("Scheduling detached re-launch via restart watcher..."))
            print()
            # Re-launch argv the watcher will execute once we exit:
            # `hyatlas --detach` — picks up where the previous run left off.
            relaunch_argv = [sys.executable, "-m", "hyatlas_memory._start", "--detach"]
            # Spawn the watcher as a fully-detached sibling.
            from hyatlas_memory._restart_watcher import _detach_kwargs
            try:
                subprocess.Popen(
                    [sys.executable, "-m", "hyatlas_memory._restart_watcher",
                     str(os.getpid()), *relaunch_argv],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    stdin=subprocess.DEVNULL,
                    env=_watcher_env(),
                    **_detach_kwargs(),
                )
            except Exception as e:
                print(fail(f"Failed to spawn restart watcher: {e}"))
                print(dim("  Stack will stay down. Run `hyatlas` to start it manually."))
                sys.exit(1)
            print(dim("  Restart watcher spawned. It will re-launch the stack"))
            print(dim("  once this `hyatlas` process exits."))
            print(dim("  Your terminal is free — close it whenever."))
            print()
            return
        else:
            banner()
            print(f"  {BOLD}{GREEN}All services already running.{RESET}")
            print()
            _print_service_urls()
            print(dim("  Run 'hyatlas stop' to shut down, or re-run with restart confirmation."))
            print()
            # Exit cleanly (no health check loop since we're not managing services)
            return

    banner()
    print(f"  {BOLD}Starting HyAtlas Memory stack (foreground)...{RESET}")
    print(dim(f"  project root: {project_root}"))
    print()

    for svc in services:
        if not start_service(svc):
            print()
            print(fail("Startup aborted. See errors above."))
            print(dim("  Previously started services are still running."))
            print(dim("  Run 'hyatlas --stop' to clean up."))
            sys.exit(1)

    print()
    print(f"  {BOLD}{GREEN}All services running!{RESET}")
    print()
    _print_service_urls()
    print(dim("  Press Ctrl+C to stop all services"))
    print()

    # LLM / embedder / mode summary from ~/.hermes/hy_memory.json.
    # Opt-in: set HYATLAS_SHOW_CONFIG=1 to see it.
    if os.environ.get("HYATLAS_SHOW_CONFIG") == "1":
        _print_config_summary()

    try:
        mem_url = f"http://127.0.0.1:{UPSTREAM_PORT}/info"
        with urllib.request.urlopen(mem_url, timeout=3) as resp:
            info_json = json.loads(resp.read().decode("utf-8", errors="replace"))
        version = info_json.get("version", "?")
        print(f"  {CYAN}🧠 Hy-Memory v{version}{RESET} — running on :{UPSTREAM_PORT}")
        print(f"  {CYAN}📊 Dashboard{RESET}          — running on :{DASHBOARD_PORT}")
        print(f"  {CYAN}🗄️  Qdrant{RESET}              — running on :{QDRANT_PORT}")
        print()
    except Exception:
        pass

    try:
        while True:
            all_healthy = True
            for svc in services:
                if not health_check(svc["url"], svc["expect"]):
                    if is_port_listening(svc["port"]):
                        print(warn(f"{svc['name']} on port {svc['port']} — port occupied but unhealthy"))
                    else:
                        print(fail(f"{svc['name']} on port {svc['port']} — stopped"))
                        all_healthy = False
            if not all_healthy:
                print(dim("  Run 'hyatlas --stop' then 'hyatlas start' to restart."))
                sys.exit(1)
            time.sleep(5)
    except KeyboardInterrupt:
        print()
        print(dim("  Shutting down..."))
        shutdown()
        print(ok("All services stopped."))
        sys.exit(0)


# ── Shutdown ────────────────────────────────────────────────────────────

def shutdown():
    """Kill all child processes in reverse order."""
    for proc in reversed(children):
        try:
            if proc.poll() is not None:
                continue
            if platform.system() == "Windows":
                subprocess.run(
                    ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                    capture_output=True, timeout=5,
                )
            else:
                proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass


# ── Status ──────────────────────────────────────────────────────────────

def _warn_legacy_paths() -> None:
    from . import layout
    warnings = []
    cfg = layout.active_config_path()
    if cfg and cfg in layout.legacy_cfgs():
        warnings.append(f"using legacy config at {cfg}")
    for p in (Path.home() / ".hy_memory", Path("C:/qdrant-data") if sys.platform == "win32" else Path.home() / "qdrant-data"):
        if p.exists():
            warnings.append(f"legacy Qdrant data still present at {p}")
    if warnings:
        print(dim("Legacy paths (non-blocking):"))
        for w in warnings:
            print(dim(f"  - {w}"))
        print(dim("  Runtime uses Zvec. Archive with `hyatlas archive qdrant` or delete manually if no longer needed."))


def show_status() -> None:
    """Print the status table for each managed service."""
    _warn_legacy_paths()
    project_root = _resolve_project_root()
    if not project_root:
        print(fail("Could not find the HyAtlas project root."))
        print(dim("  Set HYATLAS_PROJECT_ROOT or cd into the project."))
        sys.exit(1)
    services = _services(project_root)
    cfg = _read_config()
    banner()
    print(f"  {BOLD}Service Status{RESET}")
    print()
    for svc in services:
        port = svc["port"]
        listening = is_port_listening(port)
        healthy = health_check(svc["url"], svc["expect"]) if listening else False
        if healthy:
            print(ok(f"{svc['name']:20s} port {port}  healthy"))
            if svc["name"] == "Qdrant":
                detail, points, _ = _qdrant_collection_status(port, cfg)
                if points is None or (points == 0 and layout.active_config_path() in layout.legacy_cfgs()):
                    print(warn(f"{'':23s}{detail}"))
                else:
                    print(dim(f"{'':23s}{detail}"))
        elif listening:
            print(warn(f"{svc['name']:20s} port {port}  listening but unhealthy"))
        else:
            print(fail(f"{svc['name']:20s} port {port}  not running"))
    if _vector_provider(cfg) == "zvec":
        detail = "zvec (server not running)"
        if is_port_listening(UPSTREAM_PORT) and health_check(
            f"http://127.0.0.1:{UPSTREAM_PORT}/info", "hy-memory-server"
        ):
            try:
                with urllib.request.urlopen(
                    f"http://127.0.0.1:{UPSTREAM_PORT}/api/v1/status", timeout=15
                ) as resp:
                    st = json.loads(resp.read().decode("utf-8", errors="replace"))
                detail = (
                    f"provider=zvec collection={st.get('vdb_collection', '?')} "
                    f"(server active)"
                )
            except Exception:
                detail = "provider=zvec (server active, status timeout)"
        else:
            detail = _zvec_status_line(cfg)
        print(ok(f"{'Zvec store':20s} {detail}"))
    print()


# ── Stop ────────────────────────────────────────────────────────────────

def _watcher_env() -> dict:
    """Build env for spawning the restart watcher.

    Inherits the current environment so the watcher sees
    HYATLAS_PROJECT_ROOT and other vars the user has set, but strips
    any in-process state that doesn't belong in the watcher's
    environment (none today, but the helper is here for future use).
    """
    return os.environ.copy()


def _wait_for_kuzu_lock_free(timeout: float = 30.0) -> bool:
    """Wait for the Kuzu DB file-system lock to be released.

    Kuzu holds an OS-level file lock on its database directory
    (``~/.hy_memory/data/kuzu_db``) while the upstream is running. After
    the upstream process dies, the OS takes a moment to actually release
    that lock. If a new upstream starts before the lock is released, it
    crashes with::

        IO exception: Could not set lock on file : C:\\Users\\...\\kuzu_db

    We can't directly probe the lock from Python (Windows file locking
    is opaque), so we use a time-based wait. 30 seconds is generous:
    in practice the lock is released within 2-5 seconds after the
    process dies. See https://docs.kuzudb.com/concurrency for details
    on Kuzu's concurrency model.

    Returns True once the wait completes.
    """
    # Conservative minimum wait after upstream port frees up.
    # Kuzu docs note that the file lock is released "as soon as the
    # last transaction commits" but in practice on Windows the OS
    # takes a few seconds to actually flush the handle. 5 seconds is
    # the sweet spot based on observed restart timings.
    time.sleep(min(timeout, 5.0))
    return True


def _wait_for_port_free(port: int, timeout: float = 10.0) -> bool:
    """Block until the port has no LISTENING socket, or timeout.

    Returns True if the port freed within the timeout, False otherwise.

    On Windows, after `taskkill /F` the OS takes a moment to actually
    release the TCP socket (it goes through TIME_WAIT, then CLOSED).
    Without this wait, the next start command sees stale TIME_WAIT
    entries and gets confused about whether the port is occupied.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not is_port_listening(port):
            return True
        time.sleep(0.3)
    return False


def _kill_on_port_sync(port: int, timeout: float = 10.0) -> int:
    """Kill anything on the port, then wait for the port to actually free.

    Returns the number of processes that were killed, or 0 if none.

    Note: the upstream service holds additional locks beyond just its
    TCP socket — Kuzu DB acquires a file-system lock on
    ``~/.hy_memory/data/kuzu_db`` that takes a moment to release after
    the process dies. If the new upstream tries to acquire that lock
    before the old one has fully released it, the new upstream crashes
    with ``GraphStore (kuzu) init failed``. We extend the wait for
    port 19527 (upstream) to give the lock time to release. See:
    https://docs.kuzudb.com/concurrency
    """
    killed = kill_on_port(port)
    if killed == 0:
        # Nothing was on the port — no need to wait.
        return 0
    # Kuzu DB locks (used by the upstream service) take noticeably
    # longer to release than plain TCP sockets.
    if timeout < 30.0:
        timeout = 30.0
    freed = _wait_for_port_free(port, timeout=timeout)
    if not freed:
        print(warn(f"Port {port} still occupied after {timeout}s — proceeding anyway."))
    return killed


def stop_all():
    services = _services(_resolve_project_root() or os.getcwd())
    banner()
    print(f"  {BOLD}Stopping all services...{RESET}")
    print()
    for svc in reversed(services):
        port = svc["port"]
        if is_port_listening(port):
            killed = _kill_on_port_sync(port)
            if killed:
                print(ok(f"{svc['name']} stopped (killed {killed} process(es))"))
                # The upstream service holds a Kuzu DB file lock that
                # takes a few extra seconds to release after the process
                # exits. Without this wait, the new upstream crashes
                # with "GraphStore (kuzu) init failed" on restart.
                # See _wait_for_kuzu_lock_free for details.
                if svc["name"] == "Hy-Memory Server":
                    _wait_for_kuzu_lock_free()
            else:
                print(dim(f"{svc['name']} — no process found on port {port}"))
        else:
            print(dim(f"{svc['name']} — not running"))
    if _vector_provider() == "zvec" and is_port_listening(QDRANT_PORT):
        killed = _kill_on_port_sync(QDRANT_PORT, timeout=10.0)
        if killed:
            print(ok(f"Legacy Qdrant stopped (killed {killed} process(es) on :{QDRANT_PORT})"))
    print()
    print(ok("Done."))


# ── Detached start (services survive terminal close) ───────────────────


def _start_detached_and_exit() -> None:
    """Start the stack as detached children and return immediately.

    Used by `hyatlas --detach` so the services outlive the terminal that
    launched them. We:

    1. Start each service as a detached subprocess (no new console,
       no parent process group — independent of the launching shell).
    2. Wait for all 3 services to pass their health check.
    3. Print a summary of the running services and exit.

    To stop services launched this way, use `hyatlas stop` (kills by port)
    or `hyatlas status` to see what's running.
    """
    project_root = _resolve_project_root()
    if not project_root:
        print(fail("Could not find the HyAtlas project root."))
        print(dim("  Set HYATLAS_PROJECT_ROOT or cd into the project."))
        sys.exit(1)

    services = _services(project_root)

    # Stop anything already running (same restart semantics as the
    # foreground prompt, but without asking — the user explicitly
    # asked to launch detached).
    if any(is_port_listening(svc["port"]) for svc in services):
        stop_all()

    # Set the module-level flag so start_service uses DETACHED_PROCESS
    # for the children it spawns below.
    global detach
    was_detach = detach
    detach = True
    try:
        _do_start(services, project_root, detached=True)
    finally:
        detach = was_detach


def _do_start(services: list, project_root: str, detached: bool) -> None:
    """Shared start path used by both foreground and detached modes."""
    banner()
    label = "detached" if detached else "foreground"
    print(f"  {BOLD}Starting HyAtlas Memory stack ({label})...{RESET}")
    print(dim(f"  project root: {project_root}"))
    print()

    for svc in services:
        if not start_service(svc):
            print()
            print(fail("Startup aborted. Some services may be running."))
            print(dim("  Run 'hyatlas stop' to clean up."))
            sys.exit(1)

    print()
    print(f"  {BOLD}{GREEN}All services running.{RESET}")
    print()
    _print_service_urls()
    if detached:
        print(dim("  Services are detached. Run 'hyatlas stop' to shut down."))
        print(dim("  They will survive closing this terminal."))
        # Pop a visible status window so the user can see HyAtlas is up.
        # Idempotent — only opens if no console window is already running.
        _launch_status_console()
        sys.exit(0)
    else:
        print(dim("  Press Ctrl+C in this terminal to stop the services."))


# ── Main ────────────────────────────────────────────────────────────────

def _status_console_already_running() -> bool:
    """Return True if a hyatlas_memory.console window is already open.

    Prevents the spawn-on-every-start pile-up: each `hyatlas start` would
    otherwise pop another blank window, leaving 8+ orphans after a few
    test cycles. We scan wmic for any python.exe whose command line
    contains `hyatlas_memory.console`. With the direct-Popen spawn path,
    the python process IS the window: if it dies, the window closes
    (CREATE_NEW_CONSOLE on a python Popen gives the new console to that
    python and its children only).
    """
    try:
        out = subprocess.run(
            ["wmic", "process", "where", "name='python.exe'", "get", "commandline"],
            capture_output=True, text=True, timeout=4,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000),
        ).stdout
    except Exception:
        return False
    return "hyatlas_memory.console" in out


def _launch_status_console() -> None:
    """Spawn ONE visible console window showing HyAtlas health.

    Safe to close: only the monitor dies, the server keeps running.
    Idempotent: if a console is already open, this is a no-op.

    Implementation notes:
      Windows: Popen python.exe with CREATE_NEW_CONSOLE so a new
      console window appears directly. Do NOT pipe stdout/stderr
      (that makes the window look blank because nothing reads the
      pipe buffer). Do NOT use `cmd /c start ...` from a non-tty
      parent shell — `start` blocks synchronously when it detects
      redirected stdio, leaving the outer cmd hung forever.
    """
    if _status_console_already_running():
        return

    if sys.platform != "win32":
        try:
            subprocess.Popen(
                [_base_python(), "-m", "hyatlas_memory.console"],
                env=_console_spawn_env(),
            )
        except Exception as e:
            print(warn(f"  console spawn failed: {e}"))
        return

    try:
        creationflags = (
            subprocess.CREATE_NEW_CONSOLE
            | subprocess.CREATE_NEW_PROCESS_GROUP
        )
        # Direct Popen of python -m hyatlas_memory.console with
        # CREATE_NEW_CONSOLE. Python's stdout is automatically
        # attached to the new console. No pipe redirection, no
        # start builtin — keeps the spawn simple and reliable.
        #
        # IMPORTANT: use sys._base_executable (the venv shim's real
        # python) instead of sys.executable. Spawning the venv shim
        # makes Python re-exec to the base python on startup, which
        # causes the console window to flicker (two short-lived
        # console windows appear in sequence).
        subprocess.Popen(
            [_base_python(), "-m", "hyatlas_memory.console"],
            creationflags=creationflags,
            close_fds=False,
            env=_console_spawn_env(),
        )
    except Exception as e:
        # Surface the error so a silent dead console doesn't strand
        # the user staring at nothing. The base python is the most
        # common failure point — env var problems, missing module, etc.
        print(warn(f"  console spawn failed: {e}"))
def _venv_site_packages() -> str | None:
    """Path to the hermes-agent venv site-packages, or None if not in a venv.

    The hermes-agent venv is a uv-managed shim: its python.exe detects
    a venv/home mismatch on startup and re-execs to the base python,
    spawning a new console window along the way (the source of the
    flicker). We work around this by spawning the BASE python directly
    with PYTHONPATH pointing at the venv site-packages and the editable
    install location.
    """
    sp = os.path.join(sys.prefix, "Lib", "site-packages")
    return sp if os.path.isdir(sp) else None


def _base_python() -> str:
    """Return the path to the real Python binary (the venv shim's target).

    Skipping the venv shim prevents the re-exec that causes the
    double-console flicker. We use sys._base_executable when available
    (always present in a venv) and fall back to sys.executable.
    """
    return getattr(sys, "_base_executable", None) or sys.executable


def _console_spawn_env() -> dict[str, str]:
    """Environment for the spawned console child.

    Sets PYTHONPATH so the base python (which we spawn to skip the
    venv shim) finds:
      1. the venv site-packages (third-party deps like zvec, openai)
      2. the editable install root — i.e. the *parent* of the
         hyatlas_memory package directory, since `import hyatlas_memory`
         resolves to `<root>/hyatlas_memory/__init__.py`.

    We REPLACE any inherited PYTHONPATH rather than appending. The
    parent shell often carries a duplicated/hermetic PYTHONPATH from
    the hermes-agent venv that bleeds into the child and produces
    confusing import errors.

    Also propagates HYATLAS_HOME / HERMES_HOME so the console's
    log path resolves, and strips MSYS/Cygwin shell pollution that can
    make CREATE_NEW_CONSOLE windows fail to paint when launched from
    Git Bash.
    """
    env = os.environ.copy()
    # MSYS/Git Bash injects these; a Win32 console child does not want them.
    for key in (
        "MSYSTEM",
        "MSYSTEM_CARCH",
        "MSYSTEM_CHOST",
        "MSYSTEM_PREFIX",
        "MINGW_PREFIX",
        "MINGW_CHOST",
        "MINGW_PACKAGE_PREFIX",
        "CYGWIN",
        "TERM_PROGRAM",
        "ORIGINAL_PATH",
    ):
        env.pop(key, None)
    paths = []
    sp = _venv_site_packages()
    if sp:
        paths.append(sp)
    # Editable install: console.py lives at <root>/hyatlas_memory/_start.py.
    # The package root is the PARENT of this file.
    package_dir = os.path.dirname(os.path.abspath(__file__))
    package_root = os.path.dirname(package_dir)
    paths.append(package_root)
    # REPLACE inherited PYTHONPATH rather than appending. The parent shell
    # often carries a duplicated/hermetic PYTHONPATH from hermes-agent that
    # bleeds into the child and produces confusing import errors.
    env["PYTHONPATH"] = os.pathsep.join(paths)
    return env


def main():
    # Strip --internal (used for console window relaunch on Windows)
    # and --detach (start services as truly detached children) and
    # --restart (force restart without prompting). These are parsed and
    # removed from sys.argv before the subcommand dispatcher sees the
    # remaining args, so subcommands like `start` aren't confused by them.
    global detach
    detach = "--detach" in sys.argv
    sys.argv = [a for a in sys.argv if a != "--detach"]
    global force_restart
    force_restart = "--restart" in sys.argv
    sys.argv = [a for a in sys.argv if a != "--restart"]
    # `--internal` is preserved as a flag if present (callers may inspect
    # it for backwards-compat reasons) but no longer drives any behavior
    # in main() — the previous "spawn a new console" path was removed.
    if "--internal" in sys.argv:
        sys.argv = [a for a in sys.argv if a != "--internal"]

    if len(sys.argv) > 1:
        arg = sys.argv[1].lstrip("-")
        if arg == "stop":
            stop_all()
        elif arg == "status":
            show_status()
        elif arg == "help" or arg == "h":
            print(__doc__)
        elif arg == "start":
            # "hyatlas start" is equivalent to bare "hyatlas" — fall through
            pass
        elif arg == "memory":
            # "hyatlas memory write|recall|list|reflect" — see _memory_cli.py
            from hyatlas_memory import _memory_cli
            sys.exit(_memory_cli.main(sys.argv[1:]))
        elif arg == "console":
            # Pop a fresh status window. Idempotent — no-op if one is
            # already open. Closing the window does NOT stop the
            # services; they keep running in the background.
            _launch_status_console()
            return
        else:
            print(fail(f"Unknown argument: {sys.argv[1]}"))
            print("Usage: hyatlas [start|stop|status|console|memory|help]")
            sys.exit(1)
    else:
        # Bare `hyatlas` (no args) starts the stack in the CURRENT terminal
        # and stays attached. This is the default behavior — services run
        # in the same console the user typed the command in, so closing
        # this terminal cleanly stops the services (no orphaned
        # popped-up consoles, no parent process group weirdness).
        #
        # To run detached (services survive terminal close), use
        # `hyatlas --detach` or `hyatlas start --detach`.
        if detach:
            _start_detached_and_exit()
        else:
            signal.signal(signal.SIGINT, lambda *_: (print(), shutdown(), sys.exit(0)))
            start_all()


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        print(f"\n  ✘ Unexpected error: {e}")
        import traceback
        traceback.print_exc()
        print("\n  Press Enter to close...")
        input()
