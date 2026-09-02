"""HTTP client for the HyAtlas v4 Go server.

Wire-compatible with the v3.5 ``HyMemoryClient`` interface that
Hermes already imports. Same endpoints, same JSON shapes. Port is
the only difference (19528 for v4, 19527 for v3.5).
"""

from __future__ import annotations

import json
import logging
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class HyatlasClientError(Exception):
    """Base exception for client errors."""


class HyatlasUnreachable(HyatlasClientError):
    """Server is not reachable on the configured host:port."""


class HyatlasClient:
    """Thin HTTP client for the v4 server's /api/v1/* endpoints."""

    def __init__(self, base_url: str = "http://127.0.0.1:19528",
                 timeout: float = 15.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._opener = urllib.request.build_opener()

    # ------------------------------------------------------------------
    # Low-level HTTP
    # ------------------------------------------------------------------

    def _request(
        self, method: str, path: str, body: Optional[Dict[str, Any]] = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        data = None
        headers = {"Accept": "application/json", "User-Agent": "hermes-hy_memory/4.0"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with self._opener.open(req, timeout=self.timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            raw = e.read()
            try:
                payload = json.loads(raw)
            except (json.JSONDecodeError, ValueError):
                payload = {"error": raw.decode("utf-8", errors="replace")[:200]}
            raise HyatlasClientError(
                f"{method} {path} -> HTTP {e.code}: {payload.get('error', payload)}"
            ) from None
        except urllib.error.URLError as e:
            raise HyatlasUnreachable(
                f"{method} {path} failed: {e.reason}"
            ) from None
        except OSError as e:
            raise HyatlasUnreachable(f"{method} {path} failed: {e}") from None

        if not raw:
            return {}
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return raw

    def _get(self, path: str) -> Any:
        return self._request("GET", path)

    def _post(self, path: str, body: Dict[str, Any]) -> Any:
        return self._request("POST", path, body)

    def _delete(self, path: str, body: Dict[str, Any]) -> Any:
        return self._request("DELETE", path, body)

    # ------------------------------------------------------------------
    # Convenience: connect probe
    # ------------------------------------------------------------------

    def is_reachable(self) -> bool:
        """True iff /healthz returns 200 — no exception thrown."""
        try:
            resp = self._get("/healthz")
            return isinstance(resp, dict) and resp.get("status") == "ok"
        except (HyatlasUnreachable, HyatlasClientError):
            return False
        except Exception as e:
            logger.debug("is_reachable unexpected error: %s", e)
            return False

    def wait_until_reachable(self, timeout: float = 30.0,
                            interval: float = 0.5) -> bool:
        """Block until the server is reachable or timeout elapses."""
        import time
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.is_reachable():
                return True
            time.sleep(interval)
        return False

    def close(self) -> None:
        """No-op: urllib's build_opener is stateless."""
        pass

    # ------------------------------------------------------------------
    # v4 server endpoints (1:1 with the Go server's /api/v1/* surface)
    # ------------------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        return self._get("/api/v1/status")

    def add(
        self,
        text: str,
        user_id: str = "",
        agent_id: str = "",
        session_id: str = "",
        metadata: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "text": text,
            "user_id": user_id,
            "agent_id": agent_id,
            "session_id": session_id,
        }
        if metadata:
            body["metadata"] = metadata
        return self._post("/api/v1/add", body)

    def search(
        self,
        query: str,
        user_id: str = "",
        agent_id: str = "",
        layer: str = "",
        limit: int = 10,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "query": query,
            "user_id": user_id,
            "agent_id": agent_id,
            "limit": limit,
        }
        if layer:
            body["layer"] = layer
        return self._post("/api/v1/search", body)

    def list_memories(
        self,
        user_id: str = "",
        agent_id: str = "",
        layer: str = "",
        limit: int = 50,
        offset: int = 0,
        include_raw: bool = False,
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "user_id": user_id,
            "agent_id": agent_id,
            "limit": limit,
            "offset": offset,
            "include_raw": include_raw,
        }
        if layer:
            body["layer"] = layer
        return self._post("/api/v1/list", body)

    def delete_all(
        self,
        user_id: str = "",
        agent_id: str = "",
        layer: str = "",
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "user_id": user_id,
            "agent_id": agent_id,
        }
        if layer:
            body["layer"] = layer
        return self._post("/api/v1/delete_all", body)

    def graph(self, node: str = "", user_id: str = "") -> Dict[str, Any]:
        path = "/api/v1/graph"
        params = []
        if node:
            params.append(f"node={urllib.parse.quote(node)}")
        if user_id:
            params.append(f"user_id={urllib.parse.quote(user_id)}")
        if params:
            path += "?" + "&".join(params)
        return self._get(path)

    def metrics(self) -> Dict[str, Any]:
        return self._get("/api/v1/metrics")

    def digest(self, user_id: str = "") -> Dict[str, Any]:
        body: Dict[str, Any] = {"user_id": user_id}
        return self._post("/api/v1/digest", body)

    def reprocess(self, user_id: str = "") -> Dict[str, Any]:
        body: Dict[str, Any] = {"user_id": user_id}
        return self._post("/api/v1/reprocess", body)
