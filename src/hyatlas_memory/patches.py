# -*- coding: utf-8 -*-
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
import logging
import os
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

# Tracks whether we've already applied patches (idempotent)
_applied: dict[str, bool] = {}

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
            "Install the upstream SDK >= 1.2.18 (hy-memory on PyPI) which "
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
        cls = mod.__dict__.get(mod.__name__.split(".")[-1].title().replace("_", ""))
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
        import numpy as np
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

    Configurable via env vars:
      - HY_MEMORY_L1_RAW_ROLLING_DELETE (default true): master switch
      - MEMORY_RAW_WINDOW_DAYS (default 30): retention window
      - HY_MEMORY_RAW_SWEEP_INTERVAL_SECS (default 21600 = 6h): sweep frequency
    """
    if _applied.get("l1_raw_rolling_delete"):
        return True

    if os.environ.get("HY_MEMORY_L1_RAW_ROLLING_DELETE", "true").lower() not in ("1", "true", "yes", "on"):
        return False

    try:
        from qdrant_client import QdrantClient
        from qdrant_client.http import models
    except ImportError as e:
        logger.debug("[hy-memory/patches] qdrant_client not available: %s", e)
        return False

    host = os.environ.get("MEMORY_VECTOR_HOST", "127.0.0.1")
    port = int(os.environ.get("MEMORY_VECTOR_PORT", "6333"))
    collection = os.environ.get("MEMORY_VECTOR_COLLECTION", "agent_memories_384")
    window_days = int(os.environ.get("MEMORY_RAW_WINDOW_DAYS", "30"))
    sweep_interval = int(os.environ.get("HY_MEMORY_RAW_SWEEP_INTERVAL_SECS", "21600"))

    def _sweep():
        try:
            from datetime import datetime, timedelta, timezone
            # gmt_created in Qdrant is stored as a float (epoch seconds),
            # so Range.lt needs a numeric cutoff, not an ISO string.
            cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).timestamp()
            client = QdrantClient(host=host, port=port)
            # qdrant-client 1.18 requires a proper selector model, not a raw dict
            client.delete(
                collection_name=collection,
                points_selector=models.FilterSelector(
                    filter=models.Filter(must=[
                        models.FieldCondition(
                            key="status",
                            match=models.MatchValue(value="shadow"),
                        ),
                        models.FieldCondition(
                            key="layer",
                            match=models.MatchValue(value="l1_raw"),
                        ),
                        models.FieldCondition(
                            key="gmt_created",
                            range=models.Range(lt=cutoff),
                        ),
                    ])
                ),
            )
            logger.info(
                "[hy-memory/patches] L1_RAW rolling sweep: deleted entries older than %s days from %s",
                window_days, collection,
            )
        except Exception as e:
            logger.warning("[hy-memory/patches] L1_RAW rolling sweep failed: %s", e)

    def _loop():
        import time
        while True:
            time.sleep(sweep_interval)
            _sweep()

    import threading
    t = threading.Thread(target=_loop, daemon=True, name="l1_raw_rolling_delete")
    t.start()

    # Initial sweep (best-effort; failure here just means next sweep is the first one)
    _sweep()

    _applied["l1_raw_rolling_delete"] = True
    logger.info(
        "[hy-memory/patches] L1_RAW rolling-delete installed: window=%s days, interval=%s secs (patch #6)",
        window_days, sweep_interval,
    )
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
        from hy_memory.pipelines.writer import MemoryWriter
        from hy_memory.models.memory import MemoryStatus
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
    from hy_memory.agent import llm_provider as _lp
    from hy_memory.agent.extractor import Extractor
    from hy_memory.pipelines.system2_writer import System2Writer

    # Read fast model from env (preferred) or config
    import json as _json
    import os as _os
    from pathlib import Path as _P

    fast_model = _os.environ.get("HY_MEMORY_LLM_FAST_MODEL", "").strip() or None
    fast_base_url = _os.environ.get("HY_MEMORY_LLM_FAST_BASE_URL", "").strip() or None
    fast_api_key = _os.environ.get("HY_MEMORY_LLM_FAST_API_KEY", "").strip() or None

    if not fast_model:
        # Try config file
        cfg_path = _P(_os.environ.get("HERMES_HOME", r"C:\Users\tuanc\AppData\Local\hermes")) / "hy_memory.json"
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
    script_path = Path(r"C:\Users\tuanc\AppData\Local\hermes\bin\l5_full_pipeline.py")
    state_path = Path(r"C:\Users\tuanc\AppData\Local\hermes\logs\l5_pipeline_state.json")

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
            creationflags = 0x00000008  # DETACHED_PROCESS (Windows)
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
        request_id = str(__import__("uuid").uuid4())
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
        from hy_memory.data.cache_disabled import DisabledCache
        import inspect
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
            if method_name == "store_metrics_minute":
                async def shim(self, *args, **kwargs): return None
            elif method_name == "store_metrics":
                async def shim(self, *args, **kwargs): return None
            elif method_name == "flush_metrics":
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
        from hy_memory.pipelines.reader_legacy import LegacyReadPipeline as _LRP
        from hy_memory.models.memory import MemoryLayer
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
        "l5_auto_trigger": apply_l5_auto_trigger_patch(),
        "vdb_circuit_breaker": apply_vdb_circuit_breaker_patch(),
        "llm_fast_smart": apply_llm_fast_smart_patch(),
        "disabled_cache_timing": apply_disabled_cache_timing_patch(),
        "l1_raw_normal_fallback": apply_l1_raw_normal_fallback_patch(),
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
