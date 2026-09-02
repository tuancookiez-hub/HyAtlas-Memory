"""Smoke tests for the HyAtlas v4 plugin.

Run these with:
    cd ~/.hermes/plugins && python hy_memory/tests/test_smoke.py
or directly from the v4 repo root:
    python plugins/hy_memory/tests/test_smoke.py

These tests verify the plugin's HTTP wire contract against a live v4
server on 127.0.0.1:19528. They do NOT spin up the server; assume
the user has one running (``hermes hyatlas start`` or directly
``hyatlas-go``).
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import time
import traceback

# Make `hy_memory` importable when this file is run directly.
# _HERE = .../hy_memory/tests/
# _PARENT = .../hy_memory/  (the package root where __init__.py lives)
_HERE = os.path.dirname(os.path.abspath(__file__))
_PARENT = os.path.dirname(_HERE)  # the hy_memory/ package root
_PKG = _PARENT


def _load_plugin_module():
    """Load hy_memory/__init__.py as a package with relative imports."""
    spec = importlib.util.spec_from_file_location(
        "hy_memory",
        os.path.join(_PKG, "__init__.py"),
        submodule_search_locations=[_PKG],
    )
    mod = importlib.util.module_from_spec(spec)
    sys.modules["hy_memory"] = mod
    spec.loader.exec_module(mod)
    return mod


def test_config_loads_clean() -> tuple[bool, str]:
    """Plugin's _load_config should not pick up v3.5 garbage fields."""
    try:
        mod = _load_plugin_module()
        provider = mod.HyatlasMemoryProvider()
        v3_keys = {"llm", "vector_store", "api_keys", "embedding_dims"}
        leaked = v3_keys & set(provider._config.keys())
        if leaked:
            return False, f"v3.5 keys leaked into config: {leaked}"
        if provider._config.get("server_port") != 19528:
            return False, f"wrong port: {provider._config.get('server_port')}"
        return True, f"config clean: {list(provider._config.keys())}"
    except Exception as e:
        return False, f"import/init failed: {e}"


def test_provider_metadata() -> tuple[bool, str]:
    """Provider should expose name, tool schemas, and config schema."""
    try:
        mod = _load_plugin_module()
        provider = mod.HyatlasMemoryProvider()
        if provider.name != "hy_memory":
            return False, f"wrong name: {provider.name}"
        tools = [s["name"] for s in provider.get_tool_schemas()]
        expected = {"hyatlas_status", "hyatlas_search", "hyatlas_recent", "hyatlas_add"}
        missing = expected - set(tools)
        if missing:
            return False, f"missing tools: {missing}"
        cfg_keys = {f["key"] for f in provider.get_config_schema()}
        if not {"server_host", "server_port", "user_id", "agent_id"} <= cfg_keys:
            return False, f"config schema missing keys: {cfg_keys}"
        return True, f"name={provider.name}, tools={len(tools)}, cfg_keys={len(cfg_keys)}"
    except Exception as e:
        return False, f"{e}\n{traceback.format_exc()}"


def test_live_server_round_trip() -> tuple[bool, str]:
    """The plugin's client must talk to a live v4 server and round-trip add+search."""
    try:
        mod = _load_plugin_module()
        provider = mod.HyatlasMemoryProvider()
        if not provider.is_available():
            return True, "SKIP (no live v4 server on 127.0.0.1:19528)"
        client = provider._ensure_client()
        marker = f"smoke-test-{int(time.time())}"
        user_id = "smoke_test_user"
        add_resp = client.add(
            text=f"This is a smoke test memory: {marker}",
            user_id=user_id,
            agent_id="smoke",
        )
        if not add_resp.get("success"):
            return False, f"add failed: {add_resp}"
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
            return False, f"search returned 0 hits for marker '{marker}'"
        return True, f"add+search round-trip OK ({total} hits)"
    except Exception as e:
        return False, f"{e}"


def test_smoke() -> int:
    """Run all smoke tests. Returns 0 on success, 1 on failure."""
    tests = [
        ("config_loads_clean", test_config_loads_clean),
        ("provider_metadata", test_provider_metadata),
        ("live_server_round_trip", test_live_server_round_trip),
    ]
    failures = 0
    for name, fn in tests:
        try:
            ok, msg = fn()
        except Exception as e:
            ok, msg = False, f"raised: {e}"
        status = "PASS" if ok and "SKIP" not in msg else "FAIL"
        if status == "FAIL":
            failures += 1
        print(f"[{status}] {name}: {msg}")
    print(f"\n{'All tests passed' if failures == 0 else f'{failures} test(s) failed'}")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(test_smoke())
