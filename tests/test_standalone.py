"""Standalone tests — no hermes-agent dependency required.

These run on CI even without a live Hermes installation."""

import pytest
from pathlib import Path


def test_version_is_semver():
    """Package version follows semver (MAJOR.MINOR.PATCH)."""
    from hyatlas_memory._version import __version__

    parts = __version__.split(".")
    assert len(parts) == 3, f"Version {__version__!r} is not semver (expected 3 parts)"
    for p in parts:
        assert p.isdigit(), f"Version part {p!r} is not numeric"


def test_version_consistency():
    """All version sources agree: _version.py == pyproject.toml."""
    from hyatlas_memory._version import __version__

    import tomllib

    pyproject = Path(__file__).parent.parent / "pyproject.toml"
    with open(pyproject, "rb") as f:
        data = tomllib.load(f)

    assert data["project"]["version"] == __version__, (
        f"_version.py={__version__} != pyproject.toml={data['project']['version']}"
    )


def test_plugin_yaml_version():
    """plugin.yaml version matches _version.py."""
    from hyatlas_memory._version import __version__

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
    """The package can be imported (at least the _version module)."""
    from hyatlas_memory._version import __version__

    assert isinstance(__version__, str)
    assert len(__version__) > 0
