from __future__ import annotations

from concurrent.futures import TimeoutError

import pytest

from hyatlas_memory.core import server
from hyatlas_memory.core.client import HyMemoryClient, _LoopThread


class Runner:
    def __init__(self):
        self.timeout = None

    def run(self, coro, timeout=300):
        self.timeout = timeout
        coro.close()
        return {"success": True}


def test_digest_uses_dedicated_long_timeout(monkeypatch):
    monkeypatch.delenv("MEMORY_DIGEST_TIMEOUT", raising=False)
    client = HyMemoryClient.__new__(HyMemoryClient)
    client._mode = "ultra"
    client._loop_thread = Runner()

    result = client.digest(user_id="hermes-user", agent_id="default")

    assert result == {"success": True}
    assert client._loop_thread.timeout == 3600


def test_digest_timeout_can_be_configured(monkeypatch):
    monkeypatch.setenv("MEMORY_DIGEST_TIMEOUT", "7200")
    client = HyMemoryClient.__new__(HyMemoryClient)
    client._mode = "ultra"
    client._loop_thread = Runner()

    client.digest(user_id="hermes-user", agent_id="default")

    assert client._loop_thread.timeout == 7200


def test_loop_thread_cancels_future_on_timeout(monkeypatch):
    class Future:
        cancelled = False

        def result(self, timeout):
            raise TimeoutError

        def cancel(self):
            self.cancelled = True

    future = Future()
    monkeypatch.setattr(
        "hyatlas_memory.core.client.asyncio.run_coroutine_threadsafe",
        lambda coro, loop: future,
    )
    thread = _LoopThread.__new__(_LoopThread)
    thread._loop = object()

    async def work():
        return None

    coro = work()
    with pytest.raises(TimeoutError):
        thread.run(coro, timeout=1)
    coro.close()

    assert future.cancelled is True


def test_digest_http_timeout_returns_clear_504(monkeypatch):
    class Client:
        def digest(self, user_id, agent_id):
            raise TimeoutError

    response = {}
    monkeypatch.setattr(server, "_get_client", lambda: Client())
    monkeypatch.setattr(
        server,
        "_json_response",
        lambda handler, code, payload: response.update(code=code, payload=payload),
    )
    handler = server.MemoryHTTPHandler.__new__(server.MemoryHTTPHandler)

    handler._handle_digest({"user_id": "hermes-user", "agent_id": "default"})

    assert response["code"] == 504
    assert response["payload"]["error"] == "digest_timeout"
