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
import re
import socket
import sys
import threading

import pytest

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

def test_system_prompt_block_names_only_real_tools():
    """The agent-facing prompt must not advertise tools that don't exist.

    A rename sweep once turned a reference to Hermes core's `memory` tool into
    a nonexistent `hyatlas_save`, so the prompt instructed the agent to call a
    tool that was never registered. Assert every backticked tool name in the
    block is one the provider actually dispatches.
    """
    import re

    srv = FakeV4Server()
    try:
        p = _provider_at(srv.base)
        block = p.system_prompt_block()
        assert "HyAtlas" in block

        # every `backticked` identifier that looks like a tool must be registered
        real = {s["name"] for s in p.get_tool_schemas()}
        assert real, "provider registered no tools"
        for name in re.findall(r"`([a-z_]+)`", block):
            if name.startswith("hyatlas_"):
                assert name in real, f"prompt advertises unknown tool {name!r}; real tools: {sorted(real)}"

        # the mirrored standard tool is Hermes core's `memory`, never a hyatlas_* alias
        assert "hyatlas_save" not in block
        assert "hy_memory_save" not in block
        assert "`memory`" in block

        # server port is interpolated, not hardcoded
        assert str(p._config.get("server_port", 19528)) in block
    finally:
        srv.stop()


# ---------------------------------------------------------------------------
# Disclosure invariants — the plugin's README and its catalog entry promise
# that no LLM credentials live in or flow through this plugin, and that it
# reaches no machine-specific location. These pin both promises, because both
# were once true in the docs and false in the code.
# ---------------------------------------------------------------------------

def _load_sibling(name):
    """Load a plugin submodule.

    ``cli`` does relative imports, so it must be loaded as a child of the
    package the module under test already registered — a bare
    ``spec_from_file_location`` leaves it with no parent package and the
    import fails before the test can assert anything.
    """
    parent = getattr(mod, "__name__", "hyatlas_unit")
    full = f"{parent}.{name}"
    spec = importlib.util.spec_from_file_location(
        full, os.path.join(_PKG, f"{name}.py"), submodule_search_locations=[])
    m = importlib.util.module_from_spec(spec)
    sys.modules[full] = m
    spec.loader.exec_module(m)
    return m


try:  # CI installs only pytest + pyyaml; the dashboard needs fastapi.
    import fastapi  # noqa: F401
    _HAS_FASTAPI = True
except Exception:  # noqa: BLE001 — a broken pydantic_core must not fail the suite
    _HAS_FASTAPI = False

requires_fastapi = pytest.mark.skipif(
    not _HAS_FASTAPI, reason="fastapi not importable (CI installs pytest + pyyaml only)")


def _load_dashboard_api(tag):
    """Load dashboard/plugin_api.py the way the desktop backend does: by file
    path, with no parent package, so relative imports are unavailable to it."""
    path = os.path.join(_PKG, "dashboard", "plugin_api.py")
    name = f"{tag}_plugin_api"
    for n in list(sys.modules):
        if "hyatlas_dashboard_settings" in n:
            del sys.modules[n]
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    sys.modules[name] = m
    spec.loader.exec_module(m)
    return m


def test_subprocess_env_forwards_no_llm_credential(monkeypatch):
    """The server subprocess must not be handed an invented LLM endpoint or key."""
    proc_mod = _load_sibling("process")
    marker = "SUPERSECRET_MARKER_VALUE"
    monkeypatch.setenv("AI2API_KEY", marker)
    monkeypatch.delenv("HYATLAS_LLM_BASE", raising=False)
    monkeypatch.delenv("HYATLAS_LLM_MODEL", raising=False)
    monkeypatch.delenv("HYATLAS_LLM_KEY", raising=False)

    env = proc_mod.HyatlasProcess({"server_port": 19528})._env()

    # No credential is copied from another variable into the server's env.
    assert env.get("HYATLAS_LLM_KEY") != marker
    # No endpoint or model is invented for the user.
    assert "HYATLAS_LLM_BASE" not in env
    assert "HYATLAS_LLM_MODEL" not in env
    assert "HYATLAS_LLM_KEY" not in env
    # The loopback-only default the plugin does own is still applied.
    assert env["HYATLAS_GO_HOST"] == "127.0.0.1"
    assert env["HYATLAS_GO_PORT"] == "19528"


def test_subprocess_env_inherits_user_llm_settings(monkeypatch):
    """What the user explicitly exported still reaches the server."""
    proc_mod = _load_sibling("process")
    monkeypatch.setenv("HYATLAS_LLM_BASE", "https://example.invalid/v1")
    monkeypatch.setenv("HYATLAS_LLM_MODEL", "some:model")
    monkeypatch.setenv("HYATLAS_LLM_KEY", "user-supplied")
    env = proc_mod.HyatlasProcess({"server_port": 20000})._env()
    assert env["HYATLAS_LLM_BASE"] == "https://example.invalid/v1"
    assert env["HYATLAS_LLM_MODEL"] == "some:model"
    assert env["HYATLAS_LLM_KEY"] == "user-supplied"
    assert env["HYATLAS_GO_PORT"] == "20000"


def test_no_machine_specific_path_in_plugin_source():
    """No absolute developer path may ship to every installer."""
    bad = ("F:/", "F:\\", "C:/Users/", "C:\\Users\\")
    for py in Path(_PKG).rglob("*.py"):
        if "tests" in py.parts:
            continue
        src = py.read_text(encoding="utf-8")
        for token in bad:
            assert token not in src, f"{py.name} ships a machine-specific path {token!r}"


def test_launcher_resolution_needs_an_explicit_path():
    """The Windows launcher is only used when the user points at one."""
    cli = _load_sibling("cli")
    # no config -> no launcher, so start spawns the binary directly
    assert cli._launcher({}) is None
    assert cli._launcher({"binary_path": "", "launcher_path": ""}) is None
    # a configured launcher that does not exist is not invented
    assert cli._launcher({"launcher_path": "/nonexistent/hyatlas-go.ps1"}) is None


def test_launcher_path_is_configurable(monkeypatch, tmp_path):
    """`launcher_path` reaches the config from both the JSON and env layers."""
    cfg = mod._load_config()
    assert cfg["launcher_path"] == ""

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HYATLAS_LAUNCHER_PATH", str(tmp_path / "go.ps1"))
    cfg = mod._load_config()
    assert cfg["launcher_path"] == str(tmp_path / "go.ps1")

    # and the settings form layer, which is what the Desktop writes
    monkeypatch.delenv("HYATLAS_LAUNCHER_PATH", raising=False)
    (tmp_path / "hyatlas.json").write_text(
        json.dumps({"launcher_path": str(tmp_path / "from-json.ps1")}), encoding="utf-8")
    assert mod._load_config()["launcher_path"] == str(tmp_path / "from-json.ps1")


# ---------------------------------------------------------------------------
# teknium1's catalog review (PR #134419): what leaves the machine, and what the
# spawned server inherits.
# ---------------------------------------------------------------------------

_AGENT_SECRETS = (
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "AI2API_KEY", "NOUS_API_KEY",
    "GITHUB_TOKEN", "HERMES_API_KEY", "AWS_SECRET_ACCESS_KEY",
    "DISCORD_BOT_TOKEN", "DATABASE_URL",
)


def _poison(monkeypatch, extra=None):
    for k in _AGENT_SECRETS:
        monkeypatch.setenv(k, "SECRET-" + k)
    for k in ("HYATLAS_LLM_BASE", "HYATLAS_LLM_MODEL", "HYATLAS_LLM_KEY"):
        monkeypatch.delenv(k, raising=False)
    for k, v in (extra or {}).items():
        monkeypatch.setenv(k, v)


def test_subprocess_env_does_not_inherit_agent_secrets(monkeypatch):
    """The spawned server gets an allowlist, not a copy of the agent's env."""
    proc_mod = _load_sibling("process")
    _poison(monkeypatch)

    env = proc_mod.HyatlasProcess({"server_port": 19528})._env()

    leaked = [k for k in _AGENT_SECRETS if env.get(k) == "SECRET-" + k]
    assert not leaked, f"agent secrets reached the server subprocess: {leaked}"
    # The env is genuinely minimal, not merely missing these nine names.
    assert len(env) < 40, f"child env is not minimal: {len(env)} vars"


def test_subprocess_env_keeps_what_the_os_needs(monkeypatch):
    """Stripping too far breaks the child — Windows Go needs SYSTEMROOT for DNS/TLS."""
    proc_mod = _load_sibling("process")
    _poison(monkeypatch, {"PATH": "/usr/bin", "HOME": "/home/u",
                          "SYSTEMROOT": r"C:\Windows", "TEMP": r"C:\Temp",
                          "USERPROFILE": r"C:\Users\u"})

    env = proc_mod.HyatlasProcess({"server_port": 19528})._env()

    assert env.get("PATH") == "/usr/bin"
    assert env.get("SYSTEMROOT") == r"C:\Windows"
    assert env.get("TEMP") == r"C:\Temp"
    assert env.get("USERPROFILE") == r"C:\Users\u"
    assert env["HYATLAS_GO_HOST"] == "127.0.0.1"


# Every setting the SERVER reads must reach the spawned server. The client-only
# ones are listed so the guard fails loudly when a new server-read setting is
# added to SCHEMA without being forwarded -- the bug this pins is data_dir, which
# was a declared, validated, documented setting that _env() never forwarded, so
# the Desktop's value was silently ignored and the server used its own default.
_SERVER_READ = {
    "data_dir": "HYATLAS_GO_DATA",
    "server_host": "HYATLAS_GO_HOST",
    "server_port": "HYATLAS_GO_PORT",
    "mode": "HYATLAS_MODE",
    "sync": "HYATLAS_SYNC_EXTRACT",
    "llm_base": "HYATLAS_LLM_BASE",
    "llm_model": "HYATLAS_LLM_MODEL",
}
_CLIENT_ONLY = {"user_id", "agent_id", "auto_start", "binary_path",
                "launcher_path", "request_timeout", "llm_key"}


def test_every_server_read_setting_is_forwarded_to_the_child(monkeypatch):
    """A config value the server reads must arrive as the env var it reads.

    Class-level guard: catches any SCHEMA entry that is meaningful to the server
    but missing from _env(). Without this, a setting can be declared, validated,
    documented and still do nothing.
    """
    proc_mod = _load_sibling("process")
    settings = _load_sibling("settings")
    _poison(monkeypatch, {"PATH": "/usr/bin", "HOME": "/home/u"})

    cfg = {"data_dir": "/srv/hyatlas-data", "server_host": "10.9.8.7",
           "server_port": 20777, "mode": "pro", "sync": "off",
           "llm_base": "https://llm.test/v1", "llm_model": "test/model"}
    env = proc_mod.HyatlasProcess(cfg)._env()

    for key, var in _SERVER_READ.items():
        assert env.get(var) == str(cfg[key]), (
            f"setting {key!r} -> {var}: server would fall back to its own "
            f"default and silently ignore the configured {cfg[key]!r}")


def test_server_read_settings_cover_every_schema_key(monkeypatch):
    """The two tables above must stay exhaustive, or the guard has blind spots."""
    settings = _load_sibling("settings")
    keys = {f["key"] for f in settings.SCHEMA}
    assert keys == set(_SERVER_READ) | _CLIENT_ONLY, (
        f"SCHEMA drifted: unclassified={sorted(keys - set(_SERVER_READ) - _CLIENT_ONLY)}, "
        f"stale={sorted((set(_SERVER_READ) | _CLIENT_ONLY) - keys)}")


def test_data_dir_setting_reaches_the_server(monkeypatch):
    """The regression itself: data_dir was a documented no-op on the spawn path."""
    proc_mod = _load_sibling("process")
    _poison(monkeypatch, {"PATH": "/usr/bin", "HOME": "/home/u"})

    env = proc_mod.HyatlasProcess({"data_dir": "/srv/hyatlas-data"})._env()
    assert env["HYATLAS_GO_DATA"] == "/srv/hyatlas-data"

    # Empty means "let the server use its own default", so nothing is set.
    env2 = proc_mod.HyatlasProcess({})._env()
    assert "HYATLAS_GO_DATA" not in env2, "an empty data_dir must not override the server default"


def test_explicit_env_beats_config_for_the_forwarded_settings(monkeypatch):
    """setdefault keeps an exported variable authoritative, matching mode/sync."""
    proc_mod = _load_sibling("process")
    _poison(monkeypatch, {"PATH": "/usr/bin", "HOME": "/home/u",
                          "HYATLAS_GO_DATA": "/from/env",
                          "HYATLAS_GO_HOST": "1.2.3.4",
                          "HYATLAS_GO_PORT": "31337"})

    env = proc_mod.HyatlasProcess({"data_dir": "/from/config",
                                   "server_host": "10.0.0.1",
                                   "server_port": 19999})._env()
    assert env["HYATLAS_GO_DATA"] == "/from/env"
    assert env["HYATLAS_GO_HOST"] == "1.2.3.4"
    assert env["HYATLAS_GO_PORT"] == "31337"


def test_subprocess_env_passes_all_hyatlas_vars(monkeypatch):
    """HYATLAS_* is the server's configuration surface, so all of it must pass."""
    proc_mod = _load_sibling("process")
    _poison(monkeypatch, {
        "HYATLAS_GO_DATA": "/srv/data",
        "HYATLAS_GRAPH_PATH": "/srv/data/graph.json",
        "HYATLAS_MODEL_DIR": "/srv/models",
        "HYATLAS_EMBED_BASE": "bge",
        "HYATLAS_LLM_BASE": "http://127.0.0.1:11434/v1",
        "HYATLAS_LLM_MODEL": "local:model",
        "HYATLAS_LLM_KEY_FILE": "/etc/hyatlas/key",
    })

    env = proc_mod.HyatlasProcess({"server_port": 19528})._env()

    for k in ("HYATLAS_GO_DATA", "HYATLAS_GRAPH_PATH", "HYATLAS_MODEL_DIR",
              "HYATLAS_EMBED_BASE", "HYATLAS_LLM_BASE", "HYATLAS_LLM_MODEL",
              "HYATLAS_LLM_KEY_FILE"):
        assert env.get(k) == os.environ[k], f"{k} did not reach the server"
    assert env["HYATLAS_LLM_BASE"] == "http://127.0.0.1:11434/v1"


def _turn_msgs(n):
    return [{"role": "user" if i % 2 == 0 else "assistant", "content": f"msg-{i}"}
            for i in range(n)]


def test_sync_sends_only_the_current_turn_not_history():
    """The whole transcript used to be re-uploaded and re-extracted every turn."""
    provider = mod.HyatlasMemoryProvider()
    thread = _turn_msgs(400)

    text = provider._build_turn_text("turn A user", "turn A reply", thread, session_id="s")

    assert "turn A user" in text and "turn A reply" in text
    assert "msg-0" not in text and "msg-399" not in text
    assert len(text) < 200, f"payload is not turn-sized: {len(text)} chars"


def test_sync_payload_stays_flat_as_conversation_grows():
    """Quadratic upload was the bug; the payload must not grow with history."""
    provider = mod.HyatlasMemoryProvider()
    sizes = []
    for turn in range(1, 8):
        thread = _turn_msgs(2 * turn)
        sizes.append(len(provider._build_turn_text(f"u{turn}", f"a{turn}", thread, session_id="s")))

    assert max(sizes) - min(sizes) < 20, f"payload grew with history: {sizes}"


def test_sync_recovers_messages_missed_since_last_turn():
    """A gap must not silently lose memory."""
    provider = mod.HyatlasMemoryProvider()
    provider._record_synced("s", 4)

    text = provider._build_turn_text("now", "reply", _turn_msgs(8), session_id="s")

    for i in (4, 5, 6, 7):
        assert f"msg-{i}" in text, f"missed message {i} was not recovered"
    assert "msg-0" not in text and "msg-3" not in text


def test_sync_keeps_current_turn_after_compression():
    """A shrunk thread must not swallow the turn being reported."""
    provider = mod.HyatlasMemoryProvider()
    provider._record_synced("s", 100)

    text = provider._build_turn_text("after compress", "after reply", _turn_msgs(40), session_id="s")

    assert "after compress" in text and "after reply" in text
    assert "msg-0" not in text
    assert provider._synced["s"] == 40


def test_sync_adopts_existing_history_without_uploading_it():
    """First turn after a gateway restart must not dump the whole thread."""
    provider = mod.HyatlasMemoryProvider()

    text = provider._build_turn_text("first user", "first reply", _turn_msgs(400), session_id="fresh")

    assert "first user" in text and "first reply" in text
    assert "msg-0" not in text
    assert provider._synced["fresh"] == 400


def test_sync_without_messages_reports_the_pair():
    """No thread given: the caller's pair is the whole turn, and it must be
    separated. Gluing them into "USER: uASSISTANT: a" would hand the extraction
    LLM one run-together token instead of two roles."""
    provider = mod.HyatlasMemoryProvider()
    assert provider._build_turn_text("u", "a", None, session_id="s") == "USER: u\n\nASSISTANT: a"


def test_sync_handles_multimodal_content_blocks():
    """Content can be a list of blocks; concatenating it would stringify dicts."""
    provider = mod.HyatlasMemoryProvider()
    provider._record_synced("s", 0)
    thread = [{"role": "user", "content": [{"type": "text", "text": "look here"},
                                           {"type": "image_url", "image_url": {"url": "x"}}]}]

    text = provider._build_turn_text("look here", "ok", thread, session_id="s")

    assert "look here" in text
    assert "image_url" not in text and "{'type'" not in text


def test_sync_never_uploads_the_system_prompt():
    provider = mod.HyatlasMemoryProvider()
    provider._record_synced("s", 0)
    thread = [{"role": "system", "content": "SECRET SYSTEM PROMPT"},
              {"role": "user", "content": "hi"}]

    text = provider._build_turn_text("hi", "hello", thread, session_id="s")

    assert "SECRET SYSTEM PROMPT" not in text


def test_synced_index_is_bounded():
    """A long-lived gateway sees more sessions than it can hold."""
    provider = mod.HyatlasMemoryProvider()
    for i in range(provider._SYNCED_MAX * 3):
        provider._record_synced(f"sess-{i}", i)

    assert len(provider._synced) <= provider._SYNCED_MAX
    assert "sess-191" in provider._synced
    assert "sess-0" not in provider._synced


def test_synced_index_is_per_session():
    provider = mod.HyatlasMemoryProvider()
    provider._build_turn_text("a", "b", _turn_msgs(10), session_id="A")
    provider._build_turn_text("c", "d", _turn_msgs(50), session_id="B")
    assert provider._synced["A"] == 10 and provider._synced["B"] == 50


def test_system_prompt_uses_configured_host():
    """The prompt hardcoded 127.0.0.1 while the client honored server_host."""
    provider = mod.HyatlasMemoryProvider()
    provider._config["server_host"] = "10.9.8.7"
    provider._config["server_port"] = 20999

    block = provider.system_prompt_block()

    assert "10.9.8.7:20999" in block
    assert "127.0.0.1" not in block
    # The client must be built from the same origin, or the prompt lies.
    assert provider._ensure_client().base_url == "http://10.9.8.7:20999"


def test_unavailable_reason_uses_configured_host():
    provider = mod.HyatlasMemoryProvider()
    provider._config["server_host"] = "10.9.8.7"
    provider._config["server_port"] = 20999
    assert "10.9.8.7:20999" in provider.unavailable_reason()


def test_backup_paths_returns_existing_directories(monkeypatch, tmp_path):
    """Relative names resolved against the agent CWD, so backup.py dropped them."""
    data = tmp_path / "hyatlas-data"
    data.mkdir()
    monkeypatch.setenv("HYATLAS_GO_DATA", str(data))

    provider = mod.HyatlasMemoryProvider()
    paths = provider.backup_paths()

    assert paths, "no backup path reported"
    for raw in paths:
        p = Path(raw)
        assert p.is_absolute(), f"backup path is not absolute: {raw}"
        assert p.exists(), f"backup path does not exist, so backup.py drops it: {raw}"
    assert any(Path(p).resolve() == data.resolve() for p in paths)


def test_backup_paths_empty_when_nothing_exists(monkeypatch, tmp_path):
    """An empty answer is correct; a nonexistent path silently archives nothing."""
    monkeypatch.delenv("HYATLAS_GO_DATA", raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "no-such-home"))

    provider = mod.HyatlasMemoryProvider()
    provider._config["data_dir"] = str(tmp_path / "absent")

    for raw in provider.backup_paths():
        assert Path(raw).exists(), f"reported a path that does not exist: {raw}"


def test_pidfile_written_on_start_and_removed_on_cleanup(monkeypatch, tmp_path):
    """stop_running() reads this file; without the write it could never match."""
    proc_mod = _load_sibling("process")
    if sys.platform == "win32":
        # A real long-running child on each platform: Popen([binary]) gets one
        # executable, so the fake server has to be a script the OS can run.
        fake = tmp_path / "hyatlas-go.bat"
        fake.write_text("@echo off\rping -n 31 127.0.0.1 > nul\r")
    else:
        fake = tmp_path / "hyatlas-go"
        fake.write_text("#!/bin/shsleep 30")
        os.chmod(fake, 0o755)
    monkeypatch.setattr(proc_mod, "LOG_DIR", tmp_path)
    monkeypatch.setattr(proc_mod, "PID_FILE", tmp_path / "hyatlas.pid")
    monkeypatch.setattr(proc_mod, "LOG_FILE", tmp_path / "hyatlas.log")

    proc = proc_mod.HyatlasProcess({"binary_path": str(fake), "server_port": 19528})
    try:
        proc.start()
        pidfile = tmp_path / "hyatlas.pid"
        assert pidfile.exists(), "start() did not write the pidfile"
        assert pidfile.read_text().strip() == str(proc._proc.pid)
    finally:
        proc.stop()
        if proc._proc is not None and proc._proc.poll() is None:
            proc._proc.kill()

    assert not (tmp_path / "hyatlas.pid").exists(), "_cleanup left a stale pidfile"


def test_stop_running_refuses_to_kill_a_recycled_pid(monkeypatch, tmp_path):
    """Writing the pidfile made a force-kill live, so a stale pid needs a name check."""
    proc_mod = _load_sibling("process")
    monkeypatch.setattr(proc_mod, "PID_FILE", tmp_path / "hyatlas.pid")
    (tmp_path / "hyatlas.pid").write_text("999999")

    killed = []
    monkeypatch.setattr(proc_mod.subprocess, "run",
                        lambda *a, **k: killed.append(a) or __import__("types").SimpleNamespace(stdout=""))
    monkeypatch.setattr(proc_mod.HyatlasProcess, "_is_server", staticmethod(lambda pid: False))

    proc_mod.HyatlasProcess.stop_running()

    assert not killed, "stop_running force-killed a pid that is not hyatlas-go"
    assert not (tmp_path / "hyatlas.pid").exists(), "stale pidfile was left behind"


@requires_fastapi
def test_dashboard_api_reads_plugin_settings(monkeypatch, tmp_path):
    """The pane hardcoded 127.0.0.1:19528 instead of the configured server."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for k in ("HYATLAS_HOST", "HYATLAS_PORT"):
        monkeypatch.delenv(k, raising=False)
    (tmp_path / "hyatlas.json").write_text(
        json.dumps({"server_host": "10.4.5.6", "server_port": 20777}), encoding="utf-8")

    api = _load_dashboard_api("dashcfg")

    assert api.BASE == "http://10.4.5.6:20777", api.BASE


@requires_fastapi
def test_dashboard_api_env_override_still_wins(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "hyatlas.json").write_text(
        json.dumps({"server_host": "10.4.5.6", "server_port": 20777}), encoding="utf-8")
    monkeypatch.setenv("HYATLAS_HOST", "127.0.0.1")
    monkeypatch.setenv("HYATLAS_PORT", "19999")

    api = _load_dashboard_api("dashenv")

    assert api.BASE == "http://127.0.0.1:19999", api.BASE


@requires_fastapi
def test_dashboard_api_defaults_to_loopback(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HYATLAS_HOST", raising=False)
    monkeypatch.delenv("HYATLAS_PORT", raising=False)

    api = _load_dashboard_api("dashdef")

    assert api.BASE == "http://127.0.0.1:19528", api.BASE


@requires_fastapi
def test_dashboard_api_does_not_touch_sys_path():
    """Catalog rule 9: no sys.path games to reach a sibling module."""
    before = list(sys.path)
    _load_dashboard_api("dashpath")
    assert sys.path == before, "loading the dashboard API mutated sys.path"


def test_provider_schema_matches_manifest_schema():
    """plugin.yaml and get_config_schema() must describe the same settings.

    They are two separate declarations of one thing — the manifest feeds the
    Desktop settings form, the provider feeds `hermes memory setup` — and they
    drifted until the provider offered five of nine keys, so four settings were
    configurable in the UI but invisible to setup. Both now derive from
    settings.SCHEMA; this pins the derivation for the manifest half, which is
    static YAML and cannot import it.
    """
    yaml = pytest.importorskip("yaml")
    manifest = yaml.safe_load((Path(_PKG) / "plugin.yaml").read_text(encoding="utf-8"))
    declared = manifest.get("config_schema") or {}
    provider = {f["key"]: f for f in mod.HyatlasMemoryProvider().get_config_schema()}

    assert set(declared) == set(provider), (
        f"manifest/provider settings diverged: "
        f"manifest-only={sorted(set(declared) - set(provider))} "
        f"provider-only={sorted(set(provider) - set(declared))}"
    )
    for key, field in declared.items():
        assert field.get("default") == provider[key]["default"], (
            f"{key}: default {field.get('default')!r} in manifest vs "
            f"{provider[key]['default']!r} in provider"
        )


def test_settings_schema_is_the_single_source():
    """KEYS, DEFAULTS and config_schema() all derive from one SCHEMA tuple."""
    settings = _load_sibling("settings")
    assert settings.KEYS == tuple(f["key"] for f in settings.SCHEMA)
    assert settings.DEFAULTS == {f["key"]: f["default"] for f in settings.SCHEMA}
    assert [f["key"] for f in settings.config_schema()] == list(settings.KEYS)
    # labels/types are manifest-only; leaking them confuses the setup form
    assert not any("label" in f or "type" in f for f in settings.config_schema())


def test_every_setting_is_documented_in_the_readme():
    """A setting nobody documents is a setting nobody can find.

    Matched as a table row (``| `name` |``), not as a bare substring: "sync"
    occurs inside "synchronous" elsewhere in this README, so a substring check
    passed while the setting itself was undocumented.
    """
    settings = _load_sibling("settings")
    readme = (Path(_PKG) / "README.md").read_text(encoding="utf-8")
    rows = set(re.findall(r"^\| `([a-z_]+)` \|", readme, re.M))
    undocumented = [k for k in settings.KEYS if k not in rows]
    assert not undocumented, f"settings absent from the plugin README table: {undocumented}"


# ---- extraction-mode setting (v4.3.0) ----

def test_mode_setting_validates_and_normalises():
    settings = _load_sibling("settings")
    assert settings.VALID_MODES == ("lite", "pro", "ultra")
    for raw, want in [("lite", "lite"), ("LITE", "lite"), ("  Pro ", "pro"),
                      ("ultra", "ultra"), ("", ""), (None, "")]:
        assert settings.mode({"mode": raw}) == want, raw


def test_mode_setting_rejects_unknown_value():
    """An invalid mode must fail in the plugin, not on server boot.

    The server treats an unrecognised HYATLAS_MODE as fatal, so forwarding it
    would produce a child that dies immediately and reports only in a log file.
    """
    settings = _load_sibling("settings")
    for bad in ("turbo", "system1", "Lite2", "0"):
        with pytest.raises(ValueError) as ei:
            settings.mode({"mode": bad})
        msg = str(ei.value)
        assert bad.lower() in msg.lower(), msg
        for valid in settings.VALID_MODES:
            assert valid in msg, msg


def test_mode_env_override_lowercases():
    settings = _load_sibling("settings")
    os.environ["HYATLAS_MODE"] = "LITE"
    try:
        assert settings.load()["mode"] == "lite"
    finally:
        os.environ.pop("HYATLAS_MODE", None)


def test_mode_declared_in_both_schemas():
    """The manifest and the provider schema must both expose mode."""
    yaml = pytest.importorskip("yaml")
    manifest = yaml.safe_load((Path(_PKG) / "plugin.yaml").read_text(encoding="utf-8"))
    declared = manifest["config_schema"]
    assert "mode" in declared
    assert declared["mode"]["choices"] == ["", "lite", "pro", "ultra"]
    provider = {f["key"]: f for f in mod.HyatlasMemoryProvider().get_config_schema()}
    assert "mode" in provider
    assert provider["mode"]["choices"] == ["", "lite", "pro", "ultra"]


def test_spawner_forwards_validated_mode_only():
    """_env() forwards the resolved mode; an unvalidated raw value never reaches the child."""
    proc = _load_sibling("process")
    hp = proc.HyatlasProcess({"mode": "lite", "server_port": 19528})
    hp._mode = "lite"
    env = hp._env()
    assert env.get("HYATLAS_MODE") == "lite"


def test_spawner_omits_mode_when_unset():
    proc = _load_sibling("process")
    hp = proc.HyatlasProcess({})
    hp._mode = ""
    env = hp._env()
    os.environ.pop("HYATLAS_MODE", None)
    assert "HYATLAS_MODE" not in env, "empty mode must not pin the child to a value"


def test_spawner_start_rejects_invalid_mode():
    """start() must raise before spawning when the configured mode is unknown."""
    proc = _load_sibling("process")
    hp = proc.HyatlasProcess({"mode": "turbo"})
    with pytest.raises(ValueError):
        hp.start()
    assert hp._proc is None, "no child may be spawned for an invalid mode"


def test_mode_forwarding_keeps_env_allowlist():
    """Forwarding the mode must not widen the allowlist or leak anything else."""
    proc = _load_sibling("process")
    os.environ["HY_TEST_SECRET"] = "should-not-leak"
    try:
        hp = proc.HyatlasProcess({"mode": "pro"})
        hp._mode = "pro"
        env = hp._env()
        assert env.get("HYATLAS_MODE") == "pro"
        assert "HY_TEST_SECRET" not in env
    finally:
        os.environ.pop("HY_TEST_SECRET", None)


# ---- sync knob (separate from mode) ----

def test_sync_setting_validates_and_aliases():
    settings = _load_sibling("settings")
    assert settings.VALID_SYNC == ("on", "off")
    # Every alias must resolve to the canonical value the server also accepts,
    # or the Desktop form and the server disagree about one setting.
    for raw, want in [("", ""), ("on", "on"), ("ON", "on"), ("true", "on"),
                      ("1", "on"), ("yes", "on"), ("off", "off"),
                      ("false", "off"), ("0", "off"), ("no", "off")]:
        assert settings.sync({"sync": raw}) == want, raw


def test_sync_setting_rejects_unknown_value():
    settings = _load_sibling("settings")
    for bad in ("maybe", "2", "on/off"):
        with pytest.raises(ValueError) as ei:
            settings.sync({"sync": bad})
        assert bad.lower() in str(ei.value).lower()


def test_sync_declared_in_both_schemas():
    """Manifest and provider schema must both expose sync."""
    yaml = pytest.importorskip("yaml")
    declared = yaml.safe_load((Path(_PKG) / "plugin.yaml").read_text(encoding="utf-8"))["config_schema"]
    assert "sync" in declared
    assert declared["sync"]["choices"] == ["", "on", "off"]
    provider = {f["key"]: f for f in mod.HyatlasMemoryProvider().get_config_schema()}
    assert "sync" in provider
    assert provider["sync"]["choices"] == ["", "on", "off"]


def test_spawner_forwards_validated_sync_only():
    proc = _load_sibling("process")
    hp = proc.HyatlasProcess({"mode": "ultra", "sync": "on"})
    hp._mode, hp._sync = "ultra", "on"
    env = hp._env()
    assert env.get("HYATLAS_SYNC_EXTRACT") == "on"
    assert env.get("HYATLAS_MODE") == "ultra"


def test_spawner_omits_sync_when_unset():
    proc = _load_sibling("process")
    hp = proc.HyatlasProcess({})
    hp._mode = hp._sync = ""
    os.environ.pop("HYATLAS_SYNC_EXTRACT", None)
    assert "HYATLAS_SYNC_EXTRACT" not in hp._env(), "empty must not pin the child"


def test_spawner_start_rejects_invalid_sync():
    proc = _load_sibling("process")
    hp = proc.HyatlasProcess({"sync": "maybe"})
    with pytest.raises(ValueError):
        hp.start()
    assert hp._proc is None


def test_mode_and_sync_are_independent_settings():
    """Setting one must not imply the other; they are orthogonal knobs."""
    settings = _load_sibling("settings")
    for m in settings.VALID_MODES:
        for s in settings.VALID_SYNC:
            cfg = {"mode": m, "sync": s}
            assert settings.mode(cfg) == m
            assert settings.sync(cfg) == s


# ---- LLM onboarding settings ----

def test_llm_key_is_declared_secret_with_env_var():
    """The secret flag is load-bearing, not decoration.

    hermes_cli.memory_setup masks secret prompts and routes their value to .env
    via env_var instead of the provider JSON. If config_schema() dropped these
    fields the API key would be written into hyatlas.json in plaintext, so this
    test pins the round trip through get_config_schema().
    """
    fields = {f["key"]: f for f in mod.HyatlasMemoryProvider().get_config_schema()}
    key = fields["llm_key"]
    assert key.get("secret") is True
    assert key.get("env_var") == "HYATLAS_LLM_KEY"
    assert key.get("url"), "a secret field should say where to get one"
    # The non-secret pair must NOT be masked.
    for name in ("llm_base", "llm_model"):
        assert not fields[name].get("secret"), f"{name} should not be secret"


def test_wizard_fields_survive_config_schema():
    """secret/env_var/url must not be stripped when deriving the provider schema.

    config_schema() used to pass through only key/description/default/choices,
    which silently demoted a secret field to a plaintext one.
    """
    settings = _load_sibling("settings")
    for f in settings.SCHEMA:
        if "secret" in f:
            got = {g["key"]: g for g in settings.config_schema()}[f["key"]]
            assert got.get("secret") == f["secret"], f["key"]
            assert got.get("env_var") == f.get("env_var"), f["key"]


def test_save_config_strips_the_llm_key():
    """Defence in depth: even a caller that hands save_config a key gets it dropped."""
    import json as _json
    import tempfile

    home = Path(tempfile.mkdtemp(prefix="hyatlas-cfg-"))
    try:
        provider = mod.HyatlasMemoryProvider()
        provider.save_config(
            {"mode": "pro", "llm_base": "https://api.example.com/v1",
             "llm_model": "m", "llm_key": "SK-SHOULD-NOT-PERSIST"},
            str(home),
        )
        raw = (home / "hyatlas.json").read_text(encoding="utf-8")
        written = _json.loads(raw)
        assert "llm_key" not in written
        assert "SHOULD-NOT-PERSIST" not in raw
        # The non-secret pair does persist, so the setting is usable.
        assert written["llm_base"] == "https://api.example.com/v1"
        assert written["llm_model"] == "m"
    finally:
        import shutil
        shutil.rmtree(home, ignore_errors=True)


def test_spawner_forwards_llm_endpoint_and_model():
    """Settings-configured endpoint must reach the child, or it is silently ignored."""
    proc = _load_sibling("process")
    for k in ("HYATLAS_LLM_BASE", "HYATLAS_LLM_MODEL"):
        os.environ.pop(k, None)
    hp = proc.HyatlasProcess({"llm_base": "https://api.example.com/v1", "llm_model": "gpt-mini"})
    hp._mode = hp._sync = ""
    env = hp._env()
    assert env.get("HYATLAS_LLM_BASE") == "https://api.example.com/v1"
    assert env.get("HYATLAS_LLM_MODEL") == "gpt-mini"


def test_spawner_never_forwards_the_key_from_config():
    """The key reaches a child only through the environment, never from settings."""
    proc = _load_sibling("process")
    os.environ.pop("HYATLAS_LLM_KEY", None)
    hp = proc.HyatlasProcess({"llm_key": "SK-FROM-CONFIG"})
    hp._mode = hp._sync = ""
    env = hp._env()
    assert "HYATLAS_LLM_KEY" not in env
    assert not any("FROM-CONFIG" in str(v) for v in env.values())


def test_spawner_exported_env_wins_over_settings():
    """setdefault semantics: an explicit export stays authoritative."""
    proc = _load_sibling("process")
    os.environ["HYATLAS_LLM_BASE"] = "http://from-env/v1"
    try:
        hp = proc.HyatlasProcess({"llm_base": "http://from-settings/v1"})
        hp._mode = hp._sync = ""
        assert hp._env().get("HYATLAS_LLM_BASE") == "http://from-env/v1"
    finally:
        os.environ.pop("HYATLAS_LLM_BASE", None)


def test_llm_settings_are_documented_in_the_readme():
    """A user cannot onboard to a field they cannot find documented."""
    readme = (Path(_PKG) / "README.md").read_text(encoding="utf-8")
    for name, var in [("llm_base", "HYATLAS_LLM_BASE"), ("llm_model", "HYATLAS_LLM_MODEL"),
                      ("llm_key", "HYATLAS_LLM_KEY")]:
        assert f"| `{name}` |" in readme, f"{name} missing from the settings table"
        assert var in readme, f"{var} missing from the plugin README"


# ---- a present-but-null config value must not override the default ----

def _write_profile(monkeypatch, payload, tmp_name="hyatlas.json"):
    """Point HERMES_HOME at a scratch dir holding one hyatlas.json."""
    import tempfile
    home = Path(tempfile.mkdtemp(prefix="hy-nullcfg-"))
    (home / tmp_name).write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    for var in ("HYATLAS_REQUEST_TIMEOUT", "HYATLAS_SERVER_PORT",
                "HYATLAS_MODE", "HYATLAS_GO_DATA"):
        monkeypatch.delenv(var, raising=False)
    return home


def test_null_values_in_profile_fall_back_to_defaults(monkeypatch):
    """The regression: `{"request_timeout": null}` crashed the plugin.

    Consumers read ``cfg.get("request_timeout", 15.0)``, whose default only
    applies when the key is ABSENT. A present-but-null key yields None, so
    ``float(None)`` raised TypeError inside _ensure_client() on the first memory
    operation -- and a null field is exactly what the desktop settings panel
    writes for a setting the user cleared.
    """
    settings = _load_sibling("settings")
    _write_profile(monkeypatch, {
        "request_timeout": None, "server_port": None, "auto_start": None,
        "data_dir": None, "mode": None, "user_id": None, "llm_base": None,
    })
    cfg = settings.load()
    for k, want in (("request_timeout", 15.0), ("server_port", 19528),
                    ("auto_start", False), ("data_dir", ""), ("mode", ""),
                    ("user_id", "default"), ("llm_base", "")):
        assert cfg[k] == want, f"{k}: null overrode the default ({cfg[k]!r})"

    # The exact expression that used to raise.
    assert float(cfg.get("request_timeout", 15.0)) == 15.0
    assert int(cfg.get("server_port", 19528)) == 19528


def test_blank_strings_in_profile_fall_back_to_defaults(monkeypatch):
    """Same hole via "": a cleared text field, not a null one."""
    settings = _load_sibling("settings")
    _write_profile(monkeypatch, {"binary_path": "   ", "agent_id": "",
                                 "llm_model": "", "data_dir": "  "})
    cfg = settings.load()
    for k in ("binary_path", "agent_id", "llm_model", "data_dir"):
        assert cfg[k] == settings.DEFAULTS[k], f"{k}: blank overrode the default"


def test_json_strings_coerce_like_environment_strings(monkeypatch):
    """A profile holding "19528" must become an int, not stay a string.

    Previously only the env layer cast, so the same value behaved differently
    depending on which of the three layers supplied it.
    """
    settings = _load_sibling("settings")
    _write_profile(monkeypatch, {"request_timeout": "30.5", "server_port": "20777",
                                 "auto_start": "true"})
    cfg = settings.load()
    assert cfg["request_timeout"] == 30.5
    assert cfg["server_port"] == 20777
    assert cfg["auto_start"] is True


def test_unparseable_values_keep_the_default(monkeypatch):
    """A numeric typo must degrade to the default, not a string in a numeric slot."""
    settings = _load_sibling("settings")
    _write_profile(monkeypatch, {"request_timeout": "soon", "server_port": "lots"})
    cfg = settings.load()
    assert cfg["request_timeout"] == 15.0
    assert cfg["server_port"] == 19528


def _write_yaml_profile(monkeypatch, settings_block):
    """Point HERMES_HOME at a scratch dir holding config.yaml with a settings block."""
    import tempfile
    home = Path(tempfile.mkdtemp(prefix="hy-nullcfg-yaml-"))
    doc = {"plugins": {"entries": {"hyatlas": {"settings": settings_block}}}}
    (home / "config.yaml").write_text(json.dumps(doc), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    for var in ("HYATLAS_REQUEST_TIMEOUT", "HYATLAS_SERVER_PORT",
                "HYATLAS_MODE", "HYATLAS_GO_DATA"):
        monkeypatch.delenv(var, raising=False)
    return home


def test_null_values_in_config_yaml_fall_back_to_defaults(monkeypatch):
    """The config.yaml layer must coerce like hyatlas.json does.

    This is the third input layer -- `plugins.entries.hyatlas.settings` -- and it
    reads a config a user edits by hand, so a null or a mistyped numeric is just
    as likely there. Covered explicitly because a mutation that reverts this
    layer to raw assignment passes every other test.
    """
    settings = _load_sibling("settings")
    _write_yaml_profile(monkeypatch, {
        "request_timeout": None, "server_port": None, "data_dir": None,
        "binary_path": "   ", "agent_id": "",
    })
    cfg = settings.load()
    assert cfg["request_timeout"] == 15.0
    assert cfg["server_port"] == 19528
    assert cfg["data_dir"] == ""
    assert cfg["binary_path"] == ""
    assert cfg["agent_id"] == "default"
    assert float(cfg.get("request_timeout", 15.0)) == 15.0


def test_config_yaml_strings_coerce_like_the_other_layers(monkeypatch):
    """A hand-written `server_port: "20777"` must become an int, not a str."""
    settings = _load_sibling("settings")
    _write_yaml_profile(monkeypatch, {"request_timeout": "45", "server_port": "20777"})
    cfg = settings.load()
    assert cfg["server_port"] == 20777
    assert cfg["request_timeout"] == 45.0


def test_config_yaml_unparseable_keeps_the_default(monkeypatch):
    """A typo in config.yaml degrades to the default instead of poisoning the slot."""
    settings = _load_sibling("settings")
    _write_yaml_profile(monkeypatch, {"server_port": "lots", "request_timeout": None})
    cfg = settings.load()
    assert cfg["server_port"] == 19528
    assert cfg["request_timeout"] == 15.0


def test_invalid_mode_stays_fatal_rather_than_silently_defaulting(monkeypatch):
    """load() stores the raw mode; mode() refuses one the server would reject.

    Deliberate: a mode the server treats as fatal must not be quietly rewritten
    here, or a user who typed "turbo" would get ultra with no signal. The two
    layers split storage from validation, so this pins the validation half.
    """
    settings = _load_sibling("settings")
    _write_profile(monkeypatch, {"mode": "turbo"})
    cfg = settings.load()
    assert cfg["mode"] == "turbo", "load() must not rewrite the raw value"
    with pytest.raises(ValueError) as exc:
        settings.mode(cfg)
    assert "turbo" in str(exc.value)
    assert "lite" in str(exc.value) and "ultra" in str(exc.value), (
        "the error must name the valid set so the user can correct it")
    # A valid one normalises rather than raising.
    _write_profile(monkeypatch, {"mode": "PRO"})
    assert settings.mode(settings.load()) == "pro"


def test_coerce_is_the_single_interpretation_path(monkeypatch):
    """All three layers share one coercion helper -- pin that, or they drift."""
    settings = _load_sibling("settings")
    # Env layer and JSON layer must agree on the same raw string.
    _write_profile(monkeypatch, {"server_port": "20777"})
    from_json = settings.load()["server_port"]
    monkeypatch.setenv("HYATLAS_SERVER_PORT", "20777")
    from_env = settings.load()["server_port"]
    assert from_json == from_env == 20777

    # Env wins over the profile, matching the documented precedence.
    _write_profile(monkeypatch, {"server_port": "19528"})
    monkeypatch.setenv("HYATLAS_SERVER_PORT", "20777")
    assert settings.load()["server_port"] == 20777


def test_casts_table_covers_every_persisted_setting(monkeypatch):
    """CASTS is derived from ENV; a persisted setting missing there coerces as str.

    llm_key is the one intended exclusion: it is declared secret, so the wizard
    routes it to Hermes' .env and save_config strips it. The plugin never
    persists or forwards it, and the server reads it from its own environment.
    """
    settings = _load_sibling("settings")
    assert set(settings.KEYS) - set(settings.CASTS) == {"llm_key"}, (
        f"unexpected ENV/CASTS coverage: "
        f"missing={sorted(set(settings.KEYS) - set(settings.CASTS))}")
    assert "llm_key" not in settings.CASTS
    # And the exclusion is the security property, not an oversight.
    field = {f["key"]: f for f in settings.SCHEMA}["llm_key"]
    assert field.get("secret") is True
    assert field.get("env_var") == "HYATLAS_LLM_KEY"


# ---- bool field must not become a string-coerced select ----

def test_bool_field_declares_no_choices():
    """A bool field with `choices` is rendered as a select, which stringifies it.

    The desktop bridge classifies a field with choices as `select` before it
    checks the declared type, and a select's value is coerced to str. Then
    `"false"` is truthy in Python, so picking "false" in the settings panel
    silently turned auto-start ON. Pin the shape rather than trusting one
    consumer to coerce.
    """
    settings = _load_sibling("settings")
    for f in settings.config_schema():
        if f.get("type") in ("bool", "boolean"):
            assert not f.get("choices"), (
                f"{f['key']} is a bool but declares choices; the desktop panel "
                f"would render it as a select and stringify the value"
            )


def test_truthy_handles_strings_from_a_select():
    """Any bool read from config must treat the string "false" as false."""
    settings = _load_sibling("settings")
    for v in (True, "true", "True", "TRUE", "1", "yes", "on"):
        assert settings.truthy(v) is True, v
    for v in (False, "false", "False", "FALSE", "0", "no", "off", "", None, [], "banana"):
        assert settings.truthy(v) is False, v


def test_auto_start_respects_string_false():
    """The regression: a hyatlas.json holding "false" must not enable auto-start."""
    settings = _load_sibling("settings")
    # Mirrors what a select-coerced value or a hand-edited JSON file produces.
    for cfg in ({"auto_start": "false"}, {"auto_start": "False"}, {"auto_start": "0"}):
        assert settings.truthy(cfg.get("auto_start")) is False, cfg
    for cfg in ({"auto_start": "true"}, {"auto_start": True}):
        assert settings.truthy(cfg.get("auto_start")) is True, cfg


def test_schema_types_match_manifest_types():
    """SCHEMA's `type` must agree with plugin.yaml, or the two renderers disagree.

    Checked against settings.SCHEMA rather than config_schema(): the latter
    intentionally drops `type` and `label`, which belong to the manifest.
    """
    yaml = pytest.importorskip("yaml")
    settings = _load_sibling("settings")
    declared = yaml.safe_load((Path(_PKG) / "plugin.yaml").read_text(encoding="utf-8"))["config_schema"]
    aliases = {"bool": {"bool", "boolean"}, "str": {"str", "string", "text"},
               "int": {"int", "integer"}, "float": {"float", "number"}}
    for f in settings.SCHEMA:
        want = declared[f["key"]]["type"]
        allowed = aliases.get(want, {want})
        assert f.get("type") in allowed, (
            f"{f['key']}: SCHEMA type {f.get('type')!r} vs manifest {want!r}")


def test_config_schema_drops_manifest_only_fields():
    """type and label belong to the manifest, so the provider schema omits them.

    Pinned because the omission is deliberate: duplicating them invites drift
    between the CLI wizard and the Desktop panel. But `secret`, `env_var` and
    `url` MUST survive, since they change how the wizard stores a credential.
    """
    settings = _load_sibling("settings")
    for f in settings.config_schema():
        assert "type" not in f, f"{f['key']} leaks a manifest-only field"
        assert "label" not in f, f"{f['key']} leaks a manifest-only field"
    key = {f["key"]: f for f in settings.config_schema()}["llm_key"]
    assert key["secret"] is True
    assert key["env_var"] == "HYATLAS_LLM_KEY"
