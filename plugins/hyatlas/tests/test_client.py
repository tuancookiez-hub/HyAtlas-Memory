"""Tests for the client's proxy bypass, the single plugin version, and the env bool mapping.

These load client.py and settings.py by file path, so they run without Hermes
installed. Only the __init__.py test needs ``agent.memory_provider`` and is skipped
when Hermes is absent.

    python -m pytest plugins/hyatlas/tests/test_client.py -q
"""

from __future__ import annotations

import importlib.util
import sys
import json
import os
import socket
import threading

import pytest

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_HERE = os.path.dirname(os.path.abspath(__file__))
_PKG = os.path.dirname(_HERE)


def _load_module(name: str, filename: str):
    """Load one module of the package by file path, with no package context."""
    spec = importlib.util.spec_from_file_location(name, os.path.join(_PKG, filename))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _plugin_yaml_version() -> str:
    """The version as written in plugin.yaml, read independently of client.py."""
    with open(os.path.join(_PKG, "plugin.yaml"), encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("version:"):
                return line.split(":", 1)[1].strip()
    raise AssertionError("plugin.yaml has no version line")


class _Recorder(BaseHTTPRequestHandler):
    """Answers /healthz with status ok and records the User-Agent of each request."""

    seen_agents: list = []

    def do_GET(self):  # noqa: N802 - http.server naming
        type(self).seen_agents.append(self.headers.get("User-Agent", ""))
        body = json.dumps({"status": "ok"}).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args, **kwargs):  # silence the test server
        pass


@pytest.fixture
def local_server():
    """A real loopback HTTP server, shut down after the test."""
    _Recorder.seen_agents = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Recorder)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        server.server_close()


def _unreachable_proxy_url() -> str:
    """A proxy address that nothing listens on: bind a port, then close it."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return f"http://127.0.0.1:{port}"


def test_loopback_call_ignores_http_proxy(monkeypatch, local_server):
    """A loopback request must not go through HTTP_PROXY, even when it is set."""
    proxy = _unreachable_proxy_url()
    monkeypatch.setenv("HTTP_PROXY", proxy)
    monkeypatch.setenv("http_proxy", proxy)
    monkeypatch.setenv("HTTPS_PROXY", proxy)
    monkeypatch.setenv("https_proxy", proxy)
    client_mod = _load_module("hyatlas_client_proxy_test", "client.py")

    port = local_server.server_address[1]
    client = client_mod.HyatlasClient(base_url=f"http://127.0.0.1:{port}", timeout=5.0)
    assert client.is_reachable() is True


def test_plugin_version_matches_plugin_yaml():
    """PLUGIN_VERSION is the version plugin.yaml declares, never the fallback."""
    client_mod = _load_module("hyatlas_client_version_test", "client.py")
    assert client_mod.PLUGIN_VERSION == _plugin_yaml_version()
    assert client_mod.PLUGIN_VERSION != "unknown"


def test_user_agent_carries_plugin_version(local_server):
    """Every request announces the same version as the plugin."""
    client_mod = _load_module("hyatlas_client_ua_test", "client.py")
    port = local_server.server_address[1]
    client = client_mod.HyatlasClient(base_url=f"http://127.0.0.1:{port}", timeout=5.0)
    assert client.is_reachable() is True
    assert _Recorder.seen_agents, "the test server saw no request"
    assert _Recorder.seen_agents[-1] == "hermes-hyatlas/" + _plugin_yaml_version()


def test_provider_version_is_plugin_version(monkeypatch, tmp_path):
    """The provider reports the same version as the User-Agent, not a hard-coded one."""
    pytest.importorskip("agent.memory_provider")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    client_mod = _load_module("hyatlas_client_provider_test", "client.py")

    name = "hyatlas_provider_version_test"
    spec = importlib.util.spec_from_file_location(
        name,
        os.path.join(_PKG, "__init__.py"),
        submodule_search_locations=[_PKG],
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    try:
        spec.loader.exec_module(mod)
        provider = mod.HyatlasMemoryProvider()
        assert provider._version == client_mod.PLUGIN_VERSION
    finally:
        sys.modules.pop(name, None)


@pytest.mark.parametrize("raw", ["1", "true", "yes", "on", "ON"])
def test_env_bool_accepts_truthy_words(monkeypatch, tmp_path, raw):
    """HYATLAS_AUTO_START and HYATLAS_SHARE_LLM_KEY accept every word truthy() accepts."""
    settings = _load_module("hyatlas_settings_truthy_test", "settings.py")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HYATLAS_AUTO_START", raw)
    monkeypatch.setenv("HYATLAS_SHARE_LLM_KEY", raw)
    cfg = settings.load()
    assert cfg["auto_start"] is True
    assert cfg["share_llm_key"] is True


@pytest.mark.parametrize("raw", ["0", "false", "no", "off"])
def test_env_bool_rejects_falsy_words(monkeypatch, tmp_path, raw):
    """The same two variables read every falsy word as False."""
    settings = _load_module("hyatlas_settings_falsy_test", "settings.py")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HYATLAS_AUTO_START", raw)
    monkeypatch.setenv("HYATLAS_SHARE_LLM_KEY", raw)
    cfg = settings.load()
    assert cfg["auto_start"] is False
    assert cfg["share_llm_key"] is False
