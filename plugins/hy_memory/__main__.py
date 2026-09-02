"""Standalone ``python -m plugins.memory.hy_memory`` entry point.

Mirrors the pattern Hindsight uses for hermes_hindsight.memory —
runs the plugin's CLI without needing the parent Hermes binary on PATH.
"""

import sys

from .cli import _main_standalone

if __name__ == "__main__":
    sys.exit(_main_standalone())
