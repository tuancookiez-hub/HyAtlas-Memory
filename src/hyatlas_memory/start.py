"""CLI entry point: ``hyatlas``, ``hyatlas --stop``, ``hyatlas --status``.

Delegates to :mod:`hyatlas_memory._start`, which contains the actual
startup logic bundled inside the package. This thin wrapper exists so
the ``[project.scripts]`` entry point in pyproject.toml resolves to a
module the user can also import directly (``python -m
hyatlas_memory.start``).

Project root resolution (env var, cwd, or editable install) is handled
inside ``_start.main()`` — see that module for details.
"""

from __future__ import annotations

from hyatlas_memory import _start


def main() -> None:
    _start.main()


if __name__ == "__main__":
    main()
