"""CLI entry point: ``hyatlas``, ``hyatlas start``, ``hyatlas stop``.

Delegates to :mod:`hyatlas_memory._cli`, which implements the unified
subcommand parser. This thin wrapper exists so the ``[project.scripts]``
entry point in pyproject.toml resolves to a module the user can also
import directly (``python -m hyatlas_memory.start``).
"""

from __future__ import annotations

import sys

from hyatlas_memory import _cli


def main() -> None:
    sys.exit(_cli.main())


if __name__ == "__main__":
    sys.exit(main())
