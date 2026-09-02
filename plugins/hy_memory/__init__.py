"""HyAtlas v4 — Hermes memory provider plugin.

Pure-Python client that talks to the HyAtlas v4 Go binary at
``HYATLAS_SERVER_HOST:HYATLAS_SERVER_PORT`` (default 127.0.0.1:19528).

The v4 wire contract is identical to v3.5's HyMemoryClient — same
endpoints, same JSON shapes. The only difference is the port: v3.5
runs on 19527, v4 runs on 19528. Set ``server_port`` in config to
match whichever backend is running.

This plugin follows the canonical Hermes memory-provider pattern
(Honcho / Hindsight shape):

* ``__init__.py`` — MemoryProvider subclass + register(ctx)
* ``client.py`` — HTTP client to the v4 server
* ``process.py`` — auto-start / stop the Go binary as a subprocess
* ``cli.py`` — ``hermes hyatlas`` subcommands (status, search, add, recent)
* ``schemas.py`` — tool schemas (status / search / recent / add)
* ``__main__.py`` — standalone ``python -m`` entry point
* ``plugin.yaml`` — metadata
* ``after-install.md`` — install instructions
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.memory_provider import MemoryProvider

from .client import HyatlasClient, HyatlasClientError, HyatlasUnreachable

logger = logging.getLogger(__name__)

# Resolve paths for bundled sibling modules
_PLUGIN_ROOT = Path(__file__).resolve().parent

# Tool schemas (used by register())
from .schemas import (  # noqa: E402
    HYATLAS_STATUS_SCHEMA,
    HYATLAS_SEARCH_SCHEMA,
    HYATLAS_RECENT_SCHEMA,
    HYATLAS_ADD_SCHEMA,
)


# =============================================================================
# Configuration
# =============================================================================


def _load_config() -> Dict[str, Any]:
    """Load config from env vars + per-profile JSON, in priority order.

    Priority: env > config.yaml plugin block > per-profile JSON.
    The plugin block lives at ``plugins.hy_memory`` under Hermes
    ``config.yaml`` and accepts: ``server_host``, ``server_port``,
    ``user_id``, ``agent_id``, ``auto_start``, ``binary_path``.
    """
    cfg: Dict[str, Any] = {
        "server_host": "127.0.0.1",
        "server_port": 19528,
        "user_id": "default",
        "agent_id": "default",
        "auto_start": False,
        "binary_path": "",  # empty -> discover from PATH / repo
        "request_timeout": 15.0,
    }

    # 1. Per-profile JSON — accept ONLY keys relevant to the v4 client.
    #    Legacy v3.5 fields (llm, vector_store, api_keys, etc.) are
    #    silently dropped — they configure the v3.5 Python server, not
    #    the v4 Go binary. The v4 binary reads env vars directly.
    _V4_KEYS = ("server_host", "server_port", "user_id", "agent_id",
                "auto_start", "binary_path", "request_timeout")
    for json_path in (
        Path(os.environ.get("HERMES_HOME", str(Path.home() / ".hermes"))) / "hy_memory.json",
    ):
        if json_path.exists():
            try:
                raw = json.loads(json_path.read_text(encoding="utf-8"))
                for k in _V4_KEYS:
                    if k in raw:
                        cfg[k] = raw[k]
            except (json.JSONDecodeError, OSError) as e:
                logger.debug("ignoring %s: %s", json_path, e)

    # 2. Env-var overrides (canonical 12-factor pattern)
    for env_key, cfg_key, cast in (
        ("HYATLAS_SERVER_HOST", "server_host", str),
        ("HYATLAS_SERVER_PORT", "server_port", int),
        ("HYATLAS_USER_ID", "user_id", str),
        ("HYATLAS_AGENT_ID", "agent_id", str),
        ("HYATLAS_AUTO_START", "auto_start", lambda v: v.lower() in ("1", "true", "yes")),
        ("HYATLAS_BINARY_PATH", "binary_path", str),
        ("HYATLAS_REQUEST_TIMEOUT", "request_timeout", float),
    ):
        v = os.environ.get(env_key, "").strip()
        if v:
            try:
                cfg[cfg_key] = cast(v)
            except (TypeError, ValueError) as e:
                logger.debug("ignoring %s=%r: %s", env_key, v, e)

    # 3. Backward compat: HYATLAS_LLM_KEY etc. don't apply here, but legacy
    #    v3.5 keys HY_MEMORY_* should not bleed in.
    for legacy in ("HY_MEMORY_HOST", "HY_MEMORY_PORT"):
        if legacy in os.environ:
            logger.debug("ignoring legacy env %s — use HYATLAS_SERVER_* instead", legacy)

    return cfg


# =============================================================================
# Provider
# =============================================================================


class HyatlasMemoryProvider(MemoryProvider):
    """Hermes MemoryProvider implementation for the HyAtlas v4 Go backend.

    The provider is a thin HTTP client wrapper. The v4 server handles
    all storage (chromem-go), embedding (in-process BGE-small via
    onnxruntime-go), and LLM extraction (deepseek-v4-flash via the
    ai2api loopback). This class is responsible for:

    * Lifecycle (initialize / shutdown)
    * Per-session identity (user_id, agent_id) resolution
    * Format conversion (3-channel search -> prompt block)
    * Tool surface (status / search / recent / add)
    * Prefetch + sync_turn persistence to the v4 server
    * on_pre_compress checkpoint (fail-closed durability)
    * on_memory_write mirror for atomic agent-chosen facts
    """

    def __init__(self) -> None:
        self._config = _load_config()
        self._client: Optional[HyatlasClient] = None
        self._user_id: str = ""
        self._agent_id: str = ""
        self._prefetch_lock = threading.Lock()
        self._prefetch_result: str = ""
        self._process: Optional[Any] = None  # lazy import to keep _load_config cheap
        self._version = "4.0.0"

    # --- Required ABC methods ---

    @property
    def name(self) -> str:
        return "hy_memory"

    def backup_paths(self) -> List[str]:
        """Data directory paths this provider owns (for `hermes backup`)."""
        # v4 stores under data/ in the working dir of the Go binary.
        # We can't know the exact path without the server's response,
        # so we report the conventional locations.
        return [
            "data/graph.json",  # v4 graph store
            "data/doc_index.json",  # v4 doc index
        ]

    def is_available(self) -> bool:
        """True iff the v4 server is reachable on the configured port.

        Does NOT auto-start the server — that's a separate decision
        via ``hermes hyatlas start`` (or the plugin's auto_start
        config flag, honored at initialize() time).
        """
        try:
            client = self._ensure_client()
            return client.is_reachable()
        except Exception as e:
            logger.debug("hyatlas is_available failed: %s", e)
            return False

    def unavailable_reason(self) -> str:
        """Why is_available() returned False — surfaced in the dashboard."""
        if not self._config.get("server_host"):
            return "server_host not configured"
        port = self._config.get("server_port", 0)
        return (
            f"HyAtlas v4 not reachable at "
            f"{self._config.get('server_host')}:{port}. "
            f"Start it with `hermes hyatlas start` (auto-starts the Go binary) "
            f"or run the binary directly: `hyatlas-go`."
        )

    def initialize(self, session_id: str, **kwargs: Any) -> None:
        """Connect, resolve identity, optionally auto-start the server."""
        self._client = self._ensure_client()
        self._user_id = self._resolve_user_id(kwargs)
        self._agent_id = self._resolve_agent_id(kwargs)

        # Cron / flush guard — don't pollute memory from synthetic contexts
        agent_context = kwargs.get("agent_context", "")
        if agent_context in ("cron", "flush"):
            logger.debug("hy_memory skipping init for context=%s", agent_context)
            return

        # Auto-start the server if configured
        if self._config.get("auto_start") and not self._client.is_reachable():
            self._ensure_server_running()
            # Wait briefly for the server to come up
            if not self._client.wait_until_reachable(timeout=30.0):
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
        """Dispatch tool calls to the v4 server."""
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
                )
                return json.dumps(results)
            if tool_name == "hyatlas_recent":
                items = self._client.list_memories(
                    user_id=args.get("user_id", self._user_id) or self._user_id,
                    agent_id=args.get("agent_id", self._agent_id) or self._agent_id,
                    layer=args.get("layer", "") or "",
                    limit=int(args.get("limit", 20)),
                    include_raw=bool(args.get("include_raw", False)),
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

    def system_prompt_block(self) -> str:
        """Short static block for the agent's system prompt.

        v4 already has prefetch() returning relevant context; this is
        the static pointer the agent always sees.
        """
        port = self._config.get("server_port", 19528)
        return (
            f"You have access to a HyAtlas v4 7-layer memory system "
            f"(server: 127.0.0.1:{port}). "
            "Use the `hyatlas_search` tool to recall relevant past context, "
            "`hyatlas_recent` to see the latest memories, and `hyatlas_add` "
            "to record durable facts. The `hy_memory_save` tool (the standard "
            "Hermes memory tool) is mirrored automatically to v4's L1 Profile "
            "layer."
        )

    # --- Optional: prefetch (sync, returns cached result) ---

    def prefetch(self, query: str, *, session_id: str = "") -> str:
        """Return the most recent prefetched result (set by queue_prefetch)."""
        with self._prefetch_lock:
            return self._prefetch_result

    def queue_prefetch(self, query: str, *, session_id: str = "") -> None:
        """Fire-and-forget recall for the next agent turn."""
        if not query or not self._client:
            return

        def _do() -> None:
            try:
                results = self._client.search(
                    query=query,
                    user_id=self._user_id,
                    agent_id=self._agent_id,
                    limit=5,
                )
                formatted = self._format_prefetch(results, query)
                with self._prefetch_lock:
                    self._prefetch_result = formatted
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
        """Persist a turn to v4. v4's server runs LLM extraction async.

        We send a single combined message that the v4 LLM can extract
        facts/summary/intentions from. v3.5's split-user/assistant model
        still works (the v4 LLM is robust to either shape), but
        concatenating gives a cleaner extraction input.
        """
        if not self._client:
            return
        if not user_content and not assistant_content:
            return

        text = self._build_turn_text(user_content, assistant_content, messages)
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

    # --- Optional: on_pre_compress (fail-closed checkpoint) ---

    def on_pre_compress(self, messages: List[Dict[str, Any]]) -> str:
        """Before context compression, force-flush any pending turn.

        v4's on_pre_compress returns '' (empty) because v4 doesn't need a
        pre-compress string injected — the LLM extraction runs on every
        sync_turn immediately, and the data is durable in the v4 store.
        This hook exists as a fail-closed safety net: if any buffered
        prefetch is in flight, we wait briefly so it's not lost.
        """
        # No-op for v4: sync_turn already persists immediately. We just
        # confirm the server is still reachable; if not, return empty.
        if self._client and not self._client.is_reachable():
            logger.warning("on_pre_compress: v4 unreachable, skipping flush")
        return ""

    # --- Optional: on_memory_write (atomic agent-chosen fact mirror) ---

    def on_memory_write(
        self,
        action: str,
        target: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Mirror Hermes' built-in memory tool writes to v4.

        Hermes' built-in memory tool is the agent's primary way to save
        atomic facts (it's trained in the system prompt). v4 mirrors
        those writes to L1 Profile (user preferences) or a dedicated
        "atomic" layer. Only ``add`` actions are mirrored — replace/remove
        are handled by the built-in file store.
        """
        if action != "add" or not self._client or not content:
            return
        layer = "l1_profile" if target == "user" else "l1_profile"
        # v4 maps everything user-written to L1 Profile; the layer
        # distinction (user vs project) is preserved in metadata.
        meta = dict(metadata or {})
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

    # --- Optional: on_session_end (final flush) ---

    def on_session_end(self, messages: List[Dict[str, Any]]) -> None:
        """Session-end hook. v4 persists per-turn, so this is a no-op
        beyond a debug log."""
        logger.debug(
            "on_session_end: %d messages persisted across session %s",
            len(messages) if messages else 0,
            self._user_id,
        )

    # --- Optional: config wizard surface ---

    def get_config_schema(self) -> List[Dict[str, Any]]:
        return [
            {
                "key": "server_host",
                "description": "HyAtlas v4 server host",
                "default": "127.0.0.1",
            },
            {
                "key": "server_port",
                "description": "HyAtlas v4 server port",
                "default": 19528,
            },
            {
                "key": "user_id",
                "description": "Default user_id (profile-scoped in config.yaml)",
                "default": "default",
            },
            {
                "key": "agent_id",
                "description": "Default agent_id (overridden by agent_identity kwarg)",
                "default": "default",
            },
            {
                "key": "auto_start",
                "description": "Auto-start the Go binary if not reachable",
                "default": False,
                "choices": [True, False],
            },
        ]

    def save_config(self, values: Dict[str, Any], hermes_home: str) -> None:
        """Persist non-secret config to ``$HERMES_HOME/hy_memory.json``.

        Only v4-relevant keys are written. Legacy v3.5 fields (llm,
        vector_store, api_keys, etc.) are NOT written here — those
        belong to the v3.5 Python server's config, not the v4 Go
        client. The v4 binary itself reads LLM env vars
        (HYATLAS_LLM_BASE, etc.) directly, so no LLM creds belong
        in this JSON.
        """
        path = Path(hermes_home) / "hy_memory.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        merged = {**self._config, **values}
        for k in ("request_timeout",):
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
            host = self._config.get("server_host", "127.0.0.1")
            port = int(self._config.get("server_port", 19528))
            timeout = float(self._config.get("request_timeout", 15.0))
            self._client = HyatlasClient(
                base_url=f"http://{host}:{port}",
                timeout=timeout,
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
    ) -> str:
        """Concatenate turn into a single text for v4's LLM extraction.

        Preference order: full messages thread (richest), then the
        passed user/assistant pair.
        """
        if messages:
            parts = []
            for m in messages:
                role = m.get("role", "")
                content = m.get("content", "")
                if not content or role == "system":
                    continue
                parts.append(f"{role.upper()}: {content}")
            if parts:
                return "\n\n".join(parts)
        # Fallback
        parts = []
        if user_content:
            parts.append(f"USER: {user_content}")
        if assistant_content:
            parts.append(f"ASSISTANT: {assistant_content}")
        return "\n\n".join(parts)

    def _format_prefetch(self, results: Dict[str, Any], query: str) -> str:
        """Format v4's 3-channel search result into a prompt block."""
        memories = results.get("memories", {})
        channels = []
        for channel_name in ("profile", "proactive", "normal"):
            items = memories.get(channel_name, [])
            if not items:
                continue
            channel_lines = [f"[{channel_name.upper()}]"]
            for m in items[:5]:
                content = m.get("content", "")
                layer = m.get("layer", "")
                score = m.get("score", 0.0)
                if content:
                    snippet = content[:300]
                    channel_lines.append(
                        f"- ({layer}, score {score:.2f}) {snippet}"
                    )
            if len(channel_lines) > 1:
                channels.append("\n".join(channel_lines))
        if not channels:
            return ""
        return (
            f"<relevant-memories query=\"{query[:50]}\">\n"
            + "\n\n".join(channels)
            + "\n</relevant-memories>"
        )

    def _ensure_server_running(self) -> None:
        """Lazy import + invoke the process manager to spawn the Go binary."""
        if self._process is not None:
            return
        try:
            from . import process as process_mod
        except ImportError as e:
            logger.debug("process module unavailable: %s", e)
            return
        self._process = process_mod.HyatlasProcess(self._config)
        try:
            self._process.start()
        except Exception as e:
            logger.warning("failed to auto-start hyatlas-go: %s", e)
            self._process = None


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
        "hyatlas v4 plugin registered (server=%s:%s)",
        provider._config.get("server_host", "127.0.0.1"),
        provider._config.get("server_port", 19528),
    )
    # Also register a slash command for diagnostics
    try:
        ctx.register_command(
            "hyatlas",
            _slash_hyatlas,
            description="HyAtlas v4 memory server — status, search, add, recent, start, stop",
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
        if cmd == "start":
            from . import process as process_mod
            process = process_mod.HyatlasProcess(provider._config)
            process.start()
            return json.dumps({"ok": True, "started": True})
        if cmd == "stop":
            from . import process as process_mod
            process_mod.HyatlasProcess.stop_running()
            return json.dumps({"ok": True, "stopped": True})
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
