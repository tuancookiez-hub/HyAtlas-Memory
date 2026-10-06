"""Unit tests for the HyAtlas v4 plugin — no live server required.

Runs against a real localhost HTTP server that speaks the v4 wire
contract (no mocks), plus tmpdir HERMES_HOME fixtures for config
precedence. Run with:

    python -m pytest plugins/hyatlas/tests/ -q

or from the package root:

    python -m pytest tests/ -q
"""

from __future__ import annotations

import importlib.util
import json
import os
import socket
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

_HERE = os.path.dirname(os.path.abspath(__file__))
_PKG = os.path.dirname(_HERE)


def _load_plugin_module():
    spec = importlib.util.spec_from_file_location(
        "hyatlas_unit",
        os.path.join(_PKG, "__init__.py"),
        submodule_search_locations=[_PKG],
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["hyatlas_unit"] = mod
    spec.loader.exec_module(mod)
    return mod


mod = _load_plugin_module()
HyatlasClient = mod.HyatlasClient
HyatlasClientError = mod.HyatlasClientError
HyatlasUnreachable = mod.HyatlasUnreachable
HyatlasMemoryProvider = mod.HyatlasMemoryProvider


# ---------------------------------------------------------------------------
# Fake v4 server (real HTTP, in-memory store) — the wire contract, not a mock
# ---------------------------------------------------------------------------

class _V4Handler(BaseHTTPRequestHandler):
    store: dict  # class-level, set per server instance

    def log_message(self, *args):  # silence
        pass

    def _json(self, code, payload):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def do_GET(self):
        path = self.path.split("?")[0]
        if path == "/healthz":
            return self._json(200, {"status": "ok"})
        if path == "/api/v1/status":
            return self._json(200, {"status": "ok", "vdb": "ok", "embed": "ok",
                                    "llm": "ok", "layers": {"l3_fact": 1}})
        if path == "/api/v1/boom":
            return self._json(500, {"error": "kaboom"})
        return self._json(404, {"error": "not found"})

    def do_POST(self):
        path = self.path.split("?")[0]
        body = self._body()
        if path == "/api/v1/add":
            mid = f"m{len(self.store['memories'])}"
            self.store["memories"].append(
                {"memory_id": mid, "content": body.get("text", ""),
                 "layer": "l2_raw", "user_id": body.get("user_id", ""),
                 "agent_id": body.get("agent_id", "")})
            return self._json(200, {"success": True, "memory_id": mid})
        if path == "/api/v1/search":
            q = (body.get("query") or "").lower()
            hits = [m for m in self.store["memories"] if q in m["content"].lower()]
            return self._json(200, {"memories": {"normal": [
                {**m, "score": 0.9} for m in hits]}})
        if path == "/api/v1/list":
            return self._json(200, {"total": len(self.store["memories"]),
                                    "memories": self.store["memories"]})
        if path == "/api/v1/delete_all":
            # v4 server: unscoped wipe without confirm=wipe-all is refused
            scoped = any(body.get(k) for k in ("user_id", "agent_id", "layer", "id"))
            if not scoped and body.get("confirm") != "wipe-all":
                return self._json(400, {"error": "refusing unscoped wipe"})
            self.store["memories"].clear()
            return self._json(200, {"success": True})
        return self._json(404, {"error": "not found"})


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeV4Server:
    def __init__(self):
        self.store = {"memories": []}
        handler = type("H", (_V4Handler,), {"store": self.store})
        self.httpd = ThreadingHTTPServer(("127.0.0.1", _free_port()), handler)
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=5)


# ---------------------------------------------------------------------------
# Client wire contract
# ---------------------------------------------------------------------------

def test_client_round_trip():
    srv = FakeV4Server()
    try:
        c = HyatlasClient(base_url=srv.base, timeout=5.0)
        assert c.is_reachable()
        st = c.status()
        assert st["status"] == "ok"
        add = c.add(text="the launcher uses hyatlas start", user_id="u", agent_id="a")
        assert add["success"] is True
        hits = c.search(query="launcher", user_id="u", agent_id="a")
        assert len(hits["memories"]["normal"]) == 1
        items = c.list_memories(user_id="u", agent_id="a")
        assert items["total"] == 1
    finally:
        srv.stop()


def test_client_http_error_raises_typed():
    srv = FakeV4Server()
    try:
        c = HyatlasClient(base_url=srv.base, timeout=5.0)
        try:
            c._get("/api/v1/boom")
            assert False, "expected HyatlasClientError"
        except HyatlasClientError as e:
            assert "500" in str(e) and "kaboom" in str(e)
    finally:
        srv.stop()


def test_client_unreachable_raises_typed():
    port = _free_port()  # nothing listening
    c = HyatlasClient(base_url=f"http://127.0.0.1:{port}", timeout=1.0)
    assert not c.is_reachable()
    try:
        c.status()
        assert False, "expected HyatlasUnreachable"
    except HyatlasUnreachable:
        pass


# ---------------------------------------------------------------------------
# Config precedence: env > entries settings > legacy block > profile JSON
# ---------------------------------------------------------------------------

def _write_home(home: Path, yaml_text: str | None = None, json_text: str | None = None):
    home.mkdir(parents=True, exist_ok=True)
    if yaml_text is not None:
        (home / "config.yaml").write_text(yaml_text, encoding="utf-8")
    if json_text is not None:
        (home / "hyatlas.json").write_text(json_text, encoding="utf-8")


def test_config_precedence(monkeypatch, tmp_path):
    for k in list(os.environ):
        if k.startswith("HYATLAS_"):
            monkeypatch.delenv(k)
    home = tmp_path / "home"
    _write_home(
        home,
        yaml_text=(
            "plugins:\n"
            "  entries:\n"
            "    hyatlas:\n"
            "      settings:\n"
            "        server_port: 19999\n"
            "        user_id: from-settings\n"
            "  hyatlas:\n"
            "    server_port: 18888\n"
            "    agent_id: from-legacy\n"
        ),
        json_text=json.dumps({"server_port": 17777, "user_id": "from-json",
                              "agent_id": "from-json", "auto_start": True}),
    )
    monkeypatch.setenv("HERMES_HOME", str(home))

    cfg = mod._load_config()
    # entries settings beat legacy block and JSON
    assert cfg["server_port"] == 19999
    assert cfg["user_id"] == "from-settings"
    # legacy block still contributes keys settings didn't set
    assert cfg["agent_id"] == "from-legacy"
    # JSON contributes what yaml didn't
    assert cfg["auto_start"] is True
    # defaults untouched
    assert cfg["server_host"] == "127.0.0.1"

    # env beats everything
    monkeypatch.setenv("HYATLAS_SERVER_PORT", "20001")
    monkeypatch.setenv("HYATLAS_AUTO_START", "false")
    cfg2 = mod._load_config()
    assert cfg2["server_port"] == 20001
    assert cfg2["auto_start"] is False


def test_config_ignores_legacy_v35_garbage(monkeypatch, tmp_path):
    for k in list(os.environ):
        if k.startswith("HYATLAS_") or k.startswith("HY_MEMORY_"):
            monkeypatch.delenv(k)
    home = tmp_path / "home"
    _write_home(home, json_text=json.dumps({
        "server_port": 19528, "llm": {"model": "x"}, "vector_store": "qdrant",
        "api_keys": {"deepseek": "***"}, "embedding_dims": 1024,
    }))
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HY_MEMORY_HOST", "10.0.0.1")  # legacy env must not bleed
    cfg = mod._load_config()
    assert not ({"llm", "vector_store", "api_keys", "embedding_dims"} & set(cfg))
    assert cfg["server_host"] == "127.0.0.1"


def test_save_config_round_trip(tmp_path):
    p = HyatlasMemoryProvider()
    p._config = dict(p._config)
    p.save_config({"server_port": 21000, "user_id": "tuna"}, str(tmp_path))
    saved = json.loads((tmp_path / "hyatlas.json").read_text(encoding="utf-8"))
    assert saved["server_port"] == 21000
    assert saved["user_id"] == "tuna"
    assert "request_timeout" not in saved


# ---------------------------------------------------------------------------
# Provider behavior
# ---------------------------------------------------------------------------

def _provider_at(base: str) -> HyatlasMemoryProvider:
    p = HyatlasMemoryProvider()
    p._client = HyatlasClient(base_url=base, timeout=5.0)
    p._user_id = "u"
    p._agent_id = "a"
    return p


def test_handle_tool_call_dispatch():
    srv = FakeV4Server()
    try:
        p = _provider_at(srv.base)
        st = json.loads(p.handle_tool_call("hyatlas_status", {}))
        assert st["status"] == "ok"
        add = json.loads(p.handle_tool_call("hyatlas_add", {"text": "pane id is hyatlas"}))
        assert add["success"] is True
        sr = json.loads(p.handle_tool_call("hyatlas_search", {"query": "pane id"}))
        assert len(sr["memories"]["normal"]) == 1
        rc = json.loads(p.handle_tool_call("hyatlas_recent", {}))
        assert rc["total"] == 1
        unk = json.loads(p.handle_tool_call("hyatlas_nope", {}))
        assert "error" in unk
    finally:
        srv.stop()


def test_handle_tool_call_uninitialized_returns_error_json():
    p = HyatlasMemoryProvider()
    p._client = None
    out = json.loads(p.handle_tool_call("hyatlas_status", {}))
    assert "error" in out


def test_handle_tool_call_server_error_is_json_not_raise():
    srv = FakeV4Server()
    try:
        p = _provider_at(srv.base)
        # force a failing call by pointing at the 500 route via search on a dead port
        p._client = HyatlasClient(base_url=srv.base, timeout=5.0)
        dead = _free_port()
        p2 = _provider_at(f"http://127.0.0.1:{dead}")
        out = json.loads(p2.handle_tool_call("hyatlas_status", {}))
        assert "error" in out  # unreachable surfaces as JSON error, never raises
    finally:
        srv.stop()


def test_sync_turn_best_effort_never_raises():
    dead = _free_port()
    p = _provider_at(f"http://127.0.0.1:{dead}")
    p.sync_turn("hello", "world", session_id="s1")  # must not raise


def test_sync_turn_skips_empty():
    srv = FakeV4Server()
    try:
        p = _provider_at(srv.base)
        p.sync_turn("", "")
        assert len(srv.store["memories"]) == 0
        p.sync_turn("real turn", "real reply", session_id="s2")
        assert len(srv.store["memories"]) == 1
    finally:
        srv.stop()


def test_on_memory_write_mirrors_add_only():
    srv = FakeV4Server()
    try:
        p = _provider_at(srv.base)
        p.on_memory_write("add", "memory", "user prefers concise answers")
        assert len(srv.store["memories"]) == 1
        p.on_memory_write("replace", "memory", "x")  # not mirrored
        p.on_memory_write("remove", "memory", "y")  # not mirrored
        p.on_memory_write("add", "memory", "")       # empty content skipped
        assert len(srv.store["memories"]) == 1
    finally:
        srv.stop()


def test_provider_availability():
    dead = _free_port()
    p = HyatlasMemoryProvider()
    p._config = dict(p._config, server_port=dead)
    p._client = None
    assert not p.is_available()
    assert p.unavailable_reason()  # non-empty explanation


def test_delete_all_guard_survives_client():
    srv = FakeV4Server()
    try:
        c = HyatlasClient(base_url=srv.base, timeout=5.0)
        c.add(text="keep me", user_id="u", agent_id="a")
        # The v4 server refuses unscoped wipes without confirm=wipe-all.
        # The client must surface the refusal, not silently succeed.
        try:
            c.delete_all()  # unscoped, no confirm
            assert False, "expected refusal"
        except HyatlasClientError as e:
            assert "400" in str(e)
        assert len(srv.store["memories"]) == 1
    finally:
        srv.stop()
