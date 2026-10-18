"""Tests for ``hermes hyatlas doctor``: each check's pass and fail path, exit codes, no key.

The doctor imports nothing from Hermes, so these run without it. The package is
registered as a bare namespace (its ``__init__`` needs Hermes and is not executed).

    python -m pytest plugins/hyatlas/tests/test_doctor.py -q
"""

from __future__ import annotations

import importlib
import os
import sys
import types

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_PKG = os.path.dirname(_HERE)

_SECRET = "sk-doctor-SECRET-key-987"


def _load():
    name = "hyatlas_doctor_pkg"
    pkg = types.ModuleType(name)
    pkg.__path__ = [_PKG]
    sys.modules[name] = pkg
    return (importlib.import_module(name + ".doctor"),
            importlib.import_module(name + ".client"))


doctor, client_mod = _load()


class FakeClient:
    """A stand-in for HyatlasClient that answers from a dict, with switches for failures."""

    base_url = "http://127.0.0.1:19528"

    def __init__(self, **over):
        self.reachable = over.get("reachable", True)
        self.status_body = {
            "status": "ok", "version": client_mod.PLUGIN_VERSION, "mode": "ultra",
            "llm": "ok", "llm_model": "m", "unextracted": 0,
        }
        self.status_body.update(over.get("status", {}))
        self.llm_result = over.get("llm_result", {
            "ok": True, "model": "m", "base_host": "api.example.com", "latency_ms": 1200})
        self.llm_exc = over.get("llm_exc")
        self.add_resp = over.get("add_resp")
        self.find = over.get("find", True)
        self.delete_exc = over.get("delete_exc")
        self.added = []
        self.deleted = []
        self.llm_calls = 0
        self.accept_key = over.get("accept_key", False)
        self.keys_set = []

    def is_reachable(self):
        return self.reachable

    def status(self):
        return self.status_body

    def set_llm_key(self, key):
        self.keys_set.append(key)
        if self.accept_key:
            self.status_body["llm"] = "ok"
        return {"ok": True}

    def llm_test(self):
        self.llm_calls += 1
        if self.llm_exc:
            raise self.llm_exc
        return self.llm_result

    def add(self, text, user_id="", agent_id="", session_id="", metadata=None):
        self.added.append({"text": text, "user_id": user_id, "metadata": metadata})
        if self.add_resp is not None:
            return self.add_resp
        return {"success": True, "memory_id": "mem-1"}

    def search(self, query, user_id="", agent_id="", layer="", limit=10, session_id=""):
        hits = [{"memory_id": "mem-1", "content": query}] if self.find else []
        return {"memories": {"normal": hits}}

    def delete_memory(self, memory_id):
        if self.delete_exc:
            raise self.delete_exc
        self.deleted.append(memory_id)
        return {"deleted_count": 1}


def run(fake, cfg=None, env_key=None, monkeypatch=None):
    """Run the doctor with ASCII-neutral marks, returning (exit code, output text)."""
    if monkeypatch is not None:
        if env_key is None:
            monkeypatch.delenv("HYATLAS_LLM_KEY", raising=False)
        else:
            monkeypatch.setenv("HYATLAS_LLM_KEY", env_key)
    lines = []
    cfg = dict({"server_host": "127.0.0.1", "server_port": 19528, "auto_start": False, "mode": ""},
               **(cfg or {}))
    code = doctor.run_doctor(fake, cfg, emit=lines.append,
                             marks={doctor.OK: "✓", doctor.WARN: "!", doctor.FAIL: "✗"}, sleep=lambda s: None)
    return code, "\n".join(lines)


def test_healthy_setup_passes(monkeypatch):
    fake = FakeClient()
    code, out = run(fake, env_key=_SECRET, monkeypatch=monkeypatch)
    assert code == 0, out
    assert "✗" not in out and "Fix:" not in out
    assert "All good" in out
    for label in ("Provider", "Server", "Version", "Mode", "AI key", "Model", "Round trip", "Backlog"):
        assert label in out
    assert fake.deleted == ["mem-1"], "the test memory must be deleted by id"
    assert fake.added[0]["metadata"] == {"write_origin": "doctor"}
    assert fake.added[0]["text"].startswith("hyatlas doctor check ")
    assert fake.added[0]["user_id"] == doctor.DOCTOR_USER


def test_unreachable_server_fails_with_start_fix(monkeypatch):
    fake = FakeClient(reachable=False)
    code, out = run(fake, monkeypatch=monkeypatch)
    assert code == 1
    assert "✗ Server" in out and "hermes hyatlas start" in out and "auto_start is off" in out
    assert "skipped" in out
    assert fake.added == []


def test_unreachable_with_auto_start_on_points_at_the_log(monkeypatch):
    code, out = run(FakeClient(reachable=False), cfg={"auto_start": True}, monkeypatch=monkeypatch)
    assert code == 1 and "auto_start is on" in out and "hyatlas.log" in out


def test_unreachable_remote_host_mentions_allowed_hosts(monkeypatch):
    code, out = run(FakeClient(reachable=False), cfg={"server_host": "memory.lan"}, monkeypatch=monkeypatch)
    assert code == 1 and "HYATLAS_ALLOWED_HOSTS" in out


def test_version_mismatch_warns_but_passes(monkeypatch):
    code, out = run(FakeClient(status={"version": "4.9.1"}), env_key=_SECRET, monkeypatch=monkeypatch)
    assert code == 0
    assert "! Version" in out and "Fix:" in out


def test_mode_line_explains_each_mode(monkeypatch):
    for mode, phrase in (("lite", "no AI calls"), ("pro", "nothing runs in the background"),
                         ("ultra", "consolidation")):
        fake = FakeClient(status={"mode": mode, "llm": "unused" if mode == "lite" else "ok"})
        code, out = run(fake, env_key=_SECRET, monkeypatch=monkeypatch)
        assert code == 0 and phrase in out, (mode, out)


def test_mode_mismatch_with_setting_warns(monkeypatch):
    code, out = run(FakeClient(status={"mode": "lite", "llm": "unused"}),
                    cfg={"mode": "ultra — recommended: one AI call per turn"}, monkeypatch=monkeypatch)
    assert code == 0 and "! Mode" in out and "your setting is ultra" in out


def test_lite_needs_no_key_and_makes_no_model_call(monkeypatch):
    fake = FakeClient(status={"mode": "lite", "llm": "unused"})
    code, out = run(fake, monkeypatch=monkeypatch)
    assert code == 0 and "not needed in lite" in out
    assert fake.llm_calls == 0 and "Model" not in out


@pytest.mark.parametrize("cfg,env,phrase", [
    ({"auto_start": True}, _SECRET, "`hermes memory setup` and enter the API key"),
    ({"auto_start": False}, _SECRET, "handed HYATLAS_LLM_KEY to the server, but it is still waiting"),
    ({"auto_start": False}, None, "sees no key"),
    ({"server_host": "10.0.0.5"}, _SECRET, "where the server runs"),
])
def test_awaiting_key_fix_depends_on_how_the_server_starts(monkeypatch, cfg, env, phrase):
    fake = FakeClient(status={"llm": "awaiting_key"})
    code, out = run(fake, cfg=cfg, env_key=env, monkeypatch=monkeypatch)
    assert code == 1
    assert "✗ AI key" in out and phrase in out, out
    assert fake.llm_calls == 0, "no model call without a key"


def test_unconfigured_llm_fails_with_setup_fix(monkeypatch):
    code, out = run(FakeClient(status={"llm": "unconfigured"}), env_key=_SECRET, monkeypatch=monkeypatch)
    assert code == 1 and "✗ AI key" in out and "hermes memory setup" in out


def test_model_ok_shows_model_host_and_latency(monkeypatch):
    code, out = run(FakeClient(), env_key=_SECRET, monkeypatch=monkeypatch)
    assert "✓ Model" in out and "1.2 s" in out and "api.example.com" in out


@pytest.mark.parametrize("error,phrase", [
    ("LLM HTTP 401: unauthorized", "refused the key"),
    ("LLM HTTP 404: model not found", "model id is probably wrong"),
    ("LLM HTTP 429: slow down", "rate limiting"),
    ("context deadline exceeded", "did not answer in time"),
])
def test_model_error_gets_a_matching_fix(monkeypatch, error, phrase):
    fake = FakeClient(llm_result={"ok": False, "model": "m", "base_host": "h", "latency_ms": 5, "error": error})
    code, out = run(fake, env_key=_SECRET, monkeypatch=monkeypatch)
    assert code == 1
    assert "✗ Model" in out and error in out and phrase in out, out


def test_model_test_rate_limited_is_only_a_warning(monkeypatch):
    fake = FakeClient(llm_exc=client_mod.HyatlasClientError("POST /api/v1/llm/test -> HTTP 429: limited"))
    code, out = run(fake, env_key=_SECRET, monkeypatch=monkeypatch)
    assert code == 0 and "! Model" in out and "one per 10 seconds" in out


def test_old_server_without_model_test_is_a_warning(monkeypatch):
    fake = FakeClient(llm_exc=client_mod.HyatlasClientError("POST /api/v1/llm/test -> HTTP 404: nope"))
    code, out = run(fake, env_key=_SECRET, monkeypatch=monkeypatch)
    assert code == 0 and "too old" in out


def test_round_trip_miss_fails_in_ultra_and_warns_in_lite(monkeypatch):
    code, out = run(FakeClient(find=False), env_key=_SECRET, monkeypatch=monkeypatch)
    assert code == 1 and "✗ Round trip" in out
    lite = FakeClient(find=False, status={"mode": "lite", "llm": "unused"})
    code, out = run(lite, monkeypatch=monkeypatch)
    assert code == 0 and "! Round trip" in out
    assert lite.deleted == ["mem-1"], "even a miss cleans up"


def test_round_trip_add_failure_fails(monkeypatch):
    fake = FakeClient(add_resp={"success": False, "error": "boom"})
    code, out = run(fake, env_key=_SECRET, monkeypatch=monkeypatch)
    assert code == 1 and "✗ Round trip" in out and fake.deleted == []


def test_cleanup_failure_warns(monkeypatch):
    fake = FakeClient(delete_exc=client_mod.HyatlasClientError("HTTP 500"))
    code, out = run(fake, env_key=_SECRET, monkeypatch=monkeypatch)
    assert code == 0 and "! Cleanup" in out


def test_backlog_warns_without_failing(monkeypatch):
    code, out = run(FakeClient(status={"unextracted": 7}), env_key=_SECRET, monkeypatch=monkeypatch)
    assert code == 0
    assert "! Backlog" in out and "7 turns wait for extraction" in out and "HYATLAS_CATCHUP_EVERY" in out
    assert "worth a look" in out


def test_key_never_appears_in_output(monkeypatch):
    # The key is in the environment, in the config, and even inside a server error.
    err = f"LLM HTTP 401: bad key {_SECRET}"
    fake = FakeClient(status={"llm": "ok"}, llm_result={
        "ok": False, "model": "m", "base_host": "h", "latency_ms": 1, "error": err})
    code, out = run(fake, cfg={"llm_key": _SECRET}, env_key=_SECRET, monkeypatch=monkeypatch)
    assert code == 1
    assert _SECRET not in out
    for status in ({"llm": "awaiting_key"}, {"llm": "unconfigured"}):
        _, out = run(FakeClient(status=status), env_key=_SECRET, monkeypatch=monkeypatch)
        assert _SECRET not in out


def test_symbols_fall_back_to_ascii_on_a_legacy_console():
    class Console:
        encoding = "cp1252"

    assert doctor.symbols(Console())[doctor.OK] == "OK"

    class Utf8:
        encoding = "utf-8"

    assert doctor.symbols(Utf8())[doctor.OK] == "✓"


def test_real_client_helpers():
    c = client_mod.HyatlasClient("http://127.0.0.1:1")
    with pytest.raises(ValueError):
        c.delete_memory("")

def test_doctor_hands_the_key_to_a_waiting_local_server(monkeypatch):
    fake = FakeClient(status={"llm": "awaiting_key"}, accept_key=True)
    code, out = run(fake, cfg={"auto_start": False}, env_key=_SECRET, monkeypatch=monkeypatch)
    assert fake.keys_set == [_SECRET]
    assert "✓ AI key" in out and "handed it over" in out, out
    assert fake.llm_calls == 1, "the model is checked once the key arrived"
    assert _SECRET not in out


def test_doctor_never_hands_the_key_to_a_remote_server(monkeypatch):
    fake = FakeClient(status={"llm": "awaiting_key"}, accept_key=True)
    code, out = run(fake, cfg={"server_host": "10.0.0.5"}, env_key=_SECRET, monkeypatch=monkeypatch)
    assert fake.keys_set == [] and code == 1


# ---------------------------------------------------------------------------
# Settings only a spawned server reads
# ---------------------------------------------------------------------------

def test_setup_that_the_server_does_not_use_is_a_warning_with_the_fix(monkeypatch):
    fake = FakeClient(status={"llm_model": "server-model", "llm_base": "https://server.example/v1"})
    code, out = run(fake, cfg={"llm_model": "chosen-model", "llm_base": "https://chosen.example/v1"},
                    env_key=_SECRET, monkeypatch=monkeypatch)
    assert code == 0
    assert ("! Setup         your setup chose chosen-model at chosen.example, "
            "the server uses server-model at server.example") in out
    assert "Hermes passes these only to a server it starts" in out
    assert "HYATLAS_LLM_MODEL / HYATLAS_LLM_BASE / HYATLAS_MODE" in out
    assert "hermes hyatlas settings set auto_start y" in out


def test_matching_setup_and_lite_mode_say_nothing(monkeypatch):
    same = FakeClient(status={"llm_model": "m", "llm_base": "https://api.example.com/v1"})
    _, out = run(same, cfg={"llm_model": "m", "llm_base": "https://api.example.com:443/v1"},
                 env_key=_SECRET, monkeypatch=monkeypatch)
    assert "Setup" not in out
    lite = FakeClient(status={"mode": "lite", "llm": "unused", "llm_model": "x"})
    _, out = run(lite, cfg={"llm_model": "other"}, monkeypatch=monkeypatch)
    assert "Setup" not in out


def test_a_setting_still_at_its_default_is_not_compared(monkeypatch):
    fake = FakeClient(status={"llm_model": "server-model"})
    lines = []
    cfg = {"server_host": "127.0.0.1", "server_port": 19528, "auto_start": False, "mode": "",
           "llm_model": "default-model"}
    monkeypatch.setenv("HYATLAS_LLM_KEY", _SECRET)
    doctor.run_doctor(fake, cfg, emit=lines.append, sleep=lambda s: None,
                      sources={"llm_model": "default", "llm_base": "default"})
    assert "Setup" not in "\n".join(lines)
    lines.clear()
    doctor.run_doctor(fake, cfg, emit=lines.append, sleep=lambda s: None,
                      sources={"llm_model": "hyatlas.json"})
    assert "! Setup" in "\n".join(lines)


# ---------------------------------------------------------------------------
# A changed key reaches the server
# ---------------------------------------------------------------------------

class KeyFakeClient(FakeClient):
    """A server whose model test fails with 401 until it is handed a key."""

    def llm_test(self):
        self.llm_calls += 1
        if self.keys_set:
            return {"ok": True, "model": "m", "base_host": "api.example.com", "latency_ms": 900}
        return {"ok": False, "model": "m", "base_host": "api.example.com", "latency_ms": 400,
                "error": "LLM HTTP 401: unauthorized"}


def test_doctor_hands_over_a_changed_key_and_retests(monkeypatch):
    fake = KeyFakeClient()
    sleeps = []
    monkeypatch.setenv("HYATLAS_LLM_KEY", _SECRET)
    lines = []
    cfg = {"server_host": "127.0.0.1", "server_port": 19528, "auto_start": False, "mode": ""}
    code = doctor.run_doctor(fake, cfg, emit=lines.append, sleep=sleeps.append)
    out = "\n".join(lines)
    assert code == 0, out
    assert fake.keys_set == [_SECRET] and fake.llm_calls == 2
    assert "the server had an old key; the doctor handed over the current one" in out
    assert "✓ Model" in out and _SECRET not in out
    assert len(sleeps) == 1 and 0 < sleeps[0] <= 11, sleeps


def test_doctor_does_not_retest_when_the_new_key_is_refused_too(monkeypatch):
    fake = FakeClient(llm_result={"ok": False, "model": "m", "base_host": "h", "latency_ms": 5,
                                  "error": "LLM HTTP 403: forbidden"})
    code, out = run(fake, env_key=_SECRET, monkeypatch=monkeypatch)
    assert code == 1 and fake.keys_set == [_SECRET] and fake.llm_calls == 2
    assert "✗ Model" in out and "refused the key" in out and _SECRET not in out


@pytest.mark.parametrize("cfg,env,why", [
    ({"server_host": "10.0.0.5"}, _SECRET, "never to a remote server"),
    ({}, None, "no key visible"),
])
def test_doctor_hands_over_nothing_when_it_must_not(monkeypatch, cfg, env, why):
    fake = KeyFakeClient()
    code, out = run(fake, cfg=cfg, env_key=env, monkeypatch=monkeypatch)
    assert fake.keys_set == [] and fake.llm_calls == 1, why
    assert code == 1 and "✗ Model" in out


def test_a_non_auth_failure_does_not_hand_over_the_key(monkeypatch):
    fake = FakeClient(llm_result={"ok": False, "model": "m", "base_host": "h", "latency_ms": 5,
                                  "error": "LLM HTTP 404: model not found"})
    run(fake, env_key=_SECRET, monkeypatch=monkeypatch)
    assert fake.keys_set == [] and fake.llm_calls == 1


# ---------------------------------------------------------------------------
# The model test credits the fallback
# ---------------------------------------------------------------------------

def test_a_failing_model_with_a_working_fallback_is_a_warning(monkeypatch):
    fake = FakeClient(llm_result={
        "ok": False, "model": "bad-model", "base_host": "h", "latency_ms": 5,
        "error": "LLM HTTP 404: model not found", "fallback_model": "good-model",
        "fallback_ok": True})
    code, out = run(fake, env_key=_SECRET, monkeypatch=monkeypatch)
    assert code == 0, out
    assert "! Model" in out and "✗ Model" not in out
    assert "your model bad-model failed (LLM HTTP 404: model not found); the fallback good-model answered" in out
    assert "model id" in out and "settings set llm_model" in out


def test_a_failing_model_and_a_failing_fallback_is_a_failure(monkeypatch):
    fake = FakeClient(llm_result={
        "ok": False, "model": "bad-model", "base_host": "h", "latency_ms": 5,
        "error": "LLM HTTP 404: model not found", "fallback_model": "good-model",
        "fallback_ok": False})
    code, out = run(fake, env_key=_SECRET, monkeypatch=monkeypatch)
    assert code == 1 and "✗ Model" in out and "bad-model did not answer" in out


def test_the_doctor_row_is_marked_so_the_server_skips_extraction(monkeypatch):
    fake = FakeClient()
    run(fake, env_key=_SECRET, monkeypatch=monkeypatch)
    assert fake.added[0]["metadata"] == {"write_origin": "doctor"}
