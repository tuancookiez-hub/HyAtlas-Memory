"""conftest — detect hermes-agent availability for conditional test collection."""


import pytest


# Check if hermes-agent is actually available (not just mocked)
def _hermes_available():
    try:
        import agent.memory_provider  # noqa: F401
        return True
    except ImportError:
        return False

# Marker to skip tests that require a live hermes-agent installation
requires_hermes = pytest.mark.skipif(
    not _hermes_available(),
    reason="hermes-agent not installed (not on PyPI; install locally for full tests)"
)

# Make the marker available as a decorator
pytest.requires_hermes = requires_hermes
