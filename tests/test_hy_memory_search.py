"""Regression tests for the Hy-Memory memory provider plugin.

Locked-in: layered server response shape (v1.2+: ``{"profile": [...],
"proactive": [...], "normal": [...]}``) must be flattened correctly by both
the on-demand ``hy_memory_search`` tool and the proactive prefetch
formatter. The empty-but-truthy-dict must NOT crash on ``str.get()``.

See: bug "Hy-Memory search returns 'str' object has no attribute 'get'"
where the plugin iterated the layered dict as a flat list.
"""

import json
from unittest.mock import MagicMock

import pytest

from hyatlas_memory import HyMemoryProvider, _format_memories

# Skip entire module if hermes-agent isn't installed (CI)
pytestmark = pytest.mark.skipif(
    True,  # will be replaced by conftest check at collection time
    reason="hermes-agent not installed"
)

# Re-evaluate at import time: if hermes-agent modules are available, run tests
try:
    import agent.memory_provider  # noqa: F401
    pytestmark = pytest.mark.skipif(False, reason="hermes-agent available")
except ImportError:
    pass

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_provider(search_return):
    """Provider with a mock client returning a fixed search payload."""
    p = HyMemoryProvider()
    p._client = MagicMock()
    p._client.search.return_value = search_return
    p._user_id = "test-user"
    p._agent_id = "test-agent"
    return p


# Layered shape — what the live v1.2.16 server returns
LAYERED_EMPTY = {
    "request_id": "abc-123",
    "memories": {"profile": [], "proactive": [], "normal": []},
    "elapsed_ms": 4049.85,
}

LAYERED_MIXED = {
    "request_id": "abc-124",
    "memories": {
        "profile": [
            {"content": "User is Tuan", "score": 0.91, "layer": "profile"},
            {"content": "Uses Hermes Agent", "score": 0.87},  # no layer field
        ],
        "proactive": [
            {"content": "Prefers direct action", "score": 0.83, "layer": "proactive"},
        ],
        "normal": [
            {"content": "Working on herm build", "score": 0.72, "layer": "normal"},
        ],
    },
}

FLAT_LIST = {
    "request_id": "abc-125",
    "memories": [
        {"content": "legacy flat item", "score": 0.6, "layer": "normal"},
    ],
}


# ---------------------------------------------------------------------------
# hy_memory_search tool — the bug we are guarding against
# ---------------------------------------------------------------------------


class TestToolSearch:
    def test_layered_empty_does_not_crash(self):
        """Empty layered dict must return the empty-result sentinel, not raise.

        Regression: previously the truthy-but-empty ``{"profile": [], ...}``
        dict bypassed the ``if not memories`` guard, then ``for m in memories``
        iterated string keys, and ``m.get("content", "")`` raised
        ``AttributeError: 'str' object has no attribute 'get'``.
        """
        p = _make_provider(LAYERED_EMPTY)
        out = json.loads(p._tool_search({"query": "anything", "limit": 5}))
        assert out == {"memories": [], "note": "No relevant memories found"}

    def test_layered_mixed_flattens_preserving_layer(self):
        p = _make_provider(LAYERED_MIXED)
        out = json.loads(p._tool_search({"query": "user", "limit": 5}))

        assert "memories" in out
        assert len(out["memories"]) == 4

        # First item keeps its server-set layer
        assert out["memories"][0] == {
            "content": "User is Tuan",
            "layer": "profile",
            "score": 0.91,
        }
        # Item missing "layer" inherits the bucket name
        assert out["memories"][1] == {
            "content": "Uses Hermes Agent",
            "layer": "profile",
            "score": 0.87,
        }
        # Cross-layer ordering: profile → proactive → normal
        layers = [m["layer"] for m in out["memories"]]
        assert layers == ["profile", "profile", "proactive", "normal"]

    def test_flat_list_back_compat(self):
        """Older server versions / other providers return a flat list."""
        p = _make_provider(FLAT_LIST)
        out = json.loads(p._tool_search({"query": "x", "limit": 5}))
        assert out["memories"] == [
            {"content": "legacy flat item", "layer": "normal", "score": 0.6}
        ]

    def test_memories_key_missing_treated_as_empty(self):
        p = _make_provider({"request_id": "x", "elapsed_ms": 1.0})  # no "memories" key
        out = json.loads(p._tool_search({"query": "x"}))
        assert out["memories"] == []
        assert "note" in out

    def test_score_is_rounded(self):
        p = _make_provider({
            "memories": {
                "normal": [{"content": "x", "score": 0.123456, "layer": "normal"}],
            }
        })
        out = json.loads(p._tool_search({"query": "x"}))
        assert out["memories"][0]["score"] == 0.123  # 3-decimal rounding

    def test_query_required(self):
        p = _make_provider(LAYERED_EMPTY)
        out = json.loads(p._tool_search({"query": "", "limit": 5}))
        assert "error" in out
        assert "query" in out["error"]

    def test_underlying_client_called_with_user_and_agent(self):
        """Confirms the wrapper still threads user/agent ids correctly."""
        p = _make_provider(LAYERED_EMPTY)
        p._tool_search({"query": "hello", "limit": 7})
        p._client.search.assert_called_once_with(
            "hello",
            user_ids=["test-user"],
            agent_ids=["test-agent"],
            limit=7,
        )


# ---------------------------------------------------------------------------
# _format_memories — used by the proactive prefetch path
# ---------------------------------------------------------------------------


class TestFormatMemories:
    def test_layered_empty_returns_empty_string(self):
        assert _format_memories({"profile": [], "proactive": [], "normal": []}) == ""

    def test_layered_full(self):
        out = _format_memories(LAYERED_MIXED["memories"])
        # OpenClaw 0.2.7 format: outer block wrapped in
        # <relevant-memories>...</relevant-memories> with a header line.
        # Each memory is "- [N] <time>  <content>". Scores are no longer
        # in the rendered block (consumed in metadata, not prompt). Verified
        # 2026-06-16: the test was originally written for the pre-0.2.7
        # format that included "[<layer>] <content> (score: X.XX)"; updated
        # to match the current output. Layer ordering is preserved by
        # _flatten_memories (profile -> proactive -> normal).
        assert out.startswith("<relevant-memories>")
        assert out.endswith("</relevant-memories>")
        assert "User is Tuan" in out
        assert "Prefers direct action" in out
        assert "Working on herm build" in out
        assert "Uses Hermes Agent" in out

    def test_flat_list(self):
        out = _format_memories([{"content": "hi", "score": 0.5, "layer": "normal"}])
        # OpenClaw 0.2.7 format: no score in the rendered block.
        assert out == (
            "<relevant-memories>\n"
            "The following are stored memories for the current user. "
            "Use them to personalize your response. Memories with evolution "
            "chains are expanded from oldest to newest:\n"
            "- [1] hi\n"
            "</relevant-memories>"
        )

    def test_handles_none_items_in_layer(self):
        """Defensive: skip non-dict items that might sneak in."""
        out = _format_memories({
            "profile": [None, {"content": "ok", "score": 0.9, "layer": "profile"}],
        })
        assert "ok" in out
        assert "None" not in out

    def test_empty_input(self):
        assert _format_memories({}) == ""
        assert _format_memories([]) == ""
        assert _format_memories(None) == ""
