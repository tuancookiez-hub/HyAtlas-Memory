"""In-process log bridge: lets a separate console process subscribe to
the memory layer's log records without touching the file logger.

v1.4.2 introduction. Used by ``hyatlas console`` to render a live
activity ticker in a visible status window. The handler is a no-op
unless ``attach()`` has been called by a console subscriber; if no one
is listening, the package behaves exactly as before.

Design constraints:
  - No dependency on any external IPC layer (no sockets, no named pipes).
    Records travel in-process through a ``queue.Queue`` registered in a
    module-level registry. Cross-process delivery (e.g. the
    ``hyatlas_memory.server.start_server`` subprocess) is intentionally
    out of scope for v1.4.2 — those logs continue to flow to the log
    file that the console tails as a fallback.
  - ``emit()`` must never raise. The handler is on the package's own
    logging path; a crash here would silently break the memory layer.
  - The handler only attaches to the ``hyatlas_memory`` logger (and its
    children), not the root logger, so it cannot capture unrelated
    records from hermes-agent or other libraries.
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Any

_REGISTRY: dict[int, "queue.Queue[logging.LogRecord]"] = {}
_REGISTRY_LOCK = threading.Lock()


def subscribe() -> "queue.Queue[logging.LogRecord]":
    """Return a private queue that will receive every log record from
    the ``hyatlas_memory`` logger tree. Caller is responsible for
    reading and unsubscribing (typically in the console process)."""
    q: "queue.Queue[logging.LogRecord]" = queue.Queue(maxsize=10000)
    with _REGISTRY_LOCK:
        _REGISTRY[id(q)] = q
    return q


def unsubscribe(q: "queue.Queue[logging.LogRecord]") -> None:
    with _REGISTRY_LOCK:
        _REGISTRY.pop(id(q), None)


def _has_subscribers() -> bool:
    with _REGISTRY_LOCK:
        return bool(_REGISTRY)


class MemoryQueueHandler(logging.Handler):
    """Pushes every record onto every subscriber queue.

    Drops the oldest record on a subscriber's queue if the subscriber
    falls behind (maxsize=10000 covers ~30 minutes of typical traffic
    at INFO level). Records are pushed non-blockingly so a stalled
    consumer cannot back-pressure the package's logger.
    """

    def emit(self, record: logging.LogRecord) -> None:
        with _REGISTRY_LOCK:
            subscribers = list(_REGISTRY.values())
        if not subscribers:
            return
        for q in subscribers:
            try:
                q.put_nowait(record)
            except queue.Full:
                try:
                    q.get_nowait()
                except queue.Empty:
                    pass
                try:
                    q.put_nowait(record)
                except queue.Full:
                    pass


_handler_singleton: MemoryQueueHandler | None = None
_handler_lock = threading.Lock()


def install_if_needed(logger: logging.Logger) -> None:
    """Attach the queue handler to ``logger`` if any subscriber exists.

    Safe to call repeatedly: idempotent. Safe to call with no
    subscribers: does nothing. Safe to call from any thread.
    """
    global _handler_singleton
    if not _has_subscribers():
        return
    with _handler_lock:
        if _handler_singleton is None:
            _handler_singleton = MemoryQueueHandler(level=logging.INFO)
        if _handler_singleton not in logger.handlers:
            logger.addHandler(_handler_singleton)
