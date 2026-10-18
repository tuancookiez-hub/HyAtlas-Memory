"""``hermes hyatlas doctor``: one command that says whether memory works, and how to fix it.

The doctor runs a fixed list of checks against the configured server and prints one
line for each, marked ``✓`` (works), ``!`` (works, but look at this) or ``✗``
(broken). Every ``!`` and ``✗`` is followed by a ``Fix:`` line with the exact command
or setting to change. The exit code is 0 when nothing failed and 1 otherwise.

The doctor talks only to the configured HyAtlas server. It never prints the AI key,
never reads a Hermes credential, and its test memory is written under its own user id,
so nothing it does shows up in your recall.

This module imports nothing from Hermes, so it runs (and is tested) without it.
"""

from __future__ import annotations

import logging
import os
import re
import secrets
import sys
import time
from urllib.parse import urlparse
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import settings
from .client import PLUGIN_VERSION, HyatlasClientError, HyatlasUnreachable

logger = logging.getLogger(__name__)

# Severity of one finished check.
OK = "ok"
WARN = "warn"
FAIL = "fail"

# The identity the round-trip test memory is written under. It is not the user's own,
# so the test row and anything extracted from it never mixes with real memories.
DOCTOR_USER = "hyatlas-doctor"
DOCTOR_AGENT = "doctor"

# One sentence per mode, shown by the mode check.
_MODE_MEANING = {
    "lite": "no AI calls at all, so memories and embeddings stay on this machine (keyword + vector search).",
    "pro": "one AI call per conversation turn extracts facts; nothing runs in the background.",
    "ultra": "one AI call per turn, plus background consolidation, a knowledge graph and patterns.",
}


def symbols(stream: Any = None) -> Dict[str, str]:
    """The three status marks, falling back to ASCII when the terminal cannot show ✓.

    A Windows console on a legacy code page raises on ``✓`` when printing, which
    would turn the doctor into a traceback.
    """
    stream = stream or sys.stdout
    enc = getattr(stream, "encoding", None) or "ascii"
    try:
        "✓✗!".encode(enc)
        return {OK: "✓", WARN: "!", FAIL: "✗"}
    except (UnicodeEncodeError, LookupError):
        return {OK: "OK", WARN: "!!", FAIL: "XX"}


class _Report:
    """Collects the doctor's lines, prints them as they are produced and counts outcomes."""

    def __init__(self, emit: Callable[[str], None], marks: Dict[str, str], secrets_: List[str]) -> None:
        self._emit = emit
        self._marks = marks
        self._secrets = [s for s in secrets_ if s]
        self.counts = {OK: 0, WARN: 0, FAIL: 0}

    def _clean(self, text: str) -> str:
        """Mask any secret that found its way into a message. The doctor never has one."""
        for s in self._secrets:
            text = text.replace(s, "<key>")
        return text

    def line(self, text: str = "") -> None:
        """Print one plain line."""
        self._emit(self._clean(text))

    def check(self, level: str, label: str, message: str, fix: str = "") -> None:
        """Print one check line, and its Fix line for a warning or failure."""
        self.counts[level] += 1
        self._emit(self._clean(f"  {self._marks[level]} {label:<13} {message}"))
        if fix and level != OK:
            self._emit(self._clean(f"      Fix: {fix}"))


def _major_minor(version: str) -> Optional[Tuple[int, int]]:
    """The (major, minor) of a version like ``5.0.0`` or ``v5.1.2-rc1``, or None."""
    m = re.match(r"\s*v?(\d+)\.(\d+)", str(version or ""))
    return (int(m.group(1)), int(m.group(2))) if m else None


def _seconds(ms: Any) -> str:
    """Format a latency in milliseconds as seconds with one decimal."""
    try:
        return f"{float(ms) / 1000.0:.1f} s"
    except (TypeError, ValueError):
        return "?"


def _found_ids(results: Any) -> List[str]:
    """Every memory id in a search response, across all of its channels."""
    ids: List[str] = []
    memories = (results or {}).get("memories") if isinstance(results, dict) else None
    if isinstance(memories, dict):
        for items in memories.values():
            if isinstance(items, list):
                ids.extend(str(m.get("memory_id") or m.get("id") or "")
                           for m in items if isinstance(m, dict))
    return ids


def _explain_model_error(error: str, mode_base: str) -> str:
    """A Fix line for the short error the server's model test returned."""
    e = error.lower()
    if "401" in e or "403" in e or "unauthor" in e or "invalid api key" in e or "forbidden" in e:
        return ("The endpoint refused the key. Check that it is valid for that endpoint, then run "
                "`hermes memory setup` and enter it again.")
    if "429" in e or "rate" in e or "quota" in e:
        return "The endpoint is rate limiting you. Wait a minute and run `hermes hyatlas doctor` again."
    if "404" in e or "model" in e or "not found" in e or "400" in e:
        return ("The model id is probably wrong. Find valid ids in your provider's model list, then "
                "run `hermes hyatlas settings set llm_model <model id>`.")
    if "deadline" in e or "timeout" in e or "timed out" in e:
        return "The endpoint did not answer in time. Check your connection and the endpoint, then run the doctor again."
    if "no such host" in e or "refused" in e or "dial" in e or "lookup" in e:
        return (f"The endpoint ({mode_base or 'the AI endpoint'}) cannot be reached. Check the address "
                "with `hermes hyatlas settings set llm_base <address>`.")
    return "Check the endpoint, key and model with `hermes memory setup`, then run the doctor again."


def _host_of(url: Any) -> str:
    """The lower-cased host of an endpoint URL, or "" when there is none."""
    text = str(url or "").strip()
    if not text:
        return ""
    try:
        return (urlparse(text if "://" in text else "//" + text).hostname or "").lower()
    except ValueError:
        return ""


def _is_auth_error(error: str) -> bool:
    """True when a model-test error says the endpoint refused the key."""
    e = error.lower()
    return ("401" in e or "403" in e or "unauthorized" in e or "forbidden" in e
            or "invalid api key" in e)


def _short(error: str, limit: int = 80) -> str:
    """An error cut to one short line for a message."""
    error = " ".join(error.split())
    return error if len(error) <= limit else error[: limit - 1] + "…"


def run_doctor(
    client: Any,
    cfg: Dict[str, Any],
    *,
    emit: Callable[[str], None] = print,
    marks: Optional[Dict[str, str]] = None,
    sleep: Callable[[float], None] = time.sleep,
    sources: Optional[Dict[str, str]] = None,
) -> int:
    """Run every check, print the report and return the exit code (0 ok, 1 failed).

    ``client`` is a :class:`~client.HyatlasClient` (or a stand-in with the same
    methods), ``cfg`` the plugin's resolved settings. ``emit`` receives each output
    line, so a test can capture them. ``sources`` says where each setting came from
    (``default``, ``hyatlas.json``, ...): a setting still at its default was not
    chosen by the user, so it is not compared with the server. Without it, every
    non-empty setting counts as chosen.
    """
    marks = marks or symbols()
    env_key = os.environ.get("HYATLAS_LLM_KEY", "").strip()
    rep = _Report(emit, marks, [env_key, str(cfg.get("llm_key") or "").strip()])
    origin = getattr(client, "base_url", None) or settings.base_url(cfg)
    host = cfg.get("server_host") or "127.0.0.1"
    auto_start = settings.truthy(cfg.get("auto_start"))
    loopback = settings.is_loopback_host(host)

    rep.line("HyAtlas doctor")
    rep.line("")

    # 1. Provider and plugin version. The command exists only while hyatlas is the
    # active memory provider, so reaching this point already answers the question.
    rep.check(OK, "Provider", f"Hermes is using HyAtlas for memory (plugin {PLUGIN_VERSION})")

    # 2. The server answers.
    if not client.is_reachable():
        if not loopback:
            fix = (f"server_host is {host}, which is not this machine. Start HyAtlas there, and if "
                   "you use a hostname add it to HYATLAS_ALLOWED_HOSTS on that server.")
        elif auto_start:
            log = settings.home() / "logs" / "hyatlas.log"
            fix = (f"auto_start is on but nothing answered. Run `hermes hyatlas start`, and if it "
                   f"fails read {log}.")
        else:
            fix = ("auto_start is off, so start the server yourself: `hermes hyatlas start` "
                   "(or run `hyatlas-go`). To have Hermes start it for you, run `hermes hyatlas "
                   "settings set auto_start y`.")
        rep.check(FAIL, "Server", f"nothing answers at {origin}", fix)
        rep.line("")
        rep.line("  The other checks need the server, so they were skipped.")
        return _finish(rep)
    rep.check(OK, "Server", f"running at {origin}")

    try:
        status = client.status() or {}
    except HyatlasClientError as e:
        rep.check(FAIL, "Status", f"the server answered /healthz but not /api/v1/status: {e}",
                  "Restart the server (`hermes hyatlas stop`, then `hermes hyatlas start`).")
        return _finish(rep)

    # 3. Server version against the plugin version.
    server_version = str(status.get("version") or "")
    sv, pv = _major_minor(server_version), _major_minor(PLUGIN_VERSION)
    if sv is None:
        rep.check(WARN, "Version", f"the server did not report a version (plugin {PLUGIN_VERSION})",
                  "Update the server to the matching release.")
    elif pv is not None and sv != pv:
        rep.check(WARN, "Version", f"server {server_version} and plugin {PLUGIN_VERSION} differ",
                  "Update both to the same release: `hermes plugins update hyatlas`, and replace "
                  "the hyatlas-go binary with the matching release.")
    else:
        rep.check(OK, "Version", f"server {server_version} matches the plugin")

    # 4. Mode.
    mode = settings.normalize_mode(status.get("mode")) or "lite"
    meaning = _MODE_MEANING.get(mode, "an unrecognised mode.")
    wanted = settings.normalize_mode(cfg.get("mode"))
    if wanted in _MODE_MEANING and wanted != mode:
        rep.check(WARN, "Mode", f"the server runs {mode}, but your setting is {wanted}",
                  "Restart the server so it picks the setting up (`hermes hyatlas stop`, then "
                  "`hermes hyatlas start`), or set HYATLAS_MODE where the server runs.")
    else:
        rep.check(OK, "Mode", f"{mode}: {meaning}")

    uses_llm = mode in ("pro", "ultra")

    # 4b. Model and endpoint the setup chose, against what the server really uses.
    # Hermes passes them only to a server it starts, so a server started separately
    # can run something else.
    if uses_llm:
        _check_setup_matches(rep, cfg, status, sources)

    # 5. Key.
    key_ok = False
    llm = str(status.get("llm") or "")
    # Hermes loads the plugin only when a conversation runs, so right after a restart
    # nothing has handed the key over yet. The doctor sees the same key and the same
    # local server, so it hands the key over itself (loopback only) and looks again.
    handed = False
    if uses_llm and llm == "awaiting_key" and loopback and env_key:
        try:
            client.set_llm_key(env_key)
            status = client.status() or status
            llm = str(status.get("llm") or "")
            handed = llm == "ok"
        except HyatlasClientError as e:
            logger.debug("doctor key hand-over failed: %s", type(e).__name__)
    if uses_llm:
        if llm == "ok":
            key_ok = True
            if handed:
                rep.check(OK, "AI key", "the server was waiting for its key; the doctor handed it over")
            else:
                rep.check(OK, "AI key", "the server has a key for the AI endpoint")
        elif llm == "awaiting_key":
            if not loopback:
                fix = "Set HYATLAS_LLM_KEY where the server runs, then restart it."
            elif auto_start:
                fix = "Run `hermes memory setup` and enter the API key."
            elif env_key:
                fix = ("The doctor handed HYATLAS_LLM_KEY to the server, but it is still waiting. "
                       "Restart the server (`hermes hyatlas stop`, then `hermes hyatlas start`) and "
                       "run `hermes hyatlas doctor` again.")
            else:
                fix = ("The plugin hands HYATLAS_LLM_KEY to a local server automatically, but this "
                       "command sees no key. Run `hermes memory setup` and enter the API key, then "
                       "restart Hermes.")
            rep.check(FAIL, "AI key", f"the server is waiting for its key ({mode} mode needs one)", fix)
        else:
            rep.check(FAIL, "AI key",
                      f"the server has no AI endpoint, model or key configured (state: {llm or 'unknown'})",
                      "Run `hermes memory setup` and enter the endpoint, key and model (or choose "
                      "lite mode, which needs none).")
    else:
        rep.check(OK, "AI key", "not needed in lite mode")

    # 6. The model answers.
    if uses_llm and key_ok:
        _check_model(client, rep, status, env_key=env_key, loopback=loopback,
                     handed=handed, sleep=sleep)

    # 7. A round trip: write, find, delete.
    _check_round_trip(client, rep, mode, sleep)

    # 8. Extraction backlog.
    try:
        waiting = int(status.get("unextracted") or 0)
    except (TypeError, ValueError):
        waiting = 0
    if waiting > 0:
        rep.check(WARN, "Backlog", f"{waiting} turn{'s' if waiting != 1 else ''} wait for extraction",
                  "Nothing to do: catch-up runs every HYATLAS_CATCHUP_EVERY (10 minutes by default). "
                  "If the number does not fall, check the AI key and model above.")
    else:
        rep.check(OK, "Backlog", "no turns are waiting for extraction")

    return _finish(rep)


def _check_setup_matches(rep: _Report, cfg: Dict[str, Any], status: Dict[str, Any],
                         sources: Optional[Dict[str, str]]) -> None:
    """Warn when the model or endpoint the setup chose is not what the server uses."""
    def chosen(key: str) -> str:
        value = str(cfg.get(key) or "").strip()
        if sources is not None and sources.get(key, "default") == "default":
            return ""
        return value

    want_model, want_host = chosen("llm_model"), _host_of(chosen("llm_base"))
    have_model = str(status.get("llm_model") or "").strip()
    have_host = _host_of(status.get("llm_base"))
    model_differs = bool(want_model and have_model and want_model != have_model)
    host_differs = bool(want_host and have_host and want_host != have_host)
    if not (model_differs or host_differs):
        return
    rep.check(WARN, "Setup",
              f"your setup chose {want_model or have_model} at {want_host or have_host}, "
              f"the server uses {have_model or want_model} at {have_host or want_host}",
              settings.SPAWNED_ONLY_NOTE)


def _check_model(
    client: Any,
    rep: _Report,
    status: Dict[str, Any],
    *,
    env_key: str = "",
    loopback: bool = False,
    handed: bool = False,
    sleep: Callable[[float], None] = time.sleep,
) -> None:
    """Check 6: ask the server to make one tiny call with the configured model.

    The server tests the primary model alone. When it fails with an auth error on
    this machine, the server may simply hold an older key than the one in the
    environment, so the doctor hands the current one over and tests once more.
    """
    res = _llm_test(client, rep)
    if res is None:
        return
    error = str(res.get("error") or "")
    if (not res.get("ok") and error != "awaiting_key" and _is_auth_error(error)
            and loopback and env_key and not handed):
        again = _retest_with_current_key(client, env_key, res, sleep)
        if again is not None:
            res = again
            if res.get("ok"):
                rep.check(OK, "Model",
                          f"the server had an old key; the doctor handed over the current one. "
                          f"{res.get('model') or status.get('llm_model') or '?'} answered in "
                          f"{_seconds(res.get('latency_ms'))}")
                return
    model = res.get("model") or status.get("llm_model") or "?"
    base = res.get("base_host") or ""
    if res.get("ok"):
        rep.check(OK, "Model", f"{model} answered in {_seconds(res.get('latency_ms'))}"
                               + (f" via {base}" if base else ""))
        return
    error = str(res.get("error") or "no answer")
    if error == "awaiting_key":
        rep.check(FAIL, "Model", "the server has no key yet, so the model was not called",
                  "Run `hermes memory setup` and enter the API key.")
        return
    if res.get("fallback_ok"):
        rep.check(WARN, "Model",
                  f"your model {model} failed ({_short(error)}); the fallback "
                  f"{res.get('fallback_model') or '?'} answered",
                  "Extraction still works through the fallback. Fix the model id: find valid ids "
                  "in your provider's model list, then run `hermes hyatlas settings set llm_model "
                  "<model id>` (and HYATLAS_LLM_MODEL where a separately started server runs).")
        return
    rep.check(FAIL, "Model", f"{model} did not answer: {error}", _explain_model_error(error, base))


def _llm_test(client: Any, rep: _Report) -> Optional[Dict[str, Any]]:
    """Run the server's model test; report a refusal or an old server and return None."""
    try:
        return client.llm_test() or {}
    except HyatlasClientError as e:
        text = str(e)
        if "429" in text:
            rep.check(WARN, "Model", "the server limits model tests to one per 10 seconds",
                      "Wait a few seconds and run `hermes hyatlas doctor` again.")
        elif "404" in text or "405" in text:
            rep.check(WARN, "Model", "this server is too old to test the model",
                      "Update the hyatlas-go binary to the matching release.")
        else:
            rep.check(FAIL, "Model", f"the model test failed: {text}",
                      "Restart the server and run the doctor again.")
        return None


def _retest_with_current_key(client: Any, env_key: str, first: Dict[str, Any],
                             sleep: Callable[[float], None]) -> Optional[Dict[str, Any]]:
    """Hand the environment's key to the local server and test the model once more.

    The server allows one model test per 10 seconds, so this waits out what is left
    of that (never more than 11 seconds). Returns the second result, or None when
    the key could not be handed over or the second test could not run.
    """
    try:
        client.set_llm_key(env_key)
    except HyatlasClientError as e:
        logger.debug("doctor key hand-over failed: %s", type(e).__name__)
        return None
    try:
        spent = float(first.get("latency_ms") or 0) / 1000.0
    except (TypeError, ValueError):
        spent = 0.0
    sleep(min(11.0, max(0.5, 10.5 - spent)))
    try:
        return client.llm_test() or {}
    except HyatlasClientError as e:
        logger.debug("doctor model retest failed: %s", type(e).__name__)
        return None


def _check_round_trip(client: Any, rep: _Report, mode: str, sleep: Callable[[float], None]) -> None:
    """Check 7: write a test memory, search for it, then delete it by id."""
    token = secrets.token_hex(4)
    text = f"hyatlas doctor check {token}"
    memory_id = ""
    try:
        resp = client.add(text=text, user_id=DOCTOR_USER, agent_id=DOCTOR_AGENT,
                          metadata={"write_origin": "doctor"})
        memory_id = str((resp or {}).get("memory_id") or "")
        if not (resp or {}).get("success") or not memory_id:
            rep.check(FAIL, "Round trip", f"the server did not store a test memory: {resp}",
                      "Check the server log (`hermes hyatlas status`, then the hyatlas.log in your "
                      "Hermes logs folder).")
            return
        found = False
        for attempt in range(3):
            if attempt:
                sleep(1.0)
            results = client.search(query=text, user_id=DOCTOR_USER, agent_id=DOCTOR_AGENT,
                                    layer="l2_raw", limit=5)
            if memory_id in _found_ids(results):
                found = True
                break
        if found:
            rep.check(OK, "Round trip", "wrote a test memory, found it again and deleted it")
        elif mode == "lite":
            rep.check(WARN, "Round trip",
                      "the test memory was stored but a search did not find it (lite relies on "
                      "keyword matching if the embedder is not semantic)",
                      "Check that the server loaded the BGE model: its log shows the embedder at "
                      "start. A release binary embeds it.")
        else:
            rep.check(FAIL, "Round trip", "the test memory was stored but a search did not find it",
                      "Restart the server and run the doctor again; if it persists, check that the "
                      "BGE model loaded (see the server log).")
    except HyatlasUnreachable as e:
        rep.check(FAIL, "Round trip", f"the server stopped answering: {e}",
                  "Run `hermes hyatlas start`, then run the doctor again.")
    except HyatlasClientError as e:
        rep.check(FAIL, "Round trip", f"the server refused the test: {e}",
                  "Restart the server and run the doctor again.")
    finally:
        if memory_id:
            try:
                client.delete_memory(memory_id)
            except HyatlasClientError as e:
                rep.check(WARN, "Cleanup", f"could not delete the test memory {memory_id}: {e}",
                          "It is stored under user 'hyatlas-doctor' and does not affect your recall.")


def _finish(rep: _Report) -> int:
    """Print the summary line and return the exit code."""
    failed, warned = rep.counts[FAIL], rep.counts[WARN]
    rep.line("")
    if failed:
        rep.line(f"  {failed} problem{'s' if failed != 1 else ''} found. Follow the Fix lines above, "
                 "then run `hermes hyatlas doctor` again.")
        return 1
    if warned:
        rep.line(f"  Memory works. {warned} thing{'s' if warned != 1 else ''} worth a look (marked "
                 f"{rep._marks[WARN]} above).")
    else:
        rep.line("  All good. Memory is working.")
    return 0
