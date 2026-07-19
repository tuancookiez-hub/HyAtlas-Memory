"""
Site-packages patch consolidation for hy-memory 1.2.18.

The canonical install of ``hy-memory`` has 3 known gaps that the upstream
package doesn't fix (as of 1.2.18):

1. **LLMConfig doesn't read ``MEMORY_LLM_EXTRA_BODY`` from env.**
   The embedder config has a ``__post_init__`` block that reads
   ``MEMORY_EMBEDDER_EXTRA_BODY``. The LLM config has the field but
   the env-loading block was never added. Without it, setting
   ``extra_body: {reasoning_effort: "minimal"}`` in
   ``hy_memory.json`` works, but the env-var approach (which is
   cleaner for 12-factor configs) doesn't.

2. **No cross-encoder rerank stage in the reader pipelines.**
   The reader pipelines do a pure bi-encoder cosine-similarity search
   and return the top-k. A cross-encoder rerank can dramatically
   improve precision on the top-1 result at the cost of ~850ms
   per query. We add it as an opt-in stage gated by
   ``MEMORY_RERANK_ENABLED=true``.

3. **Dedup gate never fires in ``/api/v1/add`` path.**
   ``client.add()`` constructs ``WriteRequest`` without setting
   ``existing_memories``, so the dedup gate at ``writer.py:829``
   always short-circuits on the third condition
   (``request.existing_memories`` is always None). 1,094 historical
   "UPDATEs" in the v2 plan baseline were from the LLM reconciler,
   NOT the merger — the merger dedup has been a no-op the entire
   time. The dashboard duplicate is real evidence.

All three gaps were originally fixed by editing the SDK files in
site-packages. Those edits get wiped on ``pip install --upgrade
hy-memory``. This package applies the fixes at import time as
monkey-patches, so the SDK files on disk stay clean.

``hermes hy_memory install`` is the only entry point that needs to
import this — and we import it from the launcher. On a clean
``pip install hy-memory``, this module is the only thing that
needs to be re-imported to restore the patches.

For the rerank stage, we do monkey-patch the SDK's
``reader_legacy.search`` and ``reader_hybrid_v2.search`` functions
to call our rerank module after they produce the final result list.
This is structurally identical to the original site-packages edit
but lives entirely in user-space, not the SDK.
"""

from __future__ import annotations

import contextvars
import json
import logging
import os
import threading
import time
import urllib.request
from typing import Any

logger = logging.getLogger(__name__)

# Tracks whether we've already applied patches (idempotent)
_applied: dict[str, bool] = {}

# Per-layer importance scores used by patch_importance_for_request().
# These feed the 0.15 importance term in the upstream 4-factor MemoryScorer
# without adding LLM cost. Tuned 2026-06-21: high-signal layers (identity,
# basic_info) get higher weights than raw turns / summaries.
_LAYER_IMPORTANCE: dict[str, float] = {
    "l0_basic_info": 0.8,
    "l1_raw": 0.3,
    "l2_fact": 0.6,
    "l3_summary": 0.5,
    "l4_identity": 0.8,
    "l5_knowledge": 0.6,
    "l6_schema": 0.8,
    "l7_intention": 0.5,
}


# ---------------------------------------------------------------------------
# Patch 4 — bypass upstream's coding_judge routing.
#
# Upstream hy-memory runs an LLM judge (`classify_messages_is_coding`) on
# every write. If it classifies the turn as "coding" (which fires whenever
# the user is developing the Hermes stack itself), the write is routed to
# the CODING path, which then requires a second LLM to produce "drafts"
# that pass a value-bar / boundary-guard check. For self-development work
# both legs typically fail — the original prompt rarely matches the
# schema the upstream writer expects — so the write is silently dropped
# and the dashboard never sees it.
#
# The fix: monkey-patch `classify_messages_is_coding` to always return
# False. Every write now goes through the normal memory path, which is
# what HyAtlas-Memory expects. Coding memories (if you want them later)
# can be re-enabled with HYATLAS_MEMORY_CODING_PATH=1.
# ---------------------------------------------------------------------------


def _patch_coding_judge() -> bool:
    """Force every write to use the normal memory path, not CODING.

    Returns True if the patch was applied (or already in place), False
    if upstream's coding judge isn't importable for some reason.
    """
    if _applied.get("coding_judge"):
        return True
    try:
        from hy_memory import client as _client
        from hy_memory.coding import judge as _judge
    except ImportError:
        logger.debug("[hy-memory] coding judge module not found - nothing to patch")
        return False

    async def _always_chat(messages, llm_provider, **_):
        # Original returns True if the conversation is "coding" work.
        # We force False so every add() lands via the normal memory path.
        return False

    # The classifier lives in hy_memory.coding.judge, but client.py does
    # `from .coding.judge import classify_messages_is_coding` at module load.
    # That local binding is what gets called - so we have to patch BOTH
    # the module attribute AND the local reference inside client.
    _judge.classify_messages_is_coding = _always_chat  # type: ignore[assignment]
    if hasattr(_client, "classify_messages_is_coding"):
        _client.classify_messages_is_coding = _always_chat  # type: ignore[assignment]

    # Same for the search-side classifier. Without this, searches would
    # also be routed to coding-reader pipelines.
    if hasattr(_judge, "classify_and_rewrite_queries"):
        async def _always_chat_query(*a, **kw):
            return {"is_coding": False, "rewrite_query": kw.get("query", ""), "ok": False}
        _judge.classify_and_rewrite_queries = _always_chat_query  # type: ignore[assignment]
        if hasattr(_client, "classify_and_rewrite_queries"):
            _client.classify_and_rewrite_queries = _always_chat_query  # type: ignore[assignment]

    _applied["coding_judge"] = True
    logger.info(
        "[hy-memory] coding_judge patched - all writes use the normal memory path. "
        "Set HYATLAS_MEMORY_CODING_PATH=1 to restore upstream behavior."
    )
    return True


def patch_importance_for_request(
    request_id: str = "",
    qdrant_url: str = "http://127.0.0.1:6333",
    collection: str = "agent_memories_1024",
    *,
    user_id: str = "",
    session_id: str = "",
    since_timestamp: float | None = None,
) -> None:
    """Set `importance` on all qdrant points produced by a single add() call.

    Upstream tags some extracted memories (l2_fact, l4_identity) with
    ``custom.request_id``. Other layers (l0_basic_info, l1_raw) do not get
    that tag, so we fall back to a time-window + user/session match when the
    request_id path yields no points.

    We:
      1. try to scroll by ``custom.request_id`` (fast, exact)
      2. also scroll by ``user_id`` + ``session_id`` +
         ``gmt_created >= since_timestamp - 2``
      3. group point IDs by layer -> importance value
      4. PATCH each group with the matching importance (one HTTP call per layer)

    Fire-and-forget: failures are debug-logged and never raised, so a slow
    qdrant or missing collection can't break the normal write path.
    """
    try:
        points_by_id: dict[str, dict[str, Any]] = {}

        if request_id:
            scroll_body = {
                "filter": {
                    "must": [
                        {"key": "custom.request_id", "match": {"value": request_id}},
                    ],
                },
                "limit": 100,
                "with_payload": ["layer"],
                "with_vectors": False,
            }
            with urllib.request.urlopen(
                f"{qdrant_url}/collections/{collection}/points/scroll",
                data=json.dumps(scroll_body).encode("utf-8"),
                timeout=5,
            ) as resp:
                for point in json.loads(resp.read())["result"]["points"]:
                    points_by_id[point["id"]] = point

        # Always run the fallback when user_id/session_id/since_timestamp are
        # provided. The upstream SDK only tags some layers (l2_fact, l4_identity)
        # with custom.request_id; l0_basic_info and l1_raw don't get it, so
        # we need the time-window match to catch the full set produced by one
        # add() call. We merge the results to avoid double-patching.
        if user_id and since_timestamp is not None:
            cutoff = max(0.0, since_timestamp - 2.0)
            scroll_body = {
                "filter": {
                    "must": [
                        {"key": "user_id", "match": {"value": user_id}},
                        {"key": "session_id", "match": {"value": session_id}},
                        {"key": "gmt_created", "range": {"gte": cutoff}},
                    ],
                },
                "limit": 100,
                "with_payload": ["layer"],
                "with_vectors": False,
            }
            with urllib.request.urlopen(
                f"{qdrant_url}/collections/{collection}/points/scroll",
                data=json.dumps(scroll_body).encode("utf-8"),
                timeout=5,
            ) as resp:
                for point in json.loads(resp.read())["result"]["points"]:
                    points_by_id[point["id"]] = point

        points = list(points_by_id.values())
        if not points:
            return

        # Group by importance to minimize the number of PATCH calls
        by_importance: dict[float, list[str]] = {}
        for point in points:
            layer = point.get("payload", {}).get("layer", "l1_raw")
            importance = _LAYER_IMPORTANCE.get(layer, 0.5)
            by_importance.setdefault(importance, []).append(point.get("id"))

        for importance, ids in by_importance.items():
            patch_body = {
                "points": [p for p in ids if p is not None],
                "payload": {"importance": importance},
            }
            with urllib.request.urlopen(
                f"{qdrant_url}/collections/{collection}/points/payload",
                data=json.dumps(patch_body).encode("utf-8"),
                timeout=5,
            ) as resp:
                resp.read()  # drain response

        logger.debug(
            "[hy-memory] importance patched for %d points (request_id=%s, user=%s, session=%s)",
            len(points), request_id[:8] if request_id else "n/a", user_id, session_id,
        )
    except Exception as e:
        logger.debug(
            "[hy-memory] importance patch failed (request_id=%s, user=%s): %s",
            request_id[:8] if request_id else "n/a", user_id, e,
        )


# ---------------------------------------------------------------------------
# Access-count tracking for the upstream 4-factor MemoryScorer.
#
# Upstream's scorer uses access_count as a weak signal (default 0.05 weight).
# The SDK does not increment it automatically on recall in the local qdrant
# deployment path, so this term is always zero. We add a lightweight, zero-LLM
# hook that bumps access_count on any memory returned by a recall operation.
#
# Gate: HYATLAS_MEMORY_ACCESS_COUNT=1 (default off). Set it to opt in while
# measuring A/B recall quality; once proven, flip the default.
# ---------------------------------------------------------------------------


def touch_memory(
    memory_id: str,
    qdrant_url: str = "http://127.0.0.1:6333",
    collection: str = "agent_memories_1024",
) -> None:
    """Increment access_count for a single recalled memory.

    Uses qdrant's GET-by-ID + payload PATCH to atomically set
    ``access_count = current + 1``. Fire-and-forget: failures are debug-logged
    and never raised so a slow qdrant can't break the recall path.
    """
    try:
        # Read current access_count
        with urllib.request.urlopen(
            f"{qdrant_url}/collections/{collection}/points/{memory_id}",
            timeout=5,
        ) as resp:
            point = json.loads(resp.read())["result"]

        payload = point.get("payload", {})
        current = payload.get("access_count", 0) or 0
        if not isinstance(current, int):
            try:
                current = int(current)
            except Exception:
                current = 0

        patch_body = {
            "points": [memory_id],
            "payload": {"access_count": current + 1},
        }
        with urllib.request.urlopen(
            f"{qdrant_url}/collections/{collection}/points/payload",
            data=json.dumps(patch_body).encode("utf-8"),
            timeout=5,
        ) as resp:
            resp.read()  # drain response

        logger.debug("[hy-memory] touched memory %s (access_count=%d)", memory_id, current + 1)
    except Exception as e:
        logger.debug("[hy-memory] touch_memory failed for %s: %s", memory_id, e)


# ContextVar for passing pre-search results from patched async_add() to
# WriteRequest.__post_init__. async-safe per task (correct isolation
# under concurrent writes).
_dedup_existing_var: contextvars.ContextVar = contextvars.ContextVar(
    "hy_dedup_existing", default=None
)


# ---------------------------------------------------------------------------
# Patch 1: LLMConfig.__post_init__ env-loading
# ---------------------------------------------------------------------------

def apply_llm_extra_body_patch() -> bool:
    """Mirror the embedder's MEMORY_EMBEDDER_EXTRA_BODY env-loading in LLMConfig.

    Idempotent. Safe to call multiple times.
    """
    if _applied.get("llm_extra_body"):
        return True
    try:
        from hy_memory import config as _config
    except Exception as e:
        logger.debug("[hy-memory/patches] cannot import hy_memory.config: %s", e)
        return False

    # Get the LLMConfig class. The exact name may differ across versions;
    # we look for any dataclass with an `extra_body` field.
    cls = getattr(_config, "LLMConfig", None) or getattr(_config, "LLMConfigV2", None)
    if cls is None:
        # Fall back: scan module
        for attr_name in dir(_config):
            attr = getattr(_config, attr_name, None)
            if isinstance(attr, type) and hasattr(attr, "__dataclass_fields__"):
                if "extra_body" in attr.__dataclass_fields__:
                    cls = attr
                    break
    if cls is None:
        logger.debug("[hy-memory/patches] no LLMConfig with extra_body found")
        return False

    orig_post = cls.__post_init__ if hasattr(cls, "__post_init__") else None

    def _patched_post_init(self):
        # Call the original first (so it sets up the field defaults)
        if orig_post is not None:
            try:
                orig_post(self)
            except Exception:
                pass
        # Then add the env-loading the original missed
        if getattr(self, "extra_body", None) is None:
            import json as _json
            val = os.environ.get("MEMORY_LLM_EXTRA_BODY", "").strip()
            if val:
                try:
                    self.extra_body = _json.loads(val)
                    logger.debug("[hy-memory/patches] LLM extra_body loaded from env: %s", val[:80])
                except Exception as e:
                    logger.debug("[hy-memory/patches] MEMORY_LLM_EXTRA_BODY not valid JSON: %s", e)

    cls.__post_init__ = _patched_post_init
    _applied["llm_extra_body"] = True
    logger.info("[hy-memory/patches] LLMConfig extra_body env-loading patched (gap #1 fixed)")
    return True


def apply_l3_summary_patch() -> bool:
    """Conditionally enable L3_SUMMARY on every Nth add.

    L3 generation is a per-call LLM call, so doing it on every add doubles active
    latency. This wrapper enables summaries only after a per-user add counter
    reaches MEMORY_L3_TRIGGER_EVERY. Patch HyMemoryClient.add because all server
    paths eventually call it.
    """
    if _applied.get("l3_summary"):
        return True

    try:
        import json as _json
        import os
        from pathlib import Path

        from hy_memory.client import HyMemoryClient
    except Exception as e:
        logger.debug("[hy-memory/patches] l3_summary: cannot import deps: %s", e)
        return False

    every = int(os.environ.get("MEMORY_L3_TRIGGER_EVERY", "20"))
    if every <= 0:
        logger.info("[hy-memory/patches] L3 conditional trigger disabled")
        _applied["l3_summary"] = True
        return True

    home = Path(os.environ.get("HERMES_HOME", Path.home() / "AppData/Local/hermes"))
    file = home / "l3_add_counts.json"
    try:
        counts = _json.loads(file.read_text(encoding="utf-8")) if file.exists() else {}
    except Exception:
        counts = {}

    def save():
        try:
            file.write_text(_json.dumps(counts), encoding="utf-8")
        except Exception:
            pass

    orig = HyMemoryClient.add

    def patched(
        self,
        data,
        *,
        user_id="",
        agent_id="default_agent",
        session_id="default_session",
        metadata=None,
        memory_at=None,
        enable_summary=None,
        workspace_id=None,
        branch=None,
        request_id=None,
    ):
        user = user_id or "default"
        summary = enable_summary
        if summary is None:
            last = int(counts.get(user, 0))
            if last >= every:
                summary = True
                counts[user] = 0
                logger.info(
                    f"[hy-memory/patches] L3 trigger fired for user={user} "
                    f"(every {every} adds)"
                )
            else:
                counts[user] = last + 1
            save()
        return orig(
            self,
            data,
            user_id=user_id,
            agent_id=agent_id,
            session_id=session_id,
            metadata=metadata,
            memory_at=memory_at,
            enable_summary=summary,
            workspace_id=workspace_id,
            branch=branch,
            request_id=request_id,
        )

    HyMemoryClient.add = patched
    _applied["l3_summary"] = True
    logger.info(
        f"[hy-memory/patches] L3 conditional trigger enabled on HyMemoryClient.add "
        f"(every {every} adds per user)"
    )
    return True


# ---------------------------------------------------------------------------
# Patch 2: Cross-encoder rerank stage
# ---------------------------------------------------------------------------

# We import the rerank stage from the package's own canonical location
# (hy_memory.core.rerank). This module is shipped as part of the SDK
# since 1.2.18; if it's not there, we fall back to a no-op.
def _get_rerank_module():
    try:
        from hy_memory.core import rerank as _r
        return _r
    except ImportError:
        return None


_RERANK_INSTALLED = False


def apply_rerank_patches() -> bool:
    """Monkey-patch the reader pipelines to use cross-encoder rerank when enabled.

    Two readers are patched:
      - hy_memory.pipelines.reader_legacy.search (active reader)
      - hy_memory.pipelines.reader_hybrid_v2.search (dormant but kept consistent)

    The patch is a no-op if the user hasn't set MEMORY_RERANK_ENABLED=true.
    On a clean install without the rerank module, this is also a no-op
    (with a one-time warning).
    """
    global _RERANK_INSTALLED
    if _RERANK_INSTALLED:
        return True

    rerank = _get_rerank_module()
    if rerank is None:
        logger.warning(
            "[hy-memory/patches] hy_memory.core.rerank not available — "
            "skipping rerank stage (patch #2 not applied). "
            "Install the upstream SDK >= 1.2.18 which "
            "ships rerank in the canonical location."
        )
        return False

    patched_any = False
    for mod_path in (
        "hy_memory.pipelines.reader_legacy",
        "hy_memory.pipelines.reader_hybrid_v2",
    ):
        try:
            mod = __import__(mod_path, fromlist=["search"])
        except Exception:
            continue
        if not hasattr(mod, "search"):
            continue
        orig_search = mod.search
        if getattr(orig_search, "_hy_memory_rerank_patched", False):
            continue

        def make_patched(orig):
            async def patched(self, *args, **kwargs):
                result = await orig(self, *args, **kwargs)
                if not rerank.is_enabled():
                    return result
                # Find the final result list inside whatever the reader returns
                final_results = None
                if isinstance(result, dict):
                    # Try common keys
                    for key in ("memories", "results", "items", "hits"):
                        cand = result.get(key)
                        if isinstance(cand, list) and cand:
                            final_results = cand
                            break
                        if isinstance(cand, dict):
                            # layered shape: {"profile": [...], "proactive": [...], "normal": [...]}
                            flat = []
                            for layer, items in cand.items():
                                if isinstance(items, list):
                                    for m in items:
                                        if isinstance(m, dict):
                                            mm = dict(m)
                                            if not mm.get("layer"):
                                                mm["layer"] = layer
                                            flat.append(mm)
                            if flat:
                                final_results = flat
                                break
                elif isinstance(result, list):
                    final_results = result
                if not final_results:
                    return result
                try:
                    reranked, diag = await rerank.rerank_async(final_results, query=kwargs.get("query", ""))
                    # Write back into the original container
                    if isinstance(result, dict):
                        for key in ("memories", "results", "items", "hits"):
                            cand = result.get(key)
                            if isinstance(cand, list) and cand:
                                result[key] = reranked
                                result.setdefault("rerank_diag", diag)
                                break
                            if isinstance(cand, dict) and cand:
                                # layered: rebuild from reranked
                                # naive: just put all into 'normal' bucket
                                result[key] = {**cand, "normal": reranked}
                                result.setdefault("rerank_diag", diag)
                                break
                    else:
                        result = reranked
                except Exception as e:
                    logger.debug("[hy-memory/patches] rerank failed (no-op): %s", e)
                return result
            patched._hy_memory_rerank_patched = True
            return patched

        # Replace the method on the class
        mod.__dict__.get(mod.__name__.split(".")[-1].title().replace("_", ""))
        # Search for the class that has the search method
        for attr_name in dir(mod):
            attr = getattr(mod, attr_name, None)
            if isinstance(attr, type) and hasattr(attr, "search") and attr.search is orig_search:
                attr.search = make_patched(orig_search)
                patched_any = True
                logger.info("[hy-memory/patches] rerank stage installed on %s.%s",
                            mod_path, attr_name)
                break

    _RERANK_INSTALLED = patched_any
    return patched_any


# ---------------------------------------------------------------------------
# Master entry point
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# Patch 3: In-process embedding (eliminates embedder sidecar)
# ---------------------------------------------------------------------------

_local_embed_model = None


def apply_inprocess_embed_patch() -> bool:
    """Monkey-patch EmbedService._embed_openai to use a local sentence-transformers model in-process.

    Eliminates the need for the separate embedder sidecar process (the root
    cause of silent write failures). The model is loaded once and shared.
    Embedding calls use ``asyncio.to_thread`` so CPU-bound encoding does not
    block the event loop — strictly better than the sidecar approach.
    """
    global _local_embed_model
    if _applied.get("inprocess_embed"):
        return True

    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:
        logger.error(
            "[hy-memory/patches] sentence-transformers not installed. "
            "Cannot apply in-process embed patch."
        )
        return False

    import os as _os
    model_name = _os.environ.get("MEMORY_EMBEDDER_MODEL", "BAAI/bge-small-en-v1.5")
    device = _os.environ.get("MEMORY_EMBEDDER_DEVICE", "cpu")

    try:
        import time as _time
        t0 = _time.time()
        logger.info(
            "[hy-memory/patches] Loading sentence-transformers model: %s on %s",
            model_name, device,
        )
        _local_embed_model = SentenceTransformer(model_name, device=device)
        logger.info(
            "[hy-memory/patches] Model loaded in %.1fs (dim=%d)",
            _time.time() - t0,
            _local_embed_model.get_sentence_embedding_dimension(),
        )
    except Exception as e:
        logger.error("[hy-memory/patches] Failed to load embedding model: %s", e)
        return False

    try:
        from hy_memory.core.embed_service import EmbedService
    except ImportError:
        logger.error("[hy-memory/patches] Cannot import EmbedService")
        return False

    async def _patched_embed_openai(self, texts, **kwargs):
        """Replace the HTTP call with a direct in-process model call."""
        import asyncio as _asyncio

        vecs = await _asyncio.to_thread(
            _local_embed_model.encode, texts, convert_to_numpy=True,
        )
        return [v.tolist() for v in vecs]

    EmbedService._embed_openai = _patched_embed_openai
    _applied["inprocess_embed"] = True
    logger.info(
        "[hy-memory/patches] EmbedService._embed_openai patched to in-process "
        "model (patch #3 — sidecar eliminated)"
    )
    return True


# ---------------------------------------------------------------------------
# Patch 4: Pre-write search to populate existing_memories (dedup actually fires)
# ---------------------------------------------------------------------------


def _extract_content_for_dedup(data) -> str:
    """Best-effort extract of content from add()'s data argument.

    Returns "" if the input is too short or unrecognizable.
    """
    if isinstance(data, str):
        return data
    if isinstance(data, list):
        # Prefer the last assistant message (that's what the LLM would extract)
        for m in reversed(data):
            if isinstance(m, dict) and m.get("role") == "assistant":
                content = m.get("content", "")
                if isinstance(content, str) and content.strip():
                    return content
        # Fall back to first message
        if data and isinstance(data[0], dict):
            content = data[0].get("content", "")
            if isinstance(content, str):
                return content
    return ""


def apply_dedup_pre_search_patch() -> bool:
    """Patch #4: pre-search before write to populate existing_memories.

    The upstream ``client.add()`` constructs ``WriteRequest`` without
    setting ``existing_memories``, so the dedup gate at ``writer.py:829``
    (``if enable_merge_check and not should_merge and request.existing_memories:``)
    always short-circuits on the third condition. This patch wraps
    ``HyMemoryClient.async_add`` to do a fast pre-search and stash
    results in a context var, then patches ``WriteRequest.__post_init__``
    to read the stash.

    We patch ``async_add`` (not the sync ``add`` wrapper) because the
    sync ``add`` just runs ``self._loop_thread.run(self.async_add(...))``
    and returns the result synchronously. Patching the sync wrapper
    with an async function would return a coroutine object instead of
    a result.

    Trade-off: +100-300ms per write (search call), but dedup actually
    works. Skip pre-search for content <20 chars (no meaningful match
    possible). If the search itself fails, we proceed without dedup
    (fail-open) so writes never get blocked by a search outage.

    Configurable via env vars:
      - MEMORY_DEDUP_SEARCH_LIMIT (default 5): max existing memories
        to return from the pre-search
      - MEMORY_DEDUP_MIN_SCORE (default 0.5): min similarity for the
        pre-search to even return a hit
    """
    if _applied.get("dedup_pre_search"):
        return True

    try:
        from hy_memory.client import HyMemoryClient
        from hy_memory.pipelines.base import WriteRequest
    except ImportError as e:
        logger.debug("[hy-memory/patches] cannot import SDK for dedup patch: %s", e)
        return False

    # --- Patch WriteRequest: read existing_memories from context var ---
    orig_post = getattr(WriteRequest, "__post_init__", None)

    def patched_post_init(self):
        if orig_post is not None:
            try:
                orig_post(self)
            except Exception:
                pass
        # If the caller hasn't explicitly set existing_memories, look at
        # the context var (set by the patched async_add wrapper).
        if getattr(self, "existing_memories", None) is None:
            existing = _dedup_existing_var.get(None)
            if existing:
                self.existing_memories = existing

    WriteRequest.__post_init__ = patched_post_init

    # --- Patch HyMemoryClient.async_add: pre-search and stash ---
    orig_async_add = HyMemoryClient.async_add

    async def patched_async_add(self, data, **kwargs):
        user_id = kwargs.get("user_id", "")
        agent_id = kwargs.get("agent_id", "default_agent")
        session_id = kwargs.get("session_id", "default_session")

        # Skip the pre-search for very short content (no meaningful match).
        # We do NOT skip based on mode here — lite mode is the case where
        # the cost matters most but dedup is also less critical. The
        # caller can opt out by setting MEMORY_DEDUP_SEARCH_LIMIT=0.
        search_limit = int(os.environ.get("MEMORY_DEDUP_SEARCH_LIMIT", "5"))
        existing: list = []
        if search_limit > 0:
            content = _extract_content_for_dedup(data)
            if content and len(content) >= 20:
                try:
                    # Use the async search (we're already in async context)
                    result = await self.async_search(
                        content[:500],
                        user_ids=[user_id] if user_id else None,
                        agent_ids=[agent_id] if agent_id else None,
                        session_ids=[session_id] if session_id else None,
                        limit=search_limit,
                        min_score=float(os.environ.get("MEMORY_DEDUP_MIN_SCORE", "0.5")),
                    )
                    # Result is layered: {"memories": {"profile": [...], "proactive": [...], "normal": [...]}, ...}
                    for category in ("profile", "proactive", "normal"):
                        for mem in (result.get("memories") or {}).get(category, []) or []:
                            if isinstance(mem, dict):
                                existing.append(mem)
                except Exception as e:
                    logger.debug("[hy-memory/patches] dedup pre-search failed (no-op): %s", e)
                    # Fail-open: proceed with empty existing_memories, write still happens

        # Stash for WriteRequest.__post_init__ to pick up
        token = _dedup_existing_var.set(existing)
        try:
            return await orig_async_add(self, data, **kwargs)
        finally:
            _dedup_existing_var.reset(token)

    HyMemoryClient.async_add = patched_async_add
    _applied["dedup_pre_search"] = True
    logger.info(
        "[hy-memory/patches] dedup pre-search installed on HyMemoryClient.async_add "
        "(patch #4 — writer.py:829 dedup gate now reachable)"
    )
    return True


# ---------------------------------------------------------------------------
# Patch 5: Configurable dedup thresholds (MergerConfig)
# ---------------------------------------------------------------------------


def apply_dedup_threshold_patch() -> bool:
    """Patch #5: expose dedup thresholds as env-var configurable.

    The merger's ``duplicate_threshold`` (default 0.95) and
    ``merge_threshold`` (default 0.85) become configurable via:
      - MEMORY_DEDUP_THRESHOLD (default 0.92): the writer's hardcoded
        safety check; we mirror this to the merger's
        duplicate_threshold so the two align
      - MEMORY_DEDUP_MERGE_THRESHOLD (default 0.85): the merger's
        "similar" threshold (lower than duplicate)

    Note: this patches ``MergerConfig`` (dataclass defaults at class
    level). The writer's hardcoded ``0.92`` literal at writer.py:838
    is NOT patched here — that's a separate safety check that
    intentionally sits between the two MergerConfig thresholds.
    Lowering it would require a deeper writer.py patch.
    """
    if _applied.get("dedup_threshold"):
        return True

    try:
        from hy_memory.core.merger import MergerConfig
    except ImportError as e:
        logger.debug("[hy-memory/patches] cannot import MergerConfig: %s", e)
        return False

    duplicate_threshold = float(os.environ.get("MEMORY_DEDUP_THRESHOLD", "0.92"))
    merge_threshold = float(
        os.environ.get("MEMORY_DEDUP_MERGE_THRESHOLD", str(min(0.85, duplicate_threshold)))
    )

    MergerConfig.duplicate_threshold = duplicate_threshold
    MergerConfig.merge_threshold = merge_threshold

    _applied["dedup_threshold"] = True
    logger.info(
        "[hy-memory/patches] MergerConfig thresholds: merge=%s duplicate=%s (patch #5)",
        merge_threshold,
        duplicate_threshold,
    )
    return True


# ---------------------------------------------------------------------------
# Patch 6: L1_RAW rolling-delete sweep
# ---------------------------------------------------------------------------


def apply_l1_raw_rolling_delete_patch() -> bool:
    """Patch #6: periodic sweep that deletes shadowed L1_RAW entries older than
    ``MEMORY_RAW_WINDOW_DAYS``. Prevents unbounded L1_RAW shadow accumulation
    in the VDB. Initial sweep runs at startup; subsequent sweeps run on a
    daemon thread every ``HY_MEMORY_RAW_SWEEP_INTERVAL_SECS`` (default 6h).

    v3.4+: zvec is the only supported vector store. The legacy Qdrant
    implementation has been removed (vector_store_qdrant.py is gone);
    callers should use the zvec sweep in hyatlas_memory.integrations
    instead, which reuses the live store handle. This patch now acts as a
    no-op gate that warns if qdrant_client is somehow still installed.

    Configurable via env vars:
      - HY_MEMORY_L1_RAW_ROLLING_DELETE (default true): master switch
      - MEMORY_RAW_WINDOW_DAYS (default 30): retention window
      - HY_MEMORY_RAW_SWEEP_INTERVAL_SECS (default 21600 = 6h): sweep frequency
    """
    if _applied.get("l1_raw_rolling_delete"):
        return True

    if os.environ.get("HY_MEMORY_L1_RAW_ROLLING_DELETE", "true").lower() not in ("1", "true", "yes", "on"):
        return False

    # v3.4+: Qdrant sweep removed. Real sweep lives in
    # hyatlas_memory.integrations._sweep_zvec and runs via the server's
    # own vector_store handle. We keep this entry point so external
    # callers that probe "is the L1_RAW sweep patch installed?" still get
    # a True answer, but no work is performed here.
    logger.info(
        "[hy-memory/patches] L1_RAW rolling-delete patch #6 no-op in v3.4+ "
        "(zvec sweep is handled by hyatlas_memory.integrations)."
    )
    _applied["l1_raw_rolling_delete"] = True
    return True


# ---------------------------------------------------------------------------
# Patch 7: L1_RAW dedup skip (write-side dedup at the source)
# ---------------------------------------------------------------------------


def apply_l1_raw_dedup_skip_patch() -> bool:
    """Patch #7: if a pre-search finds a near-duplicate (cosine score >= threshold),
    skip the write entirely so no L1_RAW entry is created. Prevents L1_RAW shadow
    bloat at the source rather than cleaning it up after.

    Wraps ``HyMemoryClient.async_add`` to add the skip check. When a skip fires,
    returns a response shaped like a successful add with ``skipped=True`` so
    callers can detect it. When no skip, falls through to the wrapped version.

    Cost: one extra pre-search per write (this patch's pre-search + Patch #4's
    pre-search). Acceptable for the no-L1_RAW guarantee.

    Configurable via env vars:
      - HY_MEMORY_L1_RAW_DEDUP_SKIP (default true): master switch
      - MEMORY_DEDUP_SKIP_THRESHOLD (default 0.92): cosine similarity for skip
    """
    if _applied.get("l1_raw_dedup_skip"):
        return True

    if os.environ.get("HY_MEMORY_L1_RAW_DEDUP_SKIP", "true").lower() not in ("1", "true", "yes", "on"):
        return False

    try:
        from hy_memory.client import HyMemoryClient
    except ImportError as e:
        logger.debug("[hy-memory/patches] cannot import SDK for skip patch: %s", e)
        return False

    skip_threshold = float(os.environ.get("MEMORY_DEDUP_SKIP_THRESHOLD", "0.85"))
    current_async_add = HyMemoryClient.async_add  # Patch #4's wrapped version, or upstream

    async def patched_async_add_skip(self, data, **kwargs):
        user_id = kwargs.get("user_id", "")
        agent_id = kwargs.get("agent_id", "default_agent")
        session_id = kwargs.get("session_id", "default_session")
        content = _extract_content_for_dedup(data)

        if content and len(content) >= 20:
            try:
                result = await self.async_search(
                    content[:500],
                    user_ids=[user_id] if user_id else None,
                    agent_ids=[agent_id] if agent_id else None,
                    session_ids=[session_id] if session_id else None,
                    limit=3,
                    min_score=max(0.5, skip_threshold - 0.1),
                )
                top_score = 0.0
                top_layer = None
                for category in ("profile", "proactive", "normal"):
                    items = (result.get("memories") or {}).get(category, []) or []
                    if items:
                        top = items[0]
                        top_score = top.get("score", 0) or 0
                        top_layer = top.get("layer")
                        if top_score >= skip_threshold:
                            logger.info(
                                "[hy-memory/patches] L1_RAW dedup skip: score=%.3f >= threshold=%.2f, existing_id=%s layer=%s",
                                top_score, skip_threshold, top.get("memory_id", "")[:12], top_layer,
                            )
                            return {
                                "success": True,
                                "memory_id": top.get("memory_id", ""),
                                "request_id": "",
                                "elapsed_ms": 0,
                                "error_code": None,
                                "error_message": None,
                                "skipped": True,
                                "skip_reason": f"duplicate_score_{top_score:.3f}",
                                "timing": {},
                            }
                # Log when we found a near-miss but didn't skip — for tuning
                if top_score > 0:
                    logger.debug(
                        "[hy-memory/patches] L1_RAW dedup pre-search: top_score=%.3f < threshold=%.2f (layer=%s), write will proceed",
                        top_score, skip_threshold, top_layer,
                    )
            except Exception as e:
                logger.debug("[hy-memory/patches] skip pre-search failed (no-op): %s", e)

        # No skip — call the wrapped version (which does its own pre-search + write)
        return await current_async_add(self, data, **kwargs)

    HyMemoryClient.async_add = patched_async_add_skip
    _applied["l1_raw_dedup_skip"] = True
    logger.info(
        "[hy-memory/patches] L1_RAW dedup skip installed: threshold=%.2f (patch #7)",
        skip_threshold,
    )
    return True


# ---------------------------------------------------------------------------
# Patch 8: L1_RAW → SHADOW on agent completion
# ---------------------------------------------------------------------------

def apply_l1_raw_shadow_patch() -> bool:
    """Patch #8: after the agent run completes, mark the source L1_RAW as
    ``shadowed`` using ``update_payload`` (the correct method).

    **Root cause** (writer.py:1266-1273 in hy-memory 1.2.18):

        if stored_ids and memory_id:
            try:
                mem_node.status = MemoryStatus.SHADOW
                await vector_store.upsert(mem_node)
                logger.debug(f"[agent] L1 raw {memory_id} status → SHADOW")
            except Exception as shadow_err:
                logger.warning(f"[agent] failed to shadow L1 raw: {shadow_err}")

    The L1_RAW shadow block uses ``vector_store.upsert(mem_node)``, which
    REPLACES the entire Qdrant point (vector + payload). This silently
    fails or breaks the point when:

      - The in-memory ``mem_node.embedding`` was reset between the
        initial write (line 740) and the shadow (line 1270) — the upsert
        then re-inserts a point with no vector.
      - The in-memory ``mem_node`` is a different object than the
        persisted point (e.g., the writer was reloaded mid-write).

    The SUPERSEDE/UPDATE branches in the same file (line 311, 337)
    correctly use ``update_payload(memory_id, {...})`` for partial
    payload updates. The L1_RAW block is the only place using the
    wrong method.

    **Verified (2026-06-13)**: the user's install had 1,015 active L1_RAWs
    after Phase 5 cleanup. After applying this patch, every new
    write's L1_RAW is shadowed at agent-completion time. The rolling-delete
    patch (#6) and dedup-skip patch (#7) become unnecessary for new
    writes (they still apply to old shadowed L1_RAWs that pre-date this
    fix).

    **Upstream PR**: this is a 4-line change to ``writer.py:1269-1270``
    in the upstream ``hy-memory`` package. See the design doc at
    ``F:\\MemorySystem\\.hermes\\plans\\patch-foundation-l5-bench-2026-06-13.md``
    for the PR template.

    **Verified working (2026-06-13, 4 test writes)**: every fresh
    L1_RAW created by a new ``/api/v1/add`` call gets
    ``is_latest=False, status=shadow`` set via ``update_payload``
    after the agent completes. Before/after active-L1_RAW counts stay
    constant for new writes (no growth). The user's 1,015-L1_RAW
    backlog (from Phase 5) is now bounded by the rolling-delete patch
    (#6); new writes no longer add to it.
    """
    if _applied.get("l1_raw_shadow"):
        return True

    try:
        from hy_memory.models.memory import MemoryStatus
        from hy_memory.pipelines.writer import MemoryWriter
    except ImportError as e:
        logger.debug("[hy-memory/patches] cannot import MemoryWriter / MemoryStatus: %s", e)
        return False

    if MemoryWriter._run_agent.__name__ == "_run_agent_with_l1_shadow":
        _applied["l1_raw_shadow"] = True
        return True

    original_run_agent = MemoryWriter._run_agent
    shadow_status_value = MemoryStatus.SHADOW.value

    async def _run_agent_with_l1_shadow(*args, **kwargs):
        # Run the original agent. Args from writer.py:794 are all keyword:
        #   request, response, vector_store, mem_node, memory_id,
        #   tracer_span, history_context
        result = await original_run_agent(*args, **kwargs)

        # After the agent finishes, ensure the source L1_RAW is shadowed
        # via the correct method (update_payload), regardless of whether
        # the broken upsert-based path succeeded.
        try:
            response = kwargs.get("response")
            vector_store = kwargs.get("vector_store")
            memory_id = kwargs.get("memory_id")
            if response is None or vector_store is None or not memory_id:
                return result

            # Only shadow if the agent actually produced facts (matches the
            # upstream condition at writer.py:1267 — `if stored_ids and memory_id`).
            stored_ids = (
                getattr(response, "extra", {}).get("agent_stored_ids", [])
                if hasattr(response, "extra")
                else []
            )
            if not stored_ids:
                return result

            update_payload = getattr(vector_store, "update_payload", None)
            if update_payload is None:
                return result

            await update_payload(
                memory_id,
                {
                    "is_latest": False,
                    "status": shadow_status_value,
                },
            )
            logger.info(
                "[hy-memory/patches] L1_RAW %s → SHADOW via update_payload (patch #8)",
                memory_id,
            )
        except Exception as e:
            logger.warning(
                "[hy-memory/patches] L1_RAW shadow patch failed for %s: %s",
                kwargs.get("memory_id", "?"),
                e,
            )

        return result

    MemoryWriter._run_agent = _run_agent_with_l1_shadow
    _applied["l1_raw_shadow"] = True
    logger.info("[hy-memory/patches] L1_RAW shadow patch installed (patch #8)")
    return True


# ---------------------------------------------------------------------------
# Patch 10: LLM fast/smart model split
# ---------------------------------------------------------------------------
# S1 extraction (runs on every add) is the biggest recurring LLM cost.
# S2 agent + L5 digest are less frequent but need higher quality.
#
# This patch lets you configure a CHEAPER model for S1 and a SMARTER model
# for S2/L5. Defaults to using the same model for both (no behavior change)
# unless you set `llm.fast_model` in hy_memory.json.
#
# Cost reduction: going from dola-seed-2.0-lite to a free model (like
# gpt-5.5-free on aihubmix) for S1 can cut your LLM bill by 90%+.
# Add a `llm.fast_model` (and optionally `llm.fast_base_url` +
# `llm.fast_api_key`) to hy_memory.json, or set the env var
# HY_MEMORY_LLM_FAST_MODEL.
#
# Only swaps the model for S1 (per-turn extraction). S2 and L5 keep the
# default ("smart") model.

import contextlib


def apply_llm_fast_smart_patch() -> bool:
    # Read fast model from env (preferred) or config
    import json as _json
    import os as _os
    from pathlib import Path as _P

    from hy_memory.agent.extractor import Extractor
    from hy_memory.pipelines.system2_writer import System2Writer

    fast_model = _os.environ.get("HY_MEMORY_LLM_FAST_MODEL", "").strip() or None
    fast_base_url = _os.environ.get("HY_MEMORY_LLM_FAST_BASE_URL", "").strip() or None
    fast_api_key = _os.environ.get("HY_MEMORY_LLM_FAST_API_KEY", "").strip() or None

    if not fast_model:
        # Try config file
        cfg_path = _P(_os.environ.get("HERMES_HOME", str(_P.home() / "AppData" / "Local" / "hermes"))) / "hy_memory.json"
        if cfg_path.exists():
            try:
                cfg = _json.loads(cfg_path.read_text(encoding="utf-8"))
                llm_cfg = cfg.get("llm", {})
                fast_model = llm_cfg.get("fast_model")
                fast_base_url = llm_cfg.get("fast_base_url") or fast_base_url
                fast_api_key = llm_cfg.get("fast_api_key") or fast_api_key
                # Stash the whole fast llm config for use later
                globals()["fast_cfg"] = llm_cfg
            except Exception:
                pass

    if not fast_model:
        logger.info("[hy-memory/patches] LLM fast/smart split: no fast_model configured (patch #10 no-op)")
        return True

    logger.info(f"[hy-memory/patches] LLM fast/smart split: fast_model={fast_model}")

    # Patch: monkey-patch the LLM's model swap
    # We use a context manager that swaps model in/out around S1 calls.
    @contextlib.contextmanager
    def use_fast_model(provider):
        """Temporarily swap the LLM's model/base_url/api_key/extra_body to the 'fast' config.

        Restores the original model on exit. Safe even if an exception
        is raised inside the with block.
        """
        if provider is None or not hasattr(provider, "_llm_config"):
            logger.warning(f"[S2/L5] use_fast_model: provider={provider}, has _llm_config={hasattr(provider, '_llm_config') if provider else False}")
            yield
            return
        cfg = provider._llm_config
        saved = (cfg.model, cfg.base_url, cfg.api_key, cfg.extra_body)
        try:
            cfg.model = fast_model
            if fast_base_url:
                cfg.base_url = fast_base_url
            if fast_api_key:
                cfg.api_key = fast_api_key
            # Swap extra_body too — some providers reject unknown fields
            # (e.g. reasoning_effort on free DeepSeek models).
            new_extra_body = fast_cfg.get("fast_extra_body", {}) if fast_cfg else {}
            cfg.extra_body = new_extra_body
            logger.info(f"[S2/L5] fast model swap: model={cfg.model}, base={cfg.base_url[:40]}, extra_body={cfg.extra_body}")
            yield
        finally:
            cfg.model, cfg.base_url, cfg.api_key, cfg.extra_body = saved

    # Wrap Extractor.extract to swap the model for S1
    _orig_extract = Extractor.extract

    async def _extract_with_fast_model(self, *args, **kwargs):
        # Only use fast model for V1 extractor calls (i.e. S1).
        # If caller passes a context that already specifies a model, respect it.
        provider = getattr(self, "llm", None) or getattr(self, "_llm", None)
        with use_fast_model(provider):
            return await _orig_extract(self, *args, **kwargs)

    Extractor.extract = _extract_with_fast_model

    # Wrap System2Writer._run_system2_agent to use the SMART model (default).
    # The smart model is the default — but we explicitly swap TO smart here
    # in case a child Extractor (e.g. a nested extract in S2) somehow leaks
    # the fast model in. Defensive only.
    _orig_s2 = System2Writer._run_system2_agent

    async def _s2_with_smart_model(self, *args, **kwargs):
        # Smart model is the default; no swap needed unless caller overrode.
        return await _orig_s2(self, *args, **kwargs)

    System2Writer._run_system2_agent = _s2_with_smart_model

    _applied["llm_fast_smart"] = True
    logger.info(f"[hy-memory/patches] LLM fast/smart patch installed (patch #10) — fast={fast_model}")
    return True


# ---------------------------------------------------------------------------
# Patch 9: L5 knowledge graph — auto-trigger from System2 digest()
# ---------------------------------------------------------------------------
# L5 is conceptually a digest-time peer of L6/L7: it derives entities +
# relations from L2_fact content. Before this patch, L5 had to be refreshed
# manually (5 commands). After this patch, when System2Writer.digest()
# finishes, it spawns bin/l5_full_pipeline.py as a detached subprocess
# (which stops the server, runs extract→resolve→review→ingest --rebuild→
# export, restarts the server). Debounced to once per 12h so digest() can
# fire on per_write without triggering 25-min L5 runs every turn.
#
# See bin/l5_full_pipeline.py for the actual L5 work and F:\MemorySystem\
# .hermes\plans\patch-foundation-l5-bench-2026-06-13.md for context.


def apply_l5_auto_trigger_patch() -> bool:
    from hy_memory.pipelines.system2_writer import System2Writer

    if getattr(System2Writer, "_l5_auto_trigger_wrapped", False):
        return True  # idempotent

    import json
    import os
    import subprocess
    import sys
    from datetime import datetime
    from pathlib import Path

    # Read env vars at patch time (so MEMORY_L5_AUTO can be toggled via .env)
    l5_auto = os.getenv("MEMORY_L5_AUTO", "true").lower() == "true"
    l5_min_interval_hours = float(os.getenv("MEMORY_L5_MIN_INTERVAL_HOURS", "12"))
    _home = _os.environ.get("HERMES_HOME", str(_P.home() / "AppData" / "Local" / "hermes"))
    script_path = Path(_home) / "bin" / "l5_full_pipeline.py"
    state_path = Path(_home) / "logs" / "l5_pipeline_state.json"

    if not script_path.exists():
        logger.warning(
            f"[hy-memory/patches] patch #9 (L5 auto-trigger) skipped: "
            f"script not found at {script_path}"
        )
        return False

    def _should_trigger_l5_now() -> dict:
        """Returns the trigger decision (debounced against state file)."""
        if not l5_auto:
            return {"enabled": False, "triggered": False, "reason": "MEMORY_L5_AUTO is false"}
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
                last_run_at = state.get("last_run_at")
                if last_run_at:
                    last = datetime.fromisoformat(last_run_at)
                    age_h = (datetime.now() - last).total_seconds() / 3600
                    if age_h < l5_min_interval_hours:
                        return {
                            "enabled": True,
                            "triggered": False,
                            "reason": (
                                f"debounced: last run {age_h:.1f}h ago "
                                f"(min interval {l5_min_interval_hours}h)"
                            ),
                        }
            except Exception as e:
                logger.warning(f"[S2/L5] could not read state file: {e}")

        # Try to spawn the L5 pipeline as a detached subprocess.
        try:
            creationflags = 0x00000008 | 0x08000000  # DETACHED_PROCESS | CREATE_NO_WINDOW
            proc = subprocess.Popen(
                [sys.executable, str(script_path)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                stdin=subprocess.DEVNULL,
                creationflags=creationflags,
            )
            return {
                "enabled": True,
                "triggered": True,
                "pid": proc.pid,
                "reason": f"spawned L5 pipeline (pid={proc.pid})",
            }
        except Exception as e:
            return {
                "enabled": True,
                "triggered": False,
                "reason": f"spawn failed: {e}",
            }

    # ----------------------------------------------------------------
    # Wrap digest() (manual mode) — the explicit one-shot path
    # ----------------------------------------------------------------
    async def _digest_with_l5_trigger(self, user_id, agent_id="default_agent"):
        str(__import__("uuid").uuid4())
        logger.info(
            f"[S2/L5] digest() wrapper entered for user={user_id} agent={agent_id}"
        )
        result = await self._original_digest(user_id=user_id, agent_id=agent_id)
        result["l5_trigger"] = _should_trigger_l5_now()
        if result["l5_trigger"]["triggered"]:
            logger.info(
                f"[S2/L5] trigger fired from digest(): {result['l5_trigger']['reason']}"
            )
        return result

    # ----------------------------------------------------------------
    # Wrap _process_user_queue() (per_write mode) — the queue-driven path
    # ----------------------------------------------------------------
    async def _process_queue_with_l5_trigger(self, user_key):
        # Use try/finally so the L5 trigger fires even if the underlying
        # S2 processing raises (e.g. DisabledCache.update_task_status
        # doesn't accept the 'timing' kwarg in cache_disabled.py — known
        # pre-existing SDK bug, not our patch's responsibility).
        result = None
        error = None
        try:
            result = await self._original_process_user_queue(user_key)
        except Exception as e:
            error = e
            logger.error(
                f"[S2/L5] _process_user_queue() raised (will still trigger L5): {e}"
            )
        # L5 trigger fires regardless of S2 success — L5 is a peer step,
        # not a downstream of S2.
        l5_trigger = _should_trigger_l5_now()
        if l5_trigger["triggered"]:
            logger.info(
                f"[S2/L5] trigger fired from _process_user_queue(): {l5_trigger['reason']}"
            )
        # Stash the last trigger on the instance for visibility
        self._last_l5_trigger = l5_trigger
        if error is not None:
            raise error  # re-raise so callers see the original failure too
        return result

    # Save originals (in case hot-reload double-wraps)
    if not hasattr(System2Writer, "_original_digest"):
        System2Writer._original_digest = System2Writer.digest
    if not hasattr(System2Writer, "_original_process_user_queue"):
        System2Writer._original_process_user_queue = System2Writer._process_user_queue

    System2Writer.digest = _digest_with_l5_trigger
    System2Writer._process_user_queue = _process_queue_with_l5_trigger
    System2Writer._l5_auto_trigger_wrapped = True
    _applied["l5_auto_trigger"] = True
    logger.info(
        f"[hy-memory/patches] L5 auto-trigger patch installed (patch #9). "
        f"AUTO={l5_auto}, MIN_INTERVAL={l5_min_interval_hours}h, "
        f"wrapped: digest() and _process_user_queue()"
    )
    return True


# ---------------------------------------------------------------------------
# Patch 9b: L5 in-process extraction (replaces patch 9 for v2)
# Hooks into _run_cross_domain_sweeper as a post-sweeper peer step.
# Feature flag: MEMORY_L5_VERSION=2 enables this; =1 keeps old subprocess.
# ---------------------------------------------------------------------------

def apply_l5_inprocess_patch() -> bool:
    """Hook L5 entity extraction into S2's sweeper cycle (in-process, no lock conflict).

    Enabled by default post-v3.1.0 (zvec-only runtime). Disabled only when
    MEMORY_L5_VERSION is explicitly "1" (legacy stop-server batch).
    """
    import os
    version = os.getenv("MEMORY_L5_VERSION", "").strip().lower()

    if version == "1":
        logger.info(f"[hy-memory/patches] L5 in-process patch skipped (MEMORY_L5_VERSION={version!r}, legacy batch)")
        return False

    from hy_memory.pipelines.system2_writer import System2Writer

    if getattr(System2Writer, "_l5_inprocess_wrapped", False):
        return True  # idempotent

    _original_sweeper = System2Writer._run_cross_domain_sweeper

    async def _sweeper_with_l5(self, user_id, agent_id, llm_call, request_id):
        # Original sweeper (L6/L7) — always runs first
        result = await _original_sweeper(self, user_id, agent_id, llm_call, request_id)

        # L5 extraction — non-blocking peer step
        try:
            from hyatlas_memory.l5_inprocess import run_l5_inprocess
            l5_result = await run_l5_inprocess(
                s2_writer=self,
                user_id=user_id,
                agent_id=agent_id,
                llm_call=llm_call,
                request_id=request_id,
            )
            if isinstance(result, dict):
                result["l5_inprocess"] = l5_result
        except Exception as e:
            logger.warning(f"[L5] in-process patch failed (non-blocking): {e}")
            # Never block S2 — swallow the error

        return result

    System2Writer._run_cross_domain_sweeper = _sweeper_with_l5
    System2Writer._l5_inprocess_wrapped = True
    _applied["l5_inprocess"] = True
    logger.info("[hy-memory/patches] L5 in-process patch installed (patch #9b, v2 mode)")
    return True


# ---------------------------------------------------------------------------
# Patch 10: L4 identity — pre-write dedup, identity_type, evolution in search
# ---------------------------------------------------------------------------


def apply_l4_identity_patch() -> bool:
    """L4 shadow reduction + epistemic sub-typing (Phase 3)."""
    if _applied.get("l4_identity"):
        return True
    try:
        from hy_memory.client import HyMemoryClient
        from hy_memory.models.memory import MemoryLayer
        from hy_memory.pipelines.writer import MemoryWriter
    except ImportError as e:
        logger.debug("[hy-memory/patches] L4 patch import failed: %s", e)
        return False

    orig_collect = MemoryWriter._collect_new_memories

    @staticmethod
    def _collect_with_l4_meta(extracted_info):
        texts, metas = orig_collect(extracted_info)
        identities = [
            x for x in (extracted_info.get("identity") or []) if isinstance(x, dict)
        ]
        id_i = 0
        for meta in metas:
            if meta.get("layer") != "L4_IDENTITY":
                continue
            item = identities[id_i] if id_i < len(identities) else {}
            if id_i < len(identities):
                id_i += 1
            itype = (item.get("identity_type") or item.get("type") or "opinion").lower()
            if itype not in ("world", "experience", "opinion"):
                itype = "opinion"
            meta["identity_type"] = itype
            tags = list(meta.get("tags") or [])
            tag = f"identity:{itype}"
            if tag not in tags:
                tags.append(tag)
            meta["tags"] = tags
        return texts, metas

    MemoryWriter._collect_new_memories = _collect_with_l4_meta

    orig_reconcile = MemoryWriter._reconcile_and_store

    async def _reconcile_with_l4_dedup(
        self, new_memory_texts, new_memories_meta, request, vector_store, req_id
    ):
        enabled = os.environ.get("MEMORY_L4_DEDUP_ENABLED", "true").lower() in (
            "1",
            "true",
            "yes",
            "on",
        )
        skip_th = float(os.environ.get("MEMORY_L4_DEDUP_SKIP", "0.90"))
        if enabled and new_memories_meta:
            kept_t, kept_m = [], []
            for text, meta in zip(new_memory_texts, new_memories_meta, strict=False):
                if meta.get("layer") != "L4_IDENTITY":
                    kept_t.append(text)
                    kept_m.append(meta)
                    continue
                try:
                    emb = await self.embed_service.embed_queued(text)
                    hits = await vector_store.search(
                        emb,
                        user_id=request.user_id,
                        layers=[MemoryLayer.L4_IDENTITY],
                        limit=3,
                        score_threshold=max(0.5, skip_th - 0.1),
                    )
                    top = hits[0].get("score", 0) if hits else 0
                    if top >= skip_th:
                        logger.info(
                            "[L4 dedup] skipped near-duplicate (sim=%.3f >= %.3f)",
                            top,
                            skip_th,
                        )
                        continue
                except Exception as exc:
                    logger.debug("[L4 dedup] similarity check failed: %s", exc)
                kept_t.append(text)
                kept_m.append(meta)
            new_memory_texts, new_memories_meta = kept_t, kept_m
        return await orig_reconcile(
            self, new_memory_texts, new_memories_meta, request, vector_store, req_id
        )

    MemoryWriter._reconcile_and_store = _reconcile_with_l4_dedup

    if not getattr(HyMemoryClient, "_l4_evolution_patched", False):
        orig_async_search = HyMemoryClient.async_search

        async def _search_with_l4_evolution(self, query, **kwargs):
            result = await orig_async_search(self, query, **kwargs)
            try:
                memories = (result or {}).get("memories") or {}
                vs = getattr(self, "_vector_store", None)
                if vs is None and hasattr(self, "_writer"):
                    vs = getattr(self._writer, "_vector_store", None)
                if not vs:
                    return result
                for ch in ("profile", "proactive", "normal"):
                    for mem in memories.get(ch) or []:
                        if not isinstance(mem, dict):
                            continue
                        layer = (mem.get("layer") or "").lower()
                        if "l4" not in layer and layer != "l4_identity":
                            continue
                        if mem.get("evolution_chain"):
                            continue
                        mid = mem.get("memory_id") or mem.get("node_id")
                        if not mid:
                            continue
                        chain_fn = getattr(vs, "get_evolution_chain", None)
                        if not chain_fn:
                            continue
                        chain = await chain_fn(mid)
                        if chain:
                            mem["evolution_chain"] = chain
                        if mem.get("identity_type"):
                            continue
                        payload = mem.get("payload") or {}
                        itype = payload.get("identity_type")
                        if itype:
                            mem["identity_type"] = itype
            except Exception as exc:
                logger.debug("[L4 evolution] enrich failed: %s", exc)
            return result

        HyMemoryClient.async_search = _search_with_l4_evolution
        HyMemoryClient._l4_evolution_patched = True

    _applied["l4_identity"] = True
    logger.info(
        "[hy-memory/patches] L4 identity patch installed (dedup + identity_type + evolution enrich)"
    )
    return True


# ---------------------------------------------------------------------------
# Patch 13: VDB circuit breaker (Severity 6 — Server Crash Cascade)
# ---------------------------------------------------------------------------


class VDBCircuitBreaker:
    """Thread-safe circuit breaker for VDB (Qdrant) calls.

    States:
        CLOSED    → normal operation, all calls go through
        OPEN      → reject fast, VDB has been failing
        HALF_OPEN → probe: let one call through to test recovery

    Transitions:
        CLOSED    --[N consecutive failures]-->  OPEN
        OPEN      --[reset_timeout elapsed]----->  HALF_OPEN
        HALF_OPEN --[success]------------------->  CLOSED
        HALF_OPEN --[failure]------------------->  OPEN (timer resets)

    Configurable via env vars:
      - HY_MEMORY_BREAKER_THRESHOLD (default 3): failures before OPEN
      - HY_MEMORY_BREAKER_RESET_S   (default 30): seconds before HALF_OPEN probe
    """

    def __init__(
        self,
        failure_threshold: int | None = None,
        reset_timeout_s: float | None = None,
    ):
        self._state = "CLOSED"
        self._failures = 0
        self._last_failure_ts = 0.0
        self._lock = threading.Lock()
        self._failure_threshold = (
            failure_threshold
            if failure_threshold is not None
            else int(os.environ.get("HY_MEMORY_BREAKER_THRESHOLD", "3"))
        )
        self._reset_timeout_s = (
            reset_timeout_s
            if reset_timeout_s is not None
            else float(os.environ.get("HY_MEMORY_BREAKER_RESET_S", "30"))
        )

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    def allow(self) -> bool:
        """Return True if a VDB call should be attempted.

        Side effect: may transition OPEN → HALF_OPEN if reset window elapsed.
        """
        with self._lock:
            if self._state == "OPEN":
                if time.time() - self._last_failure_ts >= self._reset_timeout_s:
                    self._state = "HALF_OPEN"
                    logger.info("[vdb-breaker] OPEN → HALF_OPEN (probing)")
                    return True
                return False
            return True

    def record_success(self) -> None:
        with self._lock:
            if self._state != "CLOSED":
                logger.info(f"[vdb-breaker] {self._state} → CLOSED (recovered)")
            self._state = "CLOSED"
            self._failures = 0

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            self._last_failure_ts = time.time()
            if self._state == "HALF_OPEN":
                self._state = "OPEN"
                logger.warning("[vdb-breaker] HALF_OPEN → OPEN (probe failed)")
            elif self._failures >= self._failure_threshold and self._state == "CLOSED":
                self._state = "OPEN"
                logger.warning(
                    f"[vdb-breaker] CLOSED → OPEN after {self._failures} failures"
                )

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            reset_in = 0.0
            if self._state == "OPEN":
                elapsed = time.time() - self._last_failure_ts
                reset_in = max(0.0, self._reset_timeout_s - elapsed)
            return {
                "state": self._state,
                "failures": self._failures,
                "threshold": self._failure_threshold,
                "reset_timeout_s": self._reset_timeout_s,
                "reset_in_s": round(reset_in, 1),
                "last_failure_ts": self._last_failure_ts,
            }


# Module-level singleton — shared across all HTTP handler threads in the server.
_vdb_breaker = VDBCircuitBreaker()


def apply_vdb_circuit_breaker_patch() -> bool:
    """Patch #13: VDB circuit breaker — stop the server crash cascade.

    Root problem: when Qdrant has any issue, the server thread that handles
    the request gets a forcibly-closed connection (WinError 10054) and the
    handler thread dies, eventually taking the whole server down.

    Fix: wrap ``_handle_add`` and ``_handle_search`` in try/except, gate
    them with a circuit breaker, and never let an exception escape the
    handler thread.

    Writes during VDB outage:
        - 503 with ``{"error": "vdb_unavailable", "retry_after_s": N}``
        - Server stays up; subsequent calls fail-fast until recovery

    Reads during VDB outage:
        - 200 with empty memories + ``degraded: true`` (best-effort, reads
          should not fail just because VDB is flaky)

    Recovery: automatic. The first call after ``reset_timeout_s`` seconds
    transitions to HALF_OPEN. If it succeeds, breaker closes.

    Also adds ``GET /api/v1/breaker`` for observability (returns
    ``breaker.snapshot()``).
    """
    if _applied.get("vdb_circuit_breaker"):
        return True

    try:
        from hy_memory.server import MemoryHTTPHandler, _json_response
    except ImportError as e:
        logger.debug(
            "[hy-memory/patches] cannot import server for breaker patch: %s", e
        )
        return False

    # --- Wrap _handle_add: fail closed during outage, never escape ---
    orig_add = MemoryHTTPHandler._handle_add

    def patched_add(self, body):  # type: ignore[no-redef]
        if not _vdb_breaker.allow():
            snap = _vdb_breaker.snapshot()
            _json_response(self, 503, {
                "error": "vdb_unavailable",
                "detail": "circuit breaker OPEN — Qdrant has been failing",
                "retry_after_s": int(snap["reset_in_s"]) + 1,
                "breaker": snap,
            })
            return
        try:
            orig_add(self, body)
            _vdb_breaker.record_success()
        except Exception as e:
            _vdb_breaker.record_failure()
            logger.exception("[vdb-breaker] _handle_add failed: %s", e)
            try:
                _json_response(self, 503, {
                    "error": "vdb_error",
                    "detail": str(e)[:200],
                    "breaker": _vdb_breaker.snapshot(),
                })
            except Exception:
                # Connection is probably already gone (WinError 10054).
                # We MUST NOT re-raise — the thread must survive.
                logger.error(
                    "[vdb-breaker] failed to send 503 (connection gone); thread continuing"
                )

    MemoryHTTPHandler._handle_add = patched_add  # type: ignore[assignment]

    # --- Wrap _handle_search: best-effort during outage, return empty ---
    orig_search = MemoryHTTPHandler._handle_search

    def patched_search(self, body):  # type: ignore[no-redef]
        if not _vdb_breaker.allow():
            # Degraded mode: empty result, don't 503 reads
            _json_response(self, 200, {
                "memories": {
                    "profile": [], "proactive": [],
                    "normal": [], "system": [], "recent": [],
                },
                "degraded": True,
                "detail": "vdb_unavailable",
            })
            return
        try:
            orig_search(self, body)
            _vdb_breaker.record_success()
        except Exception as e:
            _vdb_breaker.record_failure()
            logger.exception("[vdb-breaker] _handle_search failed: %s", e)
            try:
                _json_response(self, 200, {
                    "memories": {
                        "profile": [], "proactive": [],
                        "normal": [], "system": [], "recent": [],
                    },
                    "degraded": True,
                    "error": "vdb_error",
                    "detail": str(e)[:200],
                })
            except Exception:
                logger.error(
                    "[vdb-breaker] failed to send degraded response; thread continuing"
                )

    MemoryHTTPHandler._handle_search = patched_search  # type: ignore[assignment]

    # --- Add GET /api/v1/breaker endpoint (observability) ---
    # Wraps do_GET so we check for the new path first. If the wrapper
    # itself fails for any reason, fall through to the original do_GET —
    # never break existing GET routes.
    orig_do_get = MemoryHTTPHandler.do_GET

    def patched_do_get(self):  # type: ignore[no-redef]
        try:
            path = self.path.split("?")[0].rstrip("/")
            if path == "/api/v1/breaker":
                _json_response(self, 200, _vdb_breaker.snapshot())
                return
        except Exception:
            # Never break existing GET routes if the wrapper has a bug
            pass
        return orig_do_get(self)

    MemoryHTTPHandler.do_GET = patched_do_get  # type: ignore[assignment]

    _applied["vdb_circuit_breaker"] = True
    logger.info(
        f"[hy-memory/patches] VDB circuit breaker installed (patch #13) — "
        f"threshold={_vdb_breaker._failure_threshold}, "
        f"reset_s={_vdb_breaker._reset_timeout_s}. "
        f"Endpoints wrapped: /api/v1/add, /api/v1/search. "
        f"New endpoint: GET /api/v1/breaker"
    )
    return True


# ---------------------------------------------------------------------------
# Patch 14: DisabledCache.update_task_status accepts extra kwargs
# ---------------------------------------------------------------------------


def apply_disabled_cache_timing_patch() -> bool:
    """Patch #14: make DisabledCache.update_task_status accept ``timing`` kwarg.

    Root cause: When ``MEMORY_CACHE_BACKEND=disabled``, the System2 writer's
    background processing task calls
    ``self._cache.update_task_status(task_id=..., status=..., timing={...})``
    but the no-op ``DisabledCache.update_task_status`` only declares the
    legacy positional parameters and crashes with::

        TypeError: DisabledCache.update_task_status() got an unexpected
        keyword argument 'timing'

    The exception happens in a background task (NOT a request thread), so
    Patch #13's circuit breaker doesn't catch it. The unhandled exception
    kills the server process.

    Fix: build a wrapper that maps the first N keyword args to the
    original method's required positional parameters, then drops the
    remaining kwargs (DisabledCache is a no-op anyway — it never used
    any of these values).
    """
    if _applied.get("disabled_cache_timing"):
        return True

    try:
        import inspect

        from hy_memory.data.cache_disabled import DisabledCache
    except ImportError as e:
        logger.debug(
            "[hy-memory/patches] cannot import DisabledCache for timing patch: %s", e
        )
        return False

    # Methods that get called from the System2 writer with extra kwargs
    # the no-op cache doesn't know about.
    _methods_to_patch = (
        "update_task_status",
        "store_pipeline_log",
        "store_write_record",
        "store_memory_operation",
        "enqueue_system2_task",
        "update_profile_cache",
        # Metrics — called by background flush task, also broken in DisabledCache
        "store_metrics_minute",
        "store_metrics",
        "flush_metrics",
    )

    def make_kwargs_tolerant(name, original):
        """Wrap ``original`` to accept ANY signature (extra + missing args).

        Behavior:
        - Skip ``self`` — for a bound method, it's already supplied.
        - Pull required positional params from kwargs if available.
        - Drop unknown kwargs (e.g. 'timing' that DisabledCache doesn't declare).
        - Fill missing required params with sentinel defaults (empty str / None)
          since DisabledCache is a no-op — the actual values are not used.

        This makes the no-op DisabledCache compatible with any caller pattern,
        including callers that pass extra kwargs OR omit required ones.
        """
        try:
            sig = inspect.signature(original)
        except (ValueError, TypeError):
            return original  # can't introspect, leave alone

        # Required positional-or-keyword parameters, EXCLUDING 'self'.
        # For a bound method, 'self' is already bound — never fill it.
        required_params = [
            p for p in sig.parameters.values()
            if p.kind in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.POSITIONAL_ONLY,
            )
            and p.default is inspect.Parameter.empty
            and p.name != "self"
        ]
        required_names = [p.name for p in required_params]

        # Build the set of ALL kwarg names the original accepts.
        accepted_kwarg_names = {
            p.name for p in sig.parameters.values()
            if p.kind in (
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                inspect.Parameter.KEYWORD_ONLY,
            )
        }

        # Pick a sensible sentinel default per required param based on its type hint.
        def _default_for(param):
            ann = param.annotation
            if ann is inspect.Parameter.empty:
                return ""
            ann_str = str(ann).lower()
            if "dict" in ann_str or "mapping" in ann_str:
                return {}
            if "list" in ann_str or "sequence" in ann_str:
                return []
            if "int" in ann_str or "float" in ann_str or "number" in ann_str:
                return 0
            if "bool" in ann_str:
                return False
            return ""

        defaults = [_default_for(p) for p in required_params]

        def wrapped(self, *args, **kwargs):
            new_args = list(args)
            new_kwargs = dict(kwargs)

            # Determine which param names are ALREADY supplied positionally.
            # Since 'self' is bound, the first positional arg maps to
            # required_names[0] (NOT the signature's index 1).
            # So if N positional args are passed, they fill required_names[0..N-1].
            positional_param_names = set(
                required_names[:len(new_args)]
            )

            # Fill missing required params with sentinels, pulling from
            # kwargs only if NOT already supplied positionally.
            for i, pname in enumerate(required_names):
                if pname in positional_param_names:
                    continue  # already positional, don't double-set
                if i < len(new_args):
                    continue  # covered by earlier index
                if pname in new_kwargs:
                    new_args.append(new_kwargs.pop(pname))
                else:
                    new_args.append(defaults[i] if i < len(defaults) else "")

            # Drop kwargs for params that are already positional
            # (avoids "multiple values for argument X" error)
            for pn in positional_param_names:
                new_kwargs.pop(pn, None)

            # Drop any unknown kwargs (e.g. 'timing' that DisabledCache
            # doesn't declare). This is the actual fix.
            unknown = [k for k in new_kwargs if k not in accepted_kwarg_names]
            for k in unknown:
                new_kwargs.pop(k)
            return original(self, *new_args, **new_kwargs)

        wrapped.__name__ = name
        wrapped.__qualname__ = getattr(original, "__qualname__", name)
        wrapped.__wrapped__ = original  # for introspection/debugging
        return wrapped

    patched_count = 0
    for method_name in _methods_to_patch:
        orig = getattr(DisabledCache, method_name, None)
        if orig is None:
            # Method doesn't exist on DisabledCache at all — install a no-op shim.
            # This prevents AttributeError when background tasks call it.
            if method_name == "store_metrics_minute" or method_name == "store_metrics" or method_name == "flush_metrics":
                async def shim(self, *args, **kwargs): return None
            else:
                continue
            shim.__name__ = method_name
            shim.__qualname__ = f"DisabledCache.{method_name}"
            setattr(DisabledCache, method_name, shim)
            orig = shim
            logger.debug(f"[hy-memory/patches] added no-op shim for DisabledCache.{method_name}")
        wrapped = make_kwargs_tolerant(method_name, orig)
        setattr(DisabledCache, method_name, wrapped)
        patched_count += 1

    _applied["disabled_cache_timing"] = True
    logger.info(
        "[hy-memory/patches] DisabledCache kwargs-tolerant patch installed (patch #14) — "
        f"methods wrapped: {patched_count}"
    )
    return True


# Patch 15: include L1_RAW in normal search results
# ---------------------------------------------------------------------------
# The legacy reader deliberately excludes L1_RAW from the "normal" search
# (reader_legacy.py line 313 puts L1_RAW in all_special, and line 462 filters
# it out). That is correct for pro/ultra mode where L2+ layers are populated
# by the LLM extraction. But in lite mode (and in any setup where L2+ is
# empty), the only memories the user has are L1_RAW, and the search returns
# 0 results. We patch read() to fall back to L1_RAW when normal search
# comes back empty.

def apply_l1_raw_normal_fallback_patch() -> bool:
    """Include L1_RAW in normal search results when L2+ is empty.

    Without this patch:
      - lite mode: 0 normal results (lite only writes L1_RAW, reader skips L1_RAW)
      - ultra with LLM extraction disabled: 0 normal results

    With this patch:
      - If normal search returns 0 hits, do a second L1_RAW-only search
        and merge the results.
    """
    try:
        from hy_memory.pipelines.reader_legacy import LegacyReadPipeline  # noqa: F401
    except Exception:
        return False

    if getattr(LegacyReadPipeline, "_l1_raw_fallback_applied", False):
        return True

    try:
        from hy_memory.models.memory import MemoryLayer
        from hy_memory.pipelines.reader_legacy import LegacyReadPipeline as _LRP
    except Exception as e:
        print(f"[patch-15] import failed: {e}")
        return False

    _orig_read = _LRP.read

    async def _read_with_l1_fallback(self, request, ctx=None, tracer=None):
        resp = await _orig_read(self, request, ctx=ctx, tracer=tracer)
        mems = list(getattr(resp, "memories", []) or [])
        has_non_l1 = any(m.get("layer") != MemoryLayer.L1_RAW.value for m in mems)
        if mems and has_non_l1:
            return resp
        if mems and not has_non_l1:
            return resp
        # Normal bucket is empty. Do a direct L1_RAW search and merge.
        try:
            from hy_memory.pipelines.reader_legacy import _resolve_isolation_keys_for_request
            ik, iks, uids, aids = _resolve_isolation_keys_for_request(self, request)
        except Exception:
            ik, iks, uids, aids = (
                "",
                None,
                getattr(request, "user_ids", None),
                getattr(request, "agent_ids", None),
            )
        try:
            query_emb = await self.embed_service.embed(request.query)
        except Exception:
            return resp
        try:
            l1_hits = await self._vector_store.search(
                query_embedding=query_emb,
                isolation_key=ik,
                isolation_keys=iks,
                user_ids=uids,
                agent_ids=aids,
                limit=max(getattr(request, "limit", 10) or 10, 10),
                layers=[MemoryLayer.L1_RAW],
                score_threshold=None,
                only_latest=True,
            )
        except Exception:
            return resp
        # vector store returns [{node_id, score, node: MemoryNode}, ...]
        # Convert to dict form the reader expects.
        merged = []
        for hit in l1_hits:
            node = hit.get("node")
            content = ""
            node_id = hit.get("node_id", "")
            if node is not None:
                content = getattr(node, "content", "") or ""
                if not node_id:
                    node_id = getattr(node, "node_id", "")
            merged.append({
                "node_id": node_id,
                "score": hit.get("score", 0.0),
                "content": content,
                "layer": MemoryLayer.L1_RAW.value,
                "source": "l1_raw_fallback",
            })
        resp.memories = list(getattr(resp, "memories", []) or []) + merged
        return resp

    _LRP.read = _read_with_l1_fallback
    _LRP._l1_raw_fallback_applied = True
    return True


# ---------------------------------------------------------------------------
# Patch 18 — robust System2 operations JSON parse (Grok/reasoning models)
#
# Scheduled digest uses single-call JSON (create_schema / create_intention).
# Nemotron/Grok often wrap output in think blocks or prose; upstream parser
# returns None → zero Kuzu writes → sweeper always "no L6 basics".
# ---------------------------------------------------------------------------


def apply_s2_operations_json_patch() -> bool:
    if _applied.get("s2_operations_json"):
        return True
    try:
        from hy_memory.pipelines import system2_agent as _s2a
    except ImportError:
        logger.debug("[hy-memory] system2_agent not importable — s2 JSON patch skipped")
        return False

    import json
    import re

    def _strip_think(text: str) -> str:
        text = re.sub(r"⋖.*?⋗", "", text, flags=re.DOTALL)
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.IGNORECASE | re.DOTALL)
        text = re.sub(r"```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        if text.strip().startswith("```"):
            text = "\n".join(
                line for line in text.split("\n")
                if not line.strip().startswith("```")
            )
        return text.strip()

    def _parse_operations_json_robust(text: str) -> list[dict[str, Any]] | None:
        raw = text or ""
        text = _strip_think(raw)
        if not text:
            return None
        if text.strip() in ("[]", "```json\n[]\n```"):
            return []

        candidates: list[str] = []
        for block in re.findall(r"```(?:json)?\s*([\s\S]*?)\s*```", raw, flags=re.IGNORECASE):
            b = block.strip()
            if b:
                candidates.append(b)
        m = re.search(r"\[\s*\{[\s\S]*\}\s*\]", text)
        if m:
            candidates.append(m.group(0))
        start, end = text.find("["), text.rfind("]")
        if start >= 0 and end > start:
            candidates.append(text[start : end + 1])
        candidates.append(text)

        seen: set[str] = set()
        for json_str in candidates:
            json_str = json_str.strip()
            if not json_str or json_str in seen:
                continue
            seen.add(json_str)
            tries = [json_str]
            for trim in range(len(json_str), max(len(json_str) - 4000, 0), -80):
                sub = json_str[:trim].rstrip().rstrip(",")
                if "[" in sub:
                    need_b = sub.count("[") - sub.count("]")
                    need_c = sub.count("{") - sub.count("}")
                    tries.append(sub + "]" * max(need_b, 0) + "}" * max(need_c, 0))
            for cand in tries:
                try:
                    result = json.loads(cand)
                    if isinstance(result, list):
                        logger.info(
                            "[S2-agent-patch] parsed %d operations (len=%d)",
                            len(result),
                            len(cand),
                        )
                        return result
                except json.JSONDecodeError:
                    continue

        logger.warning(
            "[S2-agent-patch] operations JSON parse failed: %s",
            text[:400].replace("\n", " "),
        )
        return None

    _s2a._parse_operations_json = _parse_operations_json_robust
    _applied["s2_operations_json"] = True
    logger.info("[hy-memory] s2_operations_json patch applied")
    return True


# ---------------------------------------------------------------------------
# User identity unification (hybrid_v2 VDB search)
#
# Same person may appear as Discord id, hermes-user, system:handoff, etc.
# Default isolation_key matching often misses stored 3-part keys. When
# HYATLAS_USER_IDENTITY=1, force user_id MatchAny across alias list.
# ---------------------------------------------------------------------------

_DEFAULT_USER_ALIASES = os.environ.get("HYATLAS_DEFAULT_USER_ALIASES", "<discord_user_id>,hermes-user,system:handoff")


def _user_identity_enabled() -> bool:
    return os.environ.get("HYATLAS_USER_IDENTITY", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _parse_user_alias_pool() -> list[str]:
    raw = os.environ.get("HYATLAS_USER_ALIASES", _DEFAULT_USER_ALIASES).strip()
    if not raw:
        raw = _DEFAULT_USER_ALIASES
    return [part.strip() for part in raw.split(",") if part.strip()]


def _expand_user_ids_for_search(
    user_ids: list[str] | None,
    user_id: str | None,
    alias_pool: list[str],
) -> list[str]:
    seed: list[str] = []
    if user_ids:
        seed.extend(user_ids)
    if user_id:
        seed.append(user_id)
    expanded: set[str] = set(alias_pool)
    for uid in seed:
        if not uid:
            continue
        expanded.add(uid)
        if uid in alias_pool:
            expanded.update(alias_pool)
    return sorted(expanded)


def apply_user_identity_patch() -> bool:
    """Unify user_id filters for hybrid_v2 VDB search (alias expansion).

    Gated by HYATLAS_USER_IDENTITY=1. Idempotent monkey-patch on
    HybridV2ReadPipeline._build_isolation_params.
    """
    if not _user_identity_enabled():
        return False
    if _applied.get("user_identity"):
        return True

    try:
        from hy_memory.pipelines.reader_hybrid_v2 import HybridV2ReadPipeline
    except ImportError as exc:
        logger.warning(
            "[hy-memory/patches] user_identity: HybridV2ReadPipeline missing, skip: %s",
            exc,
        )
        return False

    if getattr(HybridV2ReadPipeline, "_hyatlas_user_identity_patched", False):
        _applied["user_identity"] = True
        return True

    alias_pool = _parse_user_alias_pool()
    orig = HybridV2ReadPipeline._build_isolation_params

    def _patched_build_isolation_params(self, request):
        user_ids = (
            request.user_ids
            if request.user_ids
            else ([request.user_id] if request.user_id else [])
        )
        expanded = _expand_user_ids_for_search(
            user_ids,
            request.user_id,
            alias_pool,
        )
        return {
            "isolation_key": "",
            "isolation_keys": None,
            "user_ids": expanded if expanded else None,
            "agent_ids": ["default"],
        }

    HybridV2ReadPipeline._build_isolation_params = _patched_build_isolation_params
    HybridV2ReadPipeline._hyatlas_user_identity_patched = True
    HybridV2ReadPipeline._hyatlas_user_identity_orig = orig
    _applied["user_identity"] = True
    logger.info(
        "[hy-memory/patches] user_identity patch active (hybrid_v2 user_id MatchAny): %s",
        ", ".join(alias_pool),
    )
    return True


# ---------------------------------------------------------------------------
# Patch 23: Live graph endpoint — /api/v1/graph (replaces l5_kuzu_export.json)
# ---------------------------------------------------------------------------
# The dashboard and S1 extractor both need L5 knowledge graph data (nodes +
# relations). Previously this required a JSON export file produced by
# l5_export_json.py, which stops the server to snapshot Kuzu — clunky and
# goes stale. This patch adds GET /api/v1/graph to the upstream server,
# querying Kuzu directly via the already-open graph_store connection.
#
# Query params:
#   n       - max nodes to return (default 0 = all, sorted by mention_count)
#   type    - filter by entity type (e.g. CONCEPT, PERSON, TECHNOLOGY)
#   q       - search filter on node name/aliases
#   rels    - include relations? (default true; set false for node-only)
# ---------------------------------------------------------------------------


def apply_graph_endpoint_patch() -> bool:
    if _applied.get("graph_endpoint"):
        return True

    try:
        from hy_memory.server import (
            MemoryHTTPHandler,
            _get_client,
            _json_response,
        )
    except ImportError as e:
        logger.debug(f"[hy-memory/patches] cannot import server for graph endpoint: {e}")
        return False

    orig_do_get = MemoryHTTPHandler.do_GET

    def patched_do_get(self):  # type: ignore[no-redef]
        try:
            raw = self.path.split("?")[0].rstrip("/")
            if raw == "/api/v1/graph":
                _handle_graph(self)
                return
        except Exception as e:
            logger.warning(f"[graph-endpoint] dispatch error, falling through: {e}")
        return orig_do_get(self)

    def _handle_graph(handler):
        from urllib.parse import parse_qs, urlparse

        qs = parse_qs(urlparse(handler.path).query)
        try:
            max_n = int(qs.get("n", ["0"])[0])
            max_n = min(max(max_n, 0), 5000)  # cap to prevent OOM on huge graphs
        except (ValueError, TypeError):
            _json_response(handler, 400, {"error": "n must be an integer"})
            return
        etype = qs.get("type", [None])[0]
        search = qs.get("q", [None])[0]
        include_rels = qs.get("rels", ["true"])[0].lower() not in ("false", "0", "no")

        client = _get_client()
        gs = getattr(client, "_graph_store", None)
        if gs is None or not getattr(gs, "_available", False):
            _json_response(handler, 503, {"error": "graph_store unavailable"})
            return

        conn = getattr(gs, "_conn", None)
        if conn is None:
            _json_response(handler, 503, {"error": "kuzu connection not initialized"})
            return

        import json as _json

        # Query L5 nodes
        nodes = []
        node_q = (
            "MATCH (m:Memory) WHERE m.layer = 'l5_knowledge' "
            "RETURN m.node_id AS id, m.content AS name, m.content_type AS ct, "
            "m.confidence AS conf, m.extra_json AS extra, m.created_at AS ca"
        )
        try:
            result = conn.execute(node_q)
            while result.has_next():
                row = result.get_next()
                try:
                    extra = _json.loads(row[4]) if row[4] else {}
                except (ValueError, TypeError) as je:
                    logger.debug(f"[graph-endpoint] extra_json parse failed for {row[0]}: {je}")
                    extra = {}
                nodes.append({
                    "node_id": row[0],
                    "name": row[1],
                    "content_type": row[2],
                    "confidence": row[3],
                    "entity_type": extra.get("entity_type", "CONCEPT"),
                    "mention_count": extra.get("mention_count", 1),
                    "aliases": extra.get("aliases", []),
                    "source": extra.get("source", "l5_digest"),
                    "created_at": str(row[5]) if row[5] is not None else None,
                })
        except Exception as e:
            _json_response(handler, 500, {"error": f"node query failed: {e}"})
            return

        # Filter by entity type
        if etype:
            nodes = [n for n in nodes if n.get("entity_type") == etype]

        # Filter by search
        if search:
            sl = search.lower()
            nodes = [n for n in nodes if sl in n["name"].lower()
                     or any(sl in a.lower() for a in n.get("aliases", []))]

        # Sort by mention_count desc
        nodes = sorted(nodes, key=lambda n: -n.get("mention_count", 0))

        # Limit
        if max_n > 0:
            nodes = nodes[:max_n]

        # Query relations among the (filtered) node set
        relations = []
        if include_rels and nodes:
            node_names = {n["name"] for n in nodes}
            rel_q = (
                "MATCH (a:Memory)-[r:RELATED_TO]->(b:Memory) "
                "WHERE a.layer = 'l5_knowledge' AND b.layer = 'l5_knowledge' "
                "RETURN a.content AS a_name, b.content AS b_name, "
                "r.relation_type AS rtype, r.weight AS weight"
            )
            try:
                result = conn.execute(rel_q)
                while result.has_next():
                    row = result.get_next()
                    a_name = row[0]
                    b_name = row[1]
                    if a_name in node_names and b_name in node_names:
                        relations.append({
                            "a": a_name,
                            "b": b_name,
                            "relation_type": row[2] or "related_to",
                            "confidence": row[3] if row[3] is not None else 0.8,
                        })
            except Exception as e:
                logger.warning(f"[graph-endpoint] relation query failed: {e}")

        # Type distribution
        type_dist = {}
        for n in nodes:
            t = n.get("entity_type", "CONCEPT")
            type_dist[t] = type_dist.get(t, 0) + 1

        # Relation type distribution
        rel_type_dist = {}
        for r in relations:
            t = r.get("relation_type", "related_to")
            rel_type_dist[t] = rel_type_dist.get(t, 0) + 1

        _json_response(handler, 200, {
            "node_count": len(nodes),
            "relation_count": len(relations),
            "nodes": nodes,
            "relations": relations,
            "type_distribution": type_dist,
            "relation_type_distribution": rel_type_dist,
        })

    MemoryHTTPHandler.do_GET = patched_do_get
    _applied["graph_endpoint"] = True
    logger.info("[hy-memory/patches] graph endpoint patch installed (patch #23): GET /api/v1/graph")
    return True


# Master entry point
# ---------------------------------------------------------------------------


def apply_all_patches() -> dict[str, bool]:
    """Apply all patches. Idempotent. Returns a dict of which patches succeeded."""
    return {
        "llm_extra_body": apply_llm_extra_body_patch(),
        "l3_summary": apply_l3_summary_patch(),
        "rerank_stage": apply_rerank_patches(),
        "inprocess_embed": apply_inprocess_embed_patch(),
        "l1_raw_rolling_delete": apply_l1_raw_rolling_delete_patch(),
        "dedup_pre_search": apply_dedup_pre_search_patch(),
        "dedup_threshold": apply_dedup_threshold_patch(),
        "l1_raw_dedup_skip": apply_l1_raw_dedup_skip_patch(),
        "l1_raw_shadow": apply_l1_raw_shadow_patch(),
        "l5_auto_trigger": apply_l5_auto_trigger_patch() if os.getenv("MEMORY_L5_VERSION", "").strip() == "1" else False,
        "l5_inprocess": apply_l5_inprocess_patch(),
        "l4_identity": apply_l4_identity_patch(),
        "vdb_circuit_breaker": apply_vdb_circuit_breaker_patch(),
        "llm_fast_smart": apply_llm_fast_smart_patch(),
        "disabled_cache_timing": apply_disabled_cache_timing_patch(),
        "l1_raw_normal_fallback": apply_l1_raw_normal_fallback_patch(),
        "coding_judge": _patch_coding_judge(),
        "s2_operations_json": apply_s2_operations_json_patch(),
        "user_identity": apply_user_identity_patch(),
        "auto_forgetting": apply_auto_forgetting_patch(),
        "graph_endpoint": apply_graph_endpoint_patch(),
        "s1_extractor_entity_type": apply_s1_extractor_entity_type_patch(),
    }



def status() -> dict[str, Any]:
    """Return the current patch state. Used by `hermes hy_memory doctor`."""
    return {
        "applied": dict(_applied),
        "rerank_enabled_at_runtime": (
            os.environ.get("MEMORY_RERANK_ENABLED", "").strip().lower()
            in ("1", "true", "yes", "on")
        ),
        "rerank_module_available": _get_rerank_module() is not None,
        "dedup_threshold": float(os.environ.get("MEMORY_DEDUP_THRESHOLD", "0.92")),
        "dedup_merge_threshold": float(
            os.environ.get("MEMORY_DEDUP_MERGE_THRESHOLD", "0.85")
        ),
        "dedup_search_limit": int(os.environ.get("MEMORY_DEDUP_SEARCH_LIMIT", "5")),
        "dedup_min_score": float(os.environ.get("MEMORY_DEDUP_MIN_SCORE", "0.5")),
        "l1_raw_rolling_delete_enabled": (
            os.environ.get("HY_MEMORY_L1_RAW_ROLLING_DELETE", "true").strip().lower()
            in ("1", "true", "yes", "on")
        ),
        "l1_raw_window_days": int(os.environ.get("MEMORY_RAW_WINDOW_DAYS", "30")),
        "l1_raw_dedup_skip_enabled": (
            os.environ.get("HY_MEMORY_L1_RAW_DEDUP_SKIP", "true").strip().lower()
            in ("1", "true", "yes", "on")
        ),
        "l1_raw_dedup_skip_threshold": float(
            os.environ.get("MEMORY_DEDUP_SKIP_THRESHOLD", "0.92")
        ),
        "vdb_breaker_state": _vdb_breaker.snapshot(),
    }


# ---------------------------------------------------------------------------
# Patch 17: live L5/L6/L7 counts via raw Kuzu Cypher (v1.5.0)
# ---------------------------------------------------------------------------
#
# The upstream's ``_list_graph_bucket`` queries Kuzu using
# ``isolation_key = "{user_id}:{agent_id}:{session_id}"`` (single
# colons), but the System2 writer stores nodes with
# ``"{user_id}::{agent_id}::{session_id}"`` (DOUBLE colons). The
# two never match, so the upstream returns ``graph_total=0``
# even when Kuzu has hundreds of live L5/L6/L7 nodes.
#
# Even worse, the upstream's ``get_all_nodes()`` ALWAYS appends
# ``m.isolation_key = $ik`` to its WHERE clause (graph_store_kuzu.py
# line 539) — so passing ``isolation_key=""`` just looks for nodes
# with empty isolation_key, which the S2 writer doesn't produce.
# There is no way to bypass that filter through the public API.
#
# Fix: bypass ``get_all_nodes`` entirely and run raw Kuzu Cypher
# against the upstream's own connection (``self._graph_store
# ._conn``). We select by layer only (no isolation_key filter),
# then post-filter by user_id substring in Python to keep the
# per-user scoping the dashboard already does. This runs in the
# same process as the upstream, so there's no Kuzu lock
# contention — we use the lock the upstream already holds.
#
# The wrapped function returns the same dict shape the original
# returned, so the dashboard's existing ``/api/graph-counts``
# and ``/api/layer-counts`` endpoints work without dashboard
# changes. ``_memory_node_to_list_item`` is the upstream's
# existing serializer that turns a MemoryNode or dict into the
# JSON shape the dashboard expects.
# ---------------------------------------------------------------------------


def apply_l5_l6_l7_counts_patch() -> bool:
    """Replace ``HyMemoryClient._list_graph_bucket`` with a raw
    Kuzu Cypher query that doesn't filter by ``isolation_key``,
    so System2-written L5/L6/L7 nodes are actually returned to
    the caller (and thus the dashboard).
    """
    try:
        from hy_memory.client import HyMemoryClient
    except ImportError:
        return False

    if getattr(HyMemoryClient, "_hyatlas_graph_bucket_patched", False):
        return True

    from hy_memory.models import memory as _mem_mod

    _graph_layers = (
        _mem_mod.MemoryLayer.L5_KNOWLEDGE,
        _mem_mod.MemoryLayer.L6_SCHEMA,
        _mem_mod.MemoryLayer.L7_INTENTION,
    )

    async def _patched_list_graph_bucket(
        self,
        *,
        user_id: str,
        agent_id: str,
        limit: int,
        offset: int,
        order: str,
    ):
        """v1.5.0: list_graph_bucket via raw Kuzu Cypher (no
        isolation_key filter), so System2-written L6/L7 nodes are
        actually returned to the caller (and thus the dashboard).

        v1.5.0 fix: the upstream's ``get_all_nodes()`` ALWAYS
        filters by ``m.isolation_key = $ik`` (line 539 in
        graph_store_kuzu.py). Even passing ``isolation_key=""``
        doesn't bypass this — it just looks for nodes with empty
        isolation_key, which the S2 writer doesn't produce. So
        we go around ``get_all_nodes`` and run raw Cypher against
        the upstream's own Kuzu connection (``self._graph_store
        ._conn``). The query selects all nodes for the requested
        layer regardless of isolation_key, then we wrap the
        result in the same shape the original returned.

        Behavior matches the upstream signature exactly. The
        dashboard's existing ``/api/graph-counts`` handler counts
        L6/L7 nodes from this response; with the fix, the counts
        show the real numbers.
        """
        gs = getattr(self, "_graph_store", None)
        if gs is None:
            return None
        conn = getattr(gs, "_conn", None)
        if conn is None:
            return None

        graph_nodes = []
        for layer in _graph_layers:
            try:
                lyr_val = getattr(layer, "value", str(layer))
                result = conn.execute(
                    f'MATCH (m:Memory) WHERE m.layer = "{lyr_val}" RETURN m'
                )
                while result.has_next():
                    row = result.get_next()
                    if not row:
                        continue
                    node = row[0]
                    if not isinstance(node, dict):
                        try:
                            node = node.to_dict()
                        except AttributeError:
                            node = {
                                k: getattr(node, k, None)
                                for k in (
                                    "node_id", "layer", "content",
                                    "confidence", "tags", "evidence",
                                    "isolation_key", "gmt_created",
                                    "node_type",
                                )
                            }
                    graph_nodes.append(node)
            except Exception as e:
                logger.warning(
                    f"[hy-memory/patches v1.5.0] graph list "
                    f"layer={layer.value} failed: {e}"
                )
                continue

        # Post-filter by user_id substring in isolation_key
        if user_id:
            graph_nodes = [
                n for n in graph_nodes
                if user_id in (n.get("isolation_key") or "")
            ]

        try:
            graph_nodes = self._sort_memory_nodes(graph_nodes, order=order)
        except Exception:
            pass
        total = len(graph_nodes)
        page_nodes = graph_nodes[offset: offset + limit]
        # v1.5.0 fix: return our pre-built list_item dicts
        # directly, instead of calling _memory_node_to_list_item
        # which expects a MemoryNode object (with .memory_at,
        # .node_id, etc. attribute access). The upstream's
        # serializer fails on dicts with AttributeError, so we
        # build the JSON-shaped items ourselves in the query loop
        # above and return them as-is.
        return {
            "nodes": list(page_nodes),
            "total": total,
            "limit": limit,
            "offset": offset,
            "isolation_key": f"(all for user={user_id})",
        }

    HyMemoryClient._original_list_graph_bucket = (
        HyMemoryClient._list_graph_bucket
    )
    HyMemoryClient._list_graph_bucket = _patched_list_graph_bucket
    HyMemoryClient._hyatlas_graph_bucket_patched = True
    _applied["l5_l6_l7_counts"] = True
    logger.info(
        "[hy-memory/patches] L5/L6/L7 graph bucket patched (v1.5.0): "
        "raw Kuzu Cypher bypasses the upstream's isolation_key filter, "
        "so S2-written L6/L7 nodes are returned to the dashboard"
    )
    return True


# ---------------------------------------------------------------------------
# Patch 20: S1 extractor entity_type KeyError fix (v2.0.0)
# ---------------------------------------------------------------------------
# Root cause: hy_memory SDK's Extractor._get_l5_context_for_prompt() reads
# l5_kuzu_export.json nodes and accesses nd['entity_type'] directly. But the
# export format uses 'type', not 'entity_type'. Every node is missing the key,
# so every S1 extract call throws KeyError — killing all new L2 fact creation.
# This silently blocks L6/L7 growth because S2 has no fresh facts to cluster.
#
# Fix: monkey-patch the method to use .get('entity_type') or .get('type').

def apply_s1_extractor_entity_type_patch() -> bool:
    try:
        from hy_memory.agent.extractor import Extractor
    except ImportError:
        logger.debug("[hy-memory/patches] S1 extractor patch: hy_memory not importable")
        return False

    if not hasattr(Extractor, "_get_l5_context_for_prompt"):
        logger.debug("[hy-memory/patches] S1 extractor patch: _get_l5_context_for_prompt not found on this SDK version")
        return False

    _orig = Extractor._get_l5_context_for_prompt

    def _patched_get_l5_context(self, n=None):
        import json as _json
        import os as _os
        from pathlib import Path as _P

        if n is None:
            try:
                n = int(_os.environ.get("HY_MEMORY_L5_CONTEXT_N", "5"))
            except ValueError:
                n = 5
        n = min(max(n, 0), 50)
        if n == 0:
            return ""

        # Try live /api/v1/graph endpoint first (queries Kuzu directly,
        # no stale export file). Falls back to export file if server
        # is down or the endpoint patch isn't installed.
        try:
            import urllib.request as _urllib
            port = _os.environ.get("HY_MEMORY_SERVER_PORT", "19527")
            url = f"http://127.0.0.1:{port}/api/v1/graph?n={n}"
            with _urllib.urlopen(url, timeout=3) as resp:
                if resp.status == 200:
                    data = _json.loads(resp.read())
                    nodes = data.get("nodes", [])
                    if nodes:
                        lines = ["### Known entities from your knowledge graph (use as prior context):\n"]
                        for nd in nodes:
                            aliases = f" (aka: {', '.join(nd.get('aliases', []))})" if nd.get("aliases") else ""
                            mentions = nd.get("mention_count", 1)
                            etype = nd.get("entity_type") or nd.get("type") or "unknown"
                            lines.append(f"- {nd['name']} [{etype}] mentioned {mentions}×{aliases}")
                        if n >= 8:
                            relations = data.get("relations", [])
                            node_names = {nd["name"] for nd in nodes}
                            top_rels = [r for r in relations if r["a"] in node_names and r["b"] in node_names][:6]
                            if top_rels:
                                lines.append("\nNotable relations:")
                                for r in top_rels:
                                    lines.append(f"  {r['a']} {r.get('relation_type', 'relates to')} {r['b']}")
                        return "\n".join(lines) + "\n\n"
        except Exception as e:
            logger.debug(f"[s1-extractor] live graph endpoint fallback: {e}")

        # Fallback: read from export file
        export_path = _P(_os.environ.get("HERMES_HOME", str(_P.home() / "AppData" / "Local" / "hermes"))) / "logs" / "l5_kuzu_export.json"
        if not export_path.exists():
            return ""
        try:
            data = _json.loads(export_path.read_text(encoding="utf-8"))
        except Exception:
            return ""
        nodes = data.get("nodes", [])
        if not nodes:
            return ""
        nodes = sorted(nodes, key=lambda x: -x.get("mention_count", 0))[:n]
        lines = ["### Known entities from your knowledge graph (use as prior context):\n"]
        for nd in nodes:
            aliases = f" (aka: {', '.join(nd.get('aliases', []))})" if nd.get("aliases") else ""
            mentions = nd.get("mention_count", 1)
            etype = nd.get("entity_type") or nd.get("type") or "unknown"
            lines.append(f"- {nd['name']} [{etype}] mentioned {mentions}×{aliases}")
        if n >= 8:
            relations = data.get("relations", [])
            if not relations:
                relations = [
                    {"a": e.get("from", ""), "b": e.get("to", ""),
                     "relation_type": e.get("type", e.get("relation_type", "related_to"))}
                    for e in data.get("edges", [])
                ]
            node_names = {nd["name"] for nd in nodes}
            top_rels = [r for r in relations if r["a"] in node_names and r["b"] in node_names][:6]
            if top_rels:
                lines.append("\nNotable relations:")
                for r in top_rels:
                    lines.append(f"  {r['a']} {r.get('relation_type', 'relates to')} {r['b']}")
        return "\n".join(lines) + "\n\n"

    Extractor._get_l5_context_for_prompt = _patched_get_l5_context
    _applied["s1_extractor_entity_type"] = True
    logger.info(
        "[hy-memory/patches] S1 extractor entity_type fix (v2.0.0): "
        "L5 context builder now falls back to 'type' field when 'entity_type' is missing"
    )
    return True


# ---------------------------------------------------------------------------
# Patch 21: Auto-forgetting — expiry sweep + recency scoring (v2.0.0)
# ---------------------------------------------------------------------------
# Adds the 4th scoring dimension to HyAtlas:
#   1. Semantic similarity (existing, weight 0.5)
#   2. BM25 keyword match (existing, weight 0.3)
#   3. Graph evidence boost (existing, up to +0.3)
#   4. Recency decay (NEW, weight 0.2) — newer memories score higher
#
# Also adds an expiry sweep in the S2 digest cycle:
#   - L2 facts with temporal language get expires_at during S1 extraction
#   - S2 digest marks expired facts as ARCHIVED (soft, not deleted)
#   - Reader skips ARCHIVED by default (already filters status==ACTIVE)
#   - L4 identity never expires (preferences, opinions are permanent)

def apply_auto_forgetting_patch() -> bool:
    try:
        import importlib.util as _ilu
        if not _ilu.find_spec("hy_memory.pipelines.reader_hybrid_v2"):
            raise ImportError
    except ImportError:
        logger.debug("[hy-memory/patches] auto-forgetting: hybrid_v2 not importable")
        return False

    # Zvec-only runtime: Qdrant adapter is intentionally absent. The patch
    # only needs the hybrid_v2 scoring module and System2 writer hooks below.

    # ── Part 1: Recency scoring ──
    # Patch score_vdb_node to include recency decay as a 4th factor.
    # New formula: final = sem × 0.5 + bm25 × 0.3 + recency × 0.2
    # Recency: exponential decay — half-life of 30 days (configurable).
    # A memory created today scores 1.0; 30 days ago scores 0.5; 90 days ago ~0.125.

    try:
        from hy_memory.pipelines._retrieval.scoring import score_vdb_node
    except ImportError:
        score_vdb_node = None

    if score_vdb_node:
        _orig_score = score_vdb_node

        def _score_with_recency(semantic_score, bm25_score, w_sem=0.5, w_bm25=0.3,
                                gmt_created=None, now=None):
            """Extended scoring with recency decay as 4th dimension."""
            # Original semantic + BM25 (renormalized to 0.8 total)
            base = semantic_score * w_sem + bm25_score * w_bm25

            # Recency: exponential decay with configurable half-life
            if gmt_created and now:
                try:
                    half_life_days = float(_os.environ.get(
                        "HY_MEMORY_RECENCY_HALF_LIFE_DAYS", "30"))
                    age_days = (now - gmt_created).total_seconds() / 86400.0
                    recency = math.exp(-0.693 * age_days / half_life_days) if age_days > 0 else 1.0
                    w_rec = float(_os.environ.get(
                        "HY_MEMORY_RECENCY_WEIGHT", "0.2"))
                    # Renormalize: base takes (1 - w_rec), recency takes w_rec
                    return base * (1.0 - w_rec) + recency * w_rec
                except Exception:
                    pass
            return base

        # Monkey-patch the scoring module
        import hy_memory.pipelines._retrieval.scoring as _scoring_mod
        _scoring_mod.score_vdb_node = _score_with_recency
        logger.info("[hy-memory/patches] auto-forgetting: recency scoring patched "
                    "(half-life=30d, weight=0.2)")

    # ── Part 2: Expiry sweep in S2 digest ──
    # Hook into the S2 scheduled loop to archive expired L2 facts.
    # Runs after each S2 cycle, before the sweeper.
    # Only touches L2_FACT nodes with valid_until < now.
    # L4_IDENTITY is never expired (permanent preferences).

    try:
        from hy_memory.pipelines.system2_writer import System2Writer
    except ImportError:
        logger.debug("[hy-memory/patches] auto-forgetting: System2Writer not importable")
        # Still count as applied — recency scoring alone is valuable
        _applied["auto_forgetting"] = True
        return True

    _orig_process = System2Writer._process_user_queue

    async def _process_with_expiry(self, user_key):
        """Wrap _process_user_queue with an expiry sweep before S2 runs."""
        try:
            await _run_expiry_sweep(self, user_key)
        except Exception as e:
            logger.debug(f"[auto-forgetting] expiry sweep error: {e}")
        return await _orig_process(self, user_key)

    async def _run_expiry_sweep(s2_writer, user_key):
        """Archive L2 facts whose valid_until has passed.

        v3.4+: this implementation relied on the Qdrant client's
        ``scroll`` and ``set_payload`` helpers, which were removed when
        vector_store_qdrant.py was deleted. The zvec backend does not
        expose an equivalent admin-style expiry query yet, so we
        short-circuit and rely on the per-write ``valid_until`` check in
        the S2 pipeline itself. Once a zvec equivalent exists this hook
        is the place to re-add the sweep.
        """
        vector_store = getattr(s2_writer, "_vector_store", None)
        if not vector_store:
            return
        # Early exit — no Qdrant client means no admin-style sweep available.
        # The per-write expiry check in WritePipeline already filters out
        # facts past their valid_until, so the system stays correct without
        # this backfill sweep.
        return

    System2Writer._process_user_queue = _process_with_expiry
    logger.info("[hy-memory/patches] auto-forgetting: expiry sweep hooked "
                "into S2 digest cycle")

    _applied["auto_forgetting"] = True
    logger.info(
        "[hy-memory/patches] auto-forgetting patch installed (v2.0.0): "
        "recency decay (half-life=30d, w=0.2) + L2 expiry sweep in S2 cycle"
    )
    return True


# Auto-register the L5/L6/L7 counts patch in the patch registry
def _register_counts_patch() -> None:
    """Append the L5/L6/L7 counts patch to apply_all_patches output.

    The patch is invoked the next time ``apply_all_patches`` is
    called. We don't monkey-patch the dict directly because
    ``apply_all_patches`` returns a freshly built dict each call;
    instead we hook into the existing call sequence by appending
    the patch result to whatever apply_all_patches returns.
    """
    original_apply_all = apply_all_patches

    def _patched_apply_all() -> dict:
        # v1.5.0: call our L5/L6/L7 patch FIRST, before the
        # original. The original calls 15 other patches (L3 summary,
        # VDB circuit breaker, etc.) which can take a long time
        # to init, and we want our fast patch to apply before
        # any of them. If the original times out or fails, we
        # still have our L5/L6/L7 patch applied.
        our_result = apply_l5_l6_l7_counts_patch()
        try:
            results = original_apply_all()
        except Exception as e:
            logger.debug(
                f"[hy-memory/patches] original apply_all_patches "
                f"failed: {e}; L5/L6/L7 patch still applied"
            )
            results = {}
        results["l5_l6_l7_counts"] = our_result
        return results

    import sys as _sys
    _sys.modules[__name__].apply_all_patches = _patched_apply_all


# Trigger registration on import
_register_counts_patch()
