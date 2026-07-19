"""
DisabledCache — no-op cache backend for HyAtlas-Memory v3.0.0.

All operations are instant no-ops. Suitable for single-user local systems
where Qdrant (sub-5ms) and Kuzu (sub-1ms) make read caching unnecessary.

The System2 task queue is non-functional with this backend — HyAtlas uses
direct function calls instead of the queue pattern (see system2_writer.py).

All methods accept *args, **kwargs to be compatible with any caller pattern,
including callers that pass extra kwargs the base class doesn't declare.
"""

import logging
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from .cache_base import CacheBase

logger = logging.getLogger(__name__)


class DisabledCache(CacheBase):
    """No-op cache. All reads return None/empty, all writes are silently dropped."""

    def __init__(self, config=None):
        self._config = config

    async def initialize(self) -> None:
        logger.info("DisabledCache: cache disabled (no-op mode)")

    async def close(self) -> None:
        pass

    # ── KV ──
    async def _get(self, key: str) -> str | None:
        return None

    async def _set(self, key: str, value: str, ttl: int = 0) -> None:
        pass

    async def _delete(self, key: str) -> None:
        pass

    async def _delete_pattern(self, pattern: str) -> None:
        pass

    # ── Profile ──
    async def get_profile(self, isolation_key: str) -> Any | None:
        return None

    async def update_profile_cache(self, node: Any) -> None:
        pass

    # ── Node cache ──
    async def get_node(self, node_id: str) -> Any | None:
        return None

    async def cache_node(self, node: Any) -> None:
        pass

    async def invalidate_node(self, node_id: str) -> None:
        pass

    async def invalidate_all(self, isolation_key: str) -> None:
        pass

    # ── Intention Queue ──
    async def push_intention(self, intention: Any) -> None:
        pass

    async def check_intentions(self, *args, **kwargs) -> list[Any]:
        return []

    async def mark_intention_triggered(self, intention_id: str) -> None:
        pass

    # ── Gap Map ──
    async def get_gap_map(self, isolation_key: str) -> list[Any] | None:
        return None

    async def set_gap_map(self, isolation_key: str, gaps: list[Any]) -> None:
        pass

    async def invalidate_gap_map(self, isolation_key: str) -> None:
        pass

    # ── System2 task queue ──
    async def enqueue_system2_task(self, *args, **kwargs) -> str:
        return str(uuid.uuid4())

    async def get_active_isolation_keys(self) -> list[str]:
        return []

    async def dequeue_system2_task(self, *args, **kwargs) -> dict[str, Any] | None:
        return None

    async def get_system2_queue_length(self, *args, **kwargs) -> int:
        return 0

    # ── Task status ──
    async def get_task_status(self, task_id: str) -> dict[str, Any] | None:
        return None

    async def get_task_statuses(self, task_ids: list[str]) -> list[dict[str, Any]]:
        return []

    async def update_task_status(self, *args, **kwargs) -> bool:
        return True

    # ── Session state ──
    async def get_session_state(self, session_id: str) -> dict[str, Any] | None:
        return None

    async def set_session_state(self, session_id: str, state: dict[str, Any]) -> None:
        pass

    # ── Stats ──
    async def get_stats(self) -> dict[str, Any]:
        return {"backend": "disabled", "cache_hits": 0, "cache_misses": 0, "total_entries": 0}

    # ── Write records ──
    async def store_write_record(self, *args, **kwargs) -> bool:
        return True

    async def get_write_record(self, request_id: str) -> dict[str, Any] | None:
        return None

    async def store_memory_operation(self, *args, **kwargs) -> bool:
        return True

    async def get_memory_operations(self, *args, **kwargs) -> list[dict[str, Any]]:
        return []

    async def store_pipeline_log(self, *args, **kwargs) -> bool:
        return True

    async def get_pipeline_logs(self, *args, **kwargs) -> list[dict[str, Any]]:
        return []

    # ── Metrics (shims — not in original CacheBase but called by background tasks) ──
    async def store_metrics_minute(self, *args, **kwargs) -> None:
        pass

    async def store_metrics(self, *args, **kwargs) -> None:
        pass

    async def flush_metrics(self, *args, **kwargs) -> None:
        pass

    async def cleanup_old_metrics(self, *args, **kwargs) -> None:
        # MetricsCollector._cleanup_loop calls this hourly. No-op when cache is disabled.
        pass
