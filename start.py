#!/usr/bin/env python
"""HyAtlas-Memory — thin wrapper around the installed ``hyatlas_memory._start``.

Usage (after ``pip install git+https://github.com/tuancookiez-hub/HyAtlas-Memory.git``
or ``pip install -e .``):
    python start.py            # start everything
    python start.py --stop     # stop everything
    python start.py --status   # check what's running

This shim exists for backwards compatibility with users who cloned the
repo and ran ``python start.py``. It just delegates to the package.
"""
from __future__ import annotations

import sys

try:
    from hyatlas_memory import _start
except ImportError:
    sys.stderr.write(
        "Error: hyatlas-memory is not installed in this Python environment.\n"
        "  Install with: pip install -e .   (from the repo root)\n"
        "            or: pip install git+https://github.com/tuancookiez-hub/HyAtlas-Memory.git\n"
    )
    sys.exit(1)


def main() -> None:
    _start.main()


if __name__ == "__main__":
    main()
