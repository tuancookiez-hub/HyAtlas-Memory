"""Scheduled System2 digest — delegates to the Windows-safe launcher under Hermes scripts."""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

# Canonical launcher (avoids MSYS /f/ → F:\\f\\ path mangling in background jobs).
LAUNCHER = (
    Path(os.environ.get("LOCALAPPDATA", ""))
    / "hermes"
    / "scripts"
    / "run_hyatlas_digest.py"
)
FALLBACK = Path(__file__).resolve().parents[1] / "scripts" / "run_digest_once.py"
LOG = Path.home() / ".hyatlas" / "logs" / "scheduled_digest.log"


def main() -> int:
    LOG.parent.mkdir(parents=True, exist_ok=True)
    if LAUNCHER.is_file():
        cmd = [sys.executable, str(LAUNCHER)]
    else:
        cmd = [sys.executable, str(FALLBACK), "hermes-user", "default"]
    with LOG.open("a", encoding="utf-8") as log:
        log.write(f"cmd={' '.join(cmd)}\n")
        log.flush()
        rc = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT).returncode
        log.write(f"exit={rc}\n")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
