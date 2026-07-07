"""Run HyAtlas digest — stable path for Hermes background/cron (no MSYS path mangling).

Install copy to: %LOCALAPPDATA%\\hermes\\scripts\\run_hyatlas_digest.py
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(r"F:/HyAtlas-Memory")
SCRIPT = ROOT / "scripts" / "run_digest_once.py"
LOG = Path.home() / ".hyatlas" / "logs" / "digest_run_latest.log"
DEFAULT_PY = Path(r"C:/Users/tuanc/AppData/Local/hermes/hermes-agent/venv/Scripts/python.exe")


def main() -> int:
    py = DEFAULT_PY if DEFAULT_PY.is_file() else Path(sys.executable)
    LOG.parent.mkdir(parents=True, exist_ok=True)
    cmd = [str(py), str(SCRIPT), "hermes-user", "default"]
    with LOG.open("w", encoding="utf-8") as f:
        f.write(" ".join(cmd) + "\n\n")
        f.flush()
        return subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, cwd=str(ROOT)).returncode


if __name__ == "__main__":
    raise SystemExit(main())