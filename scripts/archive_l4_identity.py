#!/usr/bin/env python3
"""Archive all l4_identity VDB rows to JSONL before L4 retirement (read-only export)."""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BASE = os.environ.get("HY_MEMORY_BASE_URL", "http://127.0.0.1:19527")
USER = sys.argv[1] if len(sys.argv) > 1 else "hermes-user"
AGENTS = sys.argv[2:] if len(sys.argv) > 2 else ["default", "default_agent"]


def post(path: str, body: dict) -> dict:
    req = urllib.request.Request(
        f"{BASE}{path}",
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read().decode())


def main() -> int:
    home = Path(os.environ.get("HYATLAS_HOME", Path.home() / ".hyatlas"))
    out_dir = home / "archive"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    out_path = out_dir / f"l4_identity_pre_migrate_{stamp}.jsonl"

    total = 0
    with out_path.open("w", encoding="utf-8") as f:
        for agent in AGENTS:
            d = post("/api/v1/list", {"user_id": USER, "agent_id": agent, "limit": 5000})
            mems = (d.get("vdb") or {}).get("memories") or []
            for m in mems:
                if m.get("layer") != "l4_identity":
                    continue
                m["_archive_agent_filter"] = agent
                f.write(json.dumps(m, ensure_ascii=False) + "\n")
                total += 1

    print(json.dumps({"archived": total, "path": str(out_path)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())