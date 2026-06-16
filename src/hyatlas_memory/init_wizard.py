# -*- coding: utf-8 -*-
"""
HY Memory init wizard for Hermes — `hermes hy-memory init`

Adapted from the canonical upstream plugin (plugins/native/hermes/init_wizard.py).
Writes non-secret config to ~/.hermes/.env in standard dotenv format. Hermes
loads this on every agent start, so the values flow into our plugin's
_load_config() and override anything in hy_memory.json.

For our local fork:
  - We DON'T auto-symlink anything (Hermes already discovers this plugin
    via the standard plugins/memory/<name>/ convention).
  - We DON'T auto-install the SDK (already in venv, or pip install hy-memory).
  - We DO write HY_MEMORY_USER_ID, HY_MEMORY_AGENT_ID, HY_MEMORY_MODE,
    HY_MEMORY_LLM_API_KEY, HY_MEMORY_EMBEDDER_API_KEY to .env.
  - The plugin picks them up via Phase 1 env-var-first config (added 2026-06-10).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

from hermes_constants import get_hermes_home


def _ask(prompt: str, default: str = "", secret: bool = False) -> str:
    """Prompt with default, optionally hiding input for secrets."""
    suffix = f" [{default}]" if default else ""
    try:
        if secret:
            import getpass
            val = getpass.getpass(f"{prompt}{suffix}: ").strip()
        else:
            val = input(f"{prompt}{suffix}: ").strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    return val or default


def _env_path() -> Path:
    return get_hermes_home() / ".env"


def _read_existing_env() -> dict[str, str]:
    """Read existing .env (Hermes dotenv format), preserving comments/blanks."""
    p = _env_path()
    if not p.exists():
        return {}
    out: dict[str, str] = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _write_env(env_vars: dict[str, str]) -> None:
    """Write env vars to .env, preserving existing structure (Hermes dotenv)."""
    p = _env_path()
    p.parent.mkdir(parents=True, exist_ok=True)

    existing_lines = p.read_text(encoding="utf-8").splitlines() if p.exists() else []
    new_lines: list[str] = []
    updated_keys: set[str] = set()

    for line in existing_lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            new_lines.append(line)
            continue
        k, _, _ = line.partition("=")
        key = k.strip()
        if key in env_vars:
            new_lines.append(f'{key}={env_vars[key]}')
            updated_keys.add(key)
        else:
            new_lines.append(line)

    for key, val in env_vars.items():
        if key not in updated_keys:
            new_lines.append(f'{key}={val}')

    p.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    try:
        import stat
        p.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass  # Windows


def run_interactive() -> int:
    """Interactive setup wizard. Writes ~/.hermes/.env with HY_MEMORY_* vars."""
    print()
    print("=" * 64)
    print("  Hy-Memory init wizard")
    print("  Writes config to ~/.hermes/.env (Hermes dotenv format)")
    print("=" * 64)
    print()

    existing = _read_existing_env()
    print(f"Existing .env has {len(existing)} entries (will preserve non-HY_MEMORY_* keys)\n")

    # === Identity ===
    print("── Identity ───────────────────────────────────────────")
    user_id = _ask(
        "HY_MEMORY_USER_ID (memory namespace; e.g. your name or 'hermes-user')",
        default=existing.get("HY_MEMORY_USER_ID", "hermes-user"),
    )
    agent_id = _ask(
        "HY_MEMORY_AGENT_ID (per-profile scoping; usually matches your profile name)",
        default=existing.get("HY_MEMORY_AGENT_ID", "default"),
    )
    mode = _ask(
        "HY_MEMORY_MODE (lite / pro / ultra; pro is recommended)",
        default=existing.get("HY_MEMORY_MODE", "pro"),
    )

    # === API keys (optional but commonly needed) ===
    print()
    print("── LLM (for fact extraction in pro/ultra mode) ─────────")
    print("    Your hy_memory.json or HY_MEMORY_LLM_API_KEY env var will be used.")
    llm_key = _ask(
        "HY_MEMORY_LLM_API_KEY (press Enter to keep existing / skip)",
        default=existing.get("HY_MEMORY_LLM_API_KEY", ""),
        secret=True,
    )

    print()
    print("── Embedder (for vector embeddings) ────────────────────")
    print("    Leave blank to use the local sentence-transformers sidecar (default).")
    emb_key = _ask(
        "HY_MEMORY_EMBEDDER_API_KEY (press Enter to keep existing / skip)",
        default=existing.get("HY_MEMORY_EMBEDDER_API_KEY", ""),
        secret=True,
    )

    # === Write ===
    env_writes: dict[str, str] = {
        "HY_MEMORY_USER_ID": user_id,
        "HY_MEMORY_AGENT_ID": agent_id,
        "HY_MEMORY_MODE": mode,
    }
    if llm_key:
        env_writes["HY_MEMORY_LLM_API_KEY"] = llm_key
    if emb_key:
        env_writes["HY_MEMORY_EMBEDDER_API_KEY"] = emb_key

    print()
    print(f"Writing {len(env_writes)} vars to {_env_path()}")
    _write_env(env_writes)
    print("✓ .env updated.\n")

    # === Next steps ===
    print("── Next steps ─────────────────────────────────────────")
    print("  1. Run `hermes hy-memory doctor` to verify connectivity.")
    print("  2. Run `hermes hy-memory add 'your first memory'` to test a write.")
    print("  3. Run `hermes hy-memory search 'something'` to test a recall.")
    print()
    print("Tip: to use this config in your current shell, run:")
    print(f"  set -a; source {_env_path()}; set +a    # bash")
    print(f"  Get-Content {_env_path()} | ForEach {{ $name, $value = $_ -split '=', 2; Set-Item -Path \"Env:$name\" -Value $value }}    # PowerShell")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(run_interactive())
