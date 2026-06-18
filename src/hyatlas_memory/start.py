"""CLI entry point: `hyatlas`, `hyatlas --stop`, `hyatlas --status`.

Thin wrapper so the `hyatlas` console_scripts entry point works after
`pip install -e .`. Delegates to the repo-root start.py which handles
all the actual startup/shutdown/health-check logic.
"""
from __future__ import annotations

import os
import runpy
import sys


def main() -> None:
    # Resolve start.py: editable install → repo root; otherwise → CWD
    pkg_dir = os.path.dirname(os.path.abspath(__file__))
    repo_root = os.path.dirname(pkg_dir)
    start_script = os.path.join(repo_root, "start.py")
    if not os.path.isfile(start_script):
        start_script = os.path.join(os.getcwd(), "start.py")
    if not os.path.isfile(start_script):
        print("Error: start.py not found.")
        print("  Run from the HyAtlas-Memory project root, or install with: pip install -e .")
        sys.exit(1)

    ns = runpy.run_path(start_script, run_name="start")
    ns["main"]()
