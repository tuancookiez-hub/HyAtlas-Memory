"""First-class integrations for HyAtlas-Memory v3.0.0.

Replaces the former monkey-patch system (patches.py).
Each integration is wired directly into the source via __init__ hooks
or called explicitly during server startup.

Integrations:
  1. VDB circuit breaker (server resilience)
  2. L1_RAW rolling delete sweep (retention management)
  3. L1_RAW dedup skip (write-side dedup at source)
  4. L5 auto-trigger (L5 pipeline spawn from S2 digest)
  5. L5 in-process extraction (L5 as S2 peer step)
  6. Graph endpoint (/api/v1/graph for dashboard + S1)
  7. L5/L6/L7 counts (raw Kuzu Cypher bypass)
  8. S1 extractor L5 context (entity_type fallback + live endpoint)
  9. User identity (alias expansion for hybrid_v2)
  10. LLM fast/smart model split (cost optimization)
  11. DisabledCache kwargs tolerance (no-op cache compatibility)
  12. Rerank stage (cross-encoder opt-in)
  13. L1_RAW normal fallback (lite mode support)
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
import time
import urllib.request
from typing import Any
from urllib.parse import parse_qs, urlparse

logger = logging.getLogger(__name__)

# ─── 1. VDB Circuit Breaker ─────────────────────────────────────────────────


class CircuitBreaker:
    """Thread-safe circuit breaker for VDB (Qdrant) calls."""

    def __init__(self):
        self._state = "CLOSED"
        self._failures = 0
        self._last_failure_ts = 0.0
        self._lock = threading.Lock()
        self._threshold = int(os.environ.get("HY_MEMORY_BREAKER_THRESHOLD", "3"))
        self._reset_s = float(os.environ.get("HY_MEMORY_BREAKER_RESET_S", "30"))

    @property
    def state(self):
        with self._lock:
            return self._state

    def allow(self) -> bool:
        with self._lock:
            if self._state == "OPEN":
                if time.time() - self._last_failure_ts >= self._reset_s:
                    self._state = "HALF_OPEN"
                    logger.info("[breaker] OPEN → HALF_OPEN (probing)")
                    return True
                return False
            return True

    def record_success(self):
        with self._lock:
            if self._state != "CLOSED":
                logger.info(f"[breaker] {self._state} → CLOSED (recovered)")
            self._state = "CLOSED"
            self._failures = 0

    def record_failure(self):
        with self._lock:
            self._failures += 1
            self._last_failure_ts = time.time()
            if self._state == "HALF_OPEN":
                self._state = "OPEN"
                logger.warning("[breaker] HALF_OPEN → OPEN (probe failed)")
            elif self._failures >= self._threshold and self._state == "CLOSED":
                self._state = "OPEN"
                logger.warning(f"[breaker] CLOSED → OPEN after {self._failures} failures")

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            reset_in = 0.0
            if self._state == "OPEN":
                elapsed = time.time() - self._last_failure_ts
                reset_in = max(0.0, self._reset_s - elapsed)
            return {
                "state": self._state,
                "failures": self._failures,
                "threshold": self._threshold,
                "reset_timeout_s": self._reset_s,
                "reset_in_s": round(reset_in, 1),
            }


breaker = CircuitBreaker()


def wire_circuit_breaker(handler_cls, json_resp_fn):
    """Wrap _handle_add and _handle_search with circuit breaker protection."""
    orig_add = handler_cls._handle_add

    def patched_add(self, body):
        if not breaker.allow():
            snap = breaker.snapshot()
            json_resp_fn(self, 503, {
                "error": "vdb_unavailable",
                "detail": "circuit breaker OPEN — Qdrant has been failing",
                "retry_after_s": int(snap["reset_in_s"]) + 1,
                "breaker": snap,
            })
            return
        try:
            orig_add(self, body)
            breaker.record_success()
        except Exception as e:
            breaker.record_failure()
            logger.exception("[breaker] _handle_add failed: %s", e)
            try:
                json_resp_fn(self, 503, {
                    "error": "vdb_error",
                    "detail": str(e)[:200],
                    "breaker": breaker.snapshot(),
                })
            except Exception:
                logger.error("[breaker] failed to send 503; thread continuing")

    handler_cls._handle_add = patched_add

    orig_search = handler_cls._handle_search

    def patched_search(self, body):
        if not breaker.allow():
            json_resp_fn(self, 200, {
                "memories": {"profile": [], "proactive": [], "normal": [], "system": [], "recent": []},
                "degraded": True,
                "detail": "vdb_unavailable",
            })
            return
        try:
            orig_search(self, body)
            breaker.record_success()
        except Exception as e:
            breaker.record_failure()
            logger.exception("[breaker] _handle_search failed: %s", e)
            try:
                json_resp_fn(self, 200, {
                    "memories": {"profile": [], "proactive": [], "normal": [], "system": [], "recent": []},
                    "degraded": True,
                    "error": "vdb_error",
                    "detail": str(e)[:200],
                })
            except Exception:
                logger.error("[breaker] failed to send degraded response; thread continuing")

    handler_cls._handle_search = patched_search
    logger.info("[integrations] circuit breaker wired on _handle_add + _handle_search")


# ─── 2. L1_RAW Rolling Delete Sweep ──────────────────────────────────────────

_l1_sweep_thread: threading.Thread | None = None


def start_l1_raw_sweep(vector_store=None):
    """Start daemon thread for periodic L1_RAW shadow cleanup.

    If `vector_store` (the live ZvecVectorStore / Qdrant store) is passed,
    the zvec sweep reuses it instead of opening a second handle (which would
    collide with the server's lock).
    """
    global _l1_sweep_thread
    if _l1_sweep_thread is not None:
        return
    if os.environ.get("HY_MEMORY_L1_RAW_ROLLING_DELETE", "true").lower() not in ("1", "true", "yes", "on"):
        return

    window_days = int(os.environ.get("MEMORY_RAW_WINDOW_DAYS", "30"))
    sweep_interval = int(os.environ.get("HY_MEMORY_RAW_SWEEP_INTERVAL_SECS", "21600"))

    def _sweep():
        try:
            from datetime import datetime, timedelta, timezone

            from . import layout

            provider = (layout.read_config() or {}).get("vector_store", {}).get("provider", "qdrant")
            cutoff = (datetime.now(timezone.utc) - timedelta(days=window_days)).timestamp()

            if provider == "zvec":
                _sweep_zvec(cutoff, vector_store=vector_store)
                return

            from qdrant_client import QdrantClient
            from qdrant_client.http import models

            host = os.environ.get("MEMORY_VECTOR_HOST", "127.0.0.1")
            port = int(os.environ.get("MEMORY_VECTOR_PORT", "6333"))
            collection = os.environ.get("MEMORY_VECTOR_COLLECTION", "agent_memories_1024")
            client = QdrantClient(host=host, port=port)
            client.delete(
                collection_name=collection,
                points_selector=models.FilterSelector(
                    filter=models.Filter(must=[
                        models.FieldCondition(key="status", match=models.MatchValue(value="shadow")),
                        models.FieldCondition(key="layer", match=models.MatchValue(value="l1_raw")),
                        models.FieldCondition(key="gmt_created", range=models.Range(lt=cutoff)),
                    ])
                ),
            )
            logger.info(f"[l1-sweep] deleted shadowed L1_RAW older than {window_days} days")
        except Exception as e:
            logger.warning(f"[l1-sweep] failed: {e}")


    def _sweep_zvec(cutoff: float, vector_store=None):
        """Delete shadowed L1_RAW on a zvec store.

        Uses the live `vector_store` handle when provided (the server's own
        collection) to avoid a second open that would collide on the lock.
        zvec stores gmt_created as ISO strings, so the time window is applied
        by reading the field and comparing after parse (best-effort retention).
        """
        if vector_store is None:
            logger.debug("[l1-sweep] zvec: no live vector store handle; skipping")
            return

        from datetime import datetime

        from .core.data.vector_store_zvec import _quote
        from .core.models.memory import MemoryLayer, MemoryStatus

        try:
            loop = getattr(vector_store, "_loop_thread", None)

            async def _do():
                vs = vector_store
                docs = await vs.search(
                    query_embedding=[0.0] * (vs.config.vector_store.embedding_dims or 1024),
                    layers=[MemoryLayer.L1_RAW],
                    status_filter=[MemoryStatus.SHADOW],
                    limit=100000,
                    only_latest=False,
                )
                killed = 0
                for item in docs:
                    gc = item.get("gmt_created")
                    if gc is not None:
                        try:
                            ts = gc.timestamp() if hasattr(gc, "timestamp") else datetime.fromisoformat(str(gc)).timestamp()
                            if ts >= cutoff:
                                continue
                        except (ValueError, TypeError, AttributeError):
                            pass
                    nid = item.get("node_id")
                    if nid:
                        await vs.delete_by_filter(f"node_id = {_quote(nid)}")
                        killed += 1
                if killed:
                    logger.info(f"[l1-sweep] zvec deleted {killed} shadowed L1_RAW older than {window_days} days")
                return killed

            if loop is not None:
                import asyncio
                asyncio.run_coroutine_threadsafe(_do(), loop).result(timeout=60)
            else:
                import asyncio
                asyncio.run(_do())
        except Exception as e:
            logger.warning(f"[l1-sweep] zvec failed: {e}")

    def _loop():
        while True:
            time.sleep(sweep_interval)
            _sweep()

    _l1_sweep_thread = threading.Thread(target=_loop, daemon=True, name="l1_raw_sweep")
    _l1_sweep_thread.start()
    _sweep()  # initial sweep
    logger.info(f"[integrations] L1_RAW sweep started: window={window_days}d, interval={sweep_interval}s")


# ─── 3. L1_RAW Dedup Skip ────────────────────────────────────────────────────

def _extract_content(data) -> str:
    if isinstance(data, str):
        return data
    if isinstance(data, list):
        for m in reversed(data):
            if isinstance(m, dict) and m.get("role") == "assistant":
                c = m.get("content", "")
                if isinstance(c, str) and c.strip():
                    return c
        if data and isinstance(data[0], dict):
            c = data[0].get("content", "")
            if isinstance(c, str):
                return c
    return ""


def wire_l1_dedup_skip(client_cls):
    """Skip writes when a near-duplicate already exists (prevents L1_RAW bloat)."""
    if os.environ.get("HY_MEMORY_L1_RAW_DEDUP_SKIP", "true").lower() not in ("1", "true", "yes", "on"):
        return
    if getattr(client_cls, "_l1_dedup_skip_wired", False):
        return

    skip_threshold = float(os.environ.get("MEMORY_DEDUP_SKIP_THRESHOLD", "0.85"))
    orig = client_cls.async_add

    async def patched(self, data, **kw):
        uid = kw.get("user_id", "")
        aid = kw.get("agent_id", "default_agent")
        sid = kw.get("session_id", "default_session")
        content = _extract_content(data)

        if content and len(content) >= 20:
            try:
                result = await self.async_search(
                    content[:500],
                    user_ids=[uid] if uid else None,
                    agent_ids=[aid] if aid else None,
                    session_ids=[sid] if sid else None,
                    limit=3,
                    min_score=max(0.5, skip_threshold - 0.1),
                )
                for cat in ("profile", "proactive", "normal"):
                    items = (result.get("memories") or {}).get(cat, []) or []
                    if items:
                        top = items[0]
                        if top.get("score", 0) >= skip_threshold:
                            logger.info(f"[l1-dedup-skip] score={top.get('score', 0):.3f} >= {skip_threshold}")
                            return {
                                "success": True, "memory_id": top.get("memory_id", ""),
                                "request_id": "", "elapsed_ms": 0,
                                "error_code": None, "error_message": None,
                                "skipped": True, "skip_reason": f"duplicate_score_{top.get('score', 0):.3f}",
                                "timing": {},
                            }
            except Exception as e:
                logger.debug(f"[l1-dedup-skip] pre-search failed (no-op): {e}")

        return await orig(self, data, **kw)

    client_cls.async_add = patched
    client_cls._l1_dedup_skip_wired = True
    logger.info(f"[integrations] L1_RAW dedup skip wired: threshold={skip_threshold}")


# ─── 4. L5 Auto-Trigger ──────────────────────────────────────────────────────

def wire_l5_auto_trigger(s2_cls):
    """Spawn L5 pipeline from S2 digest cycle (debounced).

    No-op when the in-process L5 path (MEMORY_L5_VERSION=2) is active: that path
    already extracts the knowledge graph inside digest(), so spawning the
    stop-server batch pipeline is redundant and — under `hyatlas start` — always
    fails to stop the server.
    """
    if os.getenv("MEMORY_L5_VERSION", "").strip() == "2":
        logger.info("[integrations] L5 auto-trigger disabled (in-process L5 v2 active)")
        return
    if getattr(s2_cls, "_l5_auto_trigger_wrapped", False):
        return

    l5_auto = os.getenv("MEMORY_L5_AUTO", "true").lower() == "true"
    min_interval_h = float(os.getenv("MEMORY_L5_MIN_INTERVAL_HOURS", "12"))

    from pathlib import Path
    home = os.environ.get("HERMES_HOME", str(Path.home() / "AppData" / "Local" / "hermes"))
    script_path = Path(home) / "bin" / "l5_full_pipeline.py"
    state_path = Path(home) / "logs" / "l5_pipeline_state.json"

    if not script_path.exists():
        logger.warning(f"[l5-auto] script not found at {script_path}")
        return

    def _should_trigger():
        if not l5_auto:
            return {"enabled": False, "triggered": False, "reason": "MEMORY_L5_AUTO is false"}
        if state_path.exists():
            try:
                state = json.loads(state_path.read_text(encoding="utf-8"))
                last = state.get("last_run_at")
                if last:
                    from datetime import datetime
                    age_h = (datetime.now() - datetime.fromisoformat(last)).total_seconds() / 3600
                    if age_h < min_interval_h:
                        return {"enabled": True, "triggered": False, "reason": f"debounced: {age_h:.1f}h ago"}
            except Exception as e:
                logger.warning(f"[l5-auto] state read failed: {e}")
        try:
            import subprocess
            import sys
            flags = 0x00000008  # DETACHED_PROCESS (Windows)
            proc = subprocess.Popen(
                [sys.executable, str(script_path)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL,
                creationflags=flags,
            )
            return {"enabled": True, "triggered": True, "pid": proc.pid, "reason": f"spawned pid={proc.pid}"}
        except Exception as e:
            return {"enabled": True, "triggered": False, "reason": f"spawn failed: {e}"}

    if not hasattr(s2_cls, "_original_digest"):
        s2_cls._original_digest = s2_cls.digest

    async def _digest_with_l5(self, user_id, agent_id="default"):
        result = await self._original_digest(user_id=user_id, agent_id=agent_id)
        result["l5_trigger"] = _should_trigger()
        if result["l5_trigger"]["triggered"]:
            logger.info(f"[l5-auto] trigger fired from digest(): {result['l5_trigger']['reason']}")
        return result

    s2_cls.digest = _digest_with_l5
    s2_cls._l5_auto_trigger_wrapped = True
    logger.info(f"[integrations] L5 auto-trigger wired: auto={l5_auto}, interval={min_interval_h}h")


# ─── 5. L5 In-Process Extraction ─────────────────────────────────────────────

def wire_l5_inprocess(s2_cls):
    """Hook L5 entity extraction into S2's sweeper cycle (in-process).

    Enabled by default post-v3.1.0 (zvec-only runtime). The in-process path
    reads L2 facts from the live zvec store and writes entities/relations to
    Kuzu during digest. Only disabled when MEMORY_L5_VERSION is explicitly set
    to "1" (legacy stop-server batch) or "off"/"false".
    """
    version = os.getenv("MEMORY_L5_VERSION", "").strip().lower()
    if version in ("1", "off", "false", "0"):
        logger.info(f"[integrations] L5 in-process disabled (MEMORY_L5_VERSION={version!r})")
        return
    if getattr(s2_cls, "_l5_inprocess_wrapped", False):
        return

    orig = s2_cls._run_cross_domain_sweeper

    async def _sweeper_with_l5(self, user_id, agent_id, llm_call, request_id):
        result = await orig(self, user_id, agent_id, llm_call, request_id)
        try:
            from hyatlas_memory.l5_inprocess import run_l5_inprocess
            l5_result = await run_l5_inprocess(
                s2_writer=self, user_id=user_id, agent_id=agent_id,
                llm_call=llm_call, request_id=request_id,
            )
            if isinstance(result, dict):
                result["l5_inprocess"] = l5_result
        except Exception as e:
            logger.warning(f"[l5-inprocess] failed (non-blocking): {e}")
        return result

    s2_cls._run_cross_domain_sweeper = _sweeper_with_l5
    s2_cls._l5_inprocess_wrapped = True
    logger.info("[integrations] L5 in-process extraction wired")


# ─── 6. Graph Endpoint (/api/v1/graph) ───────────────────────────────────────

def wire_graph_endpoint(handler_cls, json_resp_fn, get_client_fn):
    """Add GET /api/v1/graph endpoint for dashboard + S1 context."""
    orig_do_get = handler_cls.do_GET

    def patched_do_get(self):
        try:
            raw = self.path.split("?")[0].rstrip("/")
            if raw == "/api/v1/graph":
                _handle_graph(self, json_resp_fn, get_client_fn)
                return
            if raw == "/api/v1/breaker":
                json_resp_fn(self, 200, breaker.snapshot())
                return
        except Exception as e:
            logger.warning(f"[graph-endpoint] dispatch error, falling through: {e}")
        return orig_do_get(self)

    def _handle_graph(handler, json_resp, get_client):
        qs = parse_qs(urlparse(handler.path).query)
        try:
            max_n = min(max(int(qs.get("n", ["0"])[0]), 0), 5000)
        except (ValueError, TypeError):
            json_resp(handler, 400, {"error": "n must be an integer"})
            return
        etype = qs.get("type", [None])[0]
        search = qs.get("q", [None])[0]
        layer = (qs.get("layer", ["l5_knowledge"])[0] or "l5_knowledge").strip()
        if layer not in ("l5_knowledge", "l6_schema", "l7_intention"):
            json_resp(handler, 400, {"error": "layer must be l5_knowledge, l6_schema, or l7_intention"})
            return
        include_rels = qs.get("rels", ["true"])[0].lower() not in ("false", "0", "no")

        client = get_client()
        gs = getattr(client, "_graph_store", None)
        if gs is None or not getattr(gs, "_available", False):
            json_resp(handler, 503, {"error": "graph_store unavailable"})
            return
        conn = getattr(gs, "_conn", None)
        if conn is None:
            json_resp(handler, 503, {"error": "kuzu connection not initialized"})
            return

        nodes = []
        node_q = (
            f"MATCH (m:Memory) WHERE m.layer = '{layer}' "
            "RETURN m.node_id AS id, m.content AS name, m.content_type AS ct, "
            "m.confidence AS conf, m.extra_json AS extra, m.custom_json AS cust, "
            "m.created_at AS ca"
        )

        def _merge_meta(extra_raw, cust_raw) -> dict:
            meta: dict = {}
            for raw in (cust_raw, extra_raw):
                if not raw:
                    continue
                try:
                    blob = json.loads(raw) if isinstance(raw, str) else {}
                    if isinstance(blob, dict):
                        meta.update(blob)
                except (ValueError, TypeError):
                    pass
            return meta

        try:
            result = conn.execute(node_q)
            while result.has_next():
                row = result.get_next()
                extra = _merge_meta(row[4], row[5])
                ct = row[2] or extra.get("content_type", "")
                entity_type = extra.get("entity_type") or extra.get("entityType")
                if not entity_type and isinstance(ct, str) and ct.startswith("ENTITY_"):
                    entity_type = ct.replace("ENTITY_", "", 1)
                nodes.append({
                    "node_id": row[0], "name": row[1], "content_type": row[2],
                    "layer": layer,
                    "confidence": row[3],
                    "entity_type": entity_type or "CONCEPT",
                    "mention_count": extra.get("mention_count", 1),
                    "aliases": extra.get("aliases", []),
                    "source": extra.get("source", "l5_digest"),
                    "created_at": str(row[6]) if row[6] is not None else None,
                })
        except Exception as e:
            json_resp(handler, 500, {"error": f"node query failed: {e}"})
            return

        if etype:
            nodes = [n for n in nodes if n.get("entity_type") == etype]
        if search:
            sl = search.lower()
            nodes = [n for n in nodes if sl in n["name"].lower()
                     or any(sl in a.lower() for a in n.get("aliases", []))]
        nodes = sorted(nodes, key=lambda n: -n.get("mention_count", 0))
        if max_n > 0:
            nodes = nodes[:max_n]

        relations = []
        if include_rels and nodes and layer == "l5_knowledge":
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
                    a_name, b_name = row[0], row[1]
                    if a_name in node_names and b_name in node_names:
                        relations.append({
                            "a": a_name, "b": b_name,
                            "relation_type": row[2] or "related_to",
                            "confidence": row[3] if row[3] is not None else 0.8,
                        })
            except Exception as e:
                logger.warning(f"[graph-endpoint] relation query failed: {e}")

        type_dist: dict[str, int] = {}
        for n in nodes:
            t = n.get("entity_type", "CONCEPT")
            type_dist[t] = type_dist.get(t, 0) + 1
        rel_type_dist: dict[str, int] = {}
        for r in relations:
            t = r.get("relation_type", "related_to")
            rel_type_dist[t] = rel_type_dist.get(t, 0) + 1

        layer_counts: dict[str, int] = {}
        for layer in ("l5_knowledge", "l6_schema", "l7_intention"):
            try:
                cq = (
                    f"MATCH (m:Memory) WHERE m.layer = '{layer}' "
                    "RETURN count(m) AS c"
                )
                cres = conn.execute(cq)
                if cres.has_next():
                    layer_counts[layer] = int(cres.get_next()[0])
            except Exception as e:
                logger.warning(f"[graph-endpoint] layer count {layer} failed: {e}")

        json_resp(handler, 200, {
            "node_count": len(nodes), "relation_count": len(relations),
            "nodes": nodes, "relations": relations,
            "type_distribution": type_dist, "relation_type_distribution": rel_type_dist,
            "layer_counts": layer_counts,
            "graph_db_path": getattr(gs, "_db_path", None),
        })

    handler_cls.do_GET = patched_do_get
    logger.info("[integrations] graph endpoint wired: GET /api/v1/graph + /api/v1/breaker")


# ─── 6b. VDB dashboard helpers (zvec-safe, no second DB open) ───────────────

def wire_vdb_dashboard(handler_cls, json_resp_fn, get_client_fn):
    """Expose layer counts and scroll via the in-process vector store."""
    from . import vdb_dashboard

    orig_get = handler_cls.do_GET
    orig_post = handler_cls.do_POST

    def patched_do_get(self):
        try:
            raw = self.path.split("?")[0].rstrip("/")
            if raw == "/api/v1/vdb/layer_count":
                qs = parse_qs(urlparse(self.path).query)
                layer = (qs.get("layer") or [""])[0]
                if not layer:
                    json_resp_fn(self, 400, {"error": "layer is required"})
                    return
                latest = (qs.get("require_is_latest") or ["true"])[0].lower() not in (
                    "false",
                    "0",
                    "no",
                )
                client = get_client_fn()
                n = vdb_dashboard.layer_count(client, layer, require_is_latest=latest)
                json_resp_fn(self, 200, {"count": n, "layer": layer})
                return
        except Exception as e:
            logger.warning("[vdb-dashboard] GET dispatch error: %s", e)
        return orig_get(self)

    def patched_do_post(self):
        path = self.path.split("?")[0].rstrip("/")
        if path == "/api/v1/vdb/scroll":
            try:
                from .core.server import _read_json_body

                body = _read_json_body(self)
                if body is None:
                    json_resp_fn(self, 400, {"error": "invalid JSON body"})
                    return
                mode = body.get("mode", "")
                client = get_client_fn()
                if mode == "l1_raw":
                    uids = body.get("user_ids") or []
                    limit = int(body.get("limit") or 1500)
                    items = vdb_dashboard.scroll_l1(client, uids, limit=limit)
                    json_resp_fn(self, 200, {"items": items})
                    return
                if mode == "payload_by_ids":
                    ids = body.get("memory_ids") or []
                    by_id = vdb_dashboard.payload_by_ids(client, ids)
                    json_resp_fn(self, 200, {"payloads": by_id})
                    return
                json_resp_fn(self, 400, {"error": "unknown mode"})
                return
            except Exception as e:
                logger.warning("[vdb-dashboard] POST scroll error: %s", e)
                json_resp_fn(self, 500, {"error": str(e)})
                return
        return orig_post(self)

    handler_cls.do_GET = patched_do_get
    handler_cls.do_POST = patched_do_post
    logger.info(
        "[integrations] vdb dashboard wired: GET /api/v1/vdb/layer_count, "
        "POST /api/v1/vdb/scroll"
    )


# ─── 7. L5/L6/L7 Counts (raw Kuzu Cypher bypass) ─────────────────────────────

def wire_graph_counts(client_cls):
    """Replace _list_graph_bucket with raw Kuzu Cypher (bypasses isolation_key filter)."""
    if getattr(client_cls, "_hyatlas_graph_bucket_patched", False):
        return

    from hyatlas_memory.core.models import memory as _mem
    _graph_layers = (
        _mem.MemoryLayer.L5_KNOWLEDGE,
        _mem.MemoryLayer.L6_SCHEMA,
        _mem.MemoryLayer.L7_INTENTION,
    )

    async def _patched_list(self, *, user_id, agent_id, limit, offset, order):
        gs = getattr(self, "_graph_store", None)
        if gs is None:
            return None
        conn = getattr(gs, "_conn", None)
        if conn is None:
            return None

        graph_nodes = []
        for layer in _graph_layers:
            try:
                lyr = getattr(layer, "value", str(layer))
                result = conn.execute(f'MATCH (m:Memory) WHERE m.layer = "{lyr}" RETURN m')
                while result.has_next():
                    row = result.get_next()
                    if not row:
                        continue
                    node = row[0]
                    if not isinstance(node, dict):
                        try:
                            node = node.to_dict()
                        except AttributeError:
                            node = {k: getattr(node, k, None) for k in (
                                "node_id", "layer", "content", "confidence",
                                "tags", "evidence", "isolation_key", "gmt_created", "node_type",
                            )}
                    graph_nodes.append(node)
            except Exception as e:
                logger.warning(f"[graph-counts] layer={layer.value} failed: {e}")
                continue

        if user_id:
            graph_nodes = [n for n in graph_nodes if user_id in (n.get("isolation_key") or "")]
        with contextlib.suppress(Exception):
            graph_nodes = self._sort_memory_nodes(graph_nodes, order=order)
        total = len(graph_nodes)
        page = graph_nodes[offset: offset + limit]
        return {"nodes": list(page), "total": total, "limit": limit, "offset": offset,
                "isolation_key": f"(all for user={user_id})"}

    client_cls._original_list_graph_bucket = client_cls._list_graph_bucket
    client_cls._list_graph_bucket = _patched_list
    client_cls._hyatlas_graph_bucket_patched = True
    logger.info("[integrations] L5/L6/L7 graph counts wired (raw Kuzu Cypher)")


# ─── 8. S1 Extractor L5 Context ──────────────────────────────────────────────

def wire_s1_l5_context(extractor_cls):
    """Add _get_l5_context_for_prompt to Extractor (entity_type fallback + live endpoint)."""
    if hasattr(extractor_cls, "_get_l5_context_for_prompt"):
        return  # already exists

    def _get_l5_context(self, n=None):
        if n is None:
            try:
                n = int(os.environ.get("HY_MEMORY_L5_CONTEXT_N", "5"))
            except ValueError:
                n = 5
        n = min(max(n, 0), 50)
        if n == 0:
            return ""

        # Try live /api/v1/graph endpoint first
        try:
            port = os.environ.get("HY_MEMORY_SERVER_PORT", "19527")
            url = f"http://127.0.0.1:{port}/api/v1/graph?n={n}"
            with urllib.request.urlopen(url, timeout=3) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read())
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
            logger.debug(f"[s1-l5] live graph endpoint fallback: {e}")

        # Fallback: read from export file
        from pathlib import Path
        export_path = Path(os.environ.get("HERMES_HOME", str(Path.home() / "AppData" / "Local" / "hermes"))) / "logs" / "l5_kuzu_export.json"
        if not export_path.exists():
            return ""
        try:
            data = json.loads(export_path.read_text(encoding="utf-8"))
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
        return "\n".join(lines) + "\n\n"

    extractor_cls._get_l5_context_for_prompt = _get_l5_context
    logger.info("[integrations] S1 extractor L5 context wired")


# ─── 9. User Identity (alias expansion) ──────────────────────────────────────

_DEFAULT_ALIASES = "221727702992945152,hermes-user,system:handoff"


def wire_user_identity(reader_cls):
    """Expand user_id matching across aliases for hybrid_v2 reader."""
    if os.environ.get("HYATLAS_USER_IDENTITY", "").strip().lower() not in ("1", "true", "yes", "on"):
        return
    if getattr(reader_cls, "_hyatlas_user_identity_patched", False):
        return

    raw = os.environ.get("HYATLAS_USER_ALIASES", _DEFAULT_ALIASES).strip()
    alias_pool = [p.strip() for p in raw.split(",") if p.strip()]

    orig = reader_cls._build_isolation_params  # noqa: F841 — kept for rollback

    def _patched(self, request):
        user_ids = request.user_ids if request.user_ids else ([request.user_id] if request.user_id else [])
        expanded = set(alias_pool)
        for uid in user_ids:
            if uid:
                expanded.add(uid)
                if uid in alias_pool:
                    expanded.update(alias_pool)
        return {
            "isolation_key": "", "isolation_keys": None,
            "user_ids": sorted(expanded) if expanded else None,
            "agent_ids": ["default"],
        }

    reader_cls._build_isolation_params = _patched
    reader_cls._hyatlas_user_identity_patched = True
    logger.info(f"[integrations] user identity wired: aliases={', '.join(alias_pool)}")


# ─── 10. LLM Fast/Smart Model Split ──────────────────────────────────────────

def wire_llm_fast_smart(extractor_cls, s2_cls):
    """Use cheaper model for S1 extraction, smarter model for S2/L5."""
    import contextlib
    fast_model = os.environ.get("HY_MEMORY_LLM_FAST_MODEL", "").strip()
    fast_base = os.environ.get("HY_MEMORY_LLM_FAST_BASE_URL", "").strip()
    fast_key = os.environ.get("HY_MEMORY_LLM_FAST_API_KEY", "").strip()

    if not fast_model:
        from pathlib import Path
        cfg_path = Path(os.environ.get("HERMES_HOME", str(Path.home() / "AppData" / "Local" / "hermes"))) / "hy_memory.json"
        if cfg_path.exists():
            try:
                cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
                llm = cfg.get("llm", {})
                fast_model = llm.get("fast_model") or ""
                fast_base = fast_base or llm.get("fast_base_url") or ""
                fast_key = fast_key or llm.get("fast_api_key") or ""
            except Exception:
                pass

    if not fast_model:
        logger.info("[integrations] LLM fast/smart: no fast_model configured (no-op)")
        return

    fast_extra = {}
    if "fast_extra_body" in (cfg.get("llm", {}) if 'cfg' in dir() else {}):
        fast_extra = cfg.get("llm", {}).get("fast_extra_body", {})

    @contextlib.contextmanager
    def use_fast(provider):
        if provider is None or not hasattr(provider, "_llm_config"):
            yield
            return
        c = provider._llm_config
        saved = (c.model, c.base_url, c.api_key, c.extra_body)
        try:
            c.model = fast_model
            if fast_base:
                c.base_url = fast_base
            if fast_key:
                c.api_key = fast_key
            c.extra_body = fast_extra
            yield
        finally:
            c.model, c.base_url, c.api_key, c.extra_body = saved

    orig_extract = extractor_cls.extract

    async def _extract_with_fast(self, *args, **kwargs):
        provider = getattr(self, "llm", None) or getattr(self, "_llm", None)
        with use_fast(provider):
            return await orig_extract(self, *args, **kwargs)

    extractor_cls.extract = _extract_with_fast
    logger.info(f"[integrations] LLM fast/smart wired: fast={fast_model}")


# ─── 11. DisabledCache kwargs tolerance ──────────────────────────────────────

def wire_disabled_cache_tolerance(cache_cls):
    """Make DisabledCache accept any kwargs (no-op cache compatibility)."""
    import inspect

    methods = (
        "update_task_status", "store_pipeline_log", "store_write_record",
        "store_memory_operation", "enqueue_system2_task", "update_profile_cache",
        "store_metrics_minute", "store_metrics", "flush_metrics",
    )

    for name in methods:
        orig = getattr(cache_cls, name, None)
        if orig is None:
            if name in ("store_metrics_minute", "store_metrics", "flush_metrics"):
                async def shim(self, *a, **kw): return None
                shim.__name__ = name
                setattr(cache_cls, name, shim)
                orig = shim
            else:
                continue

        try:
            sig = inspect.signature(orig)
        except (ValueError, TypeError):
            continue

        required = [
            p for p in sig.parameters.values()
            if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.POSITIONAL_ONLY)
            and p.default is inspect.Parameter.empty and p.name != "self"
        ]
        accepted = {p.name for p in sig.parameters.values()
                    if p.kind in (inspect.Parameter.POSITIONAL_OR_KEYWORD, inspect.Parameter.KEYWORD_ONLY)}

        def _default(p):
            ann = str(p.annotation).lower() if p.annotation is not inspect.Parameter.empty else ""
            if "dict" in ann or "mapping" in ann:
                return {}
            if "list" in ann or "sequence" in ann:
                return []
            if "int" in ann or "float" in ann:
                return 0
            if "bool" in ann:
                return False
            return ""

        defaults = [_default(p) for p in required]

        def make(name, orig, required, accepted, defaults):
            def wrapped(self, *args, **kwargs):
                new_args = list(args)
                new_kwargs = dict(kwargs)
                pos_names = {p for i, p in enumerate(required) if i < len(new_args)}
                for i, pn in enumerate(required):
                    if pn in pos_names:
                        continue
                    if i < len(new_args):
                        continue
                    if pn in new_kwargs:
                        new_args.append(new_kwargs.pop(pn))
                    else:
                        new_args.append(defaults[i] if i < len(defaults) else "")
                for pn in pos_names:
                    new_kwargs.pop(pn, None)
                unknown = [k for k in new_kwargs if k not in accepted]
                for k in unknown:
                    new_kwargs.pop(k)
                return orig(self, *new_args, **new_kwargs)
            wrapped.__name__ = name
            wrapped.__wrapped__ = orig
            return wrapped

        setattr(cache_cls, name, make(name, orig, [p.name for p in required], accepted, defaults))

    logger.info("[integrations] DisabledCache kwargs tolerance wired")


# ─── 12. Rerank Stage ────────────────────────────────────────────────────────

def wire_rerank(reader_legacy_cls, reader_hybrid_cls):
    """Add cross-encoder rerank stage to reader pipelines (opt-in)."""
    if os.environ.get("MEMORY_RERANK_ENABLED", "").strip().lower() not in ("1", "true", "yes", "on"):
        return

    try:
        from hyatlas_memory.core.core import rerank as _r
    except ImportError:
        try:
            from .core import rerank as _r
        except ImportError:
            logger.warning("[integrations] rerank module not available — skipping")
            return

    for mod_cls in (reader_legacy_cls, reader_hybrid_cls):
        if getattr(mod_cls, "_rerank_wired", False):
            continue
        orig = mod_cls.search if hasattr(mod_cls, "search") else mod_cls.read
        if orig is None:
            continue

        async def make(orig):
            async def patched(self, *args, **kwargs):
                result = await orig(self, *args, **kwargs)
                if not _r.is_enabled():
                    return result
                final = None
                if isinstance(result, dict):
                    for key in ("memories", "results", "items", "hits"):
                        cand = result.get(key)
                        if isinstance(cand, list) and cand:
                            final = cand
                            break
                        if isinstance(cand, dict):
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
                                final = flat
                                break
                elif isinstance(result, list):
                    final = result
                if not final:
                    return result
                try:
                    reranked, diag = await _r.rerank_async(final, query=kwargs.get("query", ""))
                    if isinstance(result, dict):
                        for key in ("memories", "results", "items", "hits"):
                            cand = result.get(key)
                            if isinstance(cand, list) and cand:
                                result[key] = reranked
                                result.setdefault("rerank_diag", diag)
                                break
                    else:
                        result = reranked
                except Exception as e:
                    logger.debug(f"[rerank] failed (no-op): {e}")
                return result
            return patched

        if hasattr(mod_cls, "search"):
            mod_cls.search = make(orig)
        else:
            mod_cls.read = make(orig)
        mod_cls._rerank_wired = True

    logger.info("[integrations] rerank stage wired on reader pipelines")


# ─── 13. L1_RAW Normal Fallback ──────────────────────────────────────────────

def wire_l1_normal_fallback(reader_legacy_cls):
    """Include L1_RAW in normal search when L2+ is empty (lite mode support)."""
    if getattr(reader_legacy_cls, "_l1_raw_fallback_wired", False):
        return

    from hyatlas_memory.core.models.memory import MemoryLayer

    orig = reader_legacy_cls.read

    async def _read_with_fallback(self, request, ctx=None, tracer=None):
        resp = await orig(self, request, ctx=ctx, tracer=tracer)
        mems = list(getattr(resp, "memories", []) or [])
        has_non_l1 = any(m.get("layer") != MemoryLayer.L1_RAW.value for m in mems)
        if mems and has_non_l1:
            return resp
        if mems and not has_non_l1:
            return resp
        # Normal bucket empty — do L1_RAW fallback search
        try:
            query_emb = await self.embed_service.embed(request.query)
        except Exception:
            return resp
        try:
            l1_hits = await self._vector_store.search(
                query_embedding=query_emb,
                user_ids=getattr(request, "user_ids", None),
                agent_ids=getattr(request, "agent_ids", None),
                limit=max(getattr(request, "limit", 10) or 10, 10),
                layers=[MemoryLayer.L1_RAW],
                score_threshold=None,
                only_latest=True,
            )
        except Exception:
            return resp
        merged = []
        for hit in l1_hits:
            node = hit.get("node")
            content = getattr(node, "content", "") if node else ""
            nid = hit.get("node_id", "") or (getattr(node, "node_id", "") if node else "")
            merged.append({
                "node_id": nid, "score": hit.get("score", 0.0),
                "content": content, "layer": MemoryLayer.L1_RAW.value,
                "source": "l1_raw_fallback",
            })
        resp.memories = list(getattr(resp, "memories", []) or []) + merged
        return resp

    reader_legacy_cls.read = _read_with_fallback
    reader_legacy_cls._l1_raw_fallback_wired = True
    logger.info("[integrations] L1_RAW normal fallback wired")


# ─── 14. In-Process Embedding (local sentence-transformers) ─────────────────

_embed_model = None


def wire_inprocess_embed(embed_service_cls):
    """Replace the OpenAI HTTP embedding call with a local sentence-transformers model.

    Eliminates the need for an external embedding API. The model is loaded once
    and shared. Embedding calls use asyncio.to_thread so CPU-bound encoding does
    not block the event loop.
    """
    global _embed_model
    if getattr(embed_service_cls, "_inprocess_embed_wired", False):
        return

    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:
        logger.warning("[integrations] sentence-transformers not installed — skipping in-process embed")
        return

    model_name = os.environ.get("MEMORY_EMBEDDER_MODEL", "BAAI/bge-large-en-v1.5")
    device = os.environ.get("MEMORY_EMBEDDER_DEVICE", "cpu")

    try:
        import time as _t
        t0 = _t.time()
        logger.info(f"[integrations] loading embedding model: {model_name} on {device}")
        _embed_model = SentenceTransformer(model_name, device=device)
        logger.info(f"[integrations] model loaded in {_t.time() - t0:.1f}s (dim={_embed_model.get_sentence_embedding_dimension()})")
    except Exception as e:
        logger.error(f"[integrations] failed to load embedding model: {e}")
        return

    async def _local_embed(self, texts, **kwargs):
        import asyncio
        vecs = await asyncio.to_thread(_embed_model.encode, texts, convert_to_numpy=True)
        return [v.tolist() for v in vecs]

    embed_service_cls._embed_openai = _local_embed
    embed_service_cls._inprocess_embed_wired = True
    logger.info("[integrations] in-process embedding wired (no external API needed)")


# ─── Wire All ────────────────────────────────────────────────────────────────

def wire_all():
    """Wire all integrations into the forked source. Called at server startup."""
    from .core.agent.extractor import Extractor
    from .core.client import HyMemoryClient
    from .core.core.embed_service import EmbedService
    from .core.data.cache_disabled import DisabledCache
    from .core.pipelines.reader_hybrid_v2 import HybridV2ReadPipeline
    from .core.pipelines.reader_legacy import LegacyReadPipeline
    from .core.pipelines.system2_writer import System2Writer
    from .core.server import MemoryHTTPHandler, _get_client, _json_response

    # 1. Circuit breaker
    wire_circuit_breaker(MemoryHTTPHandler, _json_response)

    # 2. L1_RAW sweep
    try:
        _vs = _get_client()._vector_store
    except Exception:
        _vs = None
    start_l1_raw_sweep(vector_store=_vs)

    # 3. L1_RAW dedup skip
    wire_l1_dedup_skip(HyMemoryClient)

    # 4. L5 auto-trigger
    wire_l5_auto_trigger(System2Writer)

    # 5. L5 in-process
    wire_l5_inprocess(System2Writer)

    # 6. Graph endpoint
    wire_graph_endpoint(MemoryHTTPHandler, _json_response, _get_client)

    # 6b. Dashboard VDB (zvec / qdrant via live client)
    wire_vdb_dashboard(MemoryHTTPHandler, _json_response, _get_client)

    # 7. L5/L6/L7 counts
    wire_graph_counts(HyMemoryClient)

    # 8. S1 extractor L5 context
    wire_s1_l5_context(Extractor)

    # 9. User identity
    wire_user_identity(HybridV2ReadPipeline)

    # 10. LLM fast/smart
    wire_llm_fast_smart(Extractor, System2Writer)

    # 11. DisabledCache tolerance
    wire_disabled_cache_tolerance(DisabledCache)

    # 12. Rerank
    wire_rerank(LegacyReadPipeline, HybridV2ReadPipeline)

    # 13. L1_RAW normal fallback
    wire_l1_normal_fallback(LegacyReadPipeline)

    # 14. In-process embedding (local sentence-transformers, no API)
    wire_inprocess_embed(EmbedService)

    logger.info("[integrations] all 15 integrations wired successfully")
