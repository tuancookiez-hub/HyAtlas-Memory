"""HyAtlas-Memory — 7-layer cognitive memory for Hermes Agent.

This package provides a memory provider for the Hermes Agent CLI/TUI.
It implements a 7-layer cognitive memory architecture with System1/System2
dual processing, evolution chains, and a Kuzu graph backend.

Modes:
  lite  — embedding-only, zero LLM cost
  pro   — LLM fact extraction + reconciliation
  ultra — pro + System2 cognitive layer with Kuzu graph

Config via $HERMES_HOME/hy_memory.json or environment variables:
  HY_MEMORY_LLM_API_KEY      — LLM API key (pro/ultra)
  HY_MEMORY_EMBEDDER_API_KEY — Embedding API key (all modes)

Server: localhost:19527 by default, auto-started by the plugin.

This package is a Hermes plugin. It requires `hermes-agent` to be
installed alongside (declared as a peer dependency in pyproject.toml).
The MemoryProvider base class and `tool_error` helper come from
`agent.memory_provider` and `tools.registry` respectively, both in
the `hermes-agent` package.

Public API:
  HyMemoryProvider  — the MemoryProvider implementation (entry point).
"""

from __future__ import annotations

import contextlib
import json
import logging
import math
import os
import re
import threading
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

# Imports from hermes-agent (optional for standalone use):
try:
    from agent.memory_provider import MemoryProvider
    from hermes_constants import get_hermes_home
    from tools.registry import tool_error
    _HERMES_AVAILABLE = True
except ImportError:
    _HERMES_AVAILABLE = False
    # Provide stubs for standalone use (e.g., testing without hermes-agent)
    class MemoryProvider:
        """Stub base class when hermes-agent is not installed."""
        pass
    
    def get_hermes_home():
        """Fallback when hermes_constants is not available."""
        return os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))
    
    def tool_error(msg: str) -> str:
        """Fallback tool_error when tools.registry is not available."""
        return f"[ERROR] {msg}"
    
    # Log warning at module level for diagnostics
    import warnings
    warnings.warn(
        "hermes-agent imports not available — running in standalone mode. "
        "Some features may be limited. Install hermes-agent for full functionality.",
        ImportWarning,
        stacklevel=2
    )

from . import patches
from ._version import __version__ as __version__

logger = logging.getLogger(__name__)

__all__ = [
    "HyMemoryProvider",
    "__version__",
]

# Circuit breaker — after N consecutive failures, pause calls
_BREAKER_THRESHOLD = 5
_BREAKER_COOLDOWN_SECS = 120

_DEFAULT_PORT = 19527

# Hy-Memory upstream default Qdrant collection. Must match the SDK/server
# default (the collection name is hardcoded in the upstream server).
_DEFAULT_QDRANT_COLLECTION = "agent_memories_1024"

# Default Qdrant HTTP URL for runtime patches (importance + access_count).
_DEFAULT_QDRANT_URL = "http://127.0.0.1:6333"

# Max chars injected into system prompt (per official hermes-hy-memory)
_MAX_PREFETCH_CHARS = int(os.environ.get("HY_MEMORY_PREFETCH_MAX_CHARS", "2000"))

# Write throttling: every N turns flushes the session buffer.
# Default 1 (per-turn, Hindsight-style) — the previous default of 5 silently
# dropped all data from short Hermes sessions (which are the norm — 1-3 turns).
# Tradeoff: 5x more LLM extraction calls. Acceptable for the reliability.
# Set to 0 to disable writes entirely (not recommended).
_WRITE_TURN_WINDOW = max(0, int(os.environ.get("HY_MEMORY_WRITE_TURN_WINDOW", "1") or "1"))

# Short confirmations / greetings to skip prefetch on (per official)
_SKIP_QUERIES = frozenset({
    "ok", "好", "好的", "thanks", "谢谢", "y", "n", "yes", "no",
    "继续", "go", "嗯", "嗯嗯", "对", "对的",
})


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def _load_config() -> dict:
    """Load config from $HERMES_HOME/hy_memory.json, with env var fallbacks."""
    try:
        home = get_hermes_home()
    except Exception:
        home = Path.home() / ".hermes"

    config_path = home / "hy_memory.json"
    config: dict[str, Any] = {}
    if config_path.exists():
        with contextlib.suppress(Exception):
            config = json.loads(config_path.read_text(encoding="utf-8"))

    # Env var fallbacks for secrets
    if not config.get("llm", {}).get("api_key"):
        env_key = os.environ.get("HY_MEMORY_LLM_API_KEY", "")
        if env_key:
            config.setdefault("llm", {})["api_key"] = env_key

    if not config.get("embedder", {}).get("api_key"):
        env_key = os.environ.get("HY_MEMORY_EMBEDDER_API_KEY", "")
        if env_key:
            config.setdefault("embedder", {})["api_key"] = env_key

    return config


# ---------------------------------------------------------------------------
# Provider
# ---------------------------------------------------------------------------

class HyMemoryProvider(MemoryProvider):
    """Hermes memory provider backed by Hy-Memory."""

    def __init__(self):
        self._config: dict = {}
        self._client = None
        self._process = None
        self._user_id = "hermes-user"
        self._agent_id = "default"
        self._mode = "pro"
        # Prefetch
        self._prefetch_result = ""
        self._prefetch_lock = threading.Lock()
        self._prefetch_thread: threading.Thread | None = None
        # Sync
        self._sync_thread: threading.Thread | None = None
        # Write throttling (per official hermes-hy-memory 0.2.7)
        # Buffers N turns before flushing — saves LLM extraction calls
        # and prevents per-turn retry storms when the same content repeats.
        self._write_turn_window: int = _WRITE_TURN_WINDOW
        self._turn_buffer: dict[str, list[dict[str, str]]] = {}
        self._buffer_lock = threading.RLock()
        # Circuit breaker
        self._consecutive_failures = 0
        self._breaker_open_until = 0.0

    @property
    def name(self) -> str:
        return "hy_memory"

    # ------------------------------------------------------------------
    # Availability
    # ------------------------------------------------------------------

    def is_available(self) -> bool:
        """Plug-and-play: the provider is always installable.

        Returns True as long as the provider class can be loaded and
        configured. This is the answer to "is this provider usable in
        principle" — NOT "is the upstream currently reachable". Use
        :meth:`is_healthy` for runtime reachability.

        Gating agent init on upstream reachability causes silent
        stuck-agent failures when the upstream is briefly down at init
        time: the consumer rejects the provider, the
        ``MemoryManager`` ends up None, and every subsequent turn is a
        no-op. Decoupling "installable" from "currently operational"
        lets the consumer wire the provider in once and have the
        provider self-heal at sync time.
        """
        # If we ever get here, the class loaded. Configuration may
        # still be missing (no embedder key, no base URL) — but the
        # provider is still "available" in the installability sense;
        # sync_turn will surface a config error if it can't proceed.
        return True

    def is_healthy(self) -> bool:
        """Runtime reachability check: is the upstream currently reachable?

        Use this when you need to know "is the provider actually
        working right now" — e.g., to show a "memory connected" UI
        indicator or to gate a non-critical feature on memory being
        available. Do NOT use this for agent-init gating (use
        :meth:`is_available` for that).
        """
        return self._client is not None and self._client.is_reachable()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def initialize(self, session_id: str, **kwargs) -> None:
        """Start/connect to Hy-Memory server, store identity."""
        # Cron guard — skip for cron context
        agent_context = kwargs.get("agent_context", "")
        if agent_context in {"cron", "flush"}:
            logger.debug("Hy-Memory skipped: cron/flush context")
            return

        self._config = _load_config()
        self._mode = self._config.get("mode", "pro")

        # Identity mapping
        self._user_id = kwargs.get("user_id", "") or "hermes-user"
        self._agent_id = kwargs.get("agent_identity", "") or "default"

        # Auto-start the full stack on first use.
        self._ensure_stack_running()

        # v1.4.2: install the in-process log bridge if any console
        # subscriber is waiting (typically the `hyatlas console`
        # subprocess). No-op when no one is listening, so this is
        # safe for every existing install path.
        try:
            from ._console_handler import install_if_needed
            install_if_needed(logger)
        except Exception:
            pass

        # Create client
        from .client import HyMemoryClient
        port = self._config.get("server_port", _DEFAULT_PORT)
        host = self._config.get("server_host", "127.0.0.1")
        self._client = HyMemoryClient(f"http://{host}:{port}")

        if self._client.is_reachable():
            logger.info("[hy-memory] Connected (mode=%s, user=%s)",
                        self._mode, self._user_id)
            # Replay any pending buffers left over from a force-killed
            # previous session. This is the Hindsight-style guarantee: the
            # data is on the server, not in the CLI process.
            try:
                replayed = self._replay_pending_buffers()
                if replayed:
                    logger.info(
                        "[hy-memory] Recovered %d orphaned session buffer(s) from disk",
                        replayed,
                    )
            except Exception as e:
                logger.debug("[hy-memory] replay pass failed: %s", e)
        else:
            logger.warning("[hy-memory] Server not reachable at %s:%d", host, port)

    def _ensure_stack_running(self) -> None:
        """Auto-start the Qdrant + upstream + dashboard stack.

        Matches Hindsight's embedded daemon behavior: the provider owns
        the lifecycle of its dependencies. No manual 'hyatlas start' is
        required for end users.
        """
        if not self._config.get("auto_start", True):
            return
        try:
            home = get_hermes_home()
        except Exception:
            home = Path.home() / ".hermes"
        root = Path(__file__).parent
        from .process import StackManager
        self._process = StackManager(
            project_root=root,
            hermes_home=home,
            log_dir=home / "logs",
        )
        if not self._process.ensure_running():
            logger.error("[hy-memory] Stack failed to start")
            return

        # v1.4.2: opt-in visible console. When HERMES_HYATLAS_CONSOLE=1
        # is set, launch the console status window. Fires once per
        # process (the first time initialize() is called) so a noisy
        # agent loop does not stack up multiple console windows.
        if os.environ.get("HERMES_HYATLAS_CONSOLE") == "1":
            self._maybe_launch_console()

    def _maybe_launch_console(self) -> None:
        """Spawn the visible status window exactly once per process.

        Idempotent via a class-level set of launched PIDs. Runs the
        console in a fully detached process so closing the parent
        does not kill the status window — that defeats the point of
        a 'what is the memory system doing' surface.
        """
        import subprocess
        import sys as _sys

        if getattr(HyMemoryProvider, "_console_launched", False):
            return
        HyMemoryProvider._console_launched = True  # type: ignore[attr-defined]
        try:
            cmd = [_sys.executable, "-m", "hyatlas_memory.console"]
            creationflags = 0
            if _sys.platform == "win32":
                creationflags = (
                    subprocess.CREATE_NEW_CONSOLE
                    | subprocess.CREATE_NEW_PROCESS_GROUP
                )
            subprocess.Popen(
                cmd,
                creationflags=creationflags,
                close_fds=True,
            )
            logger.info("[hy-memory] Console launched (HERMES_HYATLAS_CONSOLE=1)")
        except Exception as e:
            logger.warning("[hy-memory] Console auto-launch failed: %s", e)

    def system_prompt_block(self) -> str:
        """Return static context about Hy-Memory status."""
        if not self._client or not self._client.is_reachable():
            return ""
        return (
            f"# Hy-Memory Active\n"
            f"Mode: {self._mode}. "
            f"7-layer memory with {'System1/System2 dual processing' if self._mode == 'ultra' else 'LLM extraction' if self._mode == 'pro' else 'embedding-only'}.\n"
            f"Memories persist across sessions. Use hy_memory_search to recall.\n"
        )

    # ------------------------------------------------------------------
    # Prefetch (background recall)
    # ------------------------------------------------------------------

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Synchronous recall — return formatted memories for the query.

        Per official hermes-hy-memory 0.2.7: search the SDK with the user's
        query, flatten the result, format it with `<relevant-memories>` tags
        and evolution-chain expansion (oldest→newest), truncated to
        _MAX_PREFETCH_CHARS. Short queries and greetings are skipped to
        avoid noisy prefetch.
        """
        if not self._client or not query:
            return ""

        q = query.strip()
        if len(q) < 3 or q.lower() in _SKIP_QUERIES:
            return ""

        try:
            result = self._client.search(
                q, user_ids=[self._user_id],
                agent_ids=[self._agent_id], limit=10,
            )
            memories = self._flatten_memories(result.get("memories"))
            if not memories:
                return ""
            return self._format_memories_for_prompt(memories)
        except Exception as e:
            self._consecutive_failures += 1
            if self._consecutive_failures >= _BREAKER_THRESHOLD:
                self._breaker_open_until = time.time() + _BREAKER_COOLDOWN_SECS
                logger.warning("[hy-memory] Circuit breaker open: %s", e)
            else:
                logger.debug("[hy-memory] prefetch failed: %s", e)
            return ""

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """Start background search for the next turn (legacy async path)."""
        if not self._client:
            return

        # Circuit breaker
        if self._consecutive_failures >= _BREAKER_THRESHOLD:
            if time.time() < self._breaker_open_until:
                return
            self._consecutive_failures = 0

        def _do_prefetch():
            try:
                result = self._client.search(
                    query, user_ids=[self._user_id],
                    agent_ids=[self._agent_id], limit=5,
                )
                memories = self._flatten_memories(result.get("memories"))
                formatted = self._format_memories_for_prompt(memories) if memories else ""
                with self._prefetch_lock:
                    self._prefetch_result = formatted
                self._consecutive_failures = 0
            except Exception as e:
                self._consecutive_failures += 1
                if self._consecutive_failures >= _BREAKER_THRESHOLD:
                    self._breaker_open_until = time.time() + _BREAKER_COOLDOWN_SECS
                    logger.warning("[hy-memory] Circuit breaker open: %s", e)
                else:
                    logger.debug("[hy-memory] Prefetch failed: %s", e)

        self._prefetch_thread = threading.Thread(target=_do_prefetch, daemon=True)
        self._prefetch_thread.start()

    # ------------------------------------------------------------------
    # Sync (background write)
    # ------------------------------------------------------------------

    def _build_client(self):
        """Construct the upstream client from current config (no-op if missing).

        Used by :meth:`sync_turn` to lazy-initialize the client when the
        consumer never called :meth:`initialize` (or it was skipped because
        the upstream was down at the time). Falls back to the default
        host/port if config is missing so a fresh install still works.
        """
        from .client import HyMemoryClient
        if not getattr(self, "_config", None):
            self._config = _load_config() or {}
        host = self._config.get("server_host", "127.0.0.1")
        port = self._config.get("server_port", _DEFAULT_PORT)
        return HyMemoryClient(f"http://{host}:{port}")

    def sync_turn(self, user_content: str, assistant_content: str, *,
                  session_id: str = "", messages: list | None = None) -> None:
        """Buffer turn; flush to memory every N turns (per official 0.2.7).

        Per official hermes-hy-memory 0.2.7 write throttling: pairs of
        (user, assistant) messages accumulate in _turn_buffer[session_id]
        and only flush when the buffer hits _write_turn_window turns. This
        batches the LLM extraction call across N turns (saves tokens, faster
        than per-turn extraction) and prevents identical per-turn duplicates
        when the user repeats themselves.

        Tail-end turns below the window are flushed on on_session_end,
        on_pre_compress, and shutdown — never lost.

        Disk persistence: every buffered turn is also appended to a
        per-session pending file. If the CLI is killed mid-session, the
        next session's __init__ scans the pending dir and replays the
        buffer. This closes the silent-data-loss window where a force-kill
        would drop the in-memory buffer.
        """
        if not user_content:
            return

        # Self-heal: if the consumer never called initialize() (or it failed
        # silently because the upstream was down at init), the client is
        # still None. Try to set it up now. This is the "plug and play"
        # path — the provider works without the consumer having to wire
        # up the lifecycle perfectly.
        if self._client is None:
            try:
                self._client = self._build_client()
            except Exception:
                return  # No config to build from — silently drop.

        # Circuit breaker
        if self._consecutive_failures >= _BREAKER_THRESHOLD:
            if time.time() < self._breaker_open_until:
                return
            self._consecutive_failures = 0

        # Self-heal: if upstream is briefly down (recoverable outage,
        # mid-startup, network blip), wait up to 3s before giving up.
        # This closes the silent-stuck-agent window where the consumer
        # is alive and the user is typing but the upstream is briefly
        # unavailable. Without this, a 1s upstream blip silently drops
        # every turn until the consumer restarts.
        if not self._client.is_reachable():
            for _ in range(6):  # up to ~3s
                time.sleep(0.5)
                if self._client.is_reachable():
                    break
            if not self._client.is_reachable():
                self._consecutive_failures += 1
                if self._consecutive_failures >= _BREAKER_THRESHOLD:
                    self._breaker_open_until = time.time() + _BREAKER_COOLDOWN_SECS
                return  # Drop this turn; buffer persists for retry on next.

        # Resolve session id (allow per-call override; default = current)
        sid = session_id or "default_session"

        with self._buffer_lock:
            buf = self._turn_buffer.setdefault(sid, [])
            if messages:
                buf.extend(messages)
            else:
                buf.append({"role": "user", "content": user_content})
                buf.append({"role": "assistant", "content": assistant_content or ""})

            # Disk persistence — write the just-appended messages to a per-session
            # pending file so a force-killed CLI doesn't lose them. The file is
            # deleted on successful flush.
            self._persist_buffer_to_disk(sid)

            turns = sum(1 for m in buf if m["role"] == "user")
            if self._write_turn_window == 0:
                # Writes disabled entirely
                return
            if turns < self._write_turn_window:
                logger.debug(
                    "[hy-memory] sync_turn buffered: %d/%d turns (session=%s) — waiting",
                    turns, self._write_turn_window, sid,
                )
                return
            # Window hit: take the batch, clear the buffer, flush async
            batch = buf[:]
            self._turn_buffer[sid] = []

        def _do_sync():
            try:
                # Reconnect-on-write: the upstream may have come up after
                # this provider was initialized (e.g., user ran `hyatlas start`
                # in another terminal mid-session). Check reachability before
                # each batch flush so existing Hermes sessions auto-attach
                # to a newly-started stack without a restart.
                if not self._client or not self._client.is_reachable():
                    # Wait briefly — upstream may be mid-startup
                    if self._client and not self._client.is_reachable():
                        for _ in range(6):  # up to ~3s
                            time.sleep(0.5)
                            if self._client.is_reachable():
                                break
                    if not self._client or not self._client.is_reachable():
                        # Still not up — silently drop this batch. The buffer
                        # is persisted to disk, so a future session can
                        # replay it. The circuit breaker will also catch
                        # repeated failures.
                        self._consecutive_failures += 1
                        if self._consecutive_failures >= _BREAKER_THRESHOLD:
                            self._breaker_open_until = (
                                time.time() + _BREAKER_COOLDOWN_SECS
                            )
                            logger.warning(
                                "[hy-memory] Circuit breaker open: upstream "
                                "not reachable at %s",
                                getattr(self._client, "base_url", "?"),
                            )
                        else:
                            logger.warning(
                                "[hy-memory] sync_turn skipped (session=%s, "
                                "%d msgs): upstream not reachable — buffer "
                                "kept on disk for retry",
                                sid, len(batch),
                            )
                        return

                since = time.time()
                result = self._client.add(
                    batch, user_id=self._user_id,
                    agent_id=self._agent_id, session_id=sid,
                )
                self._consecutive_failures = 0
                # Successful write — clear the disk pending file for this session
                self._clear_disk_buffer(sid)
                # Populate importance scores on the qdrant points this add()
                # produced. Fire-and-forget so it never blocks the write path.
                self._maybe_patch_importance(
                    result, user_id=self._user_id,
                    session_id=sid, since_timestamp=since,
                )
            except Exception as e:
                self._consecutive_failures += 1
                if self._consecutive_failures >= _BREAKER_THRESHOLD:
                    self._breaker_open_until = time.time() + _BREAKER_COOLDOWN_SECS
                    logger.warning("[hy-memory] Circuit breaker open: %s", e)
                else:
                    # Surface the failure so the user knows the write was lost
                    logger.warning(
                        "[hy-memory] sync_turn FAILED for session=%s (%d msgs): %s "
                        "— buffer remains on disk, will retry next session",
                        sid, len(batch), e,
                    )

        self._sync_thread = threading.Thread(target=_do_sync, daemon=True)
        self._sync_thread.start()

    # ------------------------------------------------------------------
    # Disk persistence (Hindsight-style: never lose a turn to a process kill)
    # ------------------------------------------------------------------

    def _pending_dir(self) -> Path | None:
        """Directory for per-session pending buffer files, or None if disabled."""
        if not self._user_id:
            return None
        d = Path(get_hermes_home()) / "logs" / "hyatlas_pending" / self._user_id
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception:
            return None
        return d

    @staticmethod
    def _safe_sid(sid: str) -> str:
        """Sanitize a session id for use as a filename."""
        return "".join(c if c.isalnum() or c in "._-" else "_" for c in sid)[:120]

    def _persist_buffer_to_disk(self, sid: str) -> None:
        """Append the current buffer for a session to its pending file."""
        d = self._pending_dir()
        if d is None:
            return
        try:
            path = d / f"{self._safe_sid(sid)}.json"
            # Read existing pending file (other-session pairs we haven't flushed)
            existing: list = []
            if path.exists():
                try:
                    existing = json.loads(path.read_text(encoding="utf-8"))
                except Exception:
                    existing = []
            # Append the just-added (user, assistant) pair
            with self._buffer_lock:
                buf = self._turn_buffer.get(sid, [])
            # Take only what's not already in 'existing' (avoid double-append on retry)
            if len(buf) > len(existing):
                new_msgs = buf[len(existing):]
                existing.extend(new_msgs)
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(existing, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
        except Exception as e:
            logger.debug("[hy-memory] persist to disk failed: %s", e)

    def _clear_disk_buffer(self, sid: str) -> None:
        """Delete the pending file for a session after a successful write."""
        d = self._pending_dir()
        if d is None:
            return
        with contextlib.suppress(Exception):
            (d / f"{self._safe_sid(sid)}.json").unlink(missing_ok=True)

    def _replay_pending_buffers(self) -> int:
        """Scan pending dir and flush any orphaned buffers. Called at provider init.

        This handles the case where the CLI was force-killed before
        on_session_end fired: the disk file is still there, and we replay it
        into the in-memory buffer and flush synchronously.
        """
        d = self._pending_dir()
        if d is None or not d.exists():
            return 0
        count = 0
        for path in d.glob("*.json"):
            try:
                msgs = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            if not msgs:
                with contextlib.suppress(Exception):
                    path.unlink()
                continue
            sid = path.stem  # inverse of _safe_sid (we don't recover the original,
                              # but server-side dedup will handle duplicates)
            try:
                since = time.time()
                result = self._client.add(
                    msgs, user_id=self._user_id,
                    agent_id=self._agent_id, session_id=sid,
                )
                path.unlink()
                count += 1
                logger.info(
                    "[hy-memory] replayed orphaned pending buffer: %d msgs (session=%s)",
                    len(msgs), sid,
                )
                # Populate importance scores on the qdrant points this add()
                # produced. Fire-and-forget so it never blocks the write path.
                self._maybe_patch_importance(
                    result, user_id=self._user_id,
                    session_id=sid, since_timestamp=since,
                )
            except Exception as e:
                logger.warning(
                    "[hy-memory] could not replay pending buffer %s: %s "
                    "— will retry next session", path.name, e,
                )
        return count

    def _maybe_patch_importance(
        self, add_result: dict | None, *,
        user_id: str = "", session_id: str = "", since_timestamp: float | None = None,
    ) -> None:
        """Fire-and-forget update of `importance` on points produced by add().

        Gated by ``HYATLAS_MEMORY_IMPORTANCE=1`` (default off). Tries to
        locate all qdrant points produced by a single write call via the
        upstream ``request_id`` (fast, exact). If the SDK didn't tag every
        layer with ``request_id`` (e.g. ``l0_basic_info`` and ``l1_raw``
        don't), we fall back to a time-window match by ``user_id`` +
        ``session_id`` + ``gmt_created``. Then PATCHes each point with an
        importance score derived from its layer. This feeds the 0.15
        importance term in the upstream 4-factor MemoryScorer without
        adding LLM cost. Runs in a daemon thread so it never blocks the write
        path.

        The upstream reconciler promotes l1_raw → l4_identity asynchronously,
        often AFTER the patch thread fires. So we also schedule a delayed
        retry (8s) to catch the reconciled points. The retry scans by
        user_id + session_id + time-window — same fallback the patch uses
        when request_id isn't set.
        """
        if os.environ.get("HYATLAS_MEMORY_IMPORTANCE", "1") != "1":
            return
        if not add_result:
            return
        qdrant_url = os.environ.get(
            "HYATLAS_MEMORY_QDRANT_URL",
            f"http://{self._config.get('qdrant_host', '127.0.0.1')}"
            f":{self._config.get('qdrant_port', '6333')}"
        )
        patch_args = (
            add_result.get("request_id", ""),
            qdrant_url,
            _DEFAULT_QDRANT_COLLECTION,
        )
        patch_kwargs = {
            "user_id": user_id,
            "session_id": session_id,
            "since_timestamp": since_timestamp,
        }

        # Immediate patch — catches points created by add() itself (l1_raw,
        # l2_fact when extracted in the same call).
        threading.Thread(
            target=patches.patch_importance_for_request,
            args=patch_args,
            kwargs=patch_kwargs,
            daemon=True,
        ).start()

        # Delayed retry — catches points created by the async reconciler
        # (l4_identity, etc.) AFTER the initial patch already ran.
        def _delayed_retry() -> None:
            time.sleep(8.0)
            with contextlib.suppress(Exception):
                patches.patch_importance_for_request(
                    *patch_args, **patch_kwargs,
                )

        threading.Thread(target=_delayed_retry, daemon=True).start()

    @staticmethod
    def _flatten_memories(memories: Any) -> list[dict[str, Any]]:
        """Flatten SDK search() return to a single ordered list.

        SDK returns either:
          - dict keyed by channel: {"profile": [...], "proactive": [...], "normal": [...]}
          - legacy flat list

        The three channels are layer-mutually-exclusive (profile=l0/l6,
        proactive=l7, normal=other), so no dedup is needed. Order
        profile → proactive → normal gives user-identity memories priority
        in the prompt budget.
        """
        if isinstance(memories, dict):
            out: list[dict[str, Any]] = []
            for ch in ("profile", "proactive", "normal"):
                out.extend(memories.get(ch) or [])
            return out
        return memories or []

    @staticmethod
    def _fmt_time(ts: Any) -> str:
        """Format unix-seconds timestamp to 'YYYY-MM-DD HH:MM' (or '' if invalid).

        Matches OpenClaw's formatTime so the injected block is readable
        and consistent across plugins.
        """
        if ts is None:
            return ""
        try:
            return _dt.datetime.fromtimestamp(int(ts)).strftime("%Y-%m-%d %H:%M")
        except Exception:
            return ""

    def _format_memories_for_prompt(self, memories: list[dict[str, Any]]) -> str:
        """Format memories as a system-prompt injection block.

        Rules (aligned with official hermes-hy-memory 0.2.7 / OpenClaw):
          - Outer block wrapped in <relevant-memories>...</relevant-memories>
            with a short header explaining the format.
          - Normal memories: `- [N] <time>  <content>`
          - Evolution chains (len > 1, latest→oldest in payload) are expanded
            oldest→newest, prefixed with `[Evolved, K versions]`.
          - Total length truncated to _MAX_PREFETCH_CHARS (default 2000).
          - Single-entry cap: 800 chars to avoid one long memory eating
            the whole budget.
        """
        items: list[str] = []
        running = 0
        idx = 0
        for mem in memories:
            # Defensive: skip None / non-dict items that might sneak in
            # (e.g. when the server returns a partially-typed layer with
            # a None placeholder for a deleted memory). Verified 2026-06-16
            # in the test suite: test_handles_none_items_in_layer expects
            # graceful skip rather than a TypeError on `.get()`.
            if not isinstance(mem, dict):
                continue
            chain = mem.get("evolution_chain")
            if chain and isinstance(chain, list) and len(chain) > 1:
                # chain[0] = newest, chain[-1] = oldest; expand oldest→newest
                lines: list[str] = []
                for i in range(len(chain) - 1, 0, -1):
                    c = chain[i] or {}
                    when = self._fmt_time(c.get("memory_at"))
                    content = (c.get("content") or "").strip()
                    lines.append(f"  [v{len(chain) - i}] {when + '  ' if when else ''}{content}")
                head = chain[0] or {}
                head_when = self._fmt_time(head.get("memory_at"))
                head_content = (head.get("content") or mem.get("content") or "").strip()
                lines.append(f"  [Latest] {head_when + '  ' if head_when else ''}{head_content}")
                entry = f"- [{idx + 1}] [Evolved, {len(chain)} versions]\n" + "\n".join(lines)
            else:
                content = (mem.get("content") or "").strip()
                if not content:
                    continue
                when = self._fmt_time(mem.get("memory_at"))
                entry = f"- [{idx + 1}] {when + '  ' if when else ''}{content}"

            # Cap single-entry length
            if len(entry) > 800:
                entry = entry[:800].rstrip() + "..."
            if running + len(entry) > _MAX_PREFETCH_CHARS:
                break
            items.append(entry)
            running += len(entry) + 1
            idx += 1

        if not items:
            return ""

        # Optional access-count tracking. Bumping access_count on every recalled
        # memory completes the upstream 4-factor MemoryScorer's access term.
        # Default ON for this prototype; set HYATLAS_MEMORY_ACCESS_COUNT=0 to
        # disable. Runs in a fire-and-forget thread so recall latency is
        # unaffected.
        if os.environ.get("HYATLAS_MEMORY_ACCESS_COUNT", "1") != "0":
            qdrant_url = self._config.get("vector_store", {}).get("url", _DEFAULT_QDRANT_URL) if self._config else _DEFAULT_QDRANT_URL
            for mem in memories:
                if not isinstance(mem, dict):
                    continue
                mid = mem.get("memory_id")
                if mid:
                    threading.Thread(
                        target=patches.touch_memory,
                        args=(mid, qdrant_url, _DEFAULT_QDRANT_COLLECTION),
                        daemon=True,
                    ).start()

        body = "\n".join(items)
        return (
            "<relevant-memories>\n"
            "The following are stored memories for the current user. Use them to "
            "personalize your response. Memories with evolution chains are expanded "
            "from oldest to newest:\n"
            f"{body}\n"
            "</relevant-memories>"
        )

    def _flush_session_buffer(self, session_id: str | None = None) -> None:
        """Flush any pending turns below the write window.

        Called by on_session_end, on_pre_compress, and shutdown. With
        session_id=None, flushes all sessions (shutdown use case).
        """
        with self._buffer_lock:
            if session_id is None:
                pending: list[tuple[str, list[dict[str, str]]]] = [
                    (sid, msgs[:]) for sid, msgs in self._turn_buffer.items() if msgs
                ]
                self._turn_buffer.clear()
            else:
                msgs = self._turn_buffer.get(session_id) or []
                pending = [(session_id, msgs[:])] if msgs else []
                if session_id in self._turn_buffer:
                    self._turn_buffer[session_id] = []

        for sid, msgs in pending:
            if not msgs:
                continue
            try:
                since = time.time()
                result = self._client.add(
                    msgs, user_id=self._user_id,
                    agent_id=self._agent_id, session_id=sid,
                )
                logger.info("[hy-memory] tail flush: %d msgs (session=%s)", len(msgs), sid)
                # Populate importance scores on the qdrant points this add()
                # produced. Fire-and-forget so it never blocks the write path.
                self._maybe_patch_importance(
                    result, user_id=self._user_id,
                    session_id=sid, since_timestamp=since,
                )
            except Exception as e:
                logger.debug("[hy-memory] tail flush failed: %s", e)

    # ------------------------------------------------------------------
    # Session hooks
    # ------------------------------------------------------------------

    def on_session_end(self, messages: list[dict[str, Any]]) -> None:
        """Write final session snapshot on session end.

        Per official 0.2.7: also flush any buffered turns that didn't
        hit the write window, so we never lose tail-end context.
        """
        if not self._client:
            return
        # Flush whatever's pending in the buffer (per official behavior)
        self._flush_session_buffer(None)

        # Cross-session continuity summary
        if messages:
            try:
                # Get final turn context
                last_user = next((m for m in reversed(messages) if m.get('role') == 'user'), None)
                last_assistant = next((m for m in reversed(messages) if m.get('role') == 'assistant'), None)
                next((m for m in reversed(messages) if m.get('role') == 'tool'), None)
                turn_count = len([m for m in messages if m.get('role') in ('user', 'assistant')])

                summary_text = f"""SESSION_SUMMARY {_dt.datetime.now().isoformat()}

Session completed.
Turn count: {turn_count}

Last user message:
{last_user.get('content', '[none]')[:800]}

Last assistant response:
{last_assistant.get('content', '[none]')[:1200]}

Next expected action:
This session ended. The next session will automatically load this summary.
"""
                since = time.time()
                result = self._client.add(
                    summary_text,
                    user_id=self._user_id,
                    agent_id=self._agent_id,
                    session_id="session-end-continuity",
                    metadata={
                        'kind': 'session_summary',
                        'version': '1.0',
                        'turn_count': turn_count,
                        'timestamp': _dt.datetime.now().isoformat()
                    },
                )
                logger.info(f"[hy-memory] wrote cross-session continuity summary, {len(summary_text)} chars")
                # Populate importance scores on the qdrant points this add()
                # produced. Fire-and-forget so it never blocks the write path.
                self._maybe_patch_importance(
                    result, user_id=self._user_id,
                    session_id="session-end-continuity", since_timestamp=since,
                )

            except Exception as e:
                logger.debug("[hy-memory] on_session_end write failed: %s", e)

    def on_pre_compress(self, messages: list[dict[str, Any]]) -> str:
        """Extract insights before context compression."""
        # Let Hy-Memory's System1 handle this via sync_turn — no extra work needed
        return ""

    # ------------------------------------------------------------------
    # Tools
    # ------------------------------------------------------------------

    def get_tool_schemas(self) -> list[dict[str, Any]]:
        return [{
            "name": "hy_memory_search",
            "description": (
                "Search persistent long-term memory across all past sessions. "
                "Returns memories about user preferences, facts, identity, "
                "and context that persist between conversations."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query — what to look for in memory",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max results to return (default 5)",
                        "default": 5,
                    },
                },
                "required": ["query"],
            },
        }]

    def handle_tool_call(self, tool_name: str, args: dict[str, Any], **kwargs) -> str:
        """Dispatch tool calls."""
        if tool_name == "hy_memory_search":
            return self._tool_search(args)
        return tool_error(f"Unknown Hy-Memory tool: {tool_name}")

    def _tool_search(self, args: dict) -> str:
        """Handle hy_memory_search tool call."""
        if not self._client:
            return json.dumps({"error": "Hy-Memory not connected"})

        query = args.get("query", "")
        limit = args.get("limit", 5)
        if not query:
            return json.dumps({"error": "query is required"})

        try:
            result = self._client.search(
                query, user_ids=[self._user_id],
                agent_ids=[self._agent_id], limit=limit,
            )
            # SDK v1.2+ returns {"memories": {"profile": [...], "proactive":
            # [...], "normal": [...]}, "request_id": ..., "elapsed_ms": ...}.
            # The inner "memories" key holds the layered dict; flatten it
            # to a single ordered list. Also handles the legacy shape
            # where the SDK returns the layered dict at the top level.
            # Without this, `result.get("memories", [])` returns the layered
            # dict and `m.get("content", "")` crashes. Verified 2026-06-16.
            inner = result.get("memories") if isinstance(result, dict) else result
            memories = self._flatten_memories(inner)
            if not memories:
                return json.dumps({"memories": [], "note": "No relevant memories found"})

            formatted = []
            for m in memories:
                # Some server-side items lack a "layer" field. Default to
                # the bucket name (set by _flatten_memories channel order:
                # profile → proactive → normal). Verified 2026-06-16:
                # test_layered_mixed_flattens_preserving_layer expects
                # layer=profile on items where the server omitted it.
                layer = m.get("layer", "") or ""
                # The bucket name is encoded in the flatten call order;
                # we recover it by inspecting which channel this list
                # element came from. Simpler: re-derive the bucket here
                # from the original layered shape.
                if not layer and isinstance(result, dict) and "memories" in result:
                    layered = result["memories"]
                    for ch in ("profile", "proactive", "normal"):
                        if ch in layered and m in (layered[ch] or []):
                            layer = ch
                            break
                formatted.append({
                    "content": m.get("content", ""),
                    "layer": layer,
                    "score": round(m.get("score", 0), 3),
                })
            return json.dumps({"memories": formatted})
        except Exception as e:
            return json.dumps({"error": str(e)})

    # ------------------------------------------------------------------
    # Shutdown
    # ------------------------------------------------------------------

    def shutdown(self) -> None:
        """Stop the server if we started it. Flushes buffered turns first."""
        # Flush any pending turns (per official 0.2.7 — never lose context
        # even on shutdown mid-window)
        if self._client:
            try:
                self._flush_session_buffer(None)
            except Exception as e:
                logger.debug("[hy-memory] shutdown flush failed: %s", e)
        if self._process:
            self._process.stop()
            self._process = None
        self._client = None

    # ------------------------------------------------------------------
    # Config schema (for hermes memory setup fallback)
    # ------------------------------------------------------------------

    def get_config_schema(self) -> list[dict[str, Any]]:
        return [
            {"key": "mode", "description": "Processing mode",
             "default": "pro", "choices": ["lite", "pro", "ultra"]},
            {"key": "server_port", "description": "Hy-Memory server port",
             "default": str(_DEFAULT_PORT)},
            {"key": "auto_start", "description": "Auto-start server with Hermes",
             "default": "true", "choices": ["true", "false"]},
            {"key": "llm_api_key", "description": "LLM API key for memory extraction",
             "secret": True, "env_var": "HY_MEMORY_LLM_API_KEY",
             "when": {"mode": ["pro", "ultra"]}},
            {"key": "llm_model", "description": "LLM model",
             "default": "gpt-4o-mini", "when": {"mode": ["pro", "ultra"]}},
            {"key": "llm_base_url", "description": "LLM API base URL",
             "default": "https://api.openai.com/v1",
             "when": {"mode": ["pro", "ultra"]}},
            {"key": "embedder_api_key", "description": "Embedding API key",
             "secret": True, "env_var": "HY_MEMORY_EMBEDDER_API_KEY"},
            {"key": "embedder_model", "description": "Embedding model",
             "default": "text-embedding-3-small"},
            {"key": "embedder_dims", "description": "Embedding dimensions",
             "default": "1536"},
            {"key": "vector_store", "description": "Vector store backend",
             "default": "chroma", "choices": ["chroma", "qdrant", "faiss"]},
        ]

    # ------------------------------------------------------------------
    # Config save
    # ------------------------------------------------------------------

    def save_config(self, values: dict[str, Any], hermes_home: str) -> None:
        """Write config to $HERMES_HOME/hy_memory.json."""
        config_path = Path(hermes_home) / "hy_memory.json"
        existing: dict[str, Any] = {}
        if config_path.exists():
            with contextlib.suppress(Exception):
                existing = json.loads(config_path.read_text(encoding="utf-8"))
        existing.update(values)
        config_path.write_text(
            json.dumps(existing, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        try:
            import stat
            config_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
        except (OSError, AttributeError):
            pass  # Windows

    # ------------------------------------------------------------------
    # post_setup — custom interactive wizard
    # ------------------------------------------------------------------

    def post_setup(self, hermes_home: str, config: dict) -> None:
        """Custom setup wizard for Hy-Memory."""
        import shutil
        import subprocess
        import sys

        from hermes_cli.config import save_config
        from hermes_cli.memory_setup import _curses_select
        from hermes_cli.secret_prompt import masked_secret_prompt

        print("\n  Configuring Hy-Memory memory:\n")

        existing_config = _load_config()

        # Step 1: Mode selection
        mode_values = ["lite", "pro", "ultra"]
        mode_items = [
            ("Lite", "Embedding only — zero LLM cost, fastest"),
            ("Pro", "LLM fact extraction — balanced quality and cost"),
            ("Ultra", "System1/System2 cognitive — highest quality, Kuzu graph"),
        ]
        existing_mode = existing_config.get("mode", "pro")
        mode_default = mode_values.index(existing_mode) if existing_mode in mode_values else 1
        mode_idx = _curses_select("  Select processing mode", mode_items, default=mode_default)
        mode = mode_values[mode_idx]

        provider_config: dict = dict(existing_config)
        provider_config["mode"] = mode
        env_writes: dict = {}

        # Step 2: Install dependencies
        print("\n  Checking dependencies...")
        uv_path = shutil.which("uv")
        deps = ["hy-memory", "kuzu", "chromadb"]
        if not uv_path:
            print("  ⚠ uv not found — install: curl -LsSf https://astral.sh/uv/install.sh | sh")
            print(f"  Then: uv pip install --python {sys.executable} {' '.join(deps)}")
        else:
            try:
                subprocess.run(
                    [uv_path, "pip", "install", "--python", sys.executable,
                     "--quiet", "--upgrade"] + deps,
                    check=True, timeout=180, capture_output=True,
                )
                print("  ✓ Dependencies up to date")
            except Exception as e:
                print(f"  ⚠ Install failed: {e}")
                print(f"  Run manually: uv pip install --python {sys.executable} {' '.join(deps)}")

        # Step 3: LLM config (pro/ultra only)
        if mode in ("pro", "ultra"):
            print("\n  LLM Configuration (for memory extraction):\n")
            llm_cfg = existing_config.get("llm", {})

            existing_key = llm_cfg.get("api_key", "") or os.environ.get("HY_MEMORY_LLM_API_KEY", "")
            if existing_key:
                masked = f"...{existing_key[-4:]}" if len(existing_key) > 4 else "set"
                sys.stdout.write(f"  LLM API key (current: {masked}, blank to keep): ")
                sys.stdout.flush()
                api_key = masked_secret_prompt("") if sys.stdin.isatty() else sys.stdin.readline().strip()
            else:
                sys.stdout.write("  LLM API key: ")
                sys.stdout.flush()
                api_key = masked_secret_prompt("") if sys.stdin.isatty() else sys.stdin.readline().strip()
            if api_key:
                env_writes["HY_MEMORY_LLM_API_KEY"] = api_key

            val = input(f"  LLM model [{llm_cfg.get('model', 'gpt-4o-mini')}]: ").strip()
            if val:
                provider_config.setdefault("llm", {})["model"] = val
            elif llm_cfg.get("model"):
                provider_config.setdefault("llm", {})["model"] = llm_cfg["model"]

            val = input(f"  LLM base URL [{llm_cfg.get('base_url', 'https://api.openai.com/v1')}]: ").strip()
            if val:
                provider_config.setdefault("llm", {})["base_url"] = val
            elif llm_cfg.get("base_url"):
                provider_config.setdefault("llm", {})["base_url"] = llm_cfg["base_url"]

        # Step 4: Embedding config
        print("\n  Embedding Configuration:\n")
        emb_cfg = existing_config.get("embedder", {})

        existing_emb_key = emb_cfg.get("api_key", "") or os.environ.get("HY_MEMORY_EMBEDDER_API_KEY", "")
        if existing_emb_key:
            masked = f"...{existing_emb_key[-4:]}" if len(existing_emb_key) > 4 else "set"
            same_as_llm = ""
            if mode in ("pro", "ultra") and env_writes.get("HY_MEMORY_LLM_API_KEY"):
                same_as_llm = " (blank to use same as LLM key)"
            sys.stdout.write(f"  Embedding API key (current: {masked}{same_as_llm}, blank to keep): ")
            sys.stdout.flush()
            emb_key = masked_secret_prompt("") if sys.stdin.isatty() else sys.stdin.readline().strip()
        else:
            if mode in ("pro", "ultra") and env_writes.get("HY_MEMORY_LLM_API_KEY"):
                sys.stdout.write("  Embedding API key (blank to use same as LLM key): ")
                sys.stdout.flush()
                emb_key = masked_secret_prompt("") if sys.stdin.isatty() else sys.stdin.readline().strip()
                if not emb_key:
                    emb_key = env_writes.get("HY_MEMORY_LLM_API_KEY", "")
            else:
                sys.stdout.write("  Embedding API key: ")
                sys.stdout.flush()
                emb_key = masked_secret_prompt("") if sys.stdin.isatty() else sys.stdin.readline().strip()
        if emb_key:
            env_writes["HY_MEMORY_EMBEDDER_API_KEY"] = emb_key

        val = input(f"  Embedding model [{emb_cfg.get('model', 'text-embedding-3-small')}]: ").strip()
        if val:
            provider_config.setdefault("embedder", {})["model"] = val
        elif emb_cfg.get("model"):
            provider_config.setdefault("embedder", {})["model"] = emb_cfg["model"]

        val = input(f"  Embedding dims [{emb_cfg.get('dims', 1536)}]: ").strip()
        if val:
            provider_config.setdefault("embedder", {})["dims"] = int(val)

        # Step 5: Vector store
        print("\n  Vector Store:\n")
        vs_values = ["chroma", "qdrant", "faiss"]
        vs_items = [
            ("Chroma", "Local, zero-config (recommended)"),
            ("Qdrant", "Remote or local Qdrant server"),
            ("FAISS", "Local, fast, Facebook AI Similarity Search"),
        ]
        existing_vs = existing_config.get("vector_store", {}).get("provider", "chroma")
        vs_default = vs_values.index(existing_vs) if existing_vs in vs_values else 0
        vs_idx = _curses_select("  Vector store backend", vs_items, default=vs_default)
        provider_config.setdefault("vector_store", {})["provider"] = vs_values[vs_idx]

        # Step 6: Server config
        val = input(f"  Server port [{existing_config.get('server_port', _DEFAULT_PORT)}]: ").strip()
        if val:
            provider_config["server_port"] = int(val)

        provider_config["auto_start"] = True

        # Step 7: Start server + health check
        print("\n  Starting Hy-Memory server...")
        # Temporarily set env vars so the server can find credentials
        for k, v in env_writes.items():
            os.environ[k] = v

        from .process import HyMemoryProcess
        proc = HyMemoryProcess(provider_config)
        if proc.start():
            from .client import HyMemoryClient
            client = HyMemoryClient(proc.base_url)
            try:
                status = client.status()
                checks = []
                for key in ("vdb", "embed", "llm"):
                    val = status.get(key, "?")
                    checks.append(f"{key}: {val}")
                print(f"  ✓ Server ready — {', '.join(checks)}")
            except Exception:
                print("  ✓ Server running (deep status check skipped)")
        else:
            print("  ⚠ Server failed to start — check logs and retry")
            print(f"    Logs: {get_hermes_home() / 'hy-memory-venv'}")

        # Step 8: Save & activate
        # Write secrets to .env
        if env_writes:
            env_path = Path(hermes_home) / ".env"
            _write_env_vars(env_path, env_writes)
            print("  ✓ API keys saved to .env")

        # Write provider config
        self.save_config(provider_config, hermes_home)

        # Activate in config.yaml
        config.setdefault("memory", {})["provider"] = "hy_memory"
        save_config(config)

        print(f"\n  ✓ Hy-Memory activated (mode: {mode})")
        print("  Start a new session to activate.\n")


def _write_env_vars(env_path: Path, env_writes: dict) -> None:
    """Append or update env vars in .env file."""
    env_path.parent.mkdir(parents=True, exist_ok=True)

    existing_lines = []
    if env_path.exists():
        existing_lines = env_path.read_text(encoding="utf-8").splitlines()

    updated_keys = set()
    new_lines = []
    for line in existing_lines:
        key_match = line.split("=", 1)[0].strip() if "=" in line else ""
        if key_match in env_writes:
            new_lines.append(f"{key_match}={env_writes[key_match]}")
            updated_keys.add(key_match)
        else:
            new_lines.append(line)

    for key, val in env_writes.items():
        if key not in updated_keys:
            new_lines.append(f"{key}={val}")

    env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    try:
        import stat
        env_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    except OSError:
        pass  # Windows


# Module-level compatibility shim for tests + external callers that
# imported the old `_format_memories` function. The current implementation
# is a method on HyMemoryProvider, so we expose a thin module-level wrapper
# that instantiates a default provider and delegates. Verified 2026-06-16:
# restoring the old import path so the carried-over test suite (test_hy_
# memory_search.py) keeps working without rewrites.
def _format_memories(memories):
    """Module-level shim for HyMemoryProvider._format_memories_for_prompt.
    New code should call the method directly via a provider instance.
    """
    if memories is None:
        return ""
    # Flatten SDK layered-dict shape ({profile, proactive, normal}) to a
    # single ordered list before delegating. _format_memories_for_prompt
    # expects a flat list of dicts, not a layered dict. Verified 2026-06-16:
    # without this, the test case `test_layered_full` crashes with
    # "'str' object has no attribute 'get'" on `mem.get("evolution_chain")`.
    return HyMemoryProvider()._format_memories_for_prompt(
        HyMemoryProvider._flatten_memories(memories)
    )
