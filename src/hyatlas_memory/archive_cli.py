"""Archive legacy vector-store data before removing Qdrant from the stack."""

from __future__ import annotations

import argparse
import shutil
import sys
from argparse import Namespace
from datetime import datetime

from . import layout


def _stamp() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def archive_qdrant(args: Namespace) -> int:
    src = layout.qdrant_data()
    if src is None or not src.exists():
        print("[hyatlas] No Qdrant data directory found (checked layout.qdrant_data()).")
        return 1
    files = sum(1 for p in src.rglob("*") if p.is_file())
    if files == 0:
        print(f"[hyatlas] Qdrant data path exists but is empty: {src}")
        return 1
    dest_dir = layout.home() / "archive"
    dest_dir.mkdir(parents=True, exist_ok=True)
    label = getattr(args, "label", None) or _stamp()
    archive_base = dest_dir / f"qdrant_{label}"
    if archive_base.with_suffix(".zip").exists() and not getattr(args, "force", False):
        print(f"[hyatlas] Archive already exists: {archive_base}.zip (use --force)")
        return 1
    print("[hyatlas] Archiving Qdrant data")
    print(f"  source: {src} ({files} files)")
    print(f"  dest:   {archive_base}.zip")
    shutil.make_archive(str(archive_base), "zip", root_dir=src.parent, base_dir=src.name)
    out = archive_base.with_suffix(".zip")
    if not out.is_file():
        print("[hyatlas] Archive failed (zip missing)")
        return 1
    print(f"[hyatlas] Done: {out} ({out.stat().st_size // (1024 * 1024)} MiB)")
    print("  Original data left untouched. Safe to stop using Qdrant after Zvec is verified.")
    return 0


def register(sub: argparse._SubParsersAction) -> None:
    arc = sub.add_parser("archive", help="Archive legacy backends (read-only copy)")
    asub = arc.add_subparsers(dest="archive_cmd", required=True)
    q = asub.add_parser("qdrant", help="Zip HYATLAS_HOME / legacy Qdrant storage for cold backup")
    q.add_argument("--label", help="Archive filename suffix (default: timestamp)")
    q.add_argument("--force", action="store_true", help="Overwrite existing archive with same label")
    q.set_defaults(func=archive_qdrant)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hyatlas archive")
    sub = parser.add_subparsers(dest="archive_cmd", required=True)
    q = sub.add_parser("qdrant")
    q.add_argument("--label")
    q.add_argument("--force", action="store_true")
    q.set_defaults(func=archive_qdrant)
    args = parser.parse_args(argv)
    return args.func(args) or 0


if __name__ == "__main__":
    sys.exit(main())
