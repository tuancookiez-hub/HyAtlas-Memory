from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from . import layout
from .core.config import MemoryConfig
from .core.data.vector_store_zvec import resolve_zvec_path


def _cfg(raw: dict) -> MemoryConfig:
    cfg = MemoryConfig()
    vec = raw.get("vector_store") or {}
    emb = raw.get("embedder") or {}
    cfg.vector_store.provider = str(vec.get("provider") or cfg.vector_store.provider)
    cfg.vector_store.collection_name = str(vec.get("collection") or vec.get("collection_name") or cfg.vector_store.collection_name)
    cfg.vector_store.embedding_dims = int(emb.get("dims") or vec.get("embedding_dims") or cfg.vector_store.embedding_dims or 1024)
    return cfg


def _locks(root: Path) -> list[Path]:
    if not root.exists():
        return []
    return sorted(p for p in root.rglob("LOCK") if p.is_file())


def _reopen(path: Path) -> tuple[bool, str]:
    if not path.exists():
        return True, "skipped (collection missing)"
    code = "\n".join([
        "import sys",
        "import zvec",
        f"path = {str(path)!r}",
        "coll = zvec.open(path)",
        "coll.flush()",
        "coll = None",
        "print('ok')",
    ])
    res = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
    if res.returncode == 0:
        return True, "ok"
    return False, (res.stderr or res.stdout).strip().splitlines()[-1] if (res.stderr or res.stdout).strip() else f"exit {res.returncode}"


def doctor(args: argparse.Namespace) -> int:
    raw = layout.read_config() or {}
    cfg = _cfg(raw)
    provider = cfg.vector_store.provider or "?"
    print("[hyatlas] zvec doctor")
    print(f"provider: {provider}")
    path = Path(args.path) if getattr(args, "path", None) else resolve_zvec_path(cfg)
    print(f"resolved path: {path}")
    print(f"collection exists: {'yes' if path.exists() else 'no'}")
    locks = _locks(path)
    print(f"LOCK files: {len(locks)}")
    for lock in locks[:5]:
        print(f"  - {lock}")
    ok, msg = _reopen(path)
    print(f"fresh subprocess reopen: {msg}")
    if provider != "zvec" and not getattr(args, "path", None):
        return 1
    return 0 if ok else 1


def register(sub: argparse._SubParsersAction) -> None:
    zvec = sub.add_parser("zvec", help="Zvec diagnostics and maintenance")
    zsub = zvec.add_subparsers(dest="zvec_cmd", required=True)
    doc = zsub.add_parser("doctor", help="Read-only Zvec lifecycle diagnostic")
    doc.add_argument("--path", help="Inspect this zvec collection path instead of config-resolved path")
    doc.set_defaults(func=doctor)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="hyatlas zvec")
    sub = parser.add_subparsers(dest="zvec_cmd", required=True)
    doc = sub.add_parser("doctor", help="Read-only Zvec lifecycle diagnostic")
    doc.add_argument("--path", help="Inspect this zvec collection path instead of config-resolved path")
    doc.set_defaults(func=doctor)
    args = parser.parse_args(argv)
    return args.func(args) or 0
