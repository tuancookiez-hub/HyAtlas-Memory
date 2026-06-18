"""HTTP client for the Hy-Memory server.

Thin wrapper around stdlib urllib — no external dependencies.
Talks to the REST API exposed by ``python -m hy_memory.server``.

Endpoints (from hy_memory/server.py):
    POST /api/v1/add          — write memory
    POST /api/v1/search       — search memories
    GET  /api/v1/memories/:id — get single memory
    POST /api/v1/list         — list memories
    PUT  /api/v1/memories/:id — update memory
    DELETE /api/v1/memories/:id — delete memory
    POST /api/v1/delete_all   — delete all user memories
    GET  /healthz             — health check
    GET  /info                — server info (version)
    GET  /api/v1/status       — deep health (VDB + embed + LLM)
    GET  /api/v1/metrics      — performance metrics
"""

from __future__ import annotations

import contextlib
import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 10
_SEARCH_TIMEOUT = 15
_ADD_TIMEOUT = 60  # LLM extraction can take a while


class HyMemoryClient:
    """HTTP client for a running Hy-Memory server."""

    def __init__(self, base_url: str = "http://127.0.0.1:19527",
                 timeout: int = _DEFAULT_TIMEOUT):
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    # ------------------------------------------------------------------
    # Low-level HTTP
    # ------------------------------------------------------------------

    def _request(self, method: str, path: str, body: dict | None = None,
                 timeout: int | None = None) -> dict:
        """Send an HTTP request and return parsed JSON response."""
        url = f"{self.base_url}{path}"
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")

        req = urllib.request.Request(
            url, data=data, method=method,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            body_text = ""
            with contextlib.suppress(Exception):
                body_text = e.read().decode("utf-8", errors="replace")[:500]
            raise RuntimeError(
                f"Hy-Memory HTTP {e.code} on {method} {path}: {body_text}"
            ) from e
        except urllib.error.URLError as e:
            raise ConnectionError(
                f"Hy-Memory unreachable at {self.base_url}: {e.reason}"
            ) from e

    # ------------------------------------------------------------------
    # Health / Info
    # ------------------------------------------------------------------

    def health(self) -> dict:
        """GET /healthz — quick liveness check."""
        return self._request("GET", "/healthz", timeout=3)

    def info(self) -> dict:
        """GET /info — server version and status."""
        return self._request("GET", "/info", timeout=3)

    def status(self) -> dict:
        """GET /api/v1/status — deep health (VDB + embed + LLM)."""
        return self._request("GET", "/api/v1/status", timeout=30)

    def metrics(self, minutes: int = 5) -> dict:
        """GET /api/v1/metrics?minutes=N."""
        return self._request("GET", f"/api/v1/metrics?minutes={minutes}", timeout=5)

    # ------------------------------------------------------------------
    # Memory CRUD
    # ------------------------------------------------------------------

    def add(self, data: str | list, *, user_id: str = "",
            agent_id: str = "default_agent", session_id: str = "default_session",
            metadata: dict | None = None) -> dict:
        """POST /api/v1/add — write a memory.

        Args:
            data: text string or list of message dicts.
            user_id: primary isolation key.
            agent_id: secondary isolation key.
            session_id: tertiary isolation key.
            metadata: optional extra metadata.

        Returns:
            {"success": True, "memory_id": "...", "request_id": "...", "elapsed_ms": ...}
        """
        body: dict[str, Any] = {"data": data}
        if user_id:
            body["user_id"] = user_id
        if agent_id:
            body["agent_id"] = agent_id
        if session_id:
            body["session_id"] = session_id
        if metadata:
            body["metadata"] = metadata
        return self._request("POST", "/api/v1/add", body, timeout=_ADD_TIMEOUT)

    def search(self, query: str, *, user_ids: list[str] | None = None,
               user_id: str = "", agent_ids: list[str] | None = None,
               session_ids: list[str] | None = None,
               limit: int = 10, min_score: float = 0.4) -> dict:
        """POST /api/v1/search — semantic search across memories.

        Returns:
            {"request_id": "...", "memories": [...], "elapsed_ms": ...}
            Each memory: {"memory_id", "content", "layer", "score", "metadata", ...}
        """
        body: dict[str, Any] = {"query": query}
        if user_ids:
            body["user_ids"] = user_ids
        elif user_id:
            body["user_id"] = user_id
        if agent_ids:
            body["agent_ids"] = agent_ids
        if session_ids:
            body["session_ids"] = session_ids
        if limit:
            body["limit"] = limit
        if min_score:
            body["min_score"] = min_score
        return self._request("POST", "/api/v1/search", body, timeout=_SEARCH_TIMEOUT)

    def get(self, memory_id: str) -> dict | None:
        """GET /api/v1/memories/:id — get single memory by ID."""
        try:
            return self._request("GET", f"/api/v1/memories/{memory_id}", timeout=5)
        except RuntimeError as e:
            if "404" in str(e):
                return None
            raise

    def list_memories(self, *, user_id: str = "", agent_id: str = "",
                      limit: int = 50, offset: int = 0) -> dict:
        """POST /api/v1/list — list memories."""
        body: dict[str, Any] = {"limit": limit, "offset": offset}
        if user_id:
            body["user_id"] = user_id
        if agent_id:
            body["agent_id"] = agent_id
        return self._request("POST", "/api/v1/list", body, timeout=10)

    def update(self, memory_id: str, content: str) -> dict:
        """PUT /api/v1/memories/:id — update memory content."""
        return self._request(
            "PUT", f"/api/v1/memories/{memory_id}",
            {"content": content}, timeout=10,
        )

    def delete(self, memory_id: str) -> dict:
        """DELETE /api/v1/memories/:id — delete a single memory."""
        return self._request("DELETE", f"/api/v1/memories/{memory_id}", timeout=5)

    def delete_all(self, *, user_id: str = "",
                   agent_ids: list[str] | None = None) -> dict:
        """POST /api/v1/delete_all — delete all memories for a user."""
        body: dict[str, Any] = {}
        if user_id:
            body["user_id"] = user_id
        if agent_ids:
            body["agent_ids"] = agent_ids
        return self._request("POST", "/api/v1/delete_all", body, timeout=10)

    # ------------------------------------------------------------------
    # Connectivity
    # ------------------------------------------------------------------

    def is_reachable(self) -> bool:
        """Quick check — can we hit /healthz?"""
        try:
            self.health()
            return True
        except Exception:
            return False

    def wait_until_ready(self, timeout: int = 60, interval: float = 1.0) -> bool:
        """Poll /healthz until the server responds or timeout.

        Returns True if ready, False if timed out.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                result = self.health()
                if result.get("status") == "ok":
                    return True
            except Exception:
                pass
            time.sleep(interval)
        return False
