"""Tests for keyshare.py, loaded by path so Hermes need not be installed."""

import importlib.util
import logging
from pathlib import Path

_spec = importlib.util.spec_from_file_location(
    "hyatlas_keyshare", Path(__file__).resolve().parent.parent / "keyshare.py"
)
ks = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(ks)

NOUS = "https://inference-api.nousresearch.com/v1"


class Fake:
    """A server status, a push recorder and a Hermes resolver with a clock."""

    def __init__(self, llm="awaiting_key", base=NOUS, key="tok-1", hermes_base=NOUS,
                 server_url="http://127.0.0.1:8765"):
        self.server_url = server_url
        self.st = {"llm": llm, "llm_base": base}
        self.key = key
        self.hermes_base = hermes_base
        self.pushed = []
        self.now = 1000.0

    def sharer(self):
        return ks.KeySharer(
            server_url=self.server_url,
            status=lambda: self.st,
            push=self.pushed.append,
            resolve=lambda: {"api_key": self.key, "base_url": self.hermes_base},
            clock=lambda: self.now,
        )


def test_pushes_when_server_awaits_key():
    f = Fake()
    assert f.sharer().check() is True
    assert f.pushed == ["tok-1"]


def test_never_sends_to_another_host():
    f = Fake(base="https://api.example.com/v1")
    assert f.sharer().check() is False
    assert f.pushed == []


def test_pushes_to_every_loopback_spelling():
    for url in ("http://127.0.0.1:8765", "http://localhost:8765", "http://[::1]:8765",
                "http://LOCALHOST:1"):
        f = Fake(server_url=url)
        assert f.sharer().check() is True, url
        assert f.pushed == ["tok-1"], url


def test_never_pushes_to_a_remote_server(caplog):
    for url in ("http://192.168.1.20:8765", "http://memory.example.com:8765",
                "http://127.0.0.1.evil.example:8765", "", "not a url"):
        f = Fake(server_url=url)
        with caplog.at_level(logging.WARNING):
            assert f.sharer().check() is False, url
        assert f.pushed == [], url
    assert "tok-1" not in caplog.text


def test_skips_when_fresh_and_unchanged_then_refreshes():
    f = Fake()
    s = f.sharer()
    s.check()
    f.st["llm"] = "ok"
    assert s.check() is False  # same key, pushed just now
    f.key = "tok-2"
    assert s.check() is True  # Hermes renewed the token
    f.now += ks.REFRESH_INTERVAL + 1
    assert s.check() is True  # periodic refresh
    assert f.pushed == ["tok-1", "tok-2", "tok-2"]


def test_server_restart_gets_key_again():
    f = Fake()
    s = f.sharer()
    s.check()
    f.st["llm"] = "awaiting_key"  # server restarted, lost its in-memory key
    assert s.check() is True
    assert f.pushed == ["tok-1", "tok-1"]


def test_nothing_without_llm_or_key_or_server():
    assert Fake(llm="unused").sharer().check() is False
    assert Fake(key="").sharer().check() is False
    f = Fake()
    s = ks.KeySharer(server_url="http://127.0.0.1:8765", status=lambda: (_ for _ in ()).throw(OSError("down")), push=f.pushed.append,
                     resolve=lambda: {"api_key": "k", "base_url": NOUS})
    assert s.check() is False and f.pushed == []


def test_key_is_never_logged(caplog):
    caplog.set_level(logging.DEBUG)
    f = Fake(key="super-secret-token")
    f.sharer().check()
    Fake(base="https://api.example.com/v1", key="super-secret-token").sharer().check()
    assert "super-secret-token" not in caplog.text


def test_same_host():
    assert ks.same_host(NOUS, "https://INFERENCE-API.nousresearch.com/other")
    assert not ks.same_host(NOUS, "https://evil.example/v1")
    assert not ks.same_host("", "")
