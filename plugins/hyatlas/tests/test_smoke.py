"""Smoke tests for the HyAtlas v4 plugin.

Run these with:
    cd ~/.hermes/plugins && python hyatlas/tests/test_smoke.py
or directly from the v4 repo root:
    python plugins/hyatlas/tests/test_smoke.py

These tests verify the plugin's HTTP wire contract against a live v4
server on 127.0.0.1:19528. They do NOT spin up the server; assume
the user has one running (``hermes hyatlas start`` or directly
``hyatlas-go``).

Each check returns ``(status, msg)`` where status is one of the ``_CHECKS``
tokens PASS / SKIP / FAIL. A check that needs a live server returns SKIP when
none is reachable — that is a pass-equivalent, NOT a failure, so this file
exits 0 on CI where no server is running. Both the standalone runner below and
the pytest wrappers consume the same contract.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
import traceback

# Explicit check outcomes. A live-server check that finds no server returns
# SKIP; the runners treat SKIP as pass-equivalent so CI (which has no server)
# is green instead of red.
PASS = "PASS"
SKIP = "SKIP"
FAIL = "FAIL"

# Make `hyatlas` importable when this file is run directly.
# _HERE = .../hyatlas/tests/
# _PARENT = .../hyatlas/  (the package root where __init__.py lives)
_HERE = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)  # the hyatlas/ package root
_PKG = _PARENT


def _load_plugin_module():
    """Load hyatlas/__init__.py as a package with relative imports."""
    spec = importlib.util.spec_from_file_location(
        "hyatlas",
        os.path.join(_PKG, "__init__.py"),
        submodule_search_locations=[_PKG],
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["hyatlas"] = mod
    spec.loader.exec_module(mod)
    return mod


def _check_config_loads_clean() -> tuple[str, str]:
    """Plugin's _load_config should not pick up v3.5 garbage fields.

    The port is checked against what the plugin's own documented precedence
    produces (HYATLAS_SERVER_PORT override, else the 19528 default) rather than
    a hardcoded 19528 — asserting the default as an invariant would spuriously
    fail whenever the supported env override is in play, as in CI.
    """
    try:
        mod = _load_plugin_module()
        provider = mod.HyatlasMemoryProvider()
        v3_keys = {"llm", "vector_store", "api_keys", "embedding_dims"}
        leaked = v3_keys & set(provider._config.keys())
        if leaked:
            return FAIL, f"v3.5 keys leaked into config: {leaked}"
        env_port = os.environ.get("HYATLAS_SERVER_PORT")
        want = int(env_port) if env_port else 19528
        got = provider._config.get("server_port")
        if got != want:
            return FAIL, f"server_port {got!r}, want {want!r} (env override {env_port!r})"
        if not isinstance(got, int) or got <= 0:
            return FAIL, f"server_port is not a positive int: {got!r}"
        return PASS, f"config clean, port {got}: {list(provider._config.keys())}"
    except Exception as e:
        return FAIL, f"import/init failed: {e}"


def _check_provider_metadata() -> tuple[str, str]:
    """Provider should expose name, tool schemas, and config schema."""
    try:
        mod = _load_plugin_module()
        provider = mod.HyatlasMemoryProvider()
        if provider.name != "hyatlas":
            return FAIL, f"wrong name: {provider.name}"
        tools = [s["name"] for s in provider.get_tool_schemas()]
        expected = {"hyatlas_status", "hyatlas_search", "hyatlas_recent", "hyatlas_add"}
        missing = expected - set(tools)
        if missing:
            return FAIL, f"missing tools: {missing}"
        cfg_keys = {f["key"] for f in provider.get_config_schema()}
        if not {"server_host", "server_port", "user_id", "agent_id"} <= cfg_keys:
            return FAIL, f"config schema missing keys: {cfg_keys}"
        return PASS, f"name={provider.name}, tools={len(tools)}, cfg_keys={len(cfg_keys)}"
    except Exception as e:
        return FAIL, f"{e}\n{traceback.format_exc()}"


def _check_live_server_round_trip() -> tuple[str, str]:
    """The plugin's client must talk to a live v4 server and round-trip add+search."""
    try:
        mod = _load_plugin_module()
        provider = mod.HyatlasMemoryProvider()
        host = provider._config.get("server_host", "127.0.0.1")
        port = provider._config.get("server_port", 19528)
        if not provider.is_available():
            return SKIP, f"no live v4 server on {host}:{port}"
        client = provider._ensure_client()
        marker = f"smoke-test-{int(time.time())}"
        user_id = "smoke_test_user"
        add_resp = client.add(
            text=f"This is a smoke test memory: {marker}",
            user_id=user_id,
            agent_id="smoke",
        )
        if not add_resp.get("success"):
            return FAIL, f"add failed: {add_resp}"
        time.sleep(2)  # LLM extraction
        search_resp = client.search(
            query=marker,
            user_id=user_id,
            agent_id="smoke",
            limit=3,
        )
        total = sum(len(v) for v in search_resp.get("memories", {}).values())
        client.delete_all(user_id=user_id, agent_id="smoke")
        if total == 0:
            return FAIL, f"search returned 0 hits for marker '{marker}'"
        return PASS, f"add+search round-trip OK ({total} hits)"
    except Exception as e:
        return FAIL, f"{e}"


_CHECKS = [
    ("config_loads_clean", _check_config_loads_clean),
    ("provider_metadata", _check_provider_metadata),
    ("live_server_round_trip", _check_live_server_round_trip),
]


def _pytest_skip(status, msg):
    """SKIP is pass-equivalent: it means the precondition (a live server) is
    absent, not that anything is broken. Raise pytest.skip so it reports as
    skipped rather than failed."""
    if status == SKIP:
        import pytest
        pytest.skip(msg)


# pytest wrappers — same (status, msg) contract as the standalone runner, with
# SKIP surfaced as a real pytest skip instead of a failure.

def test_config_loads_clean():
    status, msg = _check_config_loads_clean()
    _pytest_skip(status, msg)
    assert status == PASS, msg


def test_provider_metadata():
    status, msg = _check_provider_metadata()
    _pytest_skip(status, msg)
    assert status == PASS, msg


def test_live_server_round_trip():
    status, msg = _check_live_server_round_trip()
    _pytest_skip(status, msg)
    assert status == PASS, msg


def _run_all() -> int:
    """Run all smoke tests. Returns 0 unless a check actually FAILs.

    SKIP (no live server, as on CI) is counted separately and does not fail the
    run — only FAIL does. The status token is authoritative; the message text is
    never inspected to decide the outcome.
    """
    failures = skipped = 0
    for name, fn in _CHECKS:
        try:
            status, msg = fn()
        except Exception as e:
            status, msg = FAIL, f"raised: {e}"
        if status == FAIL:
            failures += 1
        elif status == SKIP:
            skipped += 1
        print(f"[{status}] {name}: {msg}")
    parts = []
    if failures:
        parts.append(f"{failures} failed")
    if skipped:
        parts.append(f"{skipped} skipped")
    print("\n" + (", ".join(parts) if parts else "All tests passed"))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(_run_all())
