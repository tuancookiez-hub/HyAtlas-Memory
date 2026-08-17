from __future__ import annotations


def test_missing_auto_start_does_not_launch_stack(monkeypatch):
    from hyatlas_memory import HyMemoryProvider

    provider = object.__new__(HyMemoryProvider)
    provider._config = {}
    called = False

    class Manager:
        def __init__(self, **_):
            nonlocal called
            called = True

    monkeypatch.setattr("hyatlas_memory.process.StackManager", Manager)

    provider._ensure_stack_running()

    assert called is False
