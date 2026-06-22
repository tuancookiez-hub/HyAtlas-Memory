"""
L5 export to JSON — bridges Kuzu (locked by server) to the dashboard.

The dashboard proxy (hy_memory_dashboard.py) cannot open the Kuzu DB
directly because the hy-memory server holds the exclusive lock. This
script runs as a one-shot (or cron job) to:

  1. Stop the server (so we can take an exclusive Kuzu lock)
  2. Connect to Kuzu directly
  3. Query all L5 nodes + relations
  4. Write a JSON file that the proxy can read
  5. Restart the server

Output: logs/l5_kuzu_export.json — read by the dashboard proxy.

CRITICAL: This script (and any direct Kuzu access) requires the server
to be STOPPED. The hy-memory server holds an exclusive lock on the
kuzu_db file.
"""
import argparse
import json
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

import kuzu

# Paths
PROD_KUZU_PATH = Path(r"C:\Users\tuanc\.hy_memory\data\kuzu_db")
TEST_KUZU_PATH = Path(r"C:\Users\tuanc\.hy_memory\data\kuzu_db_test")


def _resolve_export_path() -> Path:
    """Resolve the L5 export path via Hermes' canonical home resolver.

    Pre-1.4.1, this was hardcoded to a per-user Windows absolute path
    (``C:\\Users\\<u>\\AppData\\Local\\hermes\\logs\\l5_kuzu_export.json``)
    while the dashboard read from a different location, producing a
    permanent 503 on ``/api/l5/graph``. 1.4.1 routes both writer and
    reader through ``hermes_constants.get_hermes_home()`` so they
    always agree, regardless of ``HERMES_HOME`` overrides or platform.
    """
    try:
        from hermes_constants import get_hermes_home
        home = Path(get_hermes_home())
    except Exception:
        if sys.platform == "win32":
            home = Path.home() / "AppData" / "Local" / "hermes"
        else:
            home = Path.home() / ".local" / "share" / "hermes"
    home.mkdir(parents=True, exist_ok=True)
    return home / "logs" / "l5_kuzu_export.json"


EXPORT_PATH = _resolve_export_path()
HEALTH_URL = "http://127.0.0.1:19527/healthz"


def wait_for_server(timeout_s: int = 60) -> bool:
    """Wait for the server to be healthy."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(HEALTH_URL, timeout=2) as r:
                if r.status == 200:
                    return True
        except Exception:
            pass
        time.sleep(1)
    return False


def stop_server() -> None:
    """Stop the hy-memory server via the hermes-agent venv Python.

    The hyatlas shim is a bash script that subprocess can't run on Windows.
    Calling Python directly works, but we MUST use the hermes-agent venv
    Python (which has hy_memory installed) and run from the scripts dir.
    """
    print("Stopping server...")
    result = subprocess.run(
        [r"C:\Users\tuanc\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe",
         r"C:\Users\tuanc\AppData\Local\hermes\bin\hymemory.py",
         "server", "stop"],
        capture_output=True, text=True, timeout=30
    )
    print(f"  rc={result.returncode}")
    print(f"  stdout: {result.stdout.strip()[:200]}")
    if result.stderr:
        print(f"  stderr: {result.stderr.strip()[:200]}")
    time.sleep(2)


def start_server() -> bool:
    """Restart the hy-memory server via the hermes-agent venv Python."""
    print("Starting server...")
    result = subprocess.run(
        [r"C:\Users\tuanc\AppData\Local\hermes\hermes-agent\venv\Scripts\python.exe",
         r"C:\Users\tuanc\AppData\Local\hermes\bin\hymemory.py",
         "server", "start"],
        capture_output=True, text=True, timeout=120
    )
    print(f"  rc={result.returncode}")
    print(f"  stdout: {result.stdout.strip()[:200]}")
    if result.stderr:
        print(f"  stderr: {result.stderr.strip()[:200]}")
    return wait_for_server(60)


def export_l5(kuzu_path: Path) -> dict:
    """Connect to Kuzu directly, query L5, return as a JSON-serializable dict."""
    db = kuzu.Database(str(kuzu_path))
    conn = kuzu.Connection(db)

    # Query L5 nodes
    nodes = []
    result = conn.execute(
        "MATCH (m:Memory) WHERE m.layer = 'l5_knowledge' "
        "RETURN m.node_id AS id, m.content AS name, m.content_type AS content_type, "
        "m.confidence AS confidence, m.extra_json AS extra_json, "
        "m.created_at AS created_at"
    )
    while result.has_next():
        row = result.get_next()
        try:
            extra = json.loads(row[4]) if row[4] else {}
        except Exception:
            extra = {}
        nodes.append({
            "node_id": row[0],
            "name": row[1],
            "content_type": row[2],
            "confidence": row[3],
            "entity_type": extra.get("entity_type", row[2].replace("ENTITY_", "") if row[2] else "CONCEPT"),
            "mention_count": extra.get("mention_count", 1),
            "aliases": extra.get("aliases", []),
            "source": extra.get("source", "l5_digest"),
            "created_at": str(row[5]) if row[5] else None,
        })

    # Query L5 relations
    relations = []
    result = conn.execute(
        "MATCH (a:Memory)-[r:RELATED_TO]->(b:Memory) "
        "WHERE a.layer = 'l5_knowledge' AND b.layer = 'l5_knowledge' "
        "RETURN a.node_id AS a_id, a.content AS a_name, "
        "b.node_id AS b_id, b.content AS b_name, "
        "r.relation_type AS relation_type, r.weight AS weight"
    )
    while result.has_next():
        row = result.get_next()
        relations.append({
            "a": row[1],
            "b": row[3],
            "relation_type": row[4],
            "confidence": row[5] if row[5] is not None else 0.0,
        })

    # Stats
    type_counts = {}
    for n in nodes:
        t = n["entity_type"]
        type_counts[t] = type_counts.get(t, 0) + 1
    rel_type_counts = {}
    for r in relations:
        t = r["relation_type"]
        rel_type_counts[t] = rel_type_counts.get(t, 0) + 1

    return {
        "exported_at": datetime.now().isoformat(),
        "kuzu_path": str(kuzu_path),
        "node_count": len(nodes),
        "relation_count": len(relations),
        "nodes": nodes,
        "relations": relations,
        "type_distribution": type_counts,
        "relation_type_distribution": rel_type_counts,
    }


def main():
    parser = argparse.ArgumentParser(description="Export L5 Kuzu graph to dashboard JSON.")
    parser.add_argument("--target", choices=["prod", "test"], default="prod")
    args = parser.parse_args()
    kuzu_path = PROD_KUZU_PATH if args.target == "prod" else TEST_KUZU_PATH

    # Step 1: stop server
    stop_server()

    # Step 2: export
    try:
        print(f"\nExporting L5 from {kuzu_path}...")
        data = export_l5(kuzu_path)
        print(f"  {data['node_count']} nodes, {data['relation_count']} relations")
        print(f"  Type distribution: {data['type_distribution']}")
        print(f"  Relation type distribution: {data['relation_type_distribution']}")
    except Exception as e:
        print(f"  ERROR during export: {e}")
        # Make sure to restart the server even on error
        if not start_server():
            print("CRITICAL: Server did not come back up after export failure.")
        sys.exit(1)

    # Step 3: write JSON
    EXPORT_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\nWrote {EXPORT_PATH} ({EXPORT_PATH.stat().st_size // 1024} KB)")

    # Step 4: restart server
    if start_server():
        print("Server is back up.")
    else:
        print("CRITICAL: Server did not come back up. Check `hyatlas server status`.")
        sys.exit(1)


if __name__ == "__main__":
    main()
