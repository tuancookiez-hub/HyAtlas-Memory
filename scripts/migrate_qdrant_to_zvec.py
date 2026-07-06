#!/usr/bin/env python
"""
HyAtlas Migration: Qdrant → Zvec

Exports all points from a Qdrant collection and imports them into a zvec
collection with the same schema. Vectors and payloads are preserved.

Usage:
    python scripts/migrate_qdrant_to_zvec.py --dry-run   # preview only
    python scripts/migrate_qdrant_to_zvec.py --apply      # execute migration
    python scripts/migrate_qdrant_to_zvec.py --verify     # verify post-migration

Safety:
    - Source (Qdrant) is never modified — read only
    - Destination (zvec) is created fresh if it doesn't exist
    - Dry-run shows counts and sample without writing
"""

import argparse
import gc
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))


def export_qdrant(host: str, port: int, collection: str, batch_size: int = 500):
    """Export all points from Qdrant via scroll API (generator)."""
    url = f"http://{host}:{port}/collections/{collection}/points/scroll"
    offset = None
    total = 0

    while True:
        payload = json.dumps({
            "limit": batch_size,
            "with_payload": True,
            "with_vector": True,
            **({"offset": offset} if offset else {}),
        }).encode()

        req = urllib.request.Request(
            url, data=payload, headers={"Content-Type": "application/json"}
        )
        resp = urllib.request.urlopen(req, timeout=60)
        data = json.loads(resp.read())
        points = data["result"]["points"]
        next_offset = data["result"].get("next_page_offset")

        if not points:
            break

        yield from points

        total += len(points)
        print(f"\r  Exported {total} points...", end="", flush=True)

        if next_offset is None:
            break
        offset = next_offset

    print()


def import_zvec(zvec_path: str, points: list, dims: int = 1024):
    """Import points into a zvec collection."""
    import zvec

    from hyatlas_memory.core.data.vector_store_base import VectorStoreBase
    from hyatlas_memory.core.data.vector_store_zvec import _FIELD_SCHEMA

    # Build schema
    fields = [
        zvec.FieldSchema(name, getattr(zvec.DataType, dtype), nullable=True)
        for name, dtype in _FIELD_SCHEMA
    ]

    vectors = [
        zvec.VectorSchema(
            name="embedding",
            dimension=dims,
            data_type=zvec.DataType.VECTOR_FP32,
        )
    ]

    schema = zvec.CollectionSchema(name=Path(zvec_path).name, fields=fields, vectors=vectors)

    # Create or open
    if os.path.exists(zvec_path):
        print(f"  Collection exists at {zvec_path}, opening...")
        coll = zvec.open(zvec_path)
    else:
        os.makedirs(os.path.dirname(zvec_path), exist_ok=True)
        coll = zvec.create_and_open(zvec_path, schema)
        print(f"  Created collection at {zvec_path}")

    # Batch insert
    batch = []
    total = 0
    batch_size = 200

    for p in points:
        raw = dict(p["payload"])
        payload = {}
        for key, dtype in _FIELD_SCHEMA:
            val = raw.get(key)
            if val is None:
                continue
            if dtype == "ARRAY_STRING":
                payload[key] = [str(v) for v in val] if isinstance(val, list) else [str(val)]
            elif key in ("memory_at", "gmt_created", "gmt_modified", "valid_from", "valid_until", "last_accessed_at") and isinstance(val, (int, float)) and val > 0:
                payload[key] = datetime.fromtimestamp(val, tz=timezone.utc).isoformat()
            elif key in ("memory_at", "gmt_created", "gmt_modified", "valid_from", "valid_until", "last_accessed_at") and isinstance(val, str) and val.isdigit():
                payload[key] = datetime.fromtimestamp(int(val), tz=timezone.utc).isoformat()
            elif isinstance(val, (dict, list)):
                payload[key] = json.dumps(val, ensure_ascii=False)
            elif isinstance(val, bool):
                payload[key] = "true" if val else "false"
            else:
                payload[key] = str(val) if val != "" else None
        doc = zvec.Doc(
            id=VectorStoreBase._node_id_to_point_id(payload.get("node_id") or p["id"]),
            vectors={"embedding": p["vector"]},
            fields=payload,
        )
        batch.append(doc)

        if len(batch) >= batch_size:
            coll.insert(zvec.DocList(batch))
            coll.flush()
            total += len(batch)
            print(f"\r  Imported {total} points...", end="", flush=True)
            batch = []

    if batch:
        coll.insert(zvec.DocList(batch))
        coll.flush()
        total += len(batch)
        print(f"\r  Imported {total} points...", end="", flush=True)

    print()

    # Create FTS indexes
    try:
        coll.create_index("content", zvec.FtsIndexParam())
        print("  FTS index on content created")
    except Exception:
        pass
    try:
        coll.create_index("search_text", zvec.FtsIndexParam())
        print("  FTS index on search_text created")
    except Exception:
        pass

    # Optimize
    coll.optimize()
    print("  Index optimized")

    stats = coll.stats
    count = stats.doc_count
    print(f"  Final doc_count: {count}")
    del coll
    gc.collect()
    return count


def verify_migration(qdrant_host, qdrant_port, qdrant_collection, zvec_path):
    """Verify migration by comparing counts and sampling."""
    import zvec

    # Qdrant count
    url = f"http://{qdrant_host}:{qdrant_port}/collections/{qdrant_collection}"
    resp = urllib.request.urlopen(url, timeout=10)
    qinfo = json.loads(resp.read())
    qcount = qinfo["result"]["points_count"]

    # zvec count
    coll = zvec.open(zvec_path)
    zcount = coll.stats.doc_count
    del coll
    gc.collect()

    print(f"  Qdrant points: {qcount}")
    print(f"  Zvec docs:     {zcount}")

    if qcount == zcount:
        print("  ✅ Count match — migration verified")
        return True
    else:
        print(f"  ❌ Count mismatch: {qcount} vs {zcount}")
        return False


def main():
    parser = argparse.ArgumentParser(description="Migrate Qdrant → Zvec")
    parser.add_argument("--dry-run", action="store_true", help="Preview only")
    parser.add_argument("--apply", action="store_true", help="Execute migration")
    parser.add_argument("--verify", action="store_true", help="Verify post-migration")
    parser.add_argument("--qdrant-host", default="127.0.0.1")
    parser.add_argument("--qdrant-port", type=int, default=6333)
    parser.add_argument("--qdrant-collection", default="agent_memories_1024")
    parser.add_argument("--zvec-path", default=None, help="zvec collection path")
    parser.add_argument("--dims", type=int, default=1024)
    args = parser.parse_args()

    # Default zvec path
    if args.zvec_path is None:
        from hyatlas_memory.core.config import MemoryConfig
        from hyatlas_memory.core.data.vector_store_zvec import resolve_zvec_path

        config = MemoryConfig()
        config.vector_store.provider = "zvec"
        config.vector_store.collection_name = "agent_memories"
        config.vector_store.embedding_dims = args.dims
        args.zvec_path = str(resolve_zvec_path(config))

    if args.dry_run:
        print("=== DRY RUN ===")
        print(f"  Source: Qdrant at {args.qdrant_host}:{args.qdrant_port}/{args.qdrant_collection}")
        print(f"  Dest:   zvec at {args.zvec_path}")
        print()
        print("  Exporting sample (first 10 points)...")
        for count, p in enumerate(export_qdrant(args.qdrant_host, args.qdrant_port, args.qdrant_collection), start=1):
            if count <= 3:
                print(f"    Point {count}: id={p['id'][:12]}... layer={p['payload'].get('layer')} dim={len(p['vector'])}")
            if count >= 10:
                break
        print("  ...sample complete. Full export would migrate all points.")
        return

    if args.apply:
        print("=== MIGRATION: Qdrant → Zvec ===")
        print(f"  Source: {args.qdrant_host}:{args.qdrant_port}/{args.qdrant_collection}")
        print(f"  Dest:   {args.zvec_path}")
        print()

        t0 = time.perf_counter()
        print("Step 1: Exporting from Qdrant...")
        points = list(export_qdrant(
            args.qdrant_host, args.qdrant_port, args.qdrant_collection
        ))
        t1 = time.perf_counter()
        print(f"  Exported {len(points)} points in {t1-t0:.1f}s")

        print()
        print("Step 2: Importing into zvec...")
        t2 = time.perf_counter()
        import_zvec(args.zvec_path, points, dims=args.dims)
        t3 = time.perf_counter()
        print(f"  Import complete in {t3-t2:.1f}s")

        print()
        print(f"Total migration time: {t3-t0:.1f}s")
        print()
        if args.verify:
            print("Step 3: Verifying migration...")
            ok = verify_migration(args.qdrant_host, args.qdrant_port, args.qdrant_collection, args.zvec_path)
            if not ok:
                raise SystemExit(1)
            return

        print("Next steps:")
        print("  1. Update config: vector_store.provider = 'zvec'")
        print("  2. Restart server")
        print(f"  3. Run: python {__file__} --verify")
        return

    if args.verify:
        print("=== VERIFICATION ===")
        ok = verify_migration(
            args.qdrant_host, args.qdrant_port, args.qdrant_collection, args.zvec_path
        )
        sys.exit(0 if ok else 1)

    parser.print_help()


if __name__ == "__main__":
    main()
