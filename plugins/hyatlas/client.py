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
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


def _read_plugin_version() -> str:
    """Return the version from the ``version:`` line of plugin.yaml beside this package.

    The file is parsed as plain text so the client needs no YAML dependency. The
    result is "unknown" when the file or the line is missing, so a damaged install
    still imports.
    """
    try:
        text = (Path(__file__).resolve().parent / "plugin.yaml").read_text(encoding="utf-8")
    except (OSError, ValueError):
        return "unknown"
    for line in text.splitlines():
        if line.startswith("version:"):
            value = line.split(":", 1)[1].strip().strip("\"'")
            return value or "unknown"
    return "unknown"


# The plugin version, read once at import so the provider and the User-Agent agree.
PLUGIN_VERSION = _read_plugin_version()


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
        # An empty ProxyHandler stops urllib from sending loopback calls through
        # HTTP_PROXY or HTTPS_PROXY, which Python does not exempt for 127.0.0.1.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    # ------------------------------------------------------------------
    # Low-level HTTP
    # ------------------------------------------------------------------

    def _request(
        self, method: str, path: str, body: Optional[Dict[str, Any]] = None,
    ) -> Any:
        url = f"{self.base_url}{path}"
        data = None
        headers = {"Accept": "application/json", "User-Agent": "hermes-hyatlas/" + PLUGIN_VERSION}
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

    def forget(
        self,
        text: str,
        user_id: str = "",
        agent_id: str = "",
        reason: str = "",
    ) -> Dict[str, Any]:
        """Retire the memory-tool rows that match text, and what was derived from them.

        The server marks them as forgotten, which keeps them restorable. Returns
        ``forgotten`` (raw rows retired) and ``derived`` (rows derived from them).
        """
        body: Dict[str, Any] = {
            "text": text,
            "user_id": user_id,
            "agent_id": agent_id,
            "reason": reason,
        }
        return self._post("/api/v1/forget", body)

    def search(
        self,
        query: str,
        user_id: str = "",
        agent_id: str = "",
        layer: str = "",
        limit: int = 10,
        session_id: str = "",
    ) -> Dict[str, Any]:
        body: Dict[str, Any] = {
            "query": query,
            "user_id": user_id,
            "agent_id": agent_id,
            "limit": limit,
        }
        if layer:
            body["layer"] = layer
        if session_id:
            # The server ranks this session's intentions above other sessions'.
            body["session_id"] = session_id
        return self._post("/api/v1/search", body)

    def feedback(
        self,
        recalled_ids: List[str],
        reply: str,
        user_id: str = "",
        agent_id: str = "",
        query: str = "",
    ) -> Dict[str, Any]:
        """Tell the server which recalled memories the agent's reply actually used.

        The server compares the reply with what it recalled, so a memory that was
        shown and ignored can be told apart from one that was read. The call is
        advisory; callers decide whether a failure matters.
        """
        body: Dict[str, Any] = {
            "recalled_ids": list(recalled_ids),
            "reply": reply,
            "user_id": user_id,
            "agent_id": agent_id,
            # The words of the question do not count as use: a reply repeats them.
            "query": query,
        }
        return self._post("/api/v1/feedback", body)

    def _server_mode(self) -> str:
        """The server's mode from /api/v1/status ("lite", "pro", "ultra"), or ""."""
        try:
            return str((self.status() or {}).get("mode") or "")
        except HyatlasClientError:
            return ""

    def list_memories(
        self,
        user_id: str = "",
        agent_id: str = "",
        layer: str = "",
        limit: int = 50,
        offset: int = 0,
        include_raw: Optional[bool] = None,
    ) -> Dict[str, Any]:
        """List memories. ``include_raw=None`` picks the default for the server's mode.

        Lite stores only L2 raw rows, so a list that excludes raw is always empty
        there, and lite includes them by default. Other modes exclude raw by
        default. An explicit True or False always wins. The mode is read with one
        /api/v1/status call per invocation.
        """
        if include_raw is None:
            include_raw = self._server_mode() == "lite"
        body: Dict[str, Any] = {
            "user_id": user_id,
            "agent_id": agent_id,
            "limit": limit,
            "offset": offset,
            "include_raw": bool(include_raw),
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
