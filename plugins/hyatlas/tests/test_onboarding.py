"""Tests for the setup wizard's questions and the local key hand-over.

The schema, mode and loopback tests load settings.py by file path, so they run
without Hermes. The hand-over tests need the provider, which imports
``agent.memory_provider``, and are skipped when Hermes is absent.

    python -m pytest plugins/hyatlas/tests/test_onboarding.py -q
"""

from __future__ import annotations

import importlib.util
import json
import logging
import os
import re
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_PKG = os.path.dirname(_HERE)

_SECRET = "sk-test-SECRET-key-123"


def _load_settings():
    spec = importlib.util.spec_from_file_location(
        "hyatlas_onboarding_settings", os.path.join(_PKG, "settings.py"))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


settings = _load_settings()


def _load_provider_module():
    pytest.importorskip("agent.memory_provider")
    spec = importlib.util.spec_from_file_location(
        "hyatlas_onboarding_pkg", os.path.join(_PKG, "__init__.py"),
        submodule_search_locations=[_PKG])
    m = importlib.util.module_from_spec(spec)
    sys.modules["hyatlas_onboarding_pkg"] = m
    spec.loader.exec_module(m)
    return m


# ---------------------------------------------------------------------------
# The wizard's questions
# ---------------------------------------------------------------------------

def test_setup_asks_exactly_five_questions_in_order():
    keys = [f["key"] for f in settings.config_schema()]
    assert keys == ["mode", "llm_base", "llm_key", "llm_model", "auto_start"]
    assert [f["key"] for f in settings.ONBOARDING] == keys
    assert [f["key"] for f in settings.SCHEMA] == keys + [f["key"] for f in settings.ADVANCED]
    assert len(settings.SCHEMA) == 14


def test_the_five_prompts_read_as_designed():
    by_key = {f["key"]: f for f in settings.config_schema()}
    assert by_key["mode"]["description"] == (
        "How much should HyAtlas think? (your memories always stay on this machine)")
    assert [c.split()[0] for c in by_key["mode"]["choices"]] == ["ultra", "pro", "lite"]
    assert by_key["mode"]["default"] == by_key["mode"]["choices"][0]
    assert by_key["llm_base"]["description"] == "AI endpoint (Enter = Nous Portal; not used in lite)"
    assert by_key["llm_base"]["default"] == "https://inference-api.nousresearch.com/v1"
    assert by_key["llm_key"]["description"] == (
        "API key for that endpoint (saved in Hermes's .env; Enter to skip in lite)")
    assert by_key["llm_key"]["secret"] is True
    assert by_key["llm_key"]["env_var"] == "HYATLAS_LLM_KEY"
    assert by_key["llm_model"]["description"] == (
        "Model (Enter = a free Nous Portal model; not used in lite)")
    assert by_key["llm_model"]["default"] == "meituan/longcat-2.5-preview:free"
    assert by_key["auto_start"]["description"] == "Auto-start the HyAtlas server with Hermes? (y/N)"
    assert by_key["auto_start"]["default"] == "n"
    assert "choices" not in by_key["auto_start"], "auto_start is a plain text prompt, not a menu"


def test_prompts_use_no_jargon():
    for f in settings.config_schema():
        assert "host" not in f["description"].lower(), f["key"]


def test_advanced_settings_are_marked_short_and_explained():
    for f in settings.ADVANCED:
        assert f["description"].startswith("Advanced:"), f["key"]
        assert f["label"].startswith("Advanced: "), f["key"]
        assert len(f["description"]) <= 90, f["key"]
        assert f["help"], f["key"]


def test_plugin_yaml_declares_every_setting():
    yaml = pytest.importorskip("yaml")
    manifest = yaml.safe_load((Path(_PKG) / "plugin.yaml").read_text(encoding="utf-8"))
    declared = manifest["config_schema"]
    assert list(declared) == [f["key"] for f in settings.ONBOARDING + settings.ADVANCED], \
        "onboarding first, then advanced, in schema order"
    for f in settings.SCHEMA:
        d = declared[f["key"]]
        assert d["description"] == f["description"], f["key"]
        assert d["label"] == f["label"], f["key"]
        assert d["default"] == f["default"], f["key"]
        assert d.get("choices") == f.get("choices"), f["key"]
        assert bool(d.get("secret")) == bool(f.get("secret")), f["key"]
        assert d.get("env_var") == f.get("env_var"), f["key"]


@pytest.mark.parametrize("value,want", [
    ("y", True), ("Y", True), ("yes", True), ("YES", True), ("true", True), ("On", True),
    ("1", True), (True, True),
    ("n", False), ("N", False), ("no", False), ("false", False), ("OFF", False),
    ("0", False), ("", False), (None, False), (False, False),
])
def test_truthy_reads_a_typed_yes_or_no(value, want):
    assert settings.truthy(value) is want


def test_the_default_answer_means_no(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HYATLAS_AUTO_START", raising=False)
    assert settings.load()["auto_start"] is False


def test_old_bool_and_typed_answers_in_hyatlas_json_keep_working(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HYATLAS_AUTO_START", raising=False)
    for stored, want in ((True, True), (False, False), ("y", True), ("N", False), ("true", True)):
        (tmp_path / "hyatlas.json").write_text(json.dumps({"auto_start": stored}), encoding="utf-8")
        assert settings.load()["auto_start"] is want, stored


# ---------------------------------------------------------------------------
# Re-running setup keeps advanced values
# ---------------------------------------------------------------------------

def test_save_config_keeps_advanced_values_and_never_writes_the_secret(tmp_path):
    pkg = _load_provider_module()
    (tmp_path / "hyatlas.json").write_text(
        json.dumps({"server_port": 19600, "data_dir": "D:/memories", "mode": "lite"}),
        encoding="utf-8")
    p = pkg.HyatlasMemoryProvider()
    p.save_config({"mode": settings.MODE_CHOICES[1], "llm_base": "https://x.example/v1",
                   "llm_key": _SECRET, "llm_model": "m", "auto_start": "y"}, str(tmp_path))
    saved = json.loads((tmp_path / "hyatlas.json").read_text(encoding="utf-8"))
    assert saved["server_port"] == 19600 and saved["data_dir"] == "D:/memories"
    assert saved["mode"] == settings.MODE_CHOICES[1]
    assert saved["llm_base"] == "https://x.example/v1" and saved["llm_model"] == "m"
    assert saved["auto_start"] is True
    assert "llm_key" not in saved
    assert _SECRET not in (tmp_path / "hyatlas.json").read_text(encoding="utf-8")


def test_save_profile_keeps_advanced_values_without_hermes(tmp_path):
    (tmp_path / "hyatlas.json").write_text(
        json.dumps({"server_port": 19600, "data_dir": "D:/memories"}), encoding="utf-8")
    settings.save_profile({"mode": "lite", "llm_key": _SECRET, "auto_start": False}, tmp_path)
    saved = json.loads((tmp_path / "hyatlas.json").read_text(encoding="utf-8"))
    assert saved == {"server_port": 19600, "data_dir": "D:/memories", "mode": "lite",
                     "auto_start": False}


def test_save_profile_survives_a_broken_file(tmp_path):
    (tmp_path / "hyatlas.json").write_text("{not json", encoding="utf-8")
    settings.save_profile({"server_port": 20000}, tmp_path)
    assert json.loads((tmp_path / "hyatlas.json").read_text(encoding="utf-8")) == {"server_port": 20000}


# ---------------------------------------------------------------------------
# hermes hyatlas settings
# ---------------------------------------------------------------------------

def _load_cli():
    """Load cli.py as a package module, as Hermes does (it uses relative imports)."""
    spec = importlib.util.spec_from_file_location(
        "hyatlas_cli_pkg", os.path.join(_PKG, "__init__.py"), submodule_search_locations=[_PKG])
    pkg = importlib.util.module_from_spec(spec)
    sys.modules["hyatlas_cli_pkg"] = pkg
    # __init__.py needs Hermes; the CLI only needs the package namespace for its siblings.
    for name in ("settings", "client", "cli"):
        sub_spec = importlib.util.spec_from_file_location(
            f"hyatlas_cli_pkg.{name}", os.path.join(_PKG, f"{name}.py"))
        mod = importlib.util.module_from_spec(sub_spec)
        sys.modules[f"hyatlas_cli_pkg.{name}"] = mod
        sub_spec.loader.exec_module(mod)
    return sys.modules["hyatlas_cli_pkg.cli"]


@pytest.fixture
def cli_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for var in ("HYATLAS_LLM_KEY", "HYATLAS_LLM_KEY_FILE", "HYATLAS_AUTO_START", "HYATLAS_MODE",
                "HYATLAS_SERVER_PORT", "HYATLAS_SERVER_HOST"):
        monkeypatch.delenv(var, raising=False)
    return _load_cli(), tmp_path


def _run_cli(cli, *argv):
    import argparse
    parser = argparse.ArgumentParser()
    cli.register_cli(parser)
    args = parser.parse_args(list(argv))
    return args.func(args)


def test_settings_lists_every_setting_with_its_source(cli_env, capsys, monkeypatch):
    cli, home = cli_env
    (home / "hyatlas.json").write_text(json.dumps({"server_port": 19600}), encoding="utf-8")
    monkeypatch.setenv("HYATLAS_MODE", "lite")
    monkeypatch.setenv("HYATLAS_LLM_KEY", _SECRET)
    assert _run_cli(cli, "settings") == 0
    out = capsys.readouterr().out
    assert out.index("Basics") < out.index("Advanced")
    for f in settings.SCHEMA:
        assert f["key"] in out, f["key"]
    assert re.search(r"server_port\s+19600\s+\[hyatlas\.json\]", out)
    assert re.search(r"mode\s+lite\s+\[env\]", out)
    assert re.search(r"server_host\s+127\.0\.0\.1\s+\[default\]", out)
    assert "keep 127.0.0.1 unless it is on another machine" in out
    assert re.search(r"llm_key\s+set\s+\[env\]", out)
    assert _SECRET not in out


def test_settings_shows_the_key_as_unset(cli_env, capsys):
    cli, _ = cli_env
    assert _run_cli(cli, "settings") == 0
    assert re.search(r"llm_key\s+unset", capsys.readouterr().out)


def test_settings_set_writes_a_validated_value_and_keeps_the_rest(cli_env, capsys):
    cli, home = cli_env
    (home / "hyatlas.json").write_text(json.dumps({"data_dir": "D:/m"}), encoding="utf-8")
    assert _run_cli(cli, "settings", "set", "server_port", "19700") == 0
    assert _run_cli(cli, "settings", "set", "request_timeout", "30") == 0
    assert _run_cli(cli, "settings", "set", "auto_start", "Yes") == 0
    assert _run_cli(cli, "settings", "set", "mode", settings.MODE_CHOICES[2]) == 0
    saved = json.loads((home / "hyatlas.json").read_text(encoding="utf-8"))
    assert saved == {"data_dir": "D:/m", "server_port": 19700, "request_timeout": 30.0,
                     "auto_start": True, "mode": "lite"}


@pytest.mark.parametrize("key,value", [
    ("server_port", "abc"), ("request_timeout", "soon"), ("mode", "turbo"),
    ("sync", "maybe"), ("no_such_setting", "1"),
])
def test_settings_set_refuses_a_bad_value(cli_env, capsys, key, value):
    cli, home = cli_env
    assert _run_cli(cli, "settings", "set", key, value) == 1
    assert "Not changed" in capsys.readouterr().out
    assert not (home / "hyatlas.json").exists()


def test_settings_set_refuses_the_key_and_points_at_setup(cli_env, capsys):
    cli, home = cli_env
    assert _run_cli(cli, "settings", "set", "llm_key", _SECRET) == 1
    out = capsys.readouterr().out
    assert "hermes memory setup" in out and _SECRET not in out
    assert not (home / "hyatlas.json").exists()


def test_settings_set_warns_when_a_higher_layer_wins(cli_env, capsys, monkeypatch):
    cli, _ = cli_env
    monkeypatch.setenv("HYATLAS_SERVER_PORT", "19999")
    assert _run_cli(cli, "settings", "set", "server_port", "19700") == 0
    out = capsys.readouterr().out
    assert "also set in env" in out and "wins" in out


def test_no_user_facing_v4():
    """The product is HyAtlas, not "v4": no prompt, help text or README may say so."""
    pattern = re.compile(r"\bv4\b", re.IGNORECASE)
    offenders = []
    for f in settings.SCHEMA:
        for field in ("label", "description"):
            if pattern.search(str(f[field])):
                offenders.append(f"SCHEMA {f['key']}.{field}")
    for name in ("plugin.yaml", "README.md", "after-install.md", "cli.py", "client.py",
                 "__init__.py", "settings.py", "schemas.py", "process.py", "doctor.py",
                 "dashboard/plugin_api.py", "dashboard/manifest.json"):
        for n, line in enumerate((Path(_PKG) / name).read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line.replace("hyatlas-v4", "")):
                offenders.append(f"{name}:{n}")
    assert not offenders, offenders


# ---------------------------------------------------------------------------
# normalize_mode
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("stored,want", [
    ("", ""), (None, ""), ("  ", ""),
    ("lite", "lite"), ("pro", "pro"), ("ultra", "ultra"), ("ULTRA", "ultra"),
    (settings.MODE_CHOICES[0], "ultra"),
    (settings.MODE_CHOICES[1], "pro"),
    (settings.MODE_CHOICES[2], "lite"),
    ("lite — anything after", "lite"),
])
def test_normalize_mode_for_every_stored_form(stored, want):
    assert settings.normalize_mode(stored) == want


def test_mode_accepts_menu_lines_and_rejects_unknown_words():
    for line, want in zip(settings.MODE_CHOICES, ("ultra", "pro", "lite")):
        assert settings.mode({"mode": line}) == want
    assert settings.mode({"mode": ""}) == ""
    with pytest.raises(ValueError):
        settings.mode({"mode": "turbo"})


def test_environment_mode_is_normalised(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HYATLAS_MODE", settings.MODE_CHOICES[2])
    assert settings.load()["mode"] == "lite"


def test_sync_default_means_follow_the_mode():
    assert settings.sync({"sync": "default"}) == ""
    assert settings.sync({"sync": "on"}) == "on"


# ---------------------------------------------------------------------------
# The key hand-over
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("host,want", [
    ("127.0.0.1", True), ("127.5.6.7", True), ("::1", True), ("[::1]", True),
    ("localhost", True), ("LOCALHOST", True),
    ("10.0.0.5", False), ("0.0.0.0", False), ("example.com", False), ("", False),
])
def test_is_loopback_host(host, want):
    assert settings.is_loopback_host(host) is want


class _Server:
    """A local server that reports an llm state and records POST /api/v1/llm/key."""

    def __init__(self, llm: str, key_status: int = 200):
        self.llm = llm
        self.key_status = key_status
        self.keys: list = []
        self.status_calls = 0
        outer = self

        class H(BaseHTTPRequestHandler):
            def _send(self, code, payload):
                body = json.dumps(payload).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):  # noqa: N802
                if self.path == "/api/v1/status":
                    outer.status_calls += 1
                    self._send(200, {"llm": outer.llm})
                else:
                    self._send(200, {"status": "ok"})

            def do_POST(self):  # noqa: N802
                n = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(n) or b"{}")
                if self.path == "/api/v1/llm/key":
                    outer.keys.append(body.get("key"))
                    if outer.key_status == 200:
                        self._send(200, {"ok": True, "llm": "ok"})
                    else:
                        self._send(outer.key_status, {"error": "refused"})
                else:
                    self._send(404, {"error": "no"})

            def log_message(self, *a, **k):
                pass

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def provider_for(monkeypatch, tmp_path):
    pkg = _load_provider_module()
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    servers = []

    def make(llm="awaiting_key", key=_SECRET, host="127.0.0.1", key_status=200):
        srv = _Server(llm, key_status)
        servers.append(srv)
        if key is None:
            monkeypatch.delenv("HYATLAS_LLM_KEY", raising=False)
        else:
            monkeypatch.setenv("HYATLAS_LLM_KEY", key)
        p = pkg.HyatlasMemoryProvider()
        p._config = {**p._config, "server_host": host, "server_port": srv.port}
        p._client = None
        return p, srv

    yield make
    for s in servers:
        s.close()


def test_key_is_handed_to_a_local_server_awaiting_one(provider_for):
    p, srv = provider_for()
    assert p._hand_over_key(force=True) is True
    assert srv.keys == [_SECRET]


def test_key_is_not_handed_when_the_server_is_fine(provider_for):
    p, srv = provider_for(llm="ok")
    assert p._hand_over_key(force=True) is False
    assert srv.keys == []


def test_key_is_not_handed_when_it_is_empty(provider_for):
    p, srv = provider_for(key="  ")
    assert p._hand_over_key(force=True) is False
    assert srv.keys == [] and srv.status_calls == 0


def test_key_is_not_handed_without_a_key_in_the_environment(provider_for):
    p, srv = provider_for(key=None)
    assert p._hand_over_key(force=True) is False
    assert srv.keys == []


def test_key_is_not_handed_to_a_remote_host(provider_for):
    p, srv = provider_for(host="10.9.8.7")
    assert p._hand_over_key(force=True) is False
    assert srv.keys == [] and srv.status_calls == 0


def test_check_is_rate_limited_unless_forced(provider_for):
    p, srv = provider_for(llm="ok")
    p._hand_over_key(force=True)
    calls = srv.status_calls
    assert p._hand_over_key() is False
    assert srv.status_calls == calls, "a second check within 10 minutes must not call the server"
    p._hand_over_key(force=True)
    assert srv.status_calls == calls + 1


def test_key_never_appears_in_logs(provider_for, caplog):
    caplog.set_level(logging.DEBUG)
    for status in (200, 409, 400):
        p, srv = provider_for(key_status=status)
        p._hand_over_key(force=True)
    assert _SECRET not in caplog.text


def test_plugin_never_touches_hermes_provider_tokens():
    """Only the plugin's own key is shared; no Hermes sign-in or provider token is read."""
    offenders = []
    for f in sorted(Path(_PKG).rglob("*.py")):
        if "tests" in f.parts:
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        for needle in ("runtime_provider", "auth.json", "import hermes_cli", "from hermes_cli"):
            if needle in text:
                offenders.append(f"{f.name}: {needle}")
    assert not offenders, offenders
