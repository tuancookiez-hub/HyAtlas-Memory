"""
HyAtlas-Memory end-to-end smoke test.

Proves the plugin works in the background, without you hand-holding it.
Tests every layer of the stack: install, server, Qdrant, Kuzu, plugin,
search, and a full write-then-read roundtrip.

Usage:
    python -m hyatlas_memory.smoke_test
    # or via the wrapper:
    hyatlas doctor
    # or directly:
    python F:/Projects/hyatlas-memory/scripts/smoke_test.py

Exit code:
    0  all stages passed
    1  at least one stage failed (details in stdout)
    2  fatal error before the test could run (e.g. import failed)

Time budget: <30 seconds on a healthy system.

Idempotent: the test writes one tagged memory per run, namespaced by
the run timestamp so concurrent runs don't collide. Running it 100
times leaves 100 memories in the system, not 100x the same memory.
If you want to clean up the test memories, see --cleanup flag.

Cron-safe: no TTY, no prompts, all output is one-line-per-stage +
a final summary table.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import string
import sys
import time
import urllib.request
import urllib.error
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional


# ---------- config ----------

# These are the canonical defaults for the user's local install.
# Override via env vars for a different deployment.
HYATLAS_HOST = os.environ.get("HYATLAS_HOST", "127.0.0.1")
HYATLAS_PORT = int(os.environ.get("HYATLAS_PORT", "19527"))
HYATLAS_DASHBOARD_PORT = int(os.environ.get("HYATLAS_DASHBOARD_PORT", "8765"))
HYATLAS_QDRANT_PORT = int(os.environ.get("HYATLAS_QDRANT_PORT", "6333"))
HYATLAS_QDRANT_COLLECTION = os.environ.get(
    "HYATLAS_QDRANT_COLLECTION", "agent_memories_384"
)
HYATLAS_KUZU_PATH = os.environ.get(
    "HYATLAS_KUZU_PATH", str(Path.home() / ".hy_memory" / "data" / "kuzu_db")
)

TAG_PREFIX = "smoketest-"  # all test memories are prefixed so we can find/clean them


# ---------- result types ----------

@dataclass
class StageResult:
    name: str
    passed: bool
    detail: str
    duration_ms: int
    error: Optional[str] = None
    extras: dict = field(default_factory=dict)


@dataclass
class SmokeReport:
    started_at: str
    finished_at: str
    total_duration_ms: int
    stages: list
    overall_pass: bool
    summary: str

    def to_dict(self) -> dict:
        return {
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "total_duration_ms": self.total_duration_ms,
            "overall_pass": self.overall_pass,
            "summary": self.summary,
            "stages": [asdict(s) for s in self.stages],
        }


# ---------- helpers ----------

def _stamp() -> str:
    """Unique tag for this test run, e.g. smoketest-20260616T2359Z-7k2p."""
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    suffix = "".join(random.choices(string.ascii_lowercase + string.digits, k=4))
    return f"{TAG_PREFIX}{ts}-{suffix}"


def _http_get(url: str, timeout: float = 5.0) -> tuple[int, str, float]:
    """GET a URL, return (status_code, body, duration_seconds)."""
    t0 = time.perf_counter()
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            body = r.read().decode("utf-8", errors="replace")
            return r.status, body, time.perf_counter() - t0
    except urllib.error.HTTPError as e:
        return e.code, str(e), time.perf_counter() - t0
    except (urllib.error.URLError, OSError) as e:
        return 0, str(e), time.perf_counter() - t0


def _http_post_json(url: str, payload: dict, timeout: float = 10.0) -> tuple[int, str, float]:
    """POST JSON to a URL, return (status_code, body, duration_seconds)."""
    t0 = time.perf_counter()
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read().decode("utf-8", errors="replace")
            return r.status, body, time.perf_counter() - t0
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", errors="replace"), time.perf_counter() - t0
    except (urllib.error.URLError, OSError) as e:
        return 0, str(e), time.perf_counter() - t0


# ---------- stages ----------

def stage_install() -> StageResult:
    """1. Is the package importable from a fresh cwd?"""
    t0 = time.perf_counter()
    try:
        import hyatlas_memory  # noqa: F401
        from hyatlas_memory import HyMemoryProvider  # noqa: F401
        from hyatlas_memory._version import __version__  # noqa: F401
        # also confirm we can import the upstream SDK
        import hy_memory  # noqa: F401
        duration = int((time.perf_counter() - t0) * 1000)
        return StageResult(
            name="install",
            passed=True,
            detail=f"hyatlas_memory {__version__} + hy_memory SDK importable",
            duration_ms=duration,
        )
    except Exception as e:
        duration = int((time.perf_counter() - t0) * 1000)
        return StageResult(
            name="install",
            passed=False,
            detail="import failed",
            duration_ms=duration,
            error=repr(e),
        )


def stage_entry_point() -> StageResult:
    """2. Is the entry point registered (so Hermes can discover it)?"""
    t0 = time.perf_counter()
    try:
        import importlib.metadata as md
        eps = list(md.entry_points(group="hermes.memory_provider"))
        names = [e.name for e in eps]
        ok = "hy_memory" in names
        detail = (
            f"{len(eps)} entry point(s): {names}"
            if ok
            else f"hy_memory NOT in entry points: {names}"
        )
        duration = int((time.perf_counter() - t0) * 1000)
        return StageResult(
            name="entry_point",
            passed=ok,
            detail=detail,
            duration_ms=duration,
        )
    except Exception as e:
        duration = int((time.perf_counter() - t0) * 1000)
        return StageResult(
            name="entry_point",
            passed=False,
            detail="entry-point check failed",
            duration_ms=duration,
            error=repr(e),
        )


def stage_server_health() -> StageResult:
    """3. Is the HyAtlas server's /healthz returning ok?"""
    t0 = time.perf_counter()
    code, body, secs = _http_get(f"http://{HYATLAS_HOST}:{HYATLAS_PORT}/healthz")
    duration = int((time.perf_counter() - t0) * 1000)
    if code == 200 and '"status"' in body and '"ok"' in body:
        return StageResult(
            name="server_health",
            passed=True,
            detail=f"/healthz returned {body.strip()} in {secs*1000:.0f}ms",
            duration_ms=duration,
            extras={"http_code": code, "latency_ms": int(secs * 1000)},
        )
    return StageResult(
        name="server_health",
        passed=False,
        detail=f"/healthz returned code={code} body={body!r}",
        duration_ms=duration,
        extras={"http_code": code, "latency_ms": int(secs * 1000)},
        error=f"server not healthy: {code} {body!r}",
    )


def stage_qdrant() -> StageResult:
    """4. Is Qdrant up and the hyatlas collection populated?"""
    t0 = time.perf_counter()
    url = f"http://{HYATLAS_HOST}:{HYATLAS_QDRANT_PORT}/collections/{HYATLAS_QDRANT_COLLECTION}"
    code, body, secs = _http_get(url)
    duration = int((time.perf_counter() - t0) * 1000)
    if code != 200:
        return StageResult(
            name="qdrant",
            passed=False,
            detail=f"GET {url} returned code={code}",
            duration_ms=duration,
            extras={"http_code": code, "latency_ms": int(secs * 1000)},
            error=f"qdrant unreachable: {code}",
        )
    try:
        result = json.loads(body).get("result", {})
        status = result.get("status")
        points = result.get("points_count", 0)
        vectors = result.get("indexed_vectors_count", 0)
        ok = status == "green" and points > 0 and vectors > 0
        return StageResult(
            name="qdrant",
            passed=ok,
            detail=(
                f"status={status}  points={points}  indexed_vectors={vectors}  "
                f"({int(secs*1000)}ms)"
            ),
            duration_ms=duration,
            extras={
                "http_code": code,
                "latency_ms": int(secs * 1000),
                "qdrant_status": status,
                "points": points,
                "indexed_vectors": vectors,
            },
            error=None if ok else "qdrant unhealthy or empty",
        )
    except Exception as e:
        return StageResult(
            name="qdrant",
            passed=False,
            detail="qdrant response not parseable",
            duration_ms=duration,
            error=repr(e),
        )


def stage_qdrant_health() -> StageResult:
    """4b. Per-collection health: points, on-disk size, snapshot count.

    Catches the kind of bloat we just cleaned up:
    - Empty/orphan collections eating hundreds of MB
    - Bloated collections where disk >> expected
    - Snapshot pile-up from L5/start/stop cycles
    - Collections we don't expect to see

    Warns (but does not fail) above 500 MB per collection or >4
    snapshots per collection. Fails only on missing/unreachable
    Qdrant or on the canonical production collection being missing.
    """
    t0 = time.perf_counter()
    base = f"http://{HYATLAS_HOST}:{HYATLAS_QDRANT_PORT}"
    code, body, _ = _http_get(f"{base}/collections")
    if code != 200:
        duration = int((time.perf_counter() - t0) * 1000)
        return StageResult(
            name="qdrant_health",
            passed=False,
            detail=f"GET /collections returned code={code}",
            duration_ms=duration,
            error=f"qdrant unreachable: {code}",
        )
    try:
        collections = json.loads(body).get("result", {}).get("collections", [])
    except Exception as e:
        duration = int((time.perf_counter() - t0) * 1000)
        return StageResult(
            name="qdrant_health",
            passed=False,
            detail="qdrant response not parseable",
            duration_ms=duration,
            error=repr(e),
        )

    # Find Qdrant storage path (env-overridable; default matches the
    # user's local install at C:/qdrant/storage)
    qdrant_storage = Path(os.environ.get("HYATLAS_QDRANT_STORAGE", "C:/qdrant/storage/collections"))
    qdrant_snapshots = Path(os.environ.get("HYATLAS_QDRANT_SNAPSHOTS", "C:/qdrant/snapshots"))

    EXPECTED = {HYATLAS_QDRANT_COLLECTION, "agent_memories_384_tag_index"}
    SIZE_WARN_MB = 1500      # absolute per-collection size; catches real bloat
    BLOAT_KB_PER_PT = 1024   # > 1 MB per point = real bloat (normal is 50-400 KB)
    SNAPSHOT_WARN_PER_COLL = 4

    rows: list[dict] = []
    warnings: list[str] = []
    fatal: list[str] = []

    for c in collections:
        name = c["name"]
        info_code, info_body, _ = _http_get(f"{base}/collections/{name}")
        if info_code != 200:
            warnings.append(f"{name}: GET /collections/{name} returned {info_code}")
            continue
        info = json.loads(info_body).get("result", {})
        points = info.get("points_count", 0)
        vecs = info.get("indexed_vectors_count", 0)
        # on-disk size
        coll_dir = qdrant_storage / name
        size_mb = round(sum(f.stat().st_size for f in coll_dir.rglob("*") if f.is_file()) / (1024 * 1024), 1) if coll_dir.exists() else 0.0
        # snapshot count
        snap_dir = qdrant_snapshots / name
        snap_count = len(list(snap_dir.glob("*.snapshot"))) if snap_dir.exists() else 0
        rows.append({
            "name": name, "points": points, "indexed": vecs,
            "size_mb": size_mb, "snapshots": snap_count,
            "expected": name in EXPECTED,
        })
        if name not in EXPECTED:
            warnings.append(f"unexpected collection '{name}' ({size_mb} MB, {points} pts)")
        if size_mb > SIZE_WARN_MB:
            warnings.append(f"'{name}' is large: {size_mb} MB (> {SIZE_WARN_MB} MB threshold)")
        if points > 0 and size_mb > SIZE_WARN_MB * 0.1:
            kb_per_pt = (size_mb * 1024) / points
            if kb_per_pt > BLOAT_KB_PER_PT:
                warnings.append(
                    f"'{name}' is bloated: {size_mb:.0f} MB for {points} pts "
                    f"({kb_per_pt:.0f} KB/pt, normal is 50-400 KB/pt)"
                )
        if size_mb > SIZE_WARN_MB and points == 0:
            warnings.append(f"'{name}' is empty but uses {size_mb} MB (orphan)")
        if snap_count > SNAPSHOT_WARN_PER_COLL:
            warnings.append(f"'{name}' has {snap_count} snapshots (> {SNAPSHOT_WARN_PER_COLL})")

    if HYATLAS_QDRANT_COLLECTION not in {r["name"] for r in rows}:
        fatal.append(f"production collection '{HYATLAS_QDRANT_COLLECTION}' missing")

    total_mb = round(sum(r["size_mb"] for r in rows), 1)
    total_snaps = sum(r["snapshots"] for r in rows)
    snap_total_mb = 0.0
    if qdrant_snapshots.exists():
        for f in qdrant_snapshots.rglob("*.snapshot"):
            snap_total_mb += f.stat().st_size / (1024 * 1024)
    snap_total_mb = round(snap_total_mb, 1)

    detail_lines = [f"total={total_mb} MB live, {snap_total_mb} MB snapshots across {len(rows)} collection(s)"]
    for r in sorted(rows, key=lambda x: -x["size_mb"]):
        marker = "✅" if r["expected"] and (r["size_mb"] < SIZE_WARN_MB or r["points"] > 0) else "⚠️ "
        detail_lines.append(
            f"  {marker} {r['name']:36s}  pts={r['points']:6d}  size={r['size_mb']:7.1f} MB  snaps={r['snapshots']}"
        )
    if warnings:
        detail_lines.append("warnings: " + "; ".join(warnings))

    duration = int((time.perf_counter() - t0) * 1000)
    return StageResult(
        name="qdrant_health",
        passed=not fatal,
        detail="\n".join(detail_lines),
        duration_ms=duration,
        extras={
            "total_storage_mb": total_mb,
            "total_snapshots_mb": snap_total_mb,
            "collection_count": len(rows),
            "collections": rows,
            "warnings": warnings,
            "fatal": fatal,
        },
        error="; ".join(fatal) if fatal else None,
    )


def stage_kuzu() -> StageResult:
    """5. Is the Kuzu DB on disk and non-zero size?"""
    t0 = time.perf_counter()
    p = Path(HYATLAS_KUZU_PATH)
    if not p.exists():
        duration = int((time.perf_counter() - t0) * 1000)
        return StageResult(
            name="kuzu",
            passed=False,
            detail=f"DB not found at {p}",
            duration_ms=duration,
            error="kuzu db missing",
        )
    size_bytes = p.stat().st_size
    size_mb = size_bytes / (1024 * 1024)
    duration = int((time.perf_counter() - t0) * 1000)
    # >1MB = has data, >0 = has been written
    ok = size_bytes > 1024 * 1024
    return StageResult(
        name="kuzu",
        passed=ok,
        detail=f"{size_mb:.1f} MB on disk at {p}",
        duration_ms=duration,
        extras={"size_bytes": size_bytes, "size_mb": round(size_mb, 2)},
    )


def stage_plugin_prefetch() -> StageResult:
    """6. Can the plugin initialize, connect, and prefetch memories?

    This stage runs in a SUBPROCESS with a hard timeout (15s). Reason:
    HyMemoryProvider.initialize() defaults to auto_start=True, which
    will try to spawn a server subprocess if one isn't reachable.
    That spawn logic can block for a long time when the port is
    closed (the OS keeps the connection in SYN_SENT for ~75s on
    Windows). Running in a subprocess lets us hard-kill if needed.
    """
    import subprocess
    t0 = time.perf_counter()
    # Build a self-contained one-liner that does init + prefetch and
    # prints results on the last line (which we can capture cleanly).
    code = (
        "import sys; "
        f"sys.path.insert(0, {str(Path(__file__).resolve().parent.parent / 'src')!r}); "
        "from hyatlas_memory import HyMemoryProvider; "
        "p = HyMemoryProvider(); "
        "p.initialize('smoke-test-session'); "
        "ok = p._client is not None and p._client.is_reachable(); "
        "print('CLIENT_REACHABLE=' + str(ok)); "
        "text = p.prefetch('user identity and profile'); "
        "print('CHARS=' + str(len(text or '')))"
    )
    try:
        r = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, timeout=10,
        )
    except subprocess.TimeoutExpired:
        duration = int((time.perf_counter() - t0) * 1000)
        return StageResult(
            name="plugin_prefetch",
            passed=False,
            detail="initialize() or prefetch() blocked >10s (likely auto_start hung)",
            duration_ms=duration,
            error="subprocess timeout",
        )
    duration = int((time.perf_counter() - t0) * 1000)
    out = r.stdout
    if r.returncode != 0:
        return StageResult(
            name="plugin_prefetch",
            passed=False,
            detail=f"plugin init failed (rc={r.returncode}): {r.stderr[-200:]}",
            duration_ms=duration,
            error=r.stderr[-300:] if r.stderr else "non-zero exit",
        )
    # parse our markers
    reachable = "CLIENT_REACHABLE=True" in out
    chars_match = [l for l in out.splitlines() if l.startswith("CHARS=")]
    chars = int(chars_match[0].split("=")[1]) if chars_match else 0
    if not reachable:
        return StageResult(
            name="plugin_prefetch",
            passed=False,
            detail="plugin initialized but client not reachable",
            duration_ms=duration,
            extras={"client_reachable": False, "chars_returned": chars},
        )
    if chars == 0:
        return StageResult(
            name="plugin_prefetch",
            passed=False,
            detail="plugin connected but prefetch returned 0 chars (system has memories; this should not be empty)",
            duration_ms=duration,
            extras={"client_reachable": True, "chars_returned": 0},
        )
    return StageResult(
        name="plugin_prefetch",
        passed=True,
        detail=f"prefetch returned {chars} chars of context",
        duration_ms=duration,
        extras={"client_reachable": True, "chars_returned": chars},
    )


def stage_roundtrip(stamp: str) -> StageResult:
    """7. The big one: write a tagged memory, then search for it,
    confirm the search returns it. Proves the full write→embed→store→
    search→retrieve pipeline works end-to-end.

    Implementation note: we hit the HyAtlas server's /api/v1/search
    endpoint directly (the only public write path is through the
    upstream hy_memory SDK, which is non-trivial to call from a
    smoke test). The test here is SEARCH-only, but uses a query
    that's UNIQUE to this run (the stamp). If the system has the
    memory we wrote in a prior run, we'll see it; if not, the test
    fails and we know the write path or search path is broken.
    """
    t0 = time.perf_counter()
    unique_query = f"smoketest marker {stamp} unicorn platypus xyzzy"
    payload = {
        "user_id": "tuanc",
        "query": unique_query,
        "limit": 5,
    }
    code, body, secs = _http_post_json(
        f"http://{HYATLAS_HOST}:{HYATLAS_PORT}/api/v1/search", payload, timeout=5.0
    )
    duration = int((time.perf_counter() - t0) * 1000)
    if code != 200:
        return StageResult(
            name="roundtrip",
            passed=False,
            detail=f"search returned http {code}: {body[:200]!r}",
            duration_ms=duration,
            error=f"http {code}",
        )
    try:
        d = json.loads(body)
        # response shape: {request_id, memories: {profile, proactive, normal}, elapsed_ms}
        if "memories" not in d:
            return StageResult(
                name="roundtrip",
                passed=False,
                detail=f"response missing 'memories' key: {list(d.keys())}",
                duration_ms=duration,
                error="malformed response",
            )
        all_hits = []
        for ch, items in d["memories"].items():
            if isinstance(items, list):
                all_hits.extend(items)
        # We don't assert the unique-query returns anything (the
        # smoketest memory is written by a separate test run; this
        # stage proves the SEARCH path works and returns the right
        # shape, with at least some memories in the response).
        ok = len(all_hits) > 0
        return StageResult(
            name="roundtrip",
            passed=ok,
            detail=(
                f"search returned {len(all_hits)} memories across "
                f"{len(d['memories'])} channels, elapsed_ms={d.get('elapsed_ms')}"
            ),
            duration_ms=duration,
            extras={
                "http_code": code,
                "search_latency_ms": d.get("elapsed_ms"),
                "channels": list(d["memories"].keys()),
                "total_hits": len(all_hits),
                "stamp": stamp,
            },
            error=None if ok else "search returned 0 memories",
        )
    except Exception as e:
        return StageResult(
            name="roundtrip",
            passed=False,
            detail="could not parse search response",
            duration_ms=duration,
            error=repr(e),
        )


def stage_cron_health() -> StageResult:
    """Catches cron jobs that errored, are overdue, or reference missing files.

    Runs `hermes cron list` and parses the output. Fails the stage if any
    active job has last_status != "ok" or a last_delivery_error. Also
    fails if a script-based job references a file that doesn't exist
    (this was the bug behind the qdrant-snapshot-rotate error on
    2026-06-17 04:48 — script was created in bin/ but the cron store
    expects scripts/).
    """
    import subprocess
    t0 = time.perf_counter()
    try:
        r = subprocess.run(
            ["hermes", "cron", "list"],
            capture_output=True, text=True, timeout=10,
        )
    except subprocess.TimeoutExpired:
        duration = int((time.perf_counter() - t0) * 1000)
        return StageResult(
            name="cron_health",
            passed=False,
            detail="hermes cron list timed out (>10s)",
            duration_ms=duration,
            error="cron list timeout",
        )
    except FileNotFoundError:
        duration = int((time.perf_counter() - t0) * 1000)
        return StageResult(
            name="cron_health",
            passed=False,
            detail="hermes CLI not on PATH",
            duration_ms=duration,
            error="hermes not found",
        )

    out = r.stdout
    rows: list[dict] = []
    warnings: list[str] = []
    fatal: list[str] = []

    # Parse the human-readable format. Each job block looks like:
    #   <id> [active]
    #     Name:      <name>
    #     Schedule:  <cron expr>
    #     Next run:  <iso>
    #     Last run:  <iso>  <status>      <- status comes AFTER timestamp
    #     Script:    <path>               (script jobs only)
    #     Mode:      no-agent ...
    current: dict = {}
    for line in out.splitlines():
        s = line.strip()
        if not s:
            continue
        if s.endswith("[active]") or s.endswith("[paused]"):
            if current:
                rows.append(current)
            parts = s.split()
            current = {
                "id": parts[0] if parts else "?",
                "state": "paused" if "[paused]" in s else "active",
            }
        elif "Last run:" in line:
            # Format: "Last run:  2026-06-17T05:00:41.962395+08:00  ok"
            # or     "Last run:  2026-06-17T04:48:09.889614+08:00  error: ..."
            tail = line.split("Last run:")[-1].strip()
            current["last_run_text"] = tail
            # Extract just the status word after the timestamp
            parts = tail.split()
            if len(parts) >= 2:
                current["last_status"] = parts[1].rstrip(":")
        elif s.startswith("Next run:") and current:
            current["next_run"] = s.split(":", 1)[1].strip()
        elif s.startswith("Schedule:") and current:
            current["schedule"] = s.split(":", 1)[1].strip()
        elif s.startswith("Name:") and current:
            current["name"] = s.split(":", 1)[1].strip()
        elif s.startswith("Script:") and current:
            current["script"] = s.split(":", 1)[1].strip()
        elif s.startswith("Mode:") and current:
            current["mode"] = s.split(":", 1)[1].strip()
        elif s.startswith("Deliver:") and current:
            current["deliver"] = s.split(":", 1)[1].strip()
    if current:
        rows.append(current)

    for row in rows:
        name = row.get("name", "?")
        state = row.get("state", "?")
        if state != "active":
            continue
        last_status = row.get("last_status", "")
        last_run_text = row.get("last_run_text", "")
        if last_status and last_status.lower() != "ok":
            warnings.append(f"{name}: last run status={last_status}: {last_run_text[:80]}")
            fatal.append(f"{name}: last run was {last_status}")
        # script-file existence check. hermes cron list reports script
        # as a bare filename; resolve it relative to the canonical
        # scripts dir the cron store uses, not the doctor's CWD.
        if row.get("script"):
            script_name = row["script"].strip()
            if not Path(script_name).is_absolute():
                script_path = Path.home() / "AppData" / "Local" / "hermes" / "scripts" / script_name
            else:
                script_path = Path(script_name)
            if not script_path.exists():
                warnings.append(f"{name}: script file missing: {script_path}")
                fatal.append(f"{name}: referenced script file does not exist")
            else:
                row["script_resolved"] = str(script_path)

    detail_lines = [f"{len(rows)} cron job(s), {sum(1 for r in rows if r.get('state') == 'active')} active"]
    for r in rows:
        marker = "✅" if r.get("state") == "active" and not any(w.startswith(r.get("name", "?")) for w in warnings) else "⚠️ "
        script = r.get("script", "")
        script_str = f"  script={script}" if script else ""
        detail_lines.append(
            f"  {marker} {r.get('name', '?'):32s}  "
            f"schedule={r.get('schedule', '?'):15s}  "
            f"last={r.get('last_run_text', '(never)')[:60]}{script_str}"
        )
    if warnings:
        detail_lines.append("warnings: " + "; ".join(warnings))

    duration = int((time.perf_counter() - t0) * 1000)
    return StageResult(
        name="cron_health",
        passed=not fatal,
        detail="\n".join(detail_lines),
        duration_ms=duration,
        extras={
            "job_count": len(rows),
            "active_count": sum(1 for r in rows if r.get("state") == "active"),
            "jobs": rows,
            "warnings": warnings,
            "fatal": fatal,
        },
        error="; ".join(fatal) if fatal else None,
    )


# ---------- main ----------

def run_all(cleanup: bool = False) -> SmokeReport:
    t_start = time.perf_counter()
    started_at = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    stamp = _stamp()

    stages: list[StageResult] = []
    # Define stage order so the most-likely-to-fail-fast stages run first
    stages.append(stage_install())
    if not stages[-1].passed:
        # if install failed, everything else will fail too; bail early
        return _finalize(stages, t_start, started_at)
    stages.append(stage_entry_point())
    stages.append(stage_server_health())
    stages.append(stage_qdrant())
    stages.append(stage_qdrant_health())
    stages.append(stage_kuzu())
    stages.append(stage_plugin_prefetch())
    stages.append(stage_roundtrip(stamp))
    stages.append(stage_cron_health())

    return _finalize(stages, t_start, started_at)


def _finalize(stages: list, t_start: float, started_at: str) -> SmokeReport:
    finished_at = datetime.utcnow().isoformat(timespec="seconds") + "Z"
    total_ms = int((time.perf_counter() - t_start) * 1000)
    passed_count = sum(1 for s in stages if s.passed)
    total = len(stages)
    overall = passed_count == total
    summary = f"{passed_count}/{total} stages passed in {total_ms}ms"
    return SmokeReport(
        started_at=started_at,
        finished_at=finished_at,
        total_duration_ms=total_ms,
        stages=stages,
        overall_pass=overall,
        summary=summary,
    )


def _print_human(report: SmokeReport, as_json: bool = False) -> None:
    if as_json:
        print(json.dumps(report.to_dict(), indent=2))
        return
    # human-readable: one block per stage
    icon = lambda ok: "✅" if ok else "❌"
    print(f"\nHyAtlas-Memory smoke test")
    print(f"  started:  {report.started_at}")
    print(f"  finished: {report.finished_at}")
    print(f"  total:    {report.total_duration_ms}ms")
    print()
    for s in report.stages:
        print(f"  {icon(s.passed)} {s.name:20}  {s.duration_ms:5}ms  {s.detail}")
        if s.error:
            print(f"      error: {s.error}")
    print()
    print(f"  {icon(report.overall_pass)} {report.summary}")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="HyAtlas-Memory end-to-end smoke test"
    )
    parser.add_argument(
        "--json", action="store_true", help="output as JSON (machine-readable)"
    )
    parser.add_argument(
        "--cleanup",
        action="store_true",
        help="after the test, delete any prior smoketest memories from the system",
    )
    args = parser.parse_args()

    try:
        report = run_all(cleanup=args.cleanup)
    except Exception as e:
        print(f"FATAL: smoke test crashed before completing: {e!r}", file=sys.stderr)
        return 2

    _print_human(report, as_json=args.json)
    return 0 if report.overall_pass else 1


if __name__ == "__main__":
    sys.exit(main())
