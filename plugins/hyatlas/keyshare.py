"""Hand Hermes's current provider key to the HyAtlas server, over localhost.

The server needs a key for its LLM endpoint. With the Nous Portal that key is a
one-hour token that only Hermes can renew. The old way was for the server (and its
launcher) to read Hermes's credential file, ``auth.json``, directly. A program
opening another application's credential file and then calling the internet is
what credential stealers do, and behaviour-based antivirus treats it that way.

Instead this module runs inside Hermes, which owns the credential, asks Hermes for
it through ``hermes_cli.runtime_provider.resolve_runtime_provider`` (the same call
Hermes's bundled plugins use, which also renews the token), and hands it to the
server with ``POST /api/v1/llm/key``. The server keeps it in memory only. Nothing
outside Hermes ever opens ``auth.json``.

The key is only sent to a HyAtlas server on this machine (host 127.0.0.1, ::1 or
localhost), and only when that server's LLM endpoint is on the same host as the
endpoint Hermes resolved, so a Nous key never leaves the machine or reaches another
provider. It is never logged. This module imports nothing from Hermes at load time, so it can be tested
on its own.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

# How often the server's state is checked. A server that restarted reports
# "awaiting_key" and gets the key on the next check.
CHECK_INTERVAL = 60.0
# How often the key is resolved again even when the server is satisfied, so a
# renewed token replaces the old one well before the old one expires (one hour).
REFRESH_INTERVAL = 15 * 60.0


def resolve_hermes_key(provider: str = "nous") -> Dict[str, str]:
    """Return Hermes's current key and base URL for provider.

    Uses Hermes's own resolver, which renews an expiring token. Raises when Hermes is
    not importable or has no credential for the provider.
    """
    from hermes_cli.runtime_provider import resolve_runtime_provider  # type: ignore

    rt = resolve_runtime_provider(requested=provider) or {}
    return {"api_key": str(rt.get("api_key") or ""), "base_url": str(rt.get("base_url") or "")}


# Hosts that mean "this machine". The key is never pushed to any other server.
LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")


def is_loopback(url: str) -> bool:
    """Whether url names a server on this machine: 127.0.0.1, ::1 or localhost."""
    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return host in LOOPBACK_HOSTS


def same_host(a: str, b: str) -> bool:
    """Whether two URLs name the same host, ignoring case. Empty never matches."""
    ha = (urlparse(a).hostname or "").lower()
    hb = (urlparse(b).hostname or "").lower()
    return bool(ha) and ha == hb


class KeySharer:
    """Keeps the server supplied with Hermes's current key.

    server_url is the HyAtlas server's base URL; the key is only ever pushed when its
    host is loopback. status() returns the server's /api/v1/status dict; push(key)
    posts the key; resolve() returns {"api_key", "base_url"} from Hermes. The three
    callables are injected so the logic can be tested without Hermes or a server.
    """

    def __init__(
        self,
        server_url: str,
        status: Callable[[], Dict[str, Any]],
        push: Callable[[str], Any],
        resolve: Callable[[], Dict[str, str]] = resolve_hermes_key,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._server_url = server_url
        self._status = status
        self._push = push
        self._resolve = resolve
        self._clock = clock
        self._last_key = ""
        self._last_push = 0.0
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def check(self) -> bool:
        """Run one check and push when needed. Returns whether a key was sent.

        A key is sent when the server is waiting for one, when Hermes's key has
        changed since the last push, or when REFRESH_INTERVAL has passed. Nothing is
        ever sent unless the server URL is loopback. Any error is logged without the
        key and leaves the next check to try again.
        """
        if not is_loopback(self._server_url):
            logger.warning(
                "hyatlas key share: not sent, the HyAtlas server (%s) is not on this machine",
                urlparse(self._server_url).hostname or "unset",
            )
            return False
        try:
            st = self._status() or {}
        except Exception as e:  # server down: nothing to hand over to
            logger.debug("hyatlas key share: server unreachable: %s", e)
            return False
        llm = str(st.get("llm") or "")
        if llm in ("", "unused"):
            return False
        due = llm == "awaiting_key" or self._clock() - self._last_push >= REFRESH_INTERVAL
        try:
            rt = self._resolve()
        except Exception as e:
            logger.warning("hyatlas key share: Hermes has no usable key: %s", type(e).__name__)
            return False
        key = rt.get("api_key") or ""
        if not key:
            return False
        if not same_host(rt.get("base_url", ""), str(st.get("llm_base") or "")):
            logger.warning(
                "hyatlas key share: not sent, the server's LLM endpoint (%s) is not the "
                "host Hermes's key is for",
                urlparse(str(st.get("llm_base") or "")).hostname or "unset",
            )
            return False
        if not due and key == self._last_key:
            return False
        try:
            self._push(key)
        except Exception as e:
            logger.warning("hyatlas key share: hand-over failed: %s", type(e).__name__)
            return False
        self._last_key = key
        self._last_push = self._clock()
        logger.info("hyatlas key share: key handed to the server")
        return True

    def start(self) -> None:
        """Start the background loop once; later calls do nothing."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="hyatlas-keyshare", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Ask the loop to end."""
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.is_set():
            self.check()
            self._stop.wait(CHECK_INTERVAL)
