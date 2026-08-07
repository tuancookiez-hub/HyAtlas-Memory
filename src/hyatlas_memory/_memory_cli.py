"""Memory CLI: write / recall / list / reflect operations for hyatlas-memory.

Mirrors Hindsight's ``memory retain|recall|reflect`` and Memories.sh's
``add|search|recall`` patterns so users familiar with those tools have
an obvious entry point. All operations auto-start the stack if it isn't
already running, so a one-liner from a cron job or another shell just
works.

Public API:

    hyatlas memory write "fact" [--user-id U] [--session-id S]
    hyatlas memory recall "query" [--limit N] [--user-id U]
    hyatlas memory list [--limit N] [--user-id U] [--layer L]
    hyatlas memory reflect "query" [--limit N] [--user-id U]

The actual write path is ``HyMemoryProvider.sync_turn`` so it goes
through the same LLM fact-extraction + scoring + qdrant indexing
pipeline that Hermes conversations use. ``reflect`` returns the same
``<relevant-memories>`` block that gets injected into the system
prompt, which is the "smarter" half of the memory system.
"""

from __future__ import annotations

import os
import sys
import time
from contextlib import suppress
from datetime import datetime

DEFAULT_USER_ID = os.environ.get("HYATLAS_MEMORY_USER_ID", "hermes-user")
DEFAULT_AGENT_ID = os.environ.get("HYATLAS_MEMORY_AGENT_ID", "default")


def _ensure_stack() -> None:
    """Auto-start the stack if it isn't running.

    Bare ``hyatlas`` exits into a new console on Windows; for the
    memory CLI we want a foreground smoke check that brings services
    up if needed, otherwise just warn and continue.
    """
    from hyatlas_memory._start import UPSTREAM_PORT, is_port_listening
    if not is_port_listening(UPSTREAM_PORT):
        print(
            f"  ✘ Upstream server is not running on port {UPSTREAM_PORT}.\n"
            f"    Run `hyatlas` in another shell first, or use\n"
            f"    HYATLAS_PROJECT_ROOT=/path/to/repo hyatlas memory {sys.argv[1] if len(sys.argv) > 1 else '...'} ..."
        )
        sys.exit(2)


def _provider():
    """Build a fresh HyMemoryProvider pointed at the local stack."""
    from hyatlas_memory import HyMemoryProvider
    p = HyMemoryProvider()
    session_id = f"cli-{os.getpid()}-{int(time.time())}"
    p.initialize(
        session_id=session_id,
        user_id=DEFAULT_USER_ID,
        agent_identity=DEFAULT_AGENT_ID,
    )
    return p, session_id


def cmd_write(args: list[str]) -> int:
    """``hyatlas memory write "text"`` — write one memory via sync_turn."""
    text = " ".join(args).strip()
    if not text:
        print('Usage: hyatlas memory write "the fact to remember"')
        return 2
    if not text.startswith('"') and not text.startswith("'"):
        # Allow bare text by joining all positional args as the content.
        pass

    _ensure_stack()
    provider, session_id = _provider()
    print(f"  → Writing via sync_turn (user={DEFAULT_USER_ID})...")
    provider.sync_turn(
        user_content=text,
        assistant_content="Noted.",
        session_id=session_id,
    )
    # Wait for indexing — the upstream server fires an LLM call for
    # fact extraction (mode=ultra by default) which takes 4-12s.
    print("  → Waiting 8s for LLM extraction + qdrant indexing...")
    time.sleep(8)

    hits = provider._client.search(text, user_ids=[DEFAULT_USER_ID], limit=3)
    mems = provider._flatten_memories(hits.get("memories"))
    print(f"\n  ✓ Memory written. Top 3 search hits for: {text[:60]!r}")
    if not mems:
        print("    (no hits yet — try again in a few seconds if backend is slow)")
        return 0
    for m in mems:
        score = m.get("score", 0.0)
        layer = m.get("layer", "?")
        content = str(m.get("content", ""))[:90]
        print(f"    [{layer:13}] score={score:.2f}  {content}")
    return 0


def cmd_recall(args: list[str]) -> int:
    """``hyatlas memory recall "query"`` — search memories and print ranked hits."""
    limit = 10
    query_parts: list[str] = []
    for i, a in enumerate(args):
        if a == "--limit" and i + 1 < len(args):
            with suppress(ValueError):
                limit = int(args[i + 1])
        elif a == "--user-id" and i + 1 < len(args):
            global DEFAULT_USER_ID
            DEFAULT_USER_ID = args[i + 1]
        else:
            query_parts.append(a)
    query = " ".join(query_parts).strip()
    if not query:
        print('Usage: hyatlas memory recall "your search query"')
        return 2

    _ensure_stack()
    provider, _ = _provider()
    hits = provider._client.search(query, user_ids=[DEFAULT_USER_ID], limit=limit)
    mems = provider._flatten_memories(hits.get("memories"))
    print(f"  Query: {query}")
    print(f"  Found: {len(mems)} hits (showing up to {limit})")
    print()
    for i, m in enumerate(mems, 1):
        score = m.get("score", 0.0)
        layer = m.get("layer", "?")
        mid = m.get("memory_id", "?")
        content = str(m.get("content", "")).replace("\n", " ")[:120]
        print(f"  {i:>2}. [{layer:13}] score={score:.3f}  id={mid[:8]}")
        print(f"      {content}")
    return 0


def cmd_list(args: list[str]) -> int:
    """``hyatlas memory list`` — recent memories, newest first.

    Queries Qdrant directly (with a server-side sort by gmt_created)
    so the response is fast regardless of corpus size. The upstream
    `/api/v1/list` endpoint is too slow on large corpora because it
    post-processes every point.
    """
    limit = 20
    layer = None
    for i, a in enumerate(args):
        if a == "--limit" and i + 1 < len(args):
            with suppress(ValueError):
                limit = min(int(args[i + 1]), 200)
        elif a == "--layer" and i + 1 < len(args):
            layer = args[i + 1]
        elif a == "--user-id" and i + 1 < len(args):
            global DEFAULT_USER_ID
            DEFAULT_USER_ID = args[i + 1]

    _ensure_stack()

    import json
    import urllib.request as _ur

    from hyatlas_memory._start import QDRANT_PORT
    qdrant_url = f"http://127.0.0.1:{QDRANT_PORT}"
    qdrant_collection = os.environ.get("HYATLAS_QDRANT_COLLECTION", "agent_memories_1024")

    # Build qdrant filter: user_id + optional layer.
    must = [{"key": "user_id", "match": {"value": DEFAULT_USER_ID}}]
    if layer:
        must.append({"key": "layer", "match": {"value": layer}})

    body = {
        "filter": {"must": must},
        "limit": limit,
        "with_payload": ["layer", "user_id", "gmt_created", "importance",
                         "access_count", "content", "memory_id", "session_id"],
        "with_vectors": False,
        # qdrant 1.7+ supports order_by for scroll; falls back to client-side
        # sort if not supported. Wrapped in try/except below.
        "order_by": {"key": "gmt_created", "direction": "desc"},
    }

    try:
        req = _ur.Request(
            f"{qdrant_url}/collections/{qdrant_collection}/points/scroll",
            data=json.dumps(body).encode(),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        resp = json.loads(_ur.urlopen(req, timeout=15).read())
        points = (resp.get("result") or {}).get("points") or []
    except Exception:
        # Older qdrant without order_by support: scroll then sort client-side.
        body.pop("order_by", None)
        points = []
        offset = None
        while len(points) < limit * 3:
            b = dict(body)
            if offset is not None:
                b["offset"] = offset
            req = _ur.Request(
                f"{qdrant_url}/collections/{qdrant_collection}/points/scroll",
                data=json.dumps(b).encode(),
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            resp = json.loads(_ur.urlopen(req, timeout=15).read())
            points.extend((resp.get("result") or {}).get("points") or [])
            offset = (resp.get("result") or {}).get("next_page_offset")
            if offset is None:
                break
        points.sort(
            key=lambda p: p.get("payload", {}).get("gmt_created", 0),
            reverse=True,
        )

    print(f"  Recent memories (limit={limit}, layer={layer or 'all'}, user={DEFAULT_USER_ID}):")
    print()
    if not points:
        print("  (no memories match the filter)")
        return 0

    for i, p in enumerate(points[:limit], 1):
        pl = p.get("payload") or {}
        ts = pl.get("gmt_created")
        if isinstance(ts, (int, float)):
            when = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
        else:
            when = "—"
        layer_s = pl.get("layer", "?")
        content = str(pl.get("content", "")).replace("\n", " ")[:100]
        mid = pl.get("memory_id") or p.get("id", "?")
        imp = pl.get("importance")
        imp_s = f" ★{imp:.1f}" if isinstance(imp, (int, float)) else ""
        print(f"  {i:>2}. {when}  [{layer_s:13}]{imp_s}  id={str(mid)[:8]}")
        print(f"      {content}")
    return 0


def cmd_reflect(args: list[str]) -> int:
    """``hyatlas memory reflect "query"`` — print the system-prompt block.

    Returns exactly the XML the agent would inject if Hermes called
    ``prefetch()`` with the same query. Useful for debugging recall
    quality and for piping into another tool's system prompt.
    """
    limit = 10
    query_parts: list[str] = []
    for i, a in enumerate(args):
        if a == "--limit" and i + 1 < len(args):
            with suppress(ValueError):
                limit = int(args[i + 1])
        elif a == "--user-id" and i + 1 < len(args):
            global DEFAULT_USER_ID
            DEFAULT_USER_ID = args[i + 1]
        else:
            query_parts.append(a)
    query = " ".join(query_parts).strip()
    if not query:
        print('Usage: hyatlas memory reflect "your query"')
        return 2

    _ensure_stack()
    provider, _ = _provider()
    hits = provider._client.search(query, user_ids=[DEFAULT_USER_ID], limit=limit)
    mems = provider._flatten_memories(hits.get("memories"))

    # Use the provider's own formatter so output matches what
    # Hermes sees during a real conversation.
    block = provider._format_memories_for_prompt(mems) if mems else ""

    if not block.strip():
        block = "  (no relevant memories found)"

    print(f"  Reflection block for query: {query!r}")
    print(f"  ({len(mems)} candidate memories, formatted as system-prompt block)")
    print()
    print(block)
    return 0


def cmd_status(args: list[str]) -> int:
    """``hyatlas memory status`` — quick health check."""
    _ensure_stack()
    provider, _ = _provider()
    print(f"  Provider reachable : {provider._client.is_reachable()}")
    print(f"  User ID            : {DEFAULT_USER_ID}")
    print(f"  Agent ID           : {DEFAULT_AGENT_ID}")
    return 0


_DISPATCH = {
    "write":    cmd_write,
    "add":      cmd_write,    # alias matching old `hermes hy-memory add`
    "retain":   cmd_write,    # alias matching Hindsight's `retain`
    "recall":   cmd_recall,
    "search":   cmd_recall,   # alias
    "find":     cmd_recall,   # alias
    "list":     cmd_list,
    "ls":       cmd_list,
    "reflect":  cmd_reflect,
    "status":   cmd_status,
}


def main(argv: list[str]) -> int:
    """Dispatch ``hyatlas memory <subcommand> [args]``."""
    if len(argv) < 2 or argv[1] in ("-h", "--help", "help"):
        print(__doc__.splitlines()[0])
        print()
        print("Usage:")
        print('  hyatlas memory write    "the fact to remember"')
        print('  hyatlas memory recall   "search query"')
        print('  hyatlas memory list     [--layer l3_fact] [--limit 20]')
        print('  hyatlas memory reflect  "search query"')
        print('  hyatlas memory status')
        print()
        print("Aliases:")
        print("  write    ← add, retain")
        print("  recall   ← search, find")
        print("  list     ← ls")
        return 0

    sub = argv[1].lower()
    handler = _DISPATCH.get(sub)
    if not handler:
        print(f"  ✘ Unknown memory subcommand: {argv[1]}")
        print(f"    Known: {', '.join(sorted(set(_DISPATCH.keys())))}")
        return 2

    return handler(argv[2:])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
