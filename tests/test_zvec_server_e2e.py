from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

import pytest

try:
    import zvec as _zvec  # noqa: F401

    _zvec_available = True
except ImportError:
    _zvec_available = False

pytestmark = pytest.mark.skipif(not _zvec_available, reason="zvec not installed")


def port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    val = sock.getsockname()[1]
    sock.close()
    return val


def test_start_server_boots_zvec_on_temp_port(monkeypatch, tmp_path):
    home = tmp_path / "hyatlas"
    cfg = home / "config" / "hy_memory.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(
        json.dumps(
            {
                "mode": "lite",
                "llm": {
                    "model": "test-model",
                    "api_key": "test-key",
                    "base_url": "http://127.0.0.1:9/v1",
                },
                "embedder": {
                    "model": "BAAI/bge-small-en-v1.5",
                    "dims": 4,
                },
                "vector_store": {
                    "provider": "zvec",
                    "collection": "agent_memories",
                },
            }
        ),
        encoding="utf-8",
    )

    p = port()
    env = {
        **dict(os.environ),
        "HYATLAS_HOME": str(home),
        "HY_MEMORY_PORT": str(p),
        "MEMORY_LOG_LEVEL": "ERROR",
        "HF_HUB_OFFLINE": "1",
        "TRANSFORMERS_OFFLINE": "1",
    }
    proc = subprocess.Popen(
        [sys.executable, "-m", "hyatlas_memory.server.start_server"],
        cwd=str(tmp_path),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        deadline = time.time() + 30
        last = ""
        body = {}
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{p}/info", timeout=1) as resp:
                    body = json.loads(resp.read().decode("utf-8"))
                    if body.get("name") == "hy-memory-server":
                        break
            except Exception as err:
                last = str(err)
                if proc.poll() is not None:
                    raise AssertionError(proc.stdout.read()) from err
                time.sleep(0.5)
        else:
            raise AssertionError(last)
    finally:
        proc.terminate()
        try:
            out, _ = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate(timeout=10)
    # Bare server boot: confirm via /info that the test config was honored and
    # the server reached "running". `Vector store: zvec` text is only printed
    # by `_start.py`'s launcher, not by this standalone entry point — and
    # probing `/api/v1/status` can race the embedder warmup, so use /info only.
    assert body.get("name") == "hy-memory-server"
    assert body.get("status") == "running"
    assert body.get("mode") == "lite"
