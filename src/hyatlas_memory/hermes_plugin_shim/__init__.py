"""Hermes plugin shim — directory-based plugin wrapper for hyatlas-memory.

This file is copied into ``HERMES_HOME/plugins/hy_memory/__init__.py`` by
``hyatlas setup hermes``. Hermes' memory loader only scans directory-based
plugins; it does not use pip entry points. This thin shim bridges the gap:
when Hermes loads the plugin directory, it imports the real HyMemoryProvider
from the installed ``hyatlas_memory`` package and registers it.
"""

from __future__ import annotations

import logging

from hyatlas_memory import HyMemoryProvider

logger = logging.getLogger(__name__)


def register(ctx) -> None:
    """Register HyAtlas-Memory as the active memory provider."""
    ctx.register_memory_provider(HyMemoryProvider())
