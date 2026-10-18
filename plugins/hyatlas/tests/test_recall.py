"""Tests for the recall side of the plugin: the channel order the server sends and
the session id a search carries.

formatting.py and client.py are loaded by path, so Hermes is not needed.
"""

import importlib.util
import pathlib

_PKG = pathlib.Path(__file__).resolve().parent.parent


def _load(name, filename):
    spec = importlib.util.spec_from_file_location(name, _PKG / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


fmt = _load("hyatlas_formatting_recall", "formatting.py")
client_mod = _load("hyatlas_client_recall", "client.py")

MEMORIES = {"pages": [], "profile": [], "proactive": [], "experiences": [], "normal": []}


def test_server_order_leads_and_now_is_dropped():
    order = ["now", "normal", "pages", "profile", "proactive", "experiences"]
    assert fmt.channel_order(MEMORIES, order) == [
        "normal", "pages", "profile", "proactive", "experiences"]


def test_unknown_channel_follows_known_ones():
    mem = dict(MEMORIES, extra=[])
    got = fmt.channel_order(mem, ["now", "pages", "normal"])
    assert got[:2] == ["pages", "normal"]
    assert "extra" in got and got.index("extra") > 1


def test_new_channel_named_in_order_is_shown_in_place():
    mem = dict(MEMORIES, brand_new=[])
    got = fmt.channel_order(mem, ["now", "brand_new", "pages"])
    assert got[:2] == ["brand_new", "pages"]


def test_missing_order_uses_builtin_order():
    expected = ["pages", "profile", "proactive", "experiences", "normal"]
    assert fmt.channel_order(MEMORIES, None) == expected
    assert fmt.channel_order(MEMORIES, []) == expected
    assert fmt.channel_order(MEMORIES, "garbage") == expected


def test_search_adds_session_id_only_when_given():
    sent = []
    c = client_mod.HyatlasClient()
    c._post = lambda path, body: sent.append(body) or {}
    c.search("q", user_id="u", agent_id="a")
    c.search("q", user_id="u", agent_id="a", session_id="s1")
    assert "session_id" not in sent[0]
    assert sent[1]["session_id"] == "s1"
