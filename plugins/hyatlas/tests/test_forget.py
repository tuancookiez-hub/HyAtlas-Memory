"""Tests for mirroring Hermes memory replace and remove as a reversible forget.

Replace and remove forget ``metadata["previous_content"]`` on the server. Replace then
adds the new content as add does. Needs ``agent.memory_provider`` (Hermes) and is
skipped when it is absent.

    python -m pytest plugins/hyatlas/tests/test_forget.py -q
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import threading

import pytest

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

pytest.importorskip("agent.memory_provider")

_HERE = os.path.dirname(os.path.abspath(__file__))
_PKG = os.path.dirname(_HERE)


def _load_plugin_module():
    """Load the plugin package's __init__.py with its relative imports working."""
    name = "hyatlas_forget"
    spec = importlib.util.spec_from_file_location(
        name,
        os.path.join(_PKG, "__init__.py"),
        submodule_search_locations=[_PKG],
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load_plugin_module()


class _Recorder:
    """A client stand-in that records each add and forget call."""

    def __init__(self, fail_forget: bool = False) -> None:
        self.adds = []
        self.forgets = []
        self.fail_forget = fail_forget

    def add(self, text, user_id="", agent_id="", session_id="", metadata=None):
        self.adds.append({"text": text, "metadata": metadata})
        return {"success": True}

    def forget(self, text, user_id="", agent_id="", reason=""):
        if self.fail_forget:
            raise RuntimeError("server down")
        self.forgets.append({"text": text, "user_id": user_id, "agent_id": agent_id, "reason": reason})
        return {"forgotten": 1, "derived": 0}


def _provider(client: _Recorder):
    p = mod.HyatlasMemoryProvider()
    p._client = client
    p._user_id = "u"
    p._agent_id = "a"
    return p


def test_replace_forgets_previous_then_adds_new():
    c = _Recorder()
    _provider(c).on_memory_write(
        "replace", "memory", "lives in Lisbon", {"previous_content": "lives in Porto"}
    )
    assert c.forgets == [{
        "text": "lives in Porto", "user_id": "u", "agent_id": "a",
        "reason": "replaced with Hermes's memory tool",
    }]
    assert len(c.adds) == 1
    assert c.adds[0]["text"] == "lives in Lisbon"
    assert "previous_content" not in c.adds[0]["metadata"]
    assert c.adds[0]["metadata"]["write_origin"] == "memory_tool"


def test_remove_forgets_previous_and_adds_nothing():
    c = _Recorder()
    _provider(c).on_memory_write("remove", "memory", "", {"previous_content": "lives in Porto"})
    assert [f["text"] for f in c.forgets] == ["lives in Porto"]
    assert c.adds == []


def test_remove_without_previous_calls_nothing():
    c = _Recorder()
    p = _provider(c)
    p.on_memory_write("remove", "memory", "y")
    p.on_memory_write("remove", "memory", "y", {"previous_content": "   "})
    assert c.forgets == []
    assert c.adds == []


def test_replace_without_previous_still_adds_new():
    c = _Recorder()
    _provider(c).on_memory_write("replace", "memory", "new fact")
    assert c.forgets == []
    assert [a["text"] for a in c.adds] == ["new fact"]


def test_replace_adds_new_even_when_forget_fails():
    c = _Recorder(fail_forget=True)
    p = _provider(c)
    p.on_memory_write("replace", "memory", "new fact", {"previous_content": "old fact"})
    assert [a["text"] for a in c.adds] == ["new fact"]


def test_client_forget_posts_body_to_forget_route():
    seen = {}

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            seen["path"] = self.path
            seen["body"] = json.loads(self.rfile.read(n) or b"{}")
            out = json.dumps({"forgotten": 1, "derived": 2}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *args):
            pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        client = mod.HyatlasClient(base_url=f"http://127.0.0.1:{srv.server_address[1]}", timeout=5.0)
        out = client.forget("old fact", user_id="u", agent_id="a", reason="why")
    finally:
        srv.shutdown()
        srv.server_close()
    assert seen["path"] == "/api/v1/forget"
    assert seen["body"] == {"text": "old fact", "user_id": "u", "agent_id": "a", "reason": "why"}
    assert out == {"forgotten": 1, "derived": 2}
