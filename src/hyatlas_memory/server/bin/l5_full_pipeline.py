"""
L5 full pipeline — single-script orchestrator for the entire L5 knowledge
graph refresh.

Triggered by the System2 digest() in hy_memory (via a small hook added to
pipelines/system2_writer.py). Runs in a detached subprocess so it can
take an exclusive Kuzu lock (which requires the hy-memory server to be
stopped).

Steps:
  1. Stop the hy-memory server
  2. Run l5_digest_writer.py    (extract from L2_facts)
  3. Run l5_entity_resolver.py  (dedup)
  4. Run l5_quality_review.py   (filter noise)
  5. Run l5_ingest_kuzu.py --rebuild  (wipe + write to Kuzu)
  6. Run l5_export_json.py      (re-export JSON for dashboard)
  7. Restart the hy-memory server

Each step logs to logs/l5_pipeline_run.log. The dashboard proxy can be
checked at /api/l5/graph while this runs to see partial state — the
endpoint serves from the LAST export, not the in-progress Kuzu state.

Failure handling: if any step fails, the script tries to restart the
server before exiting, so the system doesn't get stuck with the server
down. Returns non-zero exit code on failure.
"""
import json
import os
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path

# ------------------------------------------------------------------
# Config: resolve paths dynamically
# ------------------------------------------------------------------
HERMES_HOME = Path(os.environ.get(
    "HERMES_HOME",
    str(Path.home() / "AppData" / "Local" / "hermes") if sys.platform == "win32"
    else str(Path.home() / ".local" / "share" / "hermes")
))
HERMES_AGENT = HERMES_HOME / "hermes-agent"
VENV_PYTHON = sys.executable
LOG_PATH = HERMES_HOME / "logs" / "l5_pipeline_run.log"
STATE_PATH = HERMES_HOME / "logs" / "l5_pipeline_state.json"
LOCK_PATH = HERMES_HOME / "logs" / "l5_pipeline.lock"
RUN_ID = f"l5_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"

# Server health endpoint
HEALTH_URL = "http://127.0.0.1:19527/healthz"
SERVER_CMD = [VENV_PYTHON, str(HERMES_HOME / "bin" / "hymemory.py")]

# Step scripts
SCRIPTS = {
    "digest":    HERMES_HOME / "bin" / "l5_digest_writer.py",
    "resolve":   HERMES_HOME / "bin" / "l5_entity_resolver.py",
    "review":    HERMES_HOME / "bin" / "l5_quality_review.py",
    "ingest":    HERMES_HOME / "bin" / "l5_ingest_kuzu.py",
    "export":    HERMES_HOME / "bin" / "l5_export_json.py",
}

# Step output paths
STEP_OUTPUTS = {
    "digest":  HERMES_HOME / "logs" / "l5_full_stats.json",
    "resolve": HERMES_HOME / "logs" / "l5_resolution_stats.json",
    "review":  HERMES_HOME / "logs" / "l5_quality_review.json",
    "ingest":  HERMES_HOME / "logs" / "l5_ingest_stats.json",
    "export":  HERMES_HOME / "logs" / "l5_kuzu_export.json",
}


# ------------------------------------------------------------------
# Logging (file only, no stdout — detached process)
# ------------------------------------------------------------------
def log(msg: str) -> None:
    line = f"{datetime.now().isoformat()}  [{RUN_ID}] {msg}"
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_PATH, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def acquire_lock() -> int | None:
    LOCK_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(LOCK_PATH), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            data = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
        except Exception:
            data = {}
        pid = data.get("pid")
        if pid_alive(pid):
            log(f"Another L5 pipeline is already running: pid={pid} run_id={data.get('run_id')}")
            write_state("failed:lock_held")
            return None
        log(f"Removing stale pipeline lock: pid={pid} run_id={data.get('run_id')}")
        try:
            LOCK_PATH.unlink()
        except FileNotFoundError:
            pass
        return acquire_lock()
    os.write(fd, json.dumps({
        "pid": os.getpid(),
        "run_id": RUN_ID,
        "started_at": datetime.now().isoformat(),
    }, indent=2).encode("utf-8"))
    return fd


def release_lock(fd: int | None) -> None:
    if fd is None:
        return
    os.close(fd)
    try:
        data = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
        if data.get("run_id") == RUN_ID:
            LOCK_PATH.unlink()
    except FileNotFoundError:
        pass
    except Exception as e:
        log(f"WARNING: could not remove pipeline lock: {e}")


# ------------------------------------------------------------------
# Server control
# ------------------------------------------------------------------
def stop_server() -> bool:
    log("Stopping server...")
    try:
        result = subprocess.run(SERVER_CMD + ["server", "stop"], capture_output=True, text=True, timeout=30)
        log(f"  rc={result.returncode} stdout={result.stdout.strip()[:200]}")
        time.sleep(2)
        return result.returncode == 0
    except Exception as e:
        log(f"  ERROR stopping server: {e}")
        return False


def start_server() -> bool:
    log("Starting server...")
    try:
        result = subprocess.run(SERVER_CMD + ["server", "start"], capture_output=True, text=True, timeout=120)
        log(f"  rc={result.returncode} stdout={result.stdout.strip()[:200]}")
    except Exception as e:
        log(f"  ERROR starting server: {e}")
        return False
    # Wait for healthy
    import urllib.request
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(HEALTH_URL, timeout=2) as r:
                if r.status == 200:
                    log("  Server healthy.")
                    return True
        except Exception:
            pass
        time.sleep(1)
    log("  WARNING: server did not become healthy within 60s.")
    return False


# ------------------------------------------------------------------
# Step runner
# ------------------------------------------------------------------
def is_step_output_fresh(name: str, max_age_min: int = 60) -> bool:
    """Check if a step's output file is fresh enough to skip re-running.

    Used for the idempotent/resume pattern: if the previous pipeline run
    died after completing step X, the next invocation can skip X and
    continue from X+1. This makes the auto-trigger reliable even when
    the parent process is killed externally.
    """
    step_files = STEP_OUTPUTS
    path = step_files.get(name)
    if not path or not path.exists():
        return False
    age_min = (time.time() - path.stat().st_mtime) / 60
    return age_min < max_age_min


def run_step(name: str, script: Path, args: list = None, skip_if_fresh: bool = False) -> bool:
    log(f"\n=== STEP: {name} ===")
    write_state(f"running:{name}")

    # Resume is disabled by default because freshness-only checks can reuse
    # stale/interleaved artifacts from another run. Re-enable only after
    # adding run-id + input-hash validation for every step artifact.
    if skip_if_fresh and is_step_output_fresh(name):
        log(f"  SKIPPED: output file is fresh (explicit resume)")
        write_state(f"step_ok:{name}")
        return True

    if not script.exists():
        log(f"  ERROR: script not found: {script}")
        write_state(f"failed:script_missing:{name}")
        return False
    args = args or []
    cmd = [str(VENV_PYTHON), str(script)] + args
    # Force UTF-8 stdout/stderr — without this, scripts that print
    # non-ASCII characters (→, Chinese, etc.) crash with UnicodeEncodeError
    # on Windows where the default console encoding is cp1252.
    env = {
        **os.environ,
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUTF8": "1",
        "PYTHONUNBUFFERED": "1",
    }
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60 * 60,
                                env=env)  # 1h max
        log(f"  rc={result.returncode}")
        if result.stdout:
            log(f"  stdout (last 1000): {result.stdout[-1000:]}")
        if result.stderr:
            log(f"  stderr (last 1000): {result.stderr[-1000:]}")
        if result.returncode != 0:
            write_state(f"failed:{name}")
            return False
        write_state(f"step_ok:{name}")
        return True
    except subprocess.TimeoutExpired:
        log(f"  ERROR: {name} timed out after 1h")
        write_state(f"failed:timeout:{name}")
        return False
    except Exception as e:
        log(f"  ERROR running {name}: {e}")
        write_state(f"failed:exception:{name}")
        return False


# ------------------------------------------------------------------
# State management (for debouncing from system2_writer)
# ------------------------------------------------------------------
# State management (for debouncing from system2_writer)
# ------------------------------------------------------------------
def read_state() -> dict:
    if not STATE_PATH.exists():
        return {"last_run_at": None, "last_status": None}
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"last_run_at": None, "last_status": None}

def write_state(status: str) -> None:
    """Write state. Always preserves last_run_at; updates last_status.

    Status values:
      - "ok": pipeline completed successfully
      - "failed:<step>": pipeline failed at <step>
      - "running:<step>": pipeline is currently at <step>
      - "step_ok:<step>": just completed <step>
    """
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    prev = read_state()
    STATE_PATH.write_text(
        json.dumps({
            "last_run_at": prev.get("last_run_at") or datetime.now().isoformat(),
            "last_status": status,
            "last_updated": datetime.now().isoformat(),
            "run_id": RUN_ID,
            "pid": os.getpid(),
        }, indent=2),
        encoding="utf-8",
    )


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------
def main():
    started_at = datetime.now()
    lock = acquire_lock()
    if lock is None:
        sys.exit(1)
    log(f"\n{'='*60}\nL5 PIPELINE STARTED (triggered by System2 digest)\n{'='*60}")
    overall_ok = False
    server_was_stopped = False

    try:
        # 1. Stop server (Kuzu lock)
        if not stop_server():
            log("Failed to stop server. Aborting pipeline.")
            write_state("failed:stop")
            sys.exit(1)
        server_was_stopped = True

        # 2-6. Run steps in strict sequence. Later destructive/export
        # steps never run after an earlier failure.
        ok = run_step("digest", SCRIPTS["digest"])
        if ok:
            ok = run_step("resolve", SCRIPTS["resolve"])
        if ok:
            ok = run_step("review", SCRIPTS["review"])
        if ok:
            ok = run_step("ingest", SCRIPTS["ingest"], args=["--rebuild", "--target", "prod", "--force"])
        if ok:
            ok = run_step("export", SCRIPTS["export"])
        if not ok:
            log("Aborting remaining steps after failure.")
        overall_ok = ok
    finally:
        # Always try to restart the server, even on failure, and make
        # cleanup failure part of the final exit status.
        if server_was_stopped:
            log("\nRestarting server (cleanup)...")
            if not start_server():
                log("CRITICAL: server failed to restart. Manual intervention needed.")
                overall_ok = False
                write_state("failed:restart")
        release_lock(lock)

    elapsed_s = (datetime.now() - started_at).total_seconds()
    log(f"\n{'='*60}\nL5 PIPELINE FINISHED: status={'OK' if overall_ok else 'FAILED'} in {elapsed_s:.1f}s\n{'='*60}")
    write_state("ok" if overall_ok else "failed")
    sys.exit(0 if overall_ok else 1)


if __name__ == "__main__":
    main()
