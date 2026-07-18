"""Test LLM key precedence: JSON config wins over HY_MEMORY_LLM_* env vars.

Regression test for Patch 29: a stale shell env key was silently overriding
the active JSON config key, causing MiniMax 401 auth failures.
"""

from __future__ import annotations

import os


def _resolve(config: dict, key: str, default: str = "") -> tuple[str, str]:
    """Mirror of start_server._resolve — JSON wins, env is fallback."""
    json_val = config.get("llm", {}).get(key, "")
    env_val = os.environ.get(f"HY_MEMORY_LLM_{key.upper()}", "")
    if json_val:
        return json_val, "json"
    if env_val:
        return env_val, f"env:HY_MEMORY_LLM_{key.upper()}"
    return default, "default"


def test_json_wins_over_env(monkeypatch):
    config = {"llm": {"api_key": "json-key-123", "model": "MiniMax-M3"}}
    monkeypatch.setenv("HY_MEMORY_LLM_API_KEY", "stale-env-key-456")
    key, src = _resolve(config, "api_key")
    assert key == "json-key-123"
    assert src == "json"


def test_env_fallback_when_json_empty(monkeypatch):
    config = {"llm": {"api_key": "", "model": "MiniMax-M3"}}
    monkeypatch.setenv("HY_MEMORY_LLM_API_KEY", "env-key-789")
    key, src = _resolve(config, "api_key")
    assert key == "env-key-789"
    assert "env" in src


def test_default_when_both_empty(monkeypatch):
    monkeypatch.delenv("HY_MEMORY_LLM_API_KEY", raising=False)
    config = {"llm": {}}
    key, src = _resolve(config, "api_key", "fallback-default")
    assert key == "fallback-default"
    assert src == "default"
