# -*- coding: utf-8 -*-
"""
Entry point for ``python -m plugins.memory.hy_memory`` (standalone CLI).

For most users, the right invocation is via the parent hermes CLI:
    hermes hy-memory doctor
    hermes hy-memory add "..."
    hermes hy-memory search "..."
    hermes hy-memory list

This __main__ is here for the rare case where you want to run the CLI
without going through hermes — e.g. for debugging, or in a subprocess
that doesn't have hermes on PATH.
"""

import sys

from .cli import _main_standalone

if __name__ == "__main__":
    sys.exit(_main_standalone())
