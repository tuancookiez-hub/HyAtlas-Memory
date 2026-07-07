"""Scheduled System2 digest for Hermes single-user stack. Logs to ~/.hyatlas/logs/."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_digest_once.py"
LOG = Path.home() / ".hyatlas" / "logs" / "scheduled_digest.log"
LATEST = Path.home() / ".hyatlas" / "logs" / "digest_run_latest.log"


def main() -> int:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, str(SCRIPT), "hermes-user", "default"]
    with LOG.open("a", encoding="utf-8") as log, LATEST.open("w", encoding="utf-8") as latest:
        latest.write("--- digest run ---\n")
        latest.flush()
        proc = subprocess.run(cmd, stdout=latest, stderr=subprocess.STDOUT, cwd=str(ROOT))
    with LOG.open("a", encoding="utf-8") as log:
        log.write(f"exit={proc.returncode}\n")
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())