"""Runtime layout snapshot and migration commands."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from argparse import Namespace
from datetime import datetime
from pathlib import Path
from typing import Any

from . import layout

KUZU_SRC = Path.home() / ".hy_memory" / "data" / "kuzu_db"
KUZU_WAL = Path.home() / ".hy_memory" / "data" / "kuzu_db.wal"


def _now() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _count(path: Path) -> tuple[int, int]:
    if not path.exists():
        return 0, 0
    if path.is_file():
        return 1, path.stat().st_size
    files = [p for p in path.rglob("*") if p.is_file()]
    return len(files), sum(p.stat().st_size for p in files)


def _redact(data: Any) -> Any:
    if isinstance(data, dict):
        return {k: ("***" if "key" in k.lower() or "token" in k.lower() else _redact(v)) for k, v in data.items()}
    if isinstance(data, list):
        return [_redact(v) for v in data]
    return data


def _hash_json(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        data = _redact(json.loads(path.read_text(encoding="utf-8")))
        raw = json.dumps(data, sort_keys=True).encode()
    except Exception:
        raw = path.read_bytes()
    return hashlib.sha256(raw).hexdigest()


def _manifest(label: str) -> dict[str, Any]:
    cfg = layout.active_config_path()
    qbin, qcfg = layout.find_qdrant()
    qsrc = layout.qdrant_data() or Path("")
    items = {
        "qdrant": (qsrc, layout.qdata()),
        "kuzu": (KUZU_SRC, layout.kdata()),
        "kuzu_wal": (KUZU_WAL, layout.datadir() / "kuzu_db.wal"),
        "config": (cfg, layout.cfgfile()) if cfg else (Path(), layout.cfgfile()),
        "qdrant_config": (qcfg or Path(), layout.qcfg()),
    }
    # Qdrant binary is recorded, not copied, unless a HYATLAS_QDRANT_BIN target is set.
    if qbin and qbin.exists():
        items["qdrant_bin"] = (qbin, Path(os.environ.get("HYATLAS_QDRANT_BIN", str(qbin))))
    out: dict[str, Any] = {
        "label": label,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "hyatlas_home": str(layout.home()),
        "items": {},
    }
    for name, pair in items.items():
        src, dst = pair
        if not src or str(src) == ".":
            out["items"][name] = {
                "source": "",
                "target": str(dst),
                "exists": False,
                "files": 0,
                "bytes": 0,
                "sha256_redacted": "",
            }
            continue
        files, size = _count(src)
        out["items"][name] = {
            "source": str(src),
            "target": str(dst),
            "exists": src.exists(),
            "files": files,
            "bytes": size,
            "sha256_redacted": _hash_json(src) if src and src.suffix == ".json" else "",
        }
    return out


def _write_manifest(kind: str, label: str) -> Path:
    layout.ensure()
    path = layout.snaps() / f"{kind}-{label}-{_now()}.json"
    path.write_text(json.dumps(_manifest(label), indent=2) + "\n", encoding="utf-8")
    return path


def snapshot(args: Namespace) -> int:
    path = _write_manifest("snapshot", args.label)
    print(f"✓ Snapshot manifest: {path}")
    return 0


def dry_run(args: Namespace) -> int:
    path = _write_manifest("dry-run", "layout")
    print(f"✓ Dry-run manifest: {path}")
    print(json.dumps(_manifest("layout"), indent=2))
    return 0


def _copy(src: Path, dst: Path) -> None:
    if not src.exists():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        shutil.copytree(src, dst, dirs_exist_ok=True)
        return
    shutil.copy2(src, dst)


def _write_qdrant_cfg() -> None:
    layout.cfgdir().mkdir(parents=True, exist_ok=True)
    p = str(layout.qdata()).replace("\\", "\\\\")
    layout.qcfg().write_text(
        f"storage:\n  storage_path: {p}\n"
        f"service:\n  host: 127.0.0.1\n  http_port: 6333\n  grpc_port: 6334\n  enable_cors: true\n",
        encoding="utf-8",
    )


def _write_config() -> None:
    src = layout.active_config_path()
    cfg = layout.read_config()
    if not cfg:
        return
    cfg.setdefault("vector_store", {})["collection"] = cfg.get("vector_store", {}).get(
        "collection", f"agent_memories_{(cfg.get('embedder') or {}).get('dims', 1024)}"
    )
    qbin, _ = layout.find_qdrant()
    if qbin and qbin.exists():
        cfg["qdrant_bin"] = str(qbin)
    layout.cfgdir().mkdir(parents=True, exist_ok=True)
    layout.cfgfile().write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")
    if src and src != layout.cfgfile():
        print(f"  copied config from {src}")


def apply(args: Namespace) -> int:
    snap = _write_manifest("pre-apply", "layout")
    print(f"✓ Pre-apply manifest: {snap}")
    layout.ensure()
    qsrc = layout.qdrant_data()
    pairs = [(qsrc, layout.qdata())] if qsrc else []
    pairs += [(KUZU_SRC, layout.kdata()), (KUZU_WAL, layout.datadir() / "kuzu_db.wal")]
    for src, dst in pairs:
        print(f"copy {src} -> {dst}")
        _copy(src, dst)
    _write_config()
    _write_qdrant_cfg()
    done = _write_manifest("post-apply", "layout")
    print(f"✓ Post-apply manifest: {done}")
    print("Old paths were left untouched.")
    return 0


def rollback(args: Namespace) -> int:
    label = f"rollback-{_now()}"
    snap = _write_manifest("pre-rollback", "layout")
    print(f"✓ Pre-rollback manifest: {snap}")
    for path in (layout.cfgfile(), layout.qcfg()):
        if path.exists():
            dst = path.with_suffix(path.suffix + f".{label}.bak")
            path.replace(dst)
            print(f"backup {path} -> {dst}")
    print("Rolled active config back to legacy fallback. Copied data remains untouched.")
    return 0


def register(sub) -> None:
    snap = sub.add_parser("snapshot", help="Create a runtime layout snapshot manifest")
    ssub = snap.add_subparsers(dest="snapshot_cmd", required=True)
    create = ssub.add_parser("create", help="Create a snapshot manifest")
    create.add_argument("--label", default="manual")
    create.set_defaults(func=snapshot)

    mig = sub.add_parser("migrate", help="Runtime layout migration commands")
    msub = mig.add_subparsers(dest="migrate_cmd", required=True)
    dry = msub.add_parser("layout", help="Migrate to HYATLAS_HOME layout")
    dry.add_argument("--dry-run", action="store_true")
    dry.add_argument("--apply", action="store_true")
    dry.add_argument("--rollback", action="store_true")
    dry.set_defaults(func=lambda args: rollback(args) if args.rollback else apply(args) if args.apply else dry_run(args))
