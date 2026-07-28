"""Standalone tests — no hermes-agent dependency required.

These run on CI even without a live Hermes installation.
"""

import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

# Load _version.py DIRECTLY without triggering __init__.py
_VERSION_PATH = Path(__file__).parent.parent / "src" / "hyatlas_memory" / "_version.py"


def _load_version():
    spec = importlib.util.spec_from_file_location("_version", _VERSION_PATH)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.__version__


def test_version_is_semver():
    """Package version follows semver (MAJOR.MINOR.PATCH)."""
    __version__ = _load_version()
    parts = __version__.split(".")
    assert len(parts) == 3, f"Version {__version__!r} is not semver (expected 3 parts)"
    for p in parts:
        assert p.isdigit(), f"Version part {p!r} is not numeric"


def test_version_consistency():
    """All version sources agree: _version.py == pyproject.toml."""
    __version__ = _load_version()

    try:
        import tomllib
    except ImportError:
        import tomli as tomllib

    pyproject = Path(__file__).parent.parent / "pyproject.toml"
    with open(pyproject, "rb") as f:
        data = tomllib.load(f)

    assert data["project"]["version"] == __version__, (
        f"_version.py={__version__} != pyproject.toml={data['project']['version']}"
    )


def test_plugin_yaml_version():
    """plugin.yaml version matches _version.py."""
    __version__ = _load_version()

    try:
        import yaml
    except ImportError:
        pytest.skip("PyYAML not installed")

    plugin_yaml = (
        Path(__file__).parent.parent / "src" / "hyatlas_memory" / "plugin.yaml"
    )
    with open(plugin_yaml) as f:
        data = yaml.safe_load(f)

    assert data["version"] == __version__, (
        f"plugin.yaml={data['version']} != _version.py={__version__}"
    )


def test_cli_importable_without_hermes_constants():
    """CLI modules fall back cleanly when hermes-agent is absent."""
    src = Path(__file__).parent.parent / "src"
    code = """
import builtins
real_import = builtins.__import__
def blocked(name, *args, **kwargs):
    if name == "hermes_constants":
        raise ModuleNotFoundError(name)
    return real_import(name, *args, **kwargs)
builtins.__import__ = blocked
import hyatlas_memory._cli
import hyatlas_memory.init_wizard
"""
    env = os.environ.copy()
    env.pop("VIRTUAL_ENV", None)
    env.pop("PYTHONPATH", None)
    env["PYTHONPATH"] = str(src)
    result = subprocess.run(
        [sys.executable, "-c", code],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_package_importable():
    """The _version module loads correctly from disk."""
    __version__ = _load_version()
    assert isinstance(__version__, str)
    assert len(__version__) > 0
