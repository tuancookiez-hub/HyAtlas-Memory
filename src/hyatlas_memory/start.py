"""CLI entry point: `hyatlas`, `hyatlas --stop`, `hyatlas --status`.

Thin wrapper so the `hyatlas` console_scripts entry point works after
`pip install -e .`. Delegates to the repo-root start.py which handles
all the actual startup/shutdown/health-check logic.
"""
from __future__ import annotations

import os
import runpy
import sys


def _find_start_py() -> str | None:
    # src/hyatlas_memory/start.py → 3 levels up = repo root (editable install)
    here = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(os.path.dirname(here))
    candidate = os.path.join(repo_root, "start.py")
    if os.path.isfile(candidate):
        return candidate

    # Fallback: current working directory
    candidate = os.path.join(os.getcwd(), "start.py")
    if os.path.isfile(candidate):
        return candidate

    return None


def main() -> None:
    start_script = _find_start_py()
    if not start_script:
        print("Error: start.py not found.")
        print("  Run from the HyAtlas-Memory project root, or install with: pip install -e .")
        sys.exit(1)

    ns = runpy.run_path(start_script, run_name="start")
    ns["main"]()


if __name__ == "__main__":
    main()
