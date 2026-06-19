#!/usr/bin/env python3
"""Backfill importance scores for existing qdrant points.

This is a one-off script that populates the `importance` payload field on all
existing memories in the local qdrant collection. It uses the same layer->
importance mapping that the runtime write path uses, so once the backfill is
complete every memory in the corpus will have a consistent importance score.

Usage:
    python scripts/backfill_importance.py --dry-run   # report only
    python scripts/backfill_importance.py --apply     # actually patch

The script is intentionally placed under scripts/ so it is excluded from the
CI linting scope (which only covers src/ and tests/). It is safe to run against
a live local qdrant because PATCH operations are idempotent and the write path
itself is gated by HYATLAS_MEMORY_IMPORTANCE.
"""
from __future__ import annotations

import argparse
import json
import os
import urllib.request
from collections import defaultdict
from typing import Any

from hyatlas_memory.patches import _LAYER_IMPORTANCE

_DEFAULT_QDRANT_URL = "http://127.0.0.1:6333"
_DEFAULT_COLLECTION = "agent_memories_384"
_BATCH_SIZE = 100


def _scroll_all_points(
    qdrant_url: str,
    collection: str,
) -> list[dict[str, Any]]:
    """Scroll the entire collection, returning payload + id for every point."""
    points: list[dict[str, Any]] = []
    offset: str | None = None

    while True:
        body: dict[str, Any] = {
            "limit": _BATCH_SIZE,
            "with_payload": ["layer", "importance", "access_count"],
            "with_vectors": False,
        }
        if offset is not None:
            body["offset"] = offset

        with urllib.request.urlopen(
            f"{qdrant_url}/collections/{collection}/points/scroll",
            data=json.dumps(body).encode("utf-8"),
            timeout=30,
        ) as resp:
            data = json.loads(resp.read())

        batch = data["result"]["points"]
        if not batch:
            break
        points.extend(batch)
        offset = data["result"].get("next_page_offset")
        if offset is None:
            break

    return points


def _patch_points(
    ids: list[str],
    value: float,
    qdrant_url: str,
    collection: str,
    payload_key: str = "importance",
) -> None:
    """PATCH a list of point IDs with the given payload value."""
    if not ids:
        return

    body = {
        "points": ids,
        "payload": {payload_key: value},
    }
    with urllib.request.urlopen(
        f"{qdrant_url}/collections/{collection}/points/payload",
        data=json.dumps(body).encode("utf-8"),
        timeout=30,
    ) as resp:
        resp.read()  # drain response


def main() -> int:
    parser = argparse.ArgumentParser(description="Backfill importance scores")
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply the PATCHes. Without this flag, only report counts.",
    )
    parser.add_argument(
        "--qdrant-url",
        default=os.environ.get("HYATLAS_MEMORY_QDRANT_URL", _DEFAULT_QDRANT_URL),
        help=f"Qdrant HTTP URL (default: {_DEFAULT_QDRANT_URL})",
    )
    parser.add_argument(
        "--collection",
        default=os.environ.get("HYATLAS_MEMORY_QDRANT_COLLECTION", _DEFAULT_COLLECTION),
        help=f"Qdrant collection name (default: {_DEFAULT_COLLECTION})",
    )
    args = parser.parse_args()

    print(f"Connecting to qdrant at {args.qdrant_url}")
    print(f"Collection: {args.collection}")
    print("Scrolling all points...")

    points = _scroll_all_points(args.qdrant_url, args.collection)

    # Group by layer, then by desired importance
    by_importance: dict[float, list[str]] = defaultdict(list)
    layer_counts: dict[str, int] = defaultdict(int)
    access_count_ids: list[str] = []
    already_set = 0
    access_count_no_set = 0
    no_layer = 0

    for point in points:
        payload = point.get("payload", {}) or {}
        layer = payload.get("layer")
        if not layer:
            layer = "l1_raw"
            no_layer += 1
        importance = _LAYER_IMPORTANCE.get(layer, 0.5)
        point_id = point.get("id")

        if point_id is None:
            continue

        if payload.get("importance") is None:
            by_importance.setdefault(importance, []).append(point_id)
            layer_counts[layer] += 1
        else:
            already_set += 1

        # Always ensure access_count is present (default 0). This lets the
        # upstream 4-factor MemoryScorer's access term have a consistent
        # value across the entire corpus, not just new memories.
        if payload.get("access_count") is None:
            access_count_ids.append(point_id)
            access_count_no_set += 1

    print(f"Total points scanned: {len(points)}")
    print(f"Already have importance set: {already_set}")
    print(f"Missing layer (defaulting to l1_raw): {no_layer}")
    print(f"Missing access_count (will be set to 0): {access_count_no_set}")
    print("\nPoints to patch by layer:")
    for layer in sorted(layer_counts, key=lambda x: _LAYER_IMPORTANCE.get(x, 0.5), reverse=True):
        count = layer_counts[layer]
        importance = _LAYER_IMPORTANCE.get(layer, 0.5)
        print(f"  {layer:15s} -> importance={importance}  count={count}")

    total_points_to_patch = sum(len(v) for v in by_importance.values()) + len(access_count_ids)
    print(f"\nTotal points to patch: {total_points_to_patch}")
    print(f"Importance PATCH calls needed: {len(by_importance)}")
    print(f"Access-count PATCH calls needed: {1 if access_count_ids else 0}")

    if not args.apply:
        print("\nDry-run: no PATCHes applied. Pass --apply to execute.")
        return 0

    if not any(by_importance.values()) and not access_count_ids:
        print("\nNothing to patch.")
        return 0

    print("\nApplying PATCHes...")
    for importance, ids in sorted(by_importance.items(), reverse=True):
        _patch_points(ids, importance, args.qdrant_url, args.collection)
        print(f"  importance={importance} -> patched {len(ids)} points")

    if access_count_ids:
        _patch_points(access_count_ids, 0, args.qdrant_url, args.collection, payload_key="access_count")
        print(f"  access_count=0 -> patched {len(access_count_ids)} points")

    print("\nBackfill complete.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
