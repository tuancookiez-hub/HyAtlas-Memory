"""
HyAtlas-Memory installer for Hermes — `hermes hy-memory install` and
`hyatlas setup hermes`.

Hermes only discovers directory-based memory plugins under
$HERMES_HOME/plugins/<name>/. This module installs the bundled plugin shim
from hyatlas_memory.hermes_plugin_shim into that directory and sets
memory.provider: hy_memory in config.yaml.

v1.4 note: the pip entry point is no longer used by Hermes' loader. The
shim directory is required for the provider to be discovered and loaded.
"""

from __future__ import annotations

import os
import shutil
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


def _install_plugin_shim(home: Path) -> bool:
    """Copy the bundled Hermes plugin shim into $HERMES_HOME/plugins/hy_memory/."""
    import shutil

    src = Path(__file__).parent / "hermes_plugin_shim"
    dst = home / "plugins" / "hy_memory"
    if not src.exists():
        print(f"   ✗ Plugin shim template not found at {src}")
        return False
    try:
        dst.mkdir(parents=True, exist_ok=True)
        for name in ("__init__.py", "plugin.yaml"):
            shutil.copy2(src / name, dst / name)
        return True
    except Exception as e:
        print(f"   ✗ Failed to copy plugin shim: {e}")
        return False


def _update_config(home: Path, provider: str) -> bool:
    """Set memory.provider in Hermes config.yaml."""
    import yaml

    cfg = home / "config.yaml"
    try:
        data = yaml.safe_load(cfg.read_text(encoding="utf-8")) or {} if cfg.exists() else {}
    except Exception as e:
        print(f"   ✗ Could not read config.yaml: {e}")
        return False

    data.setdefault("memory", {})
    data["memory"]["provider"] = provider
    try:
        cfg.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        return True
    except Exception as e:
        print(f"   ✗ Could not write config.yaml: {e}")
        return False


def run_install(hermes_python: str | None = None) -> int:
    """Install the Hermes plugin shim and activate the provider."""
    print()
    print("=" * 64)
    print("  HyAtlas-Memory install for Hermes")
    print("=" * 64)
    print()

    home = get_hermes_home()
    print(f"1. Hermes home: {home}")

    if not _install_plugin_shim(home):
        print("   ✗ Plugin shim installation failed")
        return 1
    print("   ✓ Plugin shim installed")

    if not _update_config(home, "hy_memory"):
        print("   ✗ Config update failed")
        return 1
    print("   ✓ Hermes config: memory.provider = hy_memory")

    print()
    print("Install complete. Restart Hermes to load the new plugin.")
    print()
    return 0


if __name__ == "__main__":
    sys.exit(run_install())
