"""
L5 Phase 2.4b — Kuzu graph ingestion.

Reads the recommended subset from l5_quality_review.json and writes to
the Kuzu graph database used by hy_memory. Uses the existing `Memory` node
table (with layer='l5_knowledge') and `RELATED_TO` edge type.

CRITICAL: The hy-memory server must be stopped before this script runs
because Kuzu is a single-process database and the server holds an exclusive
lock on the kuzu_db file.

Schema used (from upstream hy_memory/data/graph_store_kuzu.py):
  - Memory(node_id STRING PRIMARY KEY, isolation_key STRING, user_id STRING,
           agent_id STRING, layer STRING, content STRING, content_type STRING,
           status STRING, version INT64, confidence DOUBLE, source_type STRING,
           meta_tags STRING, source_session_id STRING, embedding FLOAT[{dims}])
  - RELATED_TO(FROM Memory TO Memory, relation_type STRING, weight DOUBLE,
               created_at TIMESTAMP)

Idempotent: re-running without --rebuild doesn't create duplicates (uses MERGE).

With --rebuild: wipes all layer='l5_knowledge' nodes (and their RELATED_TO
edges) first, then re-ingests. Use this when you want Kuzu to exactly
match the latest quality review (no leftover entities from prior runs).

Output:
  - logs/l5_ingest_stats.json: counts of nodes/edges written, any errors,
    and (if --rebuild) how many L5 nodes were wiped before the ingest
  - logs/l5_ingest_sample_query.txt: 5 sample Cypher queries + their results

Usage:
  python l5_ingest_kuzu.py            # additive (default, MERGE only)
  python l5_ingest_kuzu.py --rebuild  # wipe + re-ingest
"""
import argparse
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import kuzu

# ------------------------------------------------------------------
# Config
# ------------------------------------------------------------------
PROD_KUZU_PATH = Path(r"C:\Users\tuanc\.hy_memory\data\kuzu_db")
TEST_KUZU_PATH = Path(r"C:\Users\tuanc\.hy_memory\data\kuzu_db_test")
REVIEW_PATH = Path(r"C:\Users\tuanc\AppData\Local\hermes\logs\l5_quality_review.json")
STATS_PATH = Path(r"C:\Users\tuanc\AppData\Local\hermes\logs\l5_ingest_stats.json")
SAMPLE_PATH = Path(r"C:\Users\tuanc\AppData\Local\hermes\logs\l5_ingest_sample_query.txt")

# Isolation key: use the same format as the SDK (user_id::agent_id::session_id)
# Hermes TUI defaults: user_id=hermes-user, agent_id=default, session_id=default_session
USER_ID = "hermes-user"
AGENT_ID = "default"
SESSION_ID = "default_session"
ISOLATION_KEY = f"{USER_ID}::{AGENT_ID}::{SESSION_ID}"

# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------
def slugify(name: str) -> str:
    """Make a valid Kuzu node_id from an entity name."""
    s = name.lower().strip()
    s = re.sub(r'[^a-z0-9_]+', '_', s)
    s = re.sub(r'_+', '_', s).strip('_')
    return f"l5_{s}"


def diagnose_lock() -> str:
    try:
        import subprocess
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command",
             "Get-CimInstance Win32_Process | "
             "Where-Object { $_.CommandLine -match 'hy_memory|hymemory|l5_|kuzu' } | "
             "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"],
            capture_output=True, text=True, timeout=10,
        )
        return result.stdout.strip()[:2000] or result.stderr.strip()[:2000]
    except Exception as e:
        return f"diagnostic failed: {e}"


def connect_kuzu(path: Path, attempts: int = 12, delay: float = 5.0):
    last = None
    for i in range(1, attempts + 1):
        try:
            return kuzu.Database(str(path)), i
        except Exception as e:
            last = e
            msg = str(e).lower()
            if "lock" not in msg and "busy" not in msg and "another process" not in msg:
                raise
            print(f"  Kuzu locked/busy on attempt {i}/{attempts}: {e}")
            if i < attempts:
                time.sleep(delay)
    print("  Kuzu lock diagnostics:")
    print(diagnose_lock())
    raise RuntimeError(f"Kuzu remained locked after {attempts} attempts: {last}")


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------
def wipe_l5(conn) -> int:
    """Delete all L5 knowledge graph nodes (and their RELATED_TO edges).

    Returns the number of L5 nodes that were in the database before wipe.
    Called only with --rebuild to ensure Kuzu exactly matches the latest
    quality review (without leftover entities from prior MERGE runs).
    """
    # Count first so we can report what was wiped
    pre_count = 0
    result = conn.execute("MATCH (m:Memory {layer: 'l5_knowledge'}) RETURN COUNT(m) AS n")
    while result.has_next():
        pre_count = int(result.get_next()[0])

    # DETACH DELETE removes the nodes AND all their relationships
    # (RELATED_TO edges between L5 nodes and any other nodes they touch).
    # We use a wider MATCH (no layer filter on b) so a stray L5 → non-L5
    # edge is also cleaned up.
    print(f"  Wiping {pre_count} L5 nodes (DETACH DELETE)...")
    conn.execute("""
        MATCH (m:Memory {layer: 'l5_knowledge'})
        DETACH DELETE m
    """)
    return pre_count


def main():
    parser = argparse.ArgumentParser(
        description="Ingest L5 entities + relations into the Kuzu graph database.",
    )
    parser.add_argument(
        "--rebuild", action="store_true",
        help="WIPE all existing L5 nodes first, then ingest. Without this, the "
             "script is additive (MERGE only — old nodes from prior runs stay).",
    )
    parser.add_argument(
        "--target", choices=["prod", "test"], default="prod",
        help="Kuzu database target. Use test for subset validation; prod is the live graph.",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Required with --rebuild --target prod to prevent accidental production wipes.",
    )
    args = parser.parse_args()
    kuzu_path = PROD_KUZU_PATH if args.target == "prod" else TEST_KUZU_PATH

    if args.rebuild and args.target == "prod" and not args.force:
        print("ERROR: refusing production --rebuild without --force")
        print("  Use --target test for subset validation, or pass --force intentionally.")
        sys.exit(2)

    if not kuzu_path.exists():
        print(f"ERROR: Kuzu DB not found at {kuzu_path}")
        print("  The hy-memory server may still be running or the target has not been initialized.")
        sys.exit(1)

    review = json.loads(REVIEW_PATH.read_text(encoding="utf-8"))
    entities = review["entities"]
    relations = review["relations"]
    print(f"Input: {len(entities)} entities, {len(relations)} relations to ingest")
    if args.rebuild:
        print("  --rebuild: will wipe all existing L5 nodes before ingesting")

    # Connect to Kuzu
    print(f"\nConnecting to Kuzu at {kuzu_path}...")
    db, attempt = connect_kuzu(kuzu_path)
    conn = kuzu.Connection(db)
    print(f"  Connected on attempt {attempt}")

    # Verify the schema has the tables we need
    schema_tables = set()
    try:
        result = conn.execute("CALL show_tables() RETURN *")
        while result.has_next():
            row = result.get_next()
            schema_tables.add(row[1]["name"] if isinstance(row[1], dict) else row[1])
    except Exception as e:
        # Some Kuzu versions don't have show_tables; just try to use the table
        pass
    print(f"  Schema tables (from show_tables): {schema_tables}")

    # Optional: wipe L5 first if --rebuild
    if args.rebuild:
        print(f"\n=== --rebuild: wiping existing L5 ===")
        wiped = wipe_l5(conn)
        print(f"  Wiped {wiped} L5 nodes. Kuzu now contains 0 L5 nodes.")

    # ------------------------------------------------------------------
    # Write entities (using only the existing schema columns)
    # ------------------------------------------------------------------
    print(f"\n=== Ingesting {len(entities)} entities ===")
    n_written_e = 0
    n_skipped_e = 0
    n_errors_e = 0
    started = time.time()
    for e in entities:
        node_id = slugify(e["name"])
        # Encode type and metadata in extra_json (existing column) since
        # the Memory table doesn't have entity_type or mention_count columns.
        # We use content_type to mark it as a tool/project/etc.
        cypher = """
        MERGE (m:Memory {node_id: $node_id})
        ON CREATE SET
            m.isolation_key = $isolation_key,
            m.user_id = $user_id,
            m.agent_id = $agent_id,
            m.layer = $layer,
            m.content = $content,
            m.content_type = $content_type,
            m.status = $status,
            m.version = $version,
            m.confidence = $confidence,
            m.source_type = $source_type,
            m.created_at = $created_at,
            m.extra_json = $extra_json
        ON MATCH SET
            m.content = $content,
            m.content_type = $content_type,
            m.confidence = $confidence,
            m.extra_json = $extra_json
        RETURN m.node_id
        """
        # Use content_type as "ENTITY_<TYPE>" for easy filtering
        content_type = f"ENTITY_{e['type']}"
        extra_json = json.dumps({
            "entity_type": e["type"],
            "mention_count": e["mention_count"],
            "aliases": e.get("aliases", []),
            "source": "l5_digest_2026-06-13",
        })
        try:
            result = conn.execute(cypher, parameters={
                "node_id": node_id,
                "isolation_key": ISOLATION_KEY,
                "user_id": USER_ID,
                "agent_id": AGENT_ID,
                "layer": "l5_knowledge",
                "content": e["name"],
                "content_type": content_type,
                "status": "active",
                "version": 1,
                "confidence": e["confidence"],
                "source_type": "l5_digest",
                "created_at": datetime.now(),
                "extra_json": extra_json,
            })
            while result.has_next():
                result.get_next()
            n_written_e += 1
        except Exception as ex:
            n_errors_e += 1
            if n_errors_e <= 3:
                print(f"  ERROR on {e['name']!r}: {ex}")
    print(f"  Entities: {n_written_e} written, {n_errors_e} errors, {time.time() - started:.1f}s")

    # ------------------------------------------------------------------
    # Write relations
    # ------------------------------------------------------------------
    # The RELATED_TO table only has: relation_type, weight, created_at
    # We use `weight` to store confidence (it's a double; the SDK doesn't
    # read it for L5). We can't add a custom field without schema migration.
    print(f"\n=== Ingesting {len(relations)} relations ===")
    n_written_r = 0
    n_skipped_r = 0
    n_errors_r = 0
    started = time.time()
    for r in relations:
        a_id = slugify(r["a"])
        b_id = slugify(r["b"])
        cypher = """
        MATCH (a:Memory {node_id: $a_id}), (b:Memory {node_id: $b_id})
        MERGE (a)-[rel:RELATED_TO {relation_type: $rtype}]->(b)
        ON CREATE SET
            rel.weight = $confidence,
            rel.created_at = $created_at
        ON MATCH SET
            rel.weight = $confidence
        RETURN rel
        """
        try:
            result = conn.execute(cypher, parameters={
                "a_id": a_id,
                "b_id": b_id,
                "rtype": r["type"],
                "confidence": r["confidence"],
                "created_at": datetime.now(),
            })
            rows = 0
            while result.has_next():
                result.get_next()
                rows += 1
            if rows == 0:
                n_skipped_r += 1
            else:
                n_written_r += 1
        except Exception as ex:
            n_errors_r += 1
            if n_errors_r <= 3:
                print(f"  ERROR on {r['a']!r} → {r['b']!r}: {ex}")
    print(f"  Relations: {n_written_r} written, {n_skipped_r} skipped, {n_errors_r} errors, {time.time() - started:.1f}s")

    # ------------------------------------------------------------------
    # Verification queries
    # ------------------------------------------------------------------
    print(f"\n=== Verification queries ===")
    sample_queries = [
        ("Total L5 nodes",
         "MATCH (m:Memory {layer: 'l5_knowledge'}) RETURN COUNT(m) AS n"),
        ("Total L5 edges (RELATED_TO)",
         "MATCH ()-[r:RELATED_TO]->() WHERE r.relation_type IS NOT NULL RETURN COUNT(r) AS n"),
        ("L5 nodes by entity_type",
         "MATCH (m:Memory {layer: 'l5_knowledge'}) RETURN m.entity_type AS t, COUNT(m) AS n ORDER BY n DESC"),
        ("TunaCookie works_on (top 5)",
         "MATCH (a:Memory {layer: 'l5_knowledge', content: 'TunaCookie'})-[r:RELATED_TO {relation_type: 'works_on'}]->(b:Memory) RETURN b.content AS project LIMIT 5"),
        ("Hermes uses (top 10)",
         "MATCH (a:Memory {layer: 'l5_knowledge', content: 'Hermes'})-[r:RELATED_TO {relation_type: 'uses'}]->(b:Memory) RETURN b.content AS tool ORDER BY r.confidence DESC LIMIT 10"),
        ("Hy-Memory 2-hop neighborhood (what uses + what depends on)",
         "MATCH (a:Memory {layer: 'l5_knowledge', content: 'Hy-Memory'})-[r:RELATED_TO]->(b:Memory)-[r2:RELATED_TO]->(c:Memory) RETURN b.content AS via, type(r) AS edge1, c.content AS target, type(r2) AS edge2 LIMIT 15"),
        ("What depends on Hermes? (reverse traversal)",
         "MATCH (a:Memory)-[r:RELATED_TO]->(b:Memory {content: 'Hermes'}) RETURN a.content AS source, r.relation_type AS type, r.confidence AS conf ORDER BY r.confidence DESC LIMIT 10"),
        ("All entities in a tool→uses graph (3-hop)",
         "MATCH path = (a:Memory {layer: 'l5_knowledge'})-[:RELATED_TO*1..3]->(b:Memory) WHERE a.content = 'Hermes' RETURN a.content, b.content LIMIT 20"),
    ]
    output_lines = ["L5 Ingest Verification Queries", "=" * 60, ""]
    for title, query in sample_queries:
        output_lines.append(f"### {title}")
        output_lines.append(f"Cypher: {query}")
        try:
            result = conn.execute(query)
            n_rows = 0
            while result.has_next():
                row = result.get_next()
                # Format the row for display
                cols = result.get_column_names() if hasattr(result, 'get_column_names') else []
                if cols:
                    row_str = "  ".join(f"{c}={row[i]}" for i, c in enumerate(cols) if i < len(row))
                else:
                    row_str = "  ".join(str(x) for x in row)
                output_lines.append(f"  {row_str}")
                n_rows += 1
            if n_rows == 0:
                output_lines.append("  (no rows)")
            else:
                output_lines.append(f"  ({n_rows} rows)")
        except Exception as e:
            output_lines.append(f"  ERROR: {e}")
        output_lines.append("")

    SAMPLE_PATH.write_text("\n".join(output_lines), encoding="utf-8")
    print(f"  Sample queries saved to {SAMPLE_PATH}")

    # ------------------------------------------------------------------
    # Save stats
    # ------------------------------------------------------------------
    stats = {
        "ingested_at": datetime.now().isoformat(),
        "kuzu_path": str(kuzu_path),
        "target": args.target,
        "isolation_key": ISOLATION_KEY,
        "rebuild_mode": args.rebuild,
        "entities": {
            "input": len(entities),
            "written": n_written_e,
            "errors": n_errors_e,
        },
        "relations": {
            "input": len(relations),
            "written": n_written_r,
            "skipped_no_endpoint": n_skipped_r,
            "errors": n_errors_r,
        },
    }
    if args.rebuild:
        stats["wiped_l5_count"] = wiped
    STATS_PATH.write_text(json.dumps(stats, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n=== Summary ===")
    if args.rebuild:
        print(f"  Wiped:    {wiped} L5 nodes (before ingest)")
    print(f"  Entities:  {n_written_e}/{len(entities)} written, {n_errors_e} errors")
    print(f"  Relations: {n_written_r}/{len(relations)} written, {n_errors_r} errors")
    print(f"  Stats: {STATS_PATH}")
    print(f"  Sample queries: {SAMPLE_PATH}")


if __name__ == "__main__":
    main()
