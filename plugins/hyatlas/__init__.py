"""HyAtlas — Hermes memory provider plugin.

Pure-Python client that talks to the HyAtlas Go binary at
``server_host:server_port`` (default 127.0.0.1:19528, configurable via
the Desktop settings form, config.yaml, env vars, or per-profile JSON).

All storage (chromem-go), embedding (in-process BGE-small via
onnxruntime-go), and LLM extraction happen inside the Go binary — this
plugin is a thin HTTP client + MemoryProvider implementation. The
server is a separate install (see the repo README); the plugin does not
bundle or download it.

This plugin follows the canonical Hermes memory-provider pattern
(Honcho / Hindsight shape):

* ``__init__.py`` — MemoryProvider subclass + register(ctx)
* ``client.py`` — HTTP client to the server
* ``process.py`` — optional auto-start of the Go binary as a subprocess
* ``cli.py`` — ``hermes hyatlas`` subcommands (status, search, add, recent, start, stop)
* ``schemas.py`` — tool schemas (status / search / recent / add)
* ``__main__.py`` — standalone ``python -m`` entry point
* ``plugin.yaml`` — metadata + config_schema (Desktop settings form)
* ``after-install.md`` — install instructions
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections import OrderedDict
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider

from .client import PLUGIN_VERSION, HyatlasClient, HyatlasClientError, HyatlasUnreachable
from .formatting import channel_order, format_hit_line  # noqa: E402

logger = logging.getLogger(__name__)

# Resolve paths for bundled sibling modules
_PLUGIN_ROOT = Path(__file__).resolve().parent

# Settings resolution, shared with dashboard/plugin_api.py
from . import settings

# Tool schemas (used by register())
from .schemas import (  # noqa: E402
    HYATLAS_STATUS_SCHEMA,
    HYATLAS_SEARCH_SCHEMA,
    HYATLAS_RECENT_SCHEMA,
    HYATLAS_ADD_SCHEMA,
)

# Per-message and per-turn caps on what sync_turn sends. A turn is the user's
# message and the assistant's reply. Tool output and compaction summaries are not
# memories; sending them made raw rows of 200,000+ characters that the extraction
# LLM then had to read in full.
_MAX_MESSAGE_CHARS = 4000
_MAX_TURN_CHARS = 12000


def _clip(text: str, limit: int) -> str:
    """Cut text to limit characters, saying how much was dropped."""
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n…[truncated {len(text) - limit} chars]"


def _is_compaction(text: str) -> bool:
    """Hermes' context-compaction summary, detected the way Hermes itself does
    (agent/context_compressor.py): a marker near the start of the message."""
    head = text[:200]
    return "CONTEXT COMPACTION" in head or "Conversation Summary" in head


# =============================================================================
# Configuration
# =============================================================================


def _load_config() -> Dict[str, Any]:
    """Resolve plugin settings. See :mod:`settings` for the precedence rules.

    Kept as a module-level alias so existing callers and the smoke tests keep
    working; the implementation lives in ``settings.py`` because the dashboard's
    ``plugin_api.py`` is loaded by file path and cannot use a relative import,
    and the two must not drift on which host and port they talk to.
    """
    return settings.load()


# =============================================================================
# Provider
# =============================================================================


# Seconds between checks of whether a local server is waiting for the AI key.
_KEY_CHECK_INTERVAL = 600.0


class HyatlasMemoryProvider(MemoryProvider):
    """Hermes MemoryProvider implementation for the HyAtlas Go backend.

    The provider is a thin HTTP client wrapper. The server handles
    all storage (chromem-go), embedding (in-process BGE-small via
    onnxruntime-go), and LLM extraction (endpoint + model configured on
    the server side via HYATLAS_LLM_* env vars — no LLM credentials
    live in this plugin). This class is responsible for:

    * Lifecycle (initialize / shutdown)
    * Per-session identity (user_id, agent_id) resolution
    * Format conversion (3-channel search -> prompt block)
    * Tool surface (status / search / recent / add)
    * Prefetch + sync_turn persistence to the server
    * on_pre_compress checkpoint (fail-closed durability)
    * on_memory_write mirror for atomic agent-chosen facts
    """

    def __init__(self) -> None:
        self._config = _load_config()
        self._client: Optional[HyatlasClient] = None
        self._user_id: str = ""
        # The session the provider was initialised for; searches send it so the
        # server can rank this session's intentions first.
        self._session_id: str = ""
        self._agent_id: str = ""
        self._prefetch_lock = threading.Lock()
        # Last recall per session, the fallback when a live search fails. Keyed by
        # session so concurrent sessions (gateway and CLI) never see each other's.
        self._prefetch_cache: "OrderedDict[str, str]" = OrderedDict()
        # Memory ids the last recall put in front of the agent, per session. The
        # reply is posted against them so the server learns which recalled memories
        # were used. Bounded like _prefetch_cache and guarded by _prefetch_lock.
        self._recalled: "OrderedDict[str, List[str]]" = OrderedDict()
        # The query each recall was made with, keyed like _recalled: feedback leaves
        # its words out, because a reply naturally repeats the question.
        self._recalled_query: Dict[str, str] = {}
        self._version = PLUGIN_VERSION
        # Message count already synced per session, so _build_turn_text sends
        # only what the server has not seen. Bounded: a long-lived gateway
        # touches far more sessions than it can hold, and the cost of evicting
        # one is a re-flush of that session's current turn, not a correctness
        # failure.
        self._synced: "OrderedDict[str, int]" = OrderedDict()
        # When the key hand-over was last checked, and a lock so two checks never
        # run at once. See _hand_over_key.
        self._key_check_lock = threading.Lock()
        self._key_checked_at: float = 0.0

    # --- Required ABC methods ---

    @property
    def name(self) -> str:
        return "hyatlas"

    def backup_paths(self) -> List[str]:
        """Existing directories this provider owns, for ``hermes backup``.

        ``backup.py`` drops any declared path that does not exist, and skips —
        but reports — anything outside ``$HOME``, so the only useful answer is
        the resolved data directory the server actually writes to. Declaring
        relative names such as ``data/graph.json`` resolves against the agent's
        CWD, which is not where the server keeps anything, so those were dropped
        and the provider contributed nothing to a backup.

        The server owns this path (``HYATLAS_GO_DATA``), and the plugin learns it
        from the same sources the launcher uses: that variable, an explicit
        ``data_dir`` setting, then the conventional default locations. Only
        directories that exist are returned, so a user with a non-default layout
        gets an accurate answer rather than a path that silently archives
        nothing.
        """
        cfg = self._config
        declared = str(cfg.get("data_dir") or "").strip()
        candidates = [
            os.environ.get("HYATLAS_GO_DATA", ""),
            declared,
            str(settings.home() / "hyatlas-data"),
            str(Path.home() / ".hyatlas" / "data"),
            str(Path.home() / ".hyatlas"),
        ]
        out: List[str] = []
        seen = set()
        for raw in candidates:
            if not raw:
                continue
            p = Path(raw).expanduser()
            if not p.is_dir():
                continue
            try:
                key = p.resolve()
            except (OSError, ValueError):
                continue
            if key in seen:
                continue
            seen.add(key)
            out.append(str(p))
        return out

    def is_available(self) -> bool:
        """True iff the server is reachable on the configured port.

        Does NOT auto-start the server — that's a separate decision
        via ``hermes hyatlas start`` (or the
        plugin's auto_start config flag, honored at initialize() time).
        """
        try:
            client = self._ensure_client()
            return client.is_reachable()
        except Exception as e:
            logger.debug("hyatlas is_available failed: %s", e)
            return False

    def unavailable_reason(self) -> str:
        """Why is_available() returned False — surfaced in the dashboard."""
        return (
            f"HyAtlas not reachable at {self._origin()}. "
            "Start it with `hermes hyatlas start`. The `hermes hyatlas` command "
            "exists only while memory.provider is hyatlas (`hermes memory setup`)."
        )

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        """Connect, resolve identity, optionally auto-start the server."""
        self._client = self._ensure_client()
        self._session_id = session_id or ""
        self._user_id = self._resolve_user_id(kwargs)
        self._agent_id = self._resolve_agent_id(kwargs)

        # Cron / flush guard — don't pollute memory from synthetic contexts
        agent_context = kwargs.get("agent_context", "")
        if agent_context in ("cron", "flush"):
            logger.debug("hyatlas skipping init for context=%s", agent_context)
            return

        # Auto-start the server if configured
        if settings.truthy(self._config.get("auto_start")) and not self._client.is_reachable():
            if not self._ensure_server_running():
                logger.warning(
                    "auto_start enabled but server did not become reachable within 30s"
                )
                return

        # Touch the server to confirm it's healthy
        try:
            status = self._client.status()
            logger.info(
                "hyatlas v%s connected: vdb=%s embed=%s llm=%s layers=%s",
                self._version,
                status.get("vdb"),
                status.get("embed"),
                status.get("llm"),
                status.get("layers", {}),
            )
        except HyatlasUnreachable as e:
            logger.warning("hyatlas unreachable at initialize: %s", e)
        self._hand_over_key_async(force=True)

    def shutdown(self) -> None:
        """Clean exit. We do not stop the server (it's user-owned)."""
        if self._client is not None:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None

    # --- Required: tool schemas ---

    def get_tool_schemas(self) -> List[Dict[str, Any]]:
        return [
            HYATLAS_STATUS_SCHEMA,
            HYATLAS_SEARCH_SCHEMA,
            HYATLAS_RECENT_SCHEMA,
            HYATLAS_ADD_SCHEMA,
        ]

    def handle_tool_call(self, tool_name: str, args: Dict[str, Any], **kwargs: Any) -> str:
        """Dispatch tool calls to the server."""
        if self._client is None:
            return json.dumps({"error": "hyatlas not initialized"})

        try:
            if tool_name == "hyatlas_status":
                status = self._client.status()
                return json.dumps(status)
            if tool_name == "hyatlas_search":
                results = self._client.search(
                    query=args.get("query", ""),
                    user_id=args.get("user_id", self._user_id) or self._user_id,
                    agent_id=args.get("agent_id", self._agent_id) or self._agent_id,
                    layer=args.get("layer", "") or "",
                    limit=int(args.get("limit", 10)),
                    session_id=self._session_id,
                )
                return json.dumps(results)
            if tool_name == "hyatlas_recent":
                items = self._client.list_memories(
                    user_id=args.get("user_id", self._user_id) or self._user_id,
                    agent_id=args.get("agent_id", self._agent_id) or self._agent_id,
                    layer=args.get("layer", "") or "",
                    limit=int(args.get("limit", 20)),
                    include_raw=(None if args.get("include_raw") is None
                                 else bool(args.get("include_raw"))),
                )
                return json.dumps(items)
            if tool_name == "hyatlas_add":
                resp = self._client.add(
                    text=args.get("text", ""),
                    user_id=args.get("user_id", self._user_id) or self._user_id,
                    agent_id=args.get("agent_id", self._agent_id) or self._agent_id,
                    session_id=args.get("session_id", "") or "",
                )
                return json.dumps(resp)
            return json.dumps({"error": f"unknown tool: {tool_name}"})
        except HyatlasClientError as e:
            return json.dumps({"error": str(e)})
        except Exception as e:
            logger.exception("hyatlas tool call failed: %s", tool_name)
            return json.dumps({"error": f"hyatlas error: {e}"})

    # --- Optional: system prompt block ---

    def _origin(self) -> str:
        """The server origin, built once so every surface agrees on it."""
        host = self._config.get("server_host") or "127.0.0.1"
        port = self._config.get("server_port") or 19528
        return f"{host}:{port}"

    def system_prompt_block(self) -> str:
        """Short static block for the agent's system prompt.

        The server already has prefetch() returning relevant context; this is
        the static pointer the agent always sees.
        """
        return (
            f"You have access to a HyAtlas 7-layer memory system "
            f"(server: {self._origin()}). "
            "Use the `hyatlas_search` tool to recall relevant past context, "
            "`hyatlas_recent` to see the latest memories, and `hyatlas_add` "
            "to record durable facts. Adds through the standard Hermes "
            "`memory` tool are also stored in HyAtlas as raw memories; HyAtlas fills the "
            "L1 Profile layer when extraction labels them as user preferences "
            "(pro and ultra modes)."
        )

    # --- Optional: prefetch (sync, returns cached result) ---

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Recall for the turn about to run, searched on that turn's own message.

        Hermes passes the current message and bounds this call with a timeout, and a
        local search takes ~100 ms, so recall is live rather than the previous
        turn's. If the search fails, the session's last recall is returned instead.
        """
        if query and self._client:
            try:
                results = self._client.search(
                    query=query,
                    user_id=self._user_id,
                    agent_id=self._agent_id,
                    limit=5,
                    session_id=session_id or self._session_id,
                )
                formatted = self._format_prefetch(results, query)
                self._store_prefetch(session_id, formatted)
                self._store_recalled(session_id, results, query)
                return formatted
            except Exception as e:
                logger.debug("prefetch search failed, using cached recall: %s", e)
        with self._prefetch_lock:
            return self._prefetch_cache.get(session_id, "")

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """Fire-and-forget recall for the next agent turn."""
        if not query or not self._client:
            return
        self._hand_over_key_async()

        def _do() -> None:
            try:
                results = self._client.search(
                    query=query,
                    user_id=self._user_id,
                    agent_id=self._agent_id,
                    limit=5,
                    session_id=session_id or self._session_id,
                )
                formatted = self._format_prefetch(results, query)
                self._store_prefetch(session_id, formatted)
                self._store_recalled(session_id, results, query)
            except Exception as e:
                logger.debug("prefetch failed: %s", e)

        threading.Thread(target=_do, daemon=True, name="hyatlas-prefetch").start()

    # --- Optional: sync_turn (write after each agent turn) ---

    def sync_turn(
        self,
        user_content: str,
        assistant_content: str,
        *,
        session_id: str = "",
        messages: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        """Persist a turn. the server runs LLM extraction async.

        We send a single combined message that the LLM can extract
        facts/summary/intentions from. v3.5's split-user/assistant model
        still works (the LLM is robust to either shape), but
        concatenating gives a cleaner extraction input.
        """
        if not self._client:
            return
        if not user_content and not assistant_content:
            return

        text = self._build_turn_text(
            user_content, assistant_content, messages, session_id=session_id)
        if not text.strip():
            return

        try:
            self._client.add(
                text=text,
                user_id=self._user_id,
                agent_id=self._agent_id,
                session_id=session_id or "",
            )
        except Exception as e:
            # sync_turn must be best-effort — never raise
            logger.debug("sync_turn failed: %s", e)
        self._send_feedback(session_id, assistant_content)

    # --- Optional: on_pre_compress (fail-closed checkpoint) ---

    def on_pre_compress(self, messages: List[Dict[str, Any]]) -> str:
        """Before context compression, force-flush any pending turn.

        The server's on_pre_compress returns '' (empty) because the server doesn't need a
        pre-compress string injected — the LLM extraction runs on every
        sync_turn immediately, and the data is durable in the store.
        This hook exists as a fail-closed safety net: if any buffered
        prefetch is in flight, we wait briefly so it's not lost.
        """
        # No-op: sync_turn already persists immediately. We just
        # confirm the server is still reachable; if not, return empty.
        if self._client and not self._client.is_reachable():
            logger.warning("on_pre_compress: server unreachable, skipping flush")
        return ""

    # --- Optional: on_memory_write (atomic agent-chosen fact mirror) ---

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Mirror Hermes' built-in memory tool writes to HyAtlas.

        ``add`` is a plain ``/api/v1/add``: it is stored as a raw (L2) memory,
        tagged with ``write_origin`` and ``target`` in its metadata. It does not
        write to L1 Profile directly. L1 fills later, when extraction labels the
        memory as a user preference (pro and ultra modes).

        ``replace`` and ``remove`` forget the previous entry, which Hermes gives as
        ``metadata["previous_content"]``, so HyAtlas stops recalling it. The forget
        is reversible on the server. ``replace`` then adds the new content as
        ``add`` does; ``remove`` with no previous content does nothing. Never raises.
        """
        if not self._client or action not in ("add", "replace", "remove"):
            return
        meta = dict(metadata or {})
        if action in ("replace", "remove"):
            previous = meta.pop("previous_content", None)
            self._forget_previous(previous, action)
            if action == "remove" or not content:
                return
        if action == "add" and not content:
            return
        meta.setdefault("write_origin", "memory_tool")
        meta.setdefault("target", target)
        try:
            self._client.add(
                text=content,
                user_id=self._user_id,
                agent_id=self._agent_id,
                session_id=(meta.get("session_id", "") or ""),
                metadata=meta,
            )
        except Exception as e:
            logger.debug("on_memory_write failed: %s", e)

    def _forget_previous(self, previous: Any, action: str) -> None:
        """Forget the previous entry of a Hermes memory replace or remove.

        Nothing is sent when the previous content is missing or blank. Never raises.
        """
        if not isinstance(previous, str) or not previous.strip():
            logger.debug("on_memory_write: %s has no previous_content to forget", action)
            return
        reason = "replaced with Hermes's memory tool" if action == "replace" else ""
        try:
            self._client.forget(
                text=previous,
                user_id=self._user_id,
                agent_id=self._agent_id,
                reason=reason,
            )
        except Exception as e:
            logger.debug("on_memory_write forget failed: %s", e)

    # --- Optional: on_session_end (final flush) ---

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        """Session-end hook. The server persists per-turn, so this is a no-op
        beyond a debug log."""
        logger.debug(
            "on_session_end: %d messages persisted across session %s",
            len(messages) if messages else 0,
            self._user_id,
        )

    # --- Optional: config wizard surface ---

    def get_config_schema(self) -> List[Dict[str, Any]]:
        """Settings this provider exposes, derived from :mod:`settings`.

        Not a literal here: the manifest declares the same fourteen settings for the
        Desktop settings form, and the two drifted until the provider offered
        only five — so four settings existed in ``plugin.yaml`` but were
        invisible to ``hermes memory setup``. A test pins the two together.
        """
        return list(settings.config_schema())


    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        """Persist non-secret config to ``$HERMES_HOME/hyatlas.json``.

        Only relevant keys are written. Legacy v3.5 fields (llm,
        vector_store, api_keys, etc.) are NOT written here — those
        belong to the v3.5 Python server's config, not the Go
        client. The binary itself reads LLM env vars
        (HYATLAS_LLM_BASE, etc.) directly, so no LLM creds belong
        in this JSON.
        """
        path = Path(hermes_home) / "hyatlas.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        merged = {**self._config, **values}
        # llm_key is declared secret, so the setup wizard routes it to .env and
        # never puts it here. Strip it anyway: a defence-in-depth guard against a
        # future caller, a hand-edited config.yaml, or a Desktop form that does
        # not honour the secret flag, writing a credential into hyatlas.json.
        for k in ("request_timeout", "llm_key"):
            merged.pop(k, None)
        try:
            import stat
            path.write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")
            try:
                path.chmod(stat.S_IRUSR | stat.S_IWUSR)
            except (OSError, AttributeError):
                pass  # Windows
        except OSError as e:
            logger.warning("save_config failed: %s", e)

    # =============================================================================
    # Internal helpers
    # =============================================================================

    def _ensure_client(self) -> HyatlasClient:
        if self._client is None:
            self._client = HyatlasClient(
                base_url=f"http://{self._origin()}",
                timeout=float(self._config.get("request_timeout", 15.0)),
            )
        return self._client

    def _resolve_user_id(self, kwargs: Dict[str, Any]) -> str:
        return (
            os.environ.get("HYATLAS_USER_ID", "").strip()
            or kwargs.get("user_id", "")
            or self._config.get("user_id", "")
            or "default"
        )

    def _resolve_agent_id(self, kwargs: Dict[str, Any]) -> str:
        return (
            os.environ.get("HYATLAS_AGENT_ID", "").strip()
            or kwargs.get("agent_identity", "")
            or kwargs.get("agent_id", "")
            or self._config.get("agent_id", "")
            or "default"
        )

    def _build_turn_text(
        self,
        user_content: str,
        assistant_content: str,
        messages: Optional[List[Dict[str, Any]]] = None,
        session_id: str = "",
    ) -> str:
        """Build the text for THIS turn only, never the whole transcript.

        ``sync_turn`` is called after every turn with the full conversation so
        far, so sending all of *messages* would re-upload the entire transcript
        each time — quadratic in conversation length, and the server would
        re-extract facts from messages it already saw. Only user and assistant
        text is sent, compaction summaries are skipped, and each message and the
        whole turn are capped.

        The caller's ``user_content`` / ``assistant_content`` pair is exactly
        one turn, so that is the baseline and it is never dropped. *messages*
        is used only to close gaps: if the thread has grown past the count last
        synced for this session, the messages in between are turns this provider
        never reported (an earlier failure, or a caller that jumped straight
        here), and they are flushed so the gap does not silently lose memory.

        Growth is tracked per session. A thread that shrinks means context
        compression rewrote the history, so the index is reset and the single
        reported turn is used — re-flushing a compressed thread would re-send
        content the server already has.
        """
        if not messages:
            return self._turn_pair(user_content, assistant_content)

        seen = self._synced.get(session_id)
        if seen is None:
            # First turn seen for this session. Adopt the thread's current
            # length as the baseline instead of 0: everything already in it
            # predates this provider instance, and flushing it would upload the
            # whole transcript to the extraction LLM on the first turn after
            # every gateway restart. The turn the caller is reporting is
            # carried by user_content / assistant_content, so nothing is lost.
            self._record_synced(session_id, len(messages))
            return self._turn_pair(user_content, assistant_content)

        if len(messages) <= seen:
            # Compressed or replayed thread — the index is meaningless now.
            self._record_synced(session_id, len(messages))
            return self._turn_pair(user_content, assistant_content)

        new = messages[seen:]
        self._record_synced(session_id, len(messages))
        parts = []
        for m in new:
            role = m.get("role", "")
            content = m.get("content", "")
            if not content or role not in ("user", "assistant"):
                continue
            if isinstance(content, list):
                content = " ".join(
                    str(b.get("text", "")) for b in content
                    if isinstance(b, dict)
                ).strip()
            if content and not _is_compaction(content):
                parts.append(f"{role.upper()}: {_clip(content, _MAX_MESSAGE_CHARS)}")
        if parts:
            return _clip("\n\n".join(parts), _MAX_TURN_CHARS)
        # The new messages carried no usable content (tool stubs, empty roles);
        # fall back to the reported turn so nothing is lost.
        return self._turn_pair(user_content, assistant_content)

    _SYNCED_MAX = 64

    def _record_synced(self, session_id: str, count: int) -> None:
        """Remember how much of *session_id*'s thread has been synced.

        Evicts least-recently-used sessions past ``_SYNCED_MAX``. Losing an
        entry costs a re-adopt on that session's next turn, never a correctness
        failure, so a long-lived gateway cannot grow this without bound.
        """
        self._synced[session_id] = count
        self._synced.move_to_end(session_id)
        while len(self._synced) > self._SYNCED_MAX:
            self._synced.popitem(last=False)

    @staticmethod
    def _turn_pair(user_content: str, assistant_content: str) -> str:
        parts = []
        if user_content:
            parts.append(f"USER: {_clip(user_content, _MAX_MESSAGE_CHARS)}")
        if assistant_content:
            parts.append(f"ASSISTANT: {_clip(assistant_content, _MAX_MESSAGE_CHARS)}")
        return "\n\n".join(parts)

    def _store_prefetch(self, session_id: str, text: str) -> None:
        """Remember the last recall for session_id, bounded like _synced."""
        with self._prefetch_lock:
            self._prefetch_cache[session_id] = text
            self._prefetch_cache.move_to_end(session_id)
            while len(self._prefetch_cache) > self._SYNCED_MAX:
                self._prefetch_cache.popitem(last=False)

    def _format_prefetch(self, results: Dict[str, Any], query: str) -> str:
        """Format the server's search result into a prompt block: the Now page, then the channels.

        The Now page is the agent's working memory, a short rewrite of what is true
        at the moment, and it is read before every reply, so it leads the block and
        is not cut down to the channel snippet length. Entity pages come before the
        other channels because each one is the consolidated view of a topic, so they
        get more room than a single raw fact. Each item keeps its date so an old
        status still reads as old.
        """
        now = results.get("now") if isinstance(results.get("now"), dict) else {}
        now_text = str(now.get("text") or "").strip()
        memories = results.get("memories") or {}
        channels = []
        for channel_name in channel_order(memories, results.get("order")):
            items = memories.get(channel_name, [])
            if not items:
                continue
            limit = 600 if channel_name == "pages" else 300
            channel_lines = [f"[{channel_name.upper()}]"]
            for m in items[:5]:
                # The date lets the agent tell "true on Oct 6" from "true now":
                # without it an old status line reads like the current one. The
                # event date and superseded versions are added by format_hit_line.
                line = format_hit_line(m, limit)
                if line:
                    channel_lines.append(line)
            if len(channel_lines) > 1:
                channels.append("\n".join(channel_lines))
        if not now_text and not channels:
            return ""
        blocks = []
        if now_text:
            # The Now page carries its own date for the same reason channel items do:
            # the agent must be able to judge how fresh the working memory is.
            updated = str(now.get("updated") or "")[:10]
            header = f"[NOW] (updated {updated})" if updated else "[NOW]"
            blocks.append(f"{header}\n{now_text[:1500]}")
        return (
            f"<relevant-memories query=\"{query[:50]}\">\n"
            + "\n\n".join(blocks + channels)
            + "\n</relevant-memories>"
        )

    def _store_recalled(self, session_id: str, results: Dict[str, Any], query: str = "") -> None:
        """Remember the memory ids this recall returned, for session_id.

        Every channel counts, not only the lines the prompt shows, because feedback
        is about what the agent was given to read. The Now page has no memory id
        and is not a recalled memory, so it is left out. An empty recall clears the
        session's entry: ids from an earlier turn would describe a recall the agent
        never saw.
        """
        ids: List[str] = []
        seen = set()
        memories = results.get("memories") or {}
        if not isinstance(memories, dict):
            memories = {}
        for items in memories.values():
            if not isinstance(items, list):
                continue
            for m in items:
                mid = str(m.get("memory_id") or "") if isinstance(m, dict) else ""
                if mid and mid not in seen:
                    seen.add(mid)
                    ids.append(mid)
        with self._prefetch_lock:
            if ids:
                self._recalled[session_id] = ids
                self._recalled.move_to_end(session_id)
                self._recalled_query[session_id] = query
                while len(self._recalled) > self._SYNCED_MAX:
                    old, _ = self._recalled.popitem(last=False)
                    self._recalled_query.pop(old, None)
            else:
                self._recalled.pop(session_id, None)
                self._recalled_query.pop(session_id, None)

    def _send_feedback(self, session_id: str, assistant_content: str) -> None:
        """Post the reply against the memories recalled for session_id, off the turn's path.

        The server uses the reply to learn which recalled memories the agent used.
        The post runs in a daemon thread because sync_turn runs after every reply
        and feedback is advisory: it must never delay or fail the turn. Every
        failure is logged at debug and dropped.
        """
        try:
            if not assistant_content or not self._client:
                return
            with self._prefetch_lock:
                ids = self._recalled.pop(session_id, None)
                query = self._recalled_query.pop(session_id, "")
            if not ids:
                return
            client = self._client
            reply = assistant_content[:20000]
            user_id, agent_id = self._user_id, self._agent_id

            def _do() -> None:
                try:
                    client.feedback(ids, reply, user_id, agent_id, query=query)
                except Exception as e:
                    logger.debug("hyatlas feedback failed: %s", e)

            threading.Thread(target=_do, daemon=True, name="hyatlas-feedback").start()
        except Exception as e:
            logger.debug("hyatlas feedback not sent: %s", e)

    def _ensure_server_running(self) -> bool:
        """Start the Go binary through the shared launcher; True iff it answers.

        Same path as ``hermes hyatlas start``: an already-running server is
        detected rather than spawned over, and the pidfile is written only for a
        child that stayed alive.
        """
        try:
            from . import process as process_mod
        except ImportError as e:
            logger.debug("process module unavailable: %s", e)
            return False
        result = process_mod.start_server(self._config, timeout=30.0)
        if not result.get("ok"):
            logger.warning("failed to auto-start hyatlas-go: %s", result.get("error"))
            return False
        if result.get("already_running"):
            logger.info("hyatlas auto_start: %s", result.get("message"))
        return bool(result.get("reachable"))

    def _hand_over_key(self, force: bool = False) -> bool:
        """Give the server this plugin's own AI key when it is waiting for one.

        The key is the ``llm_key`` secret that setup stored in Hermes' ``.env`` as
        HYATLAS_LLM_KEY, never a Hermes provider token. A server the plugin spawns
        already receives it in its environment; a server started some other way
        (the launcher, a service) reads no credentials, so it reports
        ``llm: "awaiting_key"`` until someone hands one over. This does that, when
        and only when: the key is non-empty, the server says ``awaiting_key``, and
        the configured host is this machine. Checked at most every
        :data:`_KEY_CHECK_INTERVAL` seconds unless ``force`` is set. Returns True
        when a key was handed over. The key is never logged.
        """
        key = os.environ.get("HYATLAS_LLM_KEY", "").strip()
        if not key:
            return False
        if not settings.is_loopback_host(self._config.get("server_host") or "127.0.0.1"):
            return False
        if not self._key_check_lock.acquire(blocking=False):
            return False
        try:
            now = time.monotonic()
            if not force and self._key_checked_at and now - self._key_checked_at < _KEY_CHECK_INTERVAL:
                return False
            self._key_checked_at = now
            client = self._ensure_client()
            if (client.status() or {}).get("llm") != "awaiting_key":
                return False
            client.set_llm_key(key)
            logger.info("hyatlas handed its AI key to the local server")
            return True
        except Exception as e:  # noqa: BLE001 - best effort, and the message never holds the key
            logger.debug("hyatlas key hand-over skipped: %s", type(e).__name__)
            return False
        finally:
            self._key_check_lock.release()

    def _hand_over_key_async(self, force: bool = False) -> None:
        """Run :meth:`_hand_over_key` on a daemon thread, so loading never waits."""
        if (not force and self._key_checked_at
                and time.monotonic() - self._key_checked_at < _KEY_CHECK_INTERVAL):
            return
        try:
            threading.Thread(
                target=self._hand_over_key, args=(force,),
                name="hyatlas-keyshare", daemon=True,
            ).start()
        except Exception as e:  # noqa: BLE001
            logger.debug("hyatlas key hand-over not scheduled: %s", e)

    def _autostart_at_load(self) -> None:
        """Start the server when Hermes loads the plugin, not only when a session begins.

        After a reboot the Hermes gateway loads plugins at logon, but without this
        the server stayed down until the first conversation, so cron jobs that call
        the server directly failed in the meantime. Runs in a daemon thread so
        plugin loading never waits on it. _ensure_server_running uses the shared
        start lock and detects a running server, so two Hermes processes cannot
        start two servers.
        """
        try:
            if self._ensure_client().is_reachable():
                return
            ok = self._ensure_server_running()
            logger.info(
                "hyatlas auto_start at load: server %s",
                "reachable" if ok else "not reachable",
            )
            if ok:
                self._hand_over_key(force=True)
        except Exception as e:
            logger.debug("hyatlas auto_start at load failed: %s", e)


# =============================================================================
# Plugin registration
# =============================================================================


def register(ctx: Any) -> None:
    """Hermes plugin entry point.

    The synthetic ``ctx`` exposes ``register_memory_provider``. We
    instantiate the provider and hand it over.
    """
    provider = HyatlasMemoryProvider()
    ctx.register_memory_provider(provider)
    logger.info(
        "hyatlas v%s plugin registered (server=%s)",
        provider._version, provider._origin(),
    )
    # With auto_start, starting Hermes is enough: the server comes up at load, not
    # at the first conversation. Registration must never fail because of this.
    try:
        if settings.truthy(provider._config.get("auto_start")):
            threading.Thread(
                target=provider._autostart_at_load,
                name="hyatlas-autostart",
                daemon=True,
            ).start()
    except Exception as e:
        logger.debug("hyatlas auto_start at load not scheduled: %s", e)
    # A server this plugin did not start may be waiting for the key from setup.
    provider._hand_over_key_async(force=True)
    # Also register a slash command for diagnostics
    try:
        ctx.register_command(
            "hyatlas",
            _slash_hyatlas,
            description="HyAtlas memory server — status, search, add, recent, start, stop",
            args_hint="[status|search <q>|add <text>|recent|start|stop]",
        )
    except Exception as e:
        logger.debug("register_command failed: %s", e)


def _slash_hyatlas(raw_args: str) -> str:
    """Slash command handler for ``/hyatlas``."""
    args = (raw_args or "").strip()
    if not args:
        return "Usage: /hyatlas [status|search <q>|add <text>|recent|start|stop]"
    parts = args.split(maxsplit=1)
    cmd = parts[0].lower()
    rest = parts[1] if len(parts) > 1 else ""

    try:
        provider = HyatlasMemoryProvider()
        # start and stop manage the server, so they must run whether or not it answers.
        if cmd == "start":
            from . import process as process_mod
            return json.dumps(process_mod.start_server(provider._config))
        if cmd == "stop":
            from . import process as process_mod
            return json.dumps(process_mod.HyatlasProcess.stop_running(provider._config))
        if not provider.is_available():
            return json.dumps({
                "ok": False,
                "error": provider.unavailable_reason(),
            })
        provider.initialize(session_id="slash")
        client = provider._ensure_client()
        if cmd == "status":
            return json.dumps(client.status())
        if cmd == "search":
            results = client.search(query=rest, user_id=provider._user_id,
                                   agent_id=provider._agent_id, limit=5)
            return json.dumps(results)
        if cmd == "recent":
            items = client.list_memories(user_id=provider._user_id,
                                        agent_id=provider._agent_id, limit=10)
            return json.dumps(items)
        if cmd == "add":
            resp = client.add(text=rest, user_id=provider._user_id,
                              agent_id=provider._agent_id)
            return json.dumps(resp)
        return json.dumps({"ok": False, "error": f"unknown subcommand: {cmd}"})
    except Exception as e:
        return json.dumps({"ok": False, "error": str(e)})
    finally:
        try:
            provider.shutdown()
        except Exception:
            pass


__all__ = [
    "HyatlasMemoryProvider",
    "register",
]
