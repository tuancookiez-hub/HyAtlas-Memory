"""Scheduled System2 digest for Hermes single-user stack. Logs to ~/.hyatlas/logs/."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "run_digest_once.py"
LOG = Path.home() / ".hyatlas" / "logs" / "scheduled_digest.log"


def main() -> int:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    cmd = [sys.executable, str(SCRIPT), "hermes-user", "default"]
    with LOG.open("a", encoding="utf-8") as log:
        log.write(f"\n--- scheduled_digest ---\n")
        log.flush()
        proc = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT, cwd=str(ROOT))
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())