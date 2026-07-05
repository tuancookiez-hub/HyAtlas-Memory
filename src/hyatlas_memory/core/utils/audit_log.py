"""
Lightweight JSONL audit logger for HyAtlas-Memory v3.0.0.

Logs pipeline events (extraction, reconciliation, S2 digest, L5 pipeline)
to a rotating JSONL file. Not through the cache abstraction — direct file writes.

Log file: ~/.hyatlas/logs/audit.jsonl
Rotation: at 10MB, keeps 5 archives (audit.1.jsonl through audit.5.jsonl)
"""

import json
import logging
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_max_size = 10 * 1024 * 1024  # 10 MB
_keep_archives = 5


def _log_path() -> Path:
    home = Path(os.environ.get("HYATLAS_HOME", str(Path.home() / ".hyatlas")))
    d = home / "logs"
    d.mkdir(parents=True, exist_ok=True)
    return d / "audit.jsonl"


def _rotate(path: Path) -> None:
    if not path.exists() or path.stat().st_size < _max_size:
        return
    for i in range(_keep_archives, 0, -1):
        src = path.with_suffix(f".{i}.jsonl")
        dst = path.with_suffix(f".{i + 1}.jsonl") if i < _keep_archives else None
        if src.exists():
            if dst:
                src.rename(dst)
            else:
                src.unlink()
    path.rename(path.with_suffix(".1.jsonl"))


def log_event(event_type: str, data: dict[str, Any], *, session_id: str = "") -> None:
    """Write one audit event to the JSONL log. Fire-and-forget — errors are swallowed."""
    try:
        entry = {
            "ts": datetime.now().isoformat(),
            "type": event_type,
            "session": session_id,
            **data,
        }
        line = json.dumps(entry, default=str, ensure_ascii=False) + "\n"
        with _lock:
            path = _log_path()
            _rotate(path)
            with open(path, "a", encoding="utf-8") as f:
                f.write(line)
    except Exception as e:
        logger.debug(f"[audit] log_event failed: {e}")


def log_extraction(request_id: str, extracted: dict[str, Any], *, session_id: str = "") -> None:
    """Log S1 extraction results."""
    log_event("s1_extraction", {
        "request_id": request_id,
        "memories_count": len(extracted.get("memories", [])),
        "intentions_count": len(extracted.get("intentions", [])),
        "basic_info_keys": list(extracted.get("basic_info", {}).keys()) if isinstance(extracted.get("basic_info"), dict) else [],
    }, session_id=session_id)


def log_reconciliation(request_id: str, ops: Any, *, session_id: str = "") -> None:
    """Log S1 reconciliation decisions."""
    if isinstance(ops, list):
        summary = {
            "total": len(ops),
            "adds": sum(1 for o in ops if isinstance(o, dict) and o.get("op") == "ADD"),
            "supersedes": sum(1 for o in ops if isinstance(o, dict) and o.get("op") == "SUPERSEDE"),
            "updates": sum(1 for o in ops if isinstance(o, dict) and o.get("op") == "UPDATE"),
        }
    else:
        summary = {"raw": str(ops)[:200]}
    log_event("s1_reconciliation", {"request_id": request_id, **summary}, session_id=session_id)


def log_s2_digest(user_key: str, result: dict[str, Any], *, session_id: str = "") -> None:
    """Log S2 digest cycle."""
    log_event("s2_digest", {
        "user_key": user_key,
        "schemas_written": result.get("schemas_written", 0),
        "intentions_written": result.get("intentions_written", 0),
        "elapsed_ms": result.get("elapsed_ms", 0),
    }, session_id=session_id)


def log_l5_pipeline(stats: dict[str, Any], *, session_id: str = "") -> None:
    """Log L5 pipeline run."""
    log_event("l5_pipeline", {
        "entities_extracted": stats.get("entities_count", 0),
        "relations_extracted": stats.get("relations_count", 0),
        "entities_after_resolve": stats.get("resolved_count", 0),
        "nodes_ingested": stats.get("ingested_count", 0),
        "elapsed_s": stats.get("elapsed_s", 0),
    }, session_id=session_id)


def log_write(request_id: str, layer: str, content_preview: str, *, session_id: str = "") -> None:
    """Log individual memory write."""
    log_event("write", {
        "request_id": request_id,
        "layer": layer,
        "content_preview": content_preview[:200],
    }, session_id=session_id)
