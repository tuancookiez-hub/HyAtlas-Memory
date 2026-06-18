"""
HY Memory installer for Hermes — `hermes hy-memory install`

This is the community-implementation installer for the
hyatlas_memory package (formerly the in-fork plugin at
plugins/memory/hy_memory/, now a standalone pip package).

What it does:
  - The package is activated by installing it (`pip install hyatlas-memory`)
    and having Hermes discover it via the `hermes.memory_provider` entry
    point. No path-based discovery is required any more.
  - This installer therefore just verifies the setup is correct, gives
    diagnostics, and optionally bootstraps missing config.

Tasks:
  1. Detect the Hermes Python (auto or from --hermes-python)
  2. Verify hy-memory SDK is importable
  3. Verify our local client.py is importable
  4. Check ~/.hermes/config.yaml has memory.provider: hy_memory
  5. Check the sidecar is reachable (or that auto-start is configured)
  6. Print a summary; optionally run init_wizard if .env is missing
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from hermes_constants import get_hermes_home


def _detect_hermes_python() -> str | None:
    """Find the Python interpreter Hermes runs under."""
    # 1. Try the `hermes` launcher on PATH
    hermes = shutil.which("hermes")
    if hermes:
        try:
            # `hermes --version` exits fast; capture its python via `hermes doctor -v`
            # but that's overkill. Just check if hermes is a shim script.
            content = Path(hermes).read_text(encoding="utf-8", errors="replace")[:2048]
            # Look for shebang or python path
            for line in content.splitlines():
                ls = line.strip()
                if ls.startswith("#!") and "python" in ls:
                    return ls[2:].strip().split()[-1]
                if "venv" in line.lower() and "python" in line.lower():
                    # crude: "Scripts/python.exe" inside venv
                    import re
                    m = re.search(r'([A-Za-z]:[\\/][^"\']*venv[\\/]Scripts[\\/]python\.exe)', line)
                    if m:
                        return m.group(1)
                    m = re.search(r'([A-Za-z]:[\\/][^"\']*venv[\\/]bin[\\/]python)', line)
                    if m:
                        return m.group(1)
        except Exception:
            pass
    # 2. Try HERMES_PYTHON env var
    py = os.environ.get("HERMES_PYTHON")
    if py and Path(py).exists():
        return py
    # 3. Look for the standard venv layout under HERMES_HOME's parent
    try:
        hermes_home = get_hermes_home()
        for cand in [
            hermes_home / "hermes-agent" / "venv" / "Scripts" / "python.exe",  # Windows
            hermes_home / "hermes-agent" / "venv" / "bin" / "python",          # Unix
            hermes_home / ".." / "hermes-agent" / "venv" / "Scripts" / "python.exe",
            hermes_home / ".." / "hermes-agent" / "venv" / "bin" / "python",
        ]:
            if cand.exists():
                return str(cand.resolve())
    except Exception:
        pass
    # 4. Fallback: sys.executable (best guess)
    return sys.executable


def run_install(hermes_python: str | None = None) -> int:
    """Verify the plugin is properly installed. For our local fork this is mostly a no-op + diagnostics."""
    print()
    print("=" * 64)
    print("  Hy-Memory install / verify")
    print("=" * 64)
    print()
    print("Local-fork install mode: plugin is already at the standard")
    print("discovery path. This command verifies setup, doesn't copy files.")
    print()

    # 1. Hermes Python
    py = hermes_python or _detect_hermes_python()
    print(f"1. Hermes Python: {py}")
    if not py or not Path(py).exists():
        print("   ✗ Could not detect. Pass --hermes-python <path>")
        return 1
    print("   ✓ Detected")

    # 2. SDK importable
    try:
        r = subprocess.run(
            [py, "-c", "import hy_memory; print(hy_memory.__version__)"],
            capture_output=True, text=True, timeout=15,
        )
        if r.returncode == 0:
            print(f"   ✓ hy-memory SDK importable: {r.stdout.strip()}")
        else:
            print(f"   ✗ hy-memory SDK import failed: {r.stderr.strip()[:200]}")
            print("     Hint: run `pip install hy-memory` in the Hermes venv")
            return 1
    except Exception as e:
        print(f"   ✗ Could not test SDK import: {e}")
        return 1

    # 3. Our local client.py is importable
    #    (editable install means hyatlas_memory is importable from
    #    any cwd; no sys.path hack needed)
    try:
        r = subprocess.run(
            [py, "-c",
             "from hyatlas_memory.client import HyMemoryClient; "
             "print('client.py OK')"],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode == 0:
            print(f"   ✓ Local client.py importable: {r.stdout.strip()}")
        else:
            print(f"   ! Local client.py import failed: {r.stderr.strip()[:200]}")
            # Not fatal — the sidecar may not be running, but the module should import
    except Exception as e:
        print(f"   ! Could not test client.py: {e}")

    # 4. Hermes config
    home = get_hermes_home()
    cfg = home / "config.yaml"
    if cfg.exists():
        try:
            import yaml
            data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {}
            mem = data.get("memory", {}) or {}
            provider = mem.get("provider", "(not set)")
            if provider == "hy_memory":
                print(f"   ✓ Hermes config.yaml: memory.provider = {provider}")
            else:
                print(f"   ! Hermes config.yaml: memory.provider = {provider} (expected 'hy_memory')")
        except Exception as e:
            print(f"   ! Could not parse config.yaml: {e}")
    else:
        print(f"   ! No config.yaml at {cfg}")

    # 5. Sidecar reachable
    base = os.environ.get(
        "HY_MEMORY_BASE_URL",
        f"http://{os.environ.get('HY_MEMORY_HOST', '127.0.0.1')}:"
        f"{os.environ.get('HY_MEMORY_PORT', '19527')}",
    )
    try:
        import urllib.request
        req = urllib.request.Request(f"{base}/healthz", method="GET")
        with urllib.request.urlopen(req, timeout=3) as resp:
            if resp.status == 200:
                print(f"   ✓ Sidecar reachable at {base}/healthz")
            else:
                print(f"   ! Sidecar returned {resp.status}")
    except Exception as e:
        print(f"   ! Sidecar not reachable at {base}: {e}")
        print("     Hint: start with `python start_hy_memory_server.py` or check HY_MEMORY_BASE_URL")

    # 6. .env presence
    env = home / ".env"
    if env.exists():
        env_text = env.read_text(encoding="utf-8")
        hy_vars = [l for l in env_text.splitlines() if l.startswith("HY_MEMORY_")]
        print(f"   ✓ ~/.hermes/.env exists with {len(hy_vars)} HY_MEMORY_* vars")
    else:
        print("   ! No ~/.hermes/.env — run `hermes hy-memory init` to create one")

    print()
    print("Install / verify complete.")
    print("Next: `hermes hy-memory doctor` for the full health report.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(run_install())
