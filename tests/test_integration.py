"""Integration tests for the full HyAtlas-Memory stack.

These tests exercise the real local services (zvec in-process vector
store plus the upstream hy-memory server on port 19527). They are gated
by pytest markers so they only run when the stack is actually up.

Run selectively:
    python -m pytest -m integration

Run all including unit tests:
    python -m pytest

Preconditions:
    - HyAtlas memory server running on 127.0.0.1:19527
    - The hermes-agent package installed (for agent.memory_provider imports)
"""

from __future__ import annotations

import json
import os
import socket
import time
import urllib.request
import uuid

import pytest

os.environ["HY_MEMORY_WRITE_TURN_WINDOW"] = "1"

pytest.importorskip("agent.memory_provider", reason="hermes-agent not installed")
hm = pytest.importorskip("hyatlas_memory")
HyMemoryProvider = hm.HyMemoryProvider


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _is_port_open(host: str, port: int, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _check_stack() -> tuple[bool, str]:
    # v3.4+: zvec is in-process; stack readiness is just the upstream server.
    if not _is_port_open("127.0.0.1", 19527):
        return False, "Upstream hy-memory server not reachable on 127.0.0.1:19527"
    return True, ""


integration = pytest.mark.integration

requires_stack = pytest.mark.skipif(
    not _check_stack()[0],
    reason=_check_stack()[1],
)

# Legacy: pre-zvec integration tests imported direct Qdrant HTTP calls against
# :6333 to inspect raw payload fields (importance, access_count). With zvec
# in-process there is no equivalent admin HTTP endpoint; rewriting these
# tests against the new storage layer is non-trivial. Mark them with
# `requires_qdrant` so anyone running `-m integration` without a Qdrant
# sidecar gets a clear skip instead of a network error.
_QDRANT_PORT = 6333
requires_qdrant = pytest.mark.skipif(
    not _is_port_open("127.0.0.1", _QDRANT_PORT),
    reason=f"Qdrant sidecar not reachable on 127.0.0.1:{_QDRANT_PORT} (legacy test)",
)


_QDRANT_COLLECTION = os.environ.get("HYATLAS_QDRANT_COLLECTION", "agent_memories_1024")


def _qdrant_request(path: str, body: dict | None = None, method: str = "GET") -> dict:
    url = f"http://127.0.0.1:{_QDRANT_PORT}/collections/{_QDRANT_COLLECTION}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.loads(resp.read().decode())


def _cleanup_user(user_id: str) -> None:
    """Delete all qdrant points owned by a test user."""
    try:
        scroll = _qdrant_request(
            "/points/scroll",
            {
                "filter": {"must": [{"key": "user_id", "match": {"value": user_id}}]},
                "limit": 1000,
                "with_payload": False,
                "with_vectors": False,
            },
            method="POST",
        )
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return
        raise

    points = scroll.get("result", {}).get("points", [])
    if not points:
        return
    ids = [p["id"] for p in points]
    _qdrant_request(
        "/points/delete",
        {"points": ids},
        method="POST",
    )


# ---------------------------------------------------------------------------
# Integration tests
# ---------------------------------------------------------------------------


@integration
@requires_stack
class TestMemoryLifecycle:
    def setup_method(self):
        self.user_id = f"integration-test-{uuid.uuid4().hex[:8]}"
        self.session_id = f"session-{uuid.uuid4().hex[:8]}"
        self.provider = HyMemoryProvider()
        self.provider.initialize(
            session_id=self.session_id,
            user_id=self.user_id,
            agent_identity="integration-test-agent",
        )
        self.provider._write_turn_window = 1
        assert self.provider._client is not None
        assert self.provider._client.is_reachable()

    def teardown_method(self):
        # Cleanup runs only when the qdrant sidecar is reachable; otherwise
        # the user/agent_id points would never have been persisted there.
        if _is_port_open("127.0.0.1", _QDRANT_PORT):
            _cleanup_user(self.user_id)

    def test_sync_turn_writes_searchable_memory(self):
        """End-to-end: write a turn, wait for indexing, then recall it."""
        self.provider.sync_turn(
            user_content="My favorite color is teal.",
            assistant_content="Noted, your favorite color is teal.",
            session_id=self.session_id,
        )

        # Allow the upstream server to process and index the write.
        time.sleep(15)

        result = self.provider._client.search(
            "favorite color",
            user_ids=[self.user_id],
            limit=5,
        )
        memories = self.provider._flatten_memories(result.get("memories"))
        assert len(memories) >= 1, f"Expected at least one memory, got {len(memories)}"
        contents = " ".join(str(m.get("content", "")).lower() for m in memories)
        assert "teal" in contents, f"'teal' not found in recalled memories: {contents}"

    def test_importance_and_access_count_are_populated(self):
        """The 4-factor scorer fields should be present on new memories.

        The upstream reconciles l1_raw - l4_identity asynchronously, which is
        when ``memory_id`` first gets populated. Then the importance PATCH
        runs in a fire-and-forget daemon thread. So we poll up to 60s for
        the reconciled form, then poll again for the importance patch to
        land. On a loaded box (3k+ existing memories) the original 15s
        fixed sleep wasn't enough.

        Legacy: reads importance/access_count via direct Qdrant admin HTTP
        (no equivalent zvec admin endpoint yet); skipped when Qdrant is not
        running. Marked `requires_qdrant`.
        """
        self.provider.sync_turn(
            user_content="I prefer concise answers.",
            assistant_content="Got it.",
            session_id=self.session_id,
        )

        # Poll for the upstream to reconcile l1_raw -> a layer with memory_id.
        deadline = time.time() + 60
        memories: list[dict] = []
        while time.time() < deadline:
            result = self.provider._client.search(
                "concise answers",
                user_ids=[self.user_id],
                limit=5,
            )
            memories = [
                m
                for m in self.provider._flatten_memories(result.get("memories"))
                if m.get("memory_id")
            ]
            if memories:
                break
            time.sleep(2)
        assert memories, (
            "Upstream never reconciled l1_raw into a memory with a memory_id "
            f"within 60s; last search returned {[m.get('layer') for m in self.provider._flatten_memories(result.get('memories'))]}"
        )

        # Formatting triggers the access_count increment thread.
        self.provider._format_memories_for_prompt(memories)

        for m in memories:
            mid = m["memory_id"]

            # Poll for importance/access_count to land (fire-and-forget patch).
            payload: dict = {}
            patch_deadline = time.time() + 60
            while time.time() < patch_deadline:
                point = _qdrant_request(f"/points/{mid}")
                payload = point["result"]["payload"]
                if (
                    payload.get("importance") is not None
                    and payload.get("access_count") is not None
                    and payload.get("layer") is not None
                ):
                    break
                time.sleep(2)

            layer = payload.get("layer")
            importance = payload.get("importance")
            access_count = payload.get("access_count")

            assert layer is not None, f"Missing layer for {mid}"
            assert importance is not None, f"Missing importance for {mid}"
            assert access_count is not None, f"Missing access_count for {mid}"
            assert importance in {0.3, 0.5, 0.6, 0.8, 1.0}, (
                f"Unexpected importance {importance} for {mid}"
            )

    def test_prefetch_returns_formatted_block(self):
        """The proactive prefetch path should produce a non-empty prompt block."""
        self.provider.sync_turn(
            user_content="My name is IntegrationTest.",
            assistant_content="Hello.",
            session_id=self.session_id,
        )
        time.sleep(15)

        block = self.provider.prefetch(
            query="who am i",
            session_id=self.session_id,
        )
        assert isinstance(block, str)
        assert "<relevant-memories>" in block
        assert "IntegrationTest" in block or "integrationtest" in block.lower()

    def test_tool_search_returns_layered_shape(self):
        """The on-demand tool search should return the modern layered response."""
        self.provider.sync_turn(
            user_content="I work on AI memory systems.",
            assistant_content="Interesting.",
            session_id=self.session_id,
        )
        time.sleep(15)

        out = self.provider._tool_search({"query": "AI memory systems", "limit": 5})
        parsed = json.loads(out)
        assert "memories" in parsed
        assert isinstance(parsed["memories"], list)
        for m in parsed["memories"]:
            assert "content" in m
            assert "layer" in m
