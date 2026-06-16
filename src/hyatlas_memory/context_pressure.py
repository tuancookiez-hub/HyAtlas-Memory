"""
context_pressure.py — 4-tier token pressure monitor for hy-memory.

PURPOSE
=======
When a session accumulates tool calls with large outputs, the conversation
history grows toward the context window. This module monitors that growth
and triggers tiered compression, mirroring TencentDB Agent Memory's L3
compressor (compressor.ts:resolveLevel) but as a stand-alone plugin-layer
component that bolts onto hy-memory without SDK changes.

FOUR TIERS
==========
  fastpath    (< 50% usage)  no action
  mild        (>= 50%)        replace tool result content with summary + node_id;
                              full content written to refs/<session>/<node_id>.md
  aggressive  (>= 85%)        delete old tool result messages, target 80% usage
  emergency   (>= 95%)        more aggressive deletion including user/assistant
                              pairs, target 90% usage, retain >= 2 recent

DESIGN CONSTRAINTS
==================
- Stand-alone: no hy-memory core changes. Lives in plugin layer, survives
  `pip install --upgrade hy-memory`. Integration point is a single hook
  function call before the next system_prompt_block.
- Profile-scoped: paths namespaced per Hermes profile to prevent cross-profile
  leaks. Default profile uses ~/.hermes/refs/; named profiles use
  ~/.hermes/profiles/<name>/refs/.
- Async-safe: all I/O is atomic (write-temp + rename). RefsStore is safe
  for concurrent access from multiple agent processes.
- Bounded: PressureConfig caps max chars, TTL, sweep interval. No unbounded
  disk growth even with no manual cleanup.
- No LLM dependency for Phase 1. Truncation-based summary is the default.
  LLM summary is Phase 3 (deferred; this draft only includes truncation).

INTEGRATION
===========
The main hy-memory plugin calls `make_hy_memory_hook(profile, session_id)`
once at agent init, then invokes the returned hook between turns (after
sync_turn, before the next system_prompt_block). See DESIGN_NOTES at the
end of this file for the integration sketch and what the main agent
should evaluate.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Callable


# ---------------------------------------------------------------------------
# Token counting
# ---------------------------------------------------------------------------

try:
    import tiktoken

    _ENCODER = None

    def _encoder():
        global _ENCODER
        if _ENCODER is None:
            _ENCODER = tiktoken.get_encoding("o200k_base")
        return _ENCODER

    def count_tokens(text: str) -> int:
        if not text:
            return 0
        return len(_encoder().encode(text))

except ImportError:  # graceful fallback if tiktoken is not installed
    def count_tokens(text: str) -> int:
        return (len(text) + 3) // 4 if text else 0


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class PressureConfig:
    """Tunables. Env vars override defaults at construction time."""

    # Tier thresholds (fraction of context_window used)
    mild_ratio: float = 0.50
    aggressive_ratio: float = 0.85
    emergency_ratio: float = 0.95

    # Compression targets within each tier
    aggressive_target_ratio: float = 0.80
    emergency_target_ratio: float = 0.90

    # Offload floor: only compress tool results larger than this (chars)
    min_offload_chars: int = 2000

    # Truncation size for the in-context summary (chars)
    summary_max_chars: int = 200

    # Safety: never compress the most recent N tool results
    min_retain_recent: int = 5

    # Refs lifecycle
    refs_ttl_days: int = 30
    sweep_interval_secs: int = 21600  # 6 hours


class PressureTier(str, Enum):
    FASTPATH = "fastpath"
    MILD = "mild"
    AGGRESSIVE = "aggressive"
    EMERGENCY = "emergency"


def resolve_tier(ratio: float, cfg: PressureConfig) -> PressureTier:
    """Map a usage ratio to a tier. Mirrors TencentDB compressor.ts:resolveLevel."""
    if ratio >= cfg.emergency_ratio:
        return PressureTier.EMERGENCY
    if ratio >= cfg.aggressive_ratio:
        return PressureTier.AGGRESSIVE
    if ratio >= cfg.mild_ratio:
        return PressureTier.MILD
    return PressureTier.FASTPATH


# ---------------------------------------------------------------------------
# Refs store: file-system offload for full tool result text
# ---------------------------------------------------------------------------

class RefsStore:
    """Stores full tool result text on disk; in-context message keeps only summary.

    Path layout:
        ~/.hermes/refs/<session_id>/<node_id>.md     (default profile)
        ~/.hermes/profiles/<name>/refs/<session_id>/<node_id>.md  (named profile)

    Each .md file has YAML-ish frontmatter (parsed as JSON for portability)
    with metadata, then the raw content body.

    index.jsonl is a per-session list of (node_id, summary, tool_name,
    tool_call_id, ts) used for the Phase 2 task log and for sweep bookkeeping.
    """

    FRONT_DELIM = "---"

    def __init__(self, profile: str, session_id: str, root: Path | None = None):
        self.profile = profile
        self.session_id = session_id
        if root is None:
            hermes_home = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
            if profile and profile != "default":
                self.root = hermes_home / "profiles" / profile / "refs"
            else:
                self.root = hermes_home / "refs"
        else:
            self.root = root
        self.session_dir = self.root / session_id
        self.session_dir.mkdir(parents=True, exist_ok=True)
        self._index_path = self.session_dir / "index.jsonl"

    def _new_node_id(self) -> str:
        return f"ref_{int(time.time() * 1000)}_{uuid.uuid4().hex[:8]}"

    def write(
        self,
        tool_call_id: str,
        tool_name: str,
        content: str,
        summary: str,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """Write a ref atomically. Returns the node_id."""
        node_id = self._new_node_id()
        frontmatter = {
            "node_id": node_id,
            "tool_call_id": tool_call_id,
            "tool_name": tool_name,
            "timestamp_ms": int(time.time() * 1000),
            "summary": summary,
            "metadata": metadata or {},
        }

        ref_path = self.session_dir / f"{node_id}.md"
        with tempfile.NamedTemporaryFile(
            mode="w",
            dir=self.session_dir,
            delete=False,
            prefix=".tmp_ref_",
            suffix=".md",
            encoding="utf-8",
        ) as f:
            tmp_path = Path(f.name)
            f.write(f"{self.FRONT_DELIM}\n")
            f.write(json.dumps(frontmatter, ensure_ascii=False, indent=2))
            f.write(f"\n{self.FRONT_DELIM}\n")
            f.write(content)
        tmp_path.rename(ref_path)

        with self._index_path.open("a", encoding="utf-8") as f:
            f.write(json.dumps({
                "node_id": node_id,
                "summary": summary,
                "tool_name": tool_name,
                "tool_call_id": tool_call_id,
                "ts": frontmatter["timestamp_ms"],
            }, ensure_ascii=False) + "\n")

        return node_id

    def read(self, node_id: str) -> str | None:
        """Read full content. Strips frontmatter. Returns None if not found."""
        ref_path = self.session_dir / f"{node_id}.md"
        if not ref_path.exists():
            return None
        text = ref_path.read_text(encoding="utf-8")
        if text.startswith(f"{self.FRONT_DELIM}\n"):
            end = text.find(f"\n{self.FRONT_DELIM}\n", 4)
            if end > 0:
                return text[end + len(f"\n{self.FRONT_DELIM}\n"):]
        return text

    def read_with_metadata(self, node_id: str) -> dict | None:
        """Read ref with its frontmatter. Returns None if not found."""
        ref_path = self.session_dir / f"{node_id}.md"
        if not ref_path.exists():
            return None
        text = ref_path.read_text(encoding="utf-8")
        if not text.startswith(f"{self.FRONT_DELIM}\n"):
            return {"content": text, "frontmatter": {}}
        end = text.find(f"\n{self.FRONT_DELIM}\n", 4)
        if end < 0:
            return {"content": text, "frontmatter": {}}
        try:
            frontmatter = json.loads(text[4:end])
        except json.JSONDecodeError:
            frontmatter = {}
        content = text[end + len(f"\n{self.FRONT_DELIM}\n"):]
        return {"content": content, "frontmatter": frontmatter}

    def list(self, limit: int | None = None) -> list[dict]:
        """List refs in this session, most recent first."""
        if not self._index_path.exists():
            return []
        entries: list[dict] = []
        with self._index_path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entries.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        entries.reverse()
        return entries[:limit] if limit else entries

    def sweep(self, ttl_days: int) -> int:
        """Delete refs older than ttl_days. Rebuilds index. Returns count deleted."""
        if not self.session_dir.exists():
            return 0
        cutoff_ms = int((time.time() - ttl_days * 86400) * 1000)
        deleted: list[Path] = []
        for ref_path in self.session_dir.glob("ref_*.md"):
            try:
                ts_str = ref_path.stem.split("_")[1]
                if int(ts_str) < cutoff_ms:
                    deleted.append(ref_path)
            except (ValueError, IndexError):
                continue
        for p in deleted:
            try:
                p.unlink()
            except OSError:
                pass
        if deleted:
            self._rebuild_index()
        return len(deleted)

    def _rebuild_index(self) -> None:
        """Rebuild index.jsonl from remaining ref files on disk."""
        entries: list[dict] = []
        for ref_path in self.session_dir.glob("ref_*.md"):
            try:
                text = ref_path.read_text(encoding="utf-8")
                if not text.startswith(f"{self.FRONT_DELIM}\n"):
                    continue
                end = text.find(f"\n{self.FRONT_DELIM}\n", 4)
                if end < 0:
                    continue
                front = json.loads(text[4:end])
                entries.append({
                    "node_id": front["node_id"],
                    "summary": front.get("summary", ""),
                    "tool_name": front.get("tool_name", ""),
                    "tool_call_id": front.get("tool_call_id", ""),
                    "ts": front.get("timestamp_ms", 0),
                })
            except (json.JSONDecodeError, OSError, KeyError):
                continue
        with self._index_path.open("w", encoding="utf-8") as f:
            for entry in entries:
                f.write(json.dumps(entry, ensure_ascii=False) + "\n")


# ---------------------------------------------------------------------------
# Compression helpers
# ---------------------------------------------------------------------------

_TOOL_RESULT_ID_KEYS = ("tool_call_id", "tool_use_id")


def _is_tool_result(msg: Any) -> bool:
    """True if `msg` is a tool result message (OpenAI or Anthropic shape)."""
    if not isinstance(msg, dict):
        return False
    if msg.get("role") != "tool":
        return False
    return any(k in msg for k in _TOOL_RESULT_ID_KEYS)


def _tool_call_id(msg: dict) -> str:
    return msg.get("tool_call_id") or msg.get("tool_use_id") or "unknown"


def _tool_name(msg: dict) -> str:
    return msg.get("name") or msg.get("tool_name") or "tool"


def _content_to_str(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        # Anthropic content blocks: [{type: text, text: ...}, ...]
        return "\n".join(
            b.get("text", "") for b in content if isinstance(b, dict)
        )
    return str(content) if content is not None else ""


def _message_chars(msg: dict) -> int:
    """Approximate LLM-visible chars for a message."""
    return len(_content_to_str(msg.get("content", "")))


def _truncate_summary(content: str, max_chars: int) -> str:
    if len(content) <= max_chars:
        return content
    return content[:max_chars] + "…"


def _replace_with_summary(msg: dict, summary: str, node_id: str) -> dict:
    """Build a new message dict with content replaced by summary + node pointer."""
    note = f"\n\n[Offloaded to {node_id} — use hy_memory_ref_read to view full]"
    new_content = summary + note
    new = dict(msg)
    existing = new.get("content")
    if isinstance(existing, list):
        new["content"] = [{"type": "text", "text": new_content}]
    else:
        new["content"] = new_content
    return new


# ---------------------------------------------------------------------------
# Compression report
# ---------------------------------------------------------------------------

@dataclass
class PressureReport:
    """Result of a single check() pass."""
    tier: PressureTier
    ratio_before: float
    ratio_after: float
    chars_saved: int
    refs_written: list[str] = field(default_factory=list)
    messages_modified: int = 0
    messages_deleted: int = 0
    elapsed_ms: int = 0

    def to_dict(self) -> dict:
        return {
            "tier": self.tier.value,
            "ratio_before": round(self.ratio_before, 4),
            "ratio_after": round(self.ratio_after, 4),
            "chars_saved": self.chars_saved,
            "refs_written": self.refs_written,
            "messages_modified": self.messages_modified,
            "messages_deleted": self.messages_deleted,
            "elapsed_ms": self.elapsed_ms,
        }


# ---------------------------------------------------------------------------
# The main monitor
# ---------------------------------------------------------------------------

class ContextPressureMonitor:
    """Check context usage and run the appropriate compression tier.

    Pass `messages` (the conversation history) and `context_window`
    (total tokens). The monitor mutates `messages` in place by
    replacing or deleting tool result entries.
    """

    def __init__(
        self,
        cfg: PressureConfig,
        refs: RefsStore,
        on_compression: Callable[[PressureReport], None] | None = None,
    ):
        self.cfg = cfg
        self.refs = refs
        self.on_compression = on_compression
        self._last_sweep_at: float = 0.0

    @classmethod
    def from_env(
        cls,
        profile: str,
        session_id: str,
        refs_root: Path | None = None,
        on_compression: Callable[[PressureReport], None] | None = None,
    ) -> "ContextPressureMonitor":
        cfg = PressureConfig(
            mild_ratio=float(os.environ.get("HERMES_CTX_MILD_RATIO", "0.50")),
            aggressive_ratio=float(os.environ.get("HERMES_CTX_AGGRESSIVE_RATIO", "0.85")),
            emergency_ratio=float(os.environ.get("HERMES_CTX_EMERGENCY_RATIO", "0.95")),
            aggressive_target_ratio=float(os.environ.get("HERMES_CTX_AGGRESSIVE_TARGET", "0.80")),
            emergency_target_ratio=float(os.environ.get("HERMES_CTX_EMERGENCY_TARGET", "0.90")),
            min_offload_chars=int(os.environ.get("HERMES_CTX_MIN_OFFLOAD_CHARS", "2000")),
            summary_max_chars=int(os.environ.get("HERMES_CTX_SUMMARY_CHARS", "200")),
            min_retain_recent=int(os.environ.get("HERMES_CTX_MIN_RETAIN_RECENT", "5")),
            refs_ttl_days=int(os.environ.get("HERMES_CTX_REFS_TTL_DAYS", "30")),
            sweep_interval_secs=int(os.environ.get("HERMES_CTX_SWEEP_SECS", "21600")),
        )
        refs = RefsStore(profile=profile, session_id=session_id, root=refs_root)
        return cls(cfg, refs, on_compression=on_compression)

    def check(self, messages: list[dict], context_window: int) -> PressureReport:
        """Run one pressure check + compression pass. Mutates `messages` in place."""
        start = time.time()

        total_chars_before = sum(_message_chars(m) for m in messages)
        # Rough token estimate: ~4 chars per token for natural text.
        # (Avoids the cost of tiktoken-encoding the full content per check.)
        total_tokens_est = (total_chars_before + 3) // 4
        ratio = (total_tokens_est / context_window) if context_window > 0 else 0.0
        tier = resolve_tier(ratio, self.cfg)

        report = PressureReport(
            tier=tier,
            ratio_before=ratio,
            ratio_after=ratio,
            chars_saved=0,
        )

        if tier == PressureTier.FASTPATH:
            self._maybe_sweep()
            report.elapsed_ms = int((time.time() - start) * 1000)
            if self.on_compression:
                self.on_compression(report)
            return report

        # Each tier runs the prior tier first, then its own logic.
        # This means MILD only does mild, AGGRESSIVE does mild + aggressive, etc.
        self._mild(messages, report)
        if tier in (PressureTier.AGGRESSIVE, PressureTier.EMERGENCY):
            self._aggressive(messages, report, context_window)
            if tier == PressureTier.EMERGENCY:
                self._emergency(messages, report, context_window)

        # Recompute final ratio
        final_chars = sum(_message_chars(m) for m in messages)
        final_tokens = (final_chars + 3) // 4
        report.ratio_after = (final_tokens / context_window) if context_window > 0 else 0.0
        report.chars_saved = max(0, total_chars_before - final_chars)
        report.elapsed_ms = int((time.time() - start) * 1000)

        self._maybe_sweep()

        if self.on_compression:
            self.on_compression(report)

        return report

    # --- tier implementations ---

    def _mild(self, messages: list[dict], report: PressureReport) -> None:
        """Replace tool result content with summary + node_id. Skip recent N."""
        tool_indices = [i for i, m in enumerate(messages) if _is_tool_result(m)]
        if len(tool_indices) <= self.cfg.min_retain_recent:
            return

        # Process oldest first; protect the most recent N
        candidates = tool_indices[: -self.cfg.min_retain_recent]

        for i in candidates:
            msg = messages[i]
            content_str = _content_to_str(msg.get("content"))
            if len(content_str) < self.cfg.min_offload_chars:
                continue

            summary = _truncate_summary(content_str, self.cfg.summary_max_chars)
            node_id = self.refs.write(
                tool_call_id=_tool_call_id(msg),
                tool_name=_tool_name(msg),
                content=content_str,
                summary=summary,
            )
            messages[i] = _replace_with_summary(msg, summary, node_id)
            report.refs_written.append(node_id)
            report.messages_modified += 1

    def _aggressive(self, messages: list[dict], report: PressureReport, context_window: int) -> None:
        """Delete oldest tool results until under aggressive_target_ratio."""
        target_chars = int(self.cfg.aggressive_target_ratio * context_window * 4)
        current_chars = sum(_message_chars(m) for m in messages)
        if current_chars <= target_chars:
            return

        tool_indices = [i for i, m in enumerate(messages) if _is_tool_result(m)]
        if len(tool_indices) <= self.cfg.min_retain_recent:
            return

        deletable = tool_indices[: -self.cfg.min_retain_recent]
        for i in deletable:
            if current_chars <= target_chars:
                break
            current_chars -= _message_chars(messages[i])
            messages[i] = None
            report.messages_deleted += 1

        # Compact (preserves order)
        messages[:] = [m for m in messages if m is not None]

    def _emergency(self, messages: list[dict], report: PressureReport, context_window: int) -> None:
        """Aggressive tool-result deletion + oldest user/assistant pairs if still over target."""
        target_chars = int(self.cfg.emergency_target_ratio * context_window * 4)

        # More aggressive tool-result deletion than _aggressive
        tool_indices = [i for i, m in enumerate(messages) if _is_tool_result(m)]
        retain = max(2, self.cfg.min_retain_recent - 3)
        if len(tool_indices) > retain:
            deletable = tool_indices[: -retain]
            for i in deletable:
                messages[i] = None
            report.messages_deleted += len(deletable)
        messages[:] = [m for m in messages if m is not None]

        # If still over target, drop oldest user/assistant pairs (not tools)
        current_chars = sum(_message_chars(m) for m in messages)
        if current_chars <= target_chars:
            return

        i = 0
        while i < len(messages) - self.cfg.min_retain_recent and current_chars > target_chars:
            msg = messages[i]
            if msg.get("role") in ("user", "assistant"):
                current_chars -= _message_chars(msg)
                messages[i] = None
                report.messages_deleted += 1
            i += 1
        messages[:] = [m for m in messages if m is not None]

    def _maybe_sweep(self) -> None:
        """Best-effort TTL sweep. Throttled by sweep_interval_secs."""
        now = time.time()
        if now - self._last_sweep_at < self.cfg.sweep_interval_secs:
            return
        try:
            self.refs.sweep(self.cfg.refs_ttl_days)
        except OSError:
            pass
        self._last_sweep_at = now


# ---------------------------------------------------------------------------
# Integration helper for hy-memory
# ---------------------------------------------------------------------------

def make_hy_memory_hook(
    profile: str,
    session_id: str,
    refs_root: Path | None = None,
    on_compression: Callable[[PressureReport], None] | None = None,
):
    """Return a hook function for hy-memory's pre-prompt flow.

    Wire-up in the plugin (sketch — actual code goes in the plugin's
    __init__.py or a dedicated hook file):

        from .context_pressure import make_hy_memory_hook
        self._pressure_hook = make_hy_memory_hook(
            profile=get_active_profile_name(),
            session_id=session_id,
            on_compression=lambda r: log.info(f"context_pressure: {r.to_dict()}"),
        )

    Call site (between turns, before the next system_prompt_block):

        report = self._pressure_hook(messages, context_window=200_000)
        if report and report.tier != PressureTier.FASTPATH:
            # compression happened; the system prompt will use the
            # trimmed messages list on the next build
            pass
    """
    monitor = ContextPressureMonitor.from_env(
        profile=profile,
        session_id=session_id,
        refs_root=refs_root,
        on_compression=on_compression,
    )

    def hook(messages: list[dict], context_window: int = 200_000) -> PressureReport | None:
        # Skip entirely if there are no tool results to compress
        if not any(_is_tool_result(m) for m in messages):
            return None
        return monitor.check(messages, context_window=context_window)

    return hook


# ---------------------------------------------------------------------------
# Tool surface for the agent to drill back into offloaded refs
# ---------------------------------------------------------------------------

def make_hy_memory_ref_read_tool(
    profile: str,
    session_id: str,
    refs_root: Path | None = None,
):
    """Build the `hy_memory_ref_read` tool spec for the agent.

    Returns a dict with name/description/parameters. Wire into the
    plugin's get_tool_schemas() so the LLM can call it.

    The tool reads a ref's full content by node_id. Use this when a
    recalled summary (from auto-recall) is insufficient and the agent
    needs the original tool output.
    """
    refs = RefsStore(profile=profile, session_id=session_id, root=refs_root)

    def handler(node_id: str) -> str:
        result = refs.read_with_metadata(node_id)
        if result is None:
            return f"ref not found: {node_id}"
        front = result.get("frontmatter", {})
        meta = (
            f"tool: {front.get('tool_name', '?')}\n"
            f"tool_call_id: {front.get('tool_call_id', '?')}\n"
            f"summary: {front.get('summary', '?')}\n"
            f"---\n"
        )
        return meta + result["content"]

    return {
        "name": "hy_memory_ref_read",
        "description": (
            "Read the full content of an offloaded tool result by its node_id. "
            "Use this when a recalled summary (from auto-recall or shown in the "
            "Available tool result refs log) is insufficient and you need the "
            "original tool output verbatim. Returns the full text plus metadata "
            "(tool name, call id, summary)."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "node_id": {
                    "type": "string",
                    "description": "The ref node_id (e.g. 'ref_1700000000000_a1b2c3d4').",
                },
            },
            "required": ["node_id"],
        },
        "handler": handler,
    }


# ---------------------------------------------------------------------------
# Smoke test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)
        store = RefsStore("test", "session-1", root=root)

        # 1. Write + read
        node_id = store.write(
            tool_call_id="call_abc",
            tool_name="file_read",
            content="x" * 5000,
            summary="readme file (first 200 chars)",
        )
        assert store.read(node_id) == "x" * 5000, "read should return full content"
        meta = store.read_with_metadata(node_id)
        assert meta["frontmatter"]["tool_name"] == "file_read"
        print(f"[1] write+read OK (node_id={node_id})")

        # 2. List
        entries = store.list()
        assert len(entries) == 1
        assert entries[0]["summary"] == "readme file (first 200 chars)"
        print(f"[2] list OK ({len(entries)} entry)")

        # 3. Sweep (force old timestamp)
        ref_path = root / "session-1" / f"{node_id}.md"
        old_ts = int((time.time() - 100 * 86400) * 1000)  # 100 days ago
        new_name = ref_path.parent / f"ref_{old_ts}_oldref00.md"
        ref_path.rename(new_name)
        deleted = store.sweep(ttl_days=30)
        assert deleted == 1
        assert not new_name.exists()
        print(f"[3] sweep OK (deleted {deleted} stale ref)")

        # 4. Tier resolution
        cfg = PressureConfig()
        assert resolve_tier(0.3, cfg) == PressureTier.FASTPATH
        assert resolve_tier(0.6, cfg) == PressureTier.MILD
        assert resolve_tier(0.9, cfg) == PressureTier.AGGRESSIVE
        assert resolve_tier(0.97, cfg) == PressureTier.EMERGENCY
        print("[4] tier resolution OK")

        # 5. Mild compression on small context window
        realistic = "the quick brown fox jumps over the lazy dog. " * 100  # ~4500 chars
        messages = [
            {"role": "user", "content": "read the auth.py file"},
            {"role": "tool", "tool_call_id": "call_1", "name": "file_read", "content": realistic},
            {"role": "tool", "tool_call_id": "call_2", "name": "file_read", "content": realistic},
            {"role": "tool", "tool_call_id": "call_3", "name": "file_read", "content": realistic},
            {"role": "tool", "tool_call_id": "call_4", "name": "file_read", "content": realistic},
            {"role": "tool", "tool_call_id": "call_5", "name": "file_read", "content": realistic},
            {"role": "tool", "tool_call_id": "call_6", "name": "file_read", "content": realistic},
            {"role": "tool", "tool_call_id": "call_7", "name": "file_read", "content": realistic},
            {"role": "assistant", "content": "I read the file."},
        ]
        store2 = RefsStore("test", "session-2", root=root)
        # Use a smaller min_retain_recent for the test
        cfg2 = PressureConfig(min_retain_recent=2)
        monitor = ContextPressureMonitor(cfg2, store2)
        # 7 tool results × 4500 chars ≈ 31.5K chars ≈ 7900 tokens; window 12K → ratio 0.66 → MILD
        report = monitor.check(messages, context_window=12_000)
        assert report.tier == PressureTier.MILD, f"got {report.tier.value}"
        # 7 tool results minus 2 retained = 5 should be offloaded
        assert report.messages_modified == 5, f"got {report.messages_modified}"
        assert len(report.refs_written) == 5
        # Refs should be readable
        for nid in report.refs_written:
            content = store2.read(nid)
            assert content is not None
        # Most recent 2 tool results should NOT be offloaded
        for i in (-1, -2):
            msg = messages[i]
            assert "[Offloaded" not in str(msg.get("content", "")), f"recent tool result was offloaded: {msg}"
        print(f"[5] mild compression OK (chars_saved={report.chars_saved}, refs={len(report.refs_written)})")

        # 6. Aggressive compression
        messages = [
            {"role": "user", "content": "do many things"},
        ] + [
            {"role": "tool", "tool_call_id": f"call_{i}", "name": "file_read", "content": "x" * 4000}
            for i in range(20)
        ] + [{"role": "assistant", "content": "done"}]
        store3 = RefsStore("test", "session-3", root=root)
        monitor3 = ContextPressureMonitor(cfg, store3)
        # 80K+ chars ≈ 20K+ tokens / 5K context = 4.0+ ratio → EMERGENCY
        report = monitor3.check(messages, context_window=5000)
        assert report.tier in (PressureTier.AGGRESSIVE, PressureTier.EMERGENCY)
        assert report.messages_deleted > 0
        print(f"[6] aggressive/emergency compression OK (tier={report.tier.value}, deleted={report.messages_deleted})")

        # 7. Fastpath on low usage
        messages = [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ]
        store4 = RefsStore("test", "session-4", root=root)
        monitor4 = ContextPressureMonitor(cfg, store4)
        report = monitor4.check(messages, context_window=200_000)
        assert report.tier == PressureTier.FASTPATH
        assert report.messages_modified == 0
        print("[7] fastpath OK")

        # 8. Integration hook
        hook = make_hy_memory_hook("test", "session-hook", refs_root=root)
        realistic = "the quick brown fox jumps over the lazy dog. " * 100
        messages = [
            {"role": "user", "content": "test"},
            {"role": "tool", "tool_call_id": "c1", "name": "exec", "content": realistic},
            {"role": "tool", "tool_call_id": "c2", "name": "exec", "content": realistic},
            {"role": "tool", "tool_call_id": "c3", "name": "exec", "content": realistic},
        ]
        # 3 tool results × 4500 chars ≈ 3400 tokens; window 5500 → ratio 0.62 → MILD
        # (default min_retain_recent=5, so we need >= 6 to actually offload any;
        # use a tighter config via env)
        import os
        os.environ["HERMES_CTX_MIN_RETAIN_RECENT"] = "1"
        os.environ["HERMES_CTX_MILD_RATIO"] = "0.5"
        hook = make_hy_memory_hook("test", "session-hook", refs_root=root)
        report = hook(messages, context_window=5_500)
        assert report is not None, "hook should return a report when tool results present"
        assert report.tier == PressureTier.MILD
        print(f"[8] integration hook OK (refs written: {len(report.refs_written)})")

        # 9. Ref-read tool returns full content
        tool = make_hy_memory_ref_read_tool("test", "session-hook", refs_root=root)
        nid = report.refs_written[0]
        out = tool["handler"](nid)
        assert "the quick brown fox" in out
        assert "tool: exec" in out
        # Test missing ref
        assert "ref not found" in tool["handler"]("nonexistent_node_id")
        print(f"[9] ref-read tool OK")

        print("\nAll smoke tests passed.")
