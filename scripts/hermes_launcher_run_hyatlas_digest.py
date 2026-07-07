"""Hermes cron entry: delegates to run_hyatlas_digest.py (Windows-safe paths)."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

LAUNCHER = Path.home() / "AppData" / "Local" / "hermes" / "scripts" / "run_hyatlas_digest.py"


def main() -> int:
    if not LAUNCHER.is_file():
        print(f"Missing launcher: {LAUNCHER}", file=sys.stderr)
        return 2
    return subprocess.call([sys.executable, str(LAUNCHER)])


if __name__ == "__main__":
    raise SystemExit(main())