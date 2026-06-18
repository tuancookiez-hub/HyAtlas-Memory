"""Standalone tests — no hermes-agent dependency required.

These run on CI even without a live Hermes installation."""

import importlib.util
import pytest
from pathlib import Path

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

    import tomllib

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


def test_package_importable():
    """The _version module loads correctly from disk."""
    __version__ = _load_version()
    assert isinstance(__version__, str)
    assert len(__version__) > 0