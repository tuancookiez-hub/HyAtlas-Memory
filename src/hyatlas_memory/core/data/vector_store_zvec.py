"""
Agent Memory V3 - VectorStore (Zvec)

In-process vector database backend using Alibaba Zvec.
No external server process — runs inside the Python process via C++ bindings.

Advantages over Qdrant:
- 18-169x faster (no network hop, batch GEMV distance computation)
- Built-in BM25 full-text search (no fastembed dependency)
- Hybrid search with weighted reranker
- pip install zvec — no binary to manage

Filter syntax: SQL-like strings
- Equality: layer = "l2_fact"
- IN: layer IN ("l2_fact", "l3_summary")
- AND/OR: user_id = "tuna" AND layer = "l2_fact"
- Numeric: gmt_created >= 1783000000
"""

import asyncio
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..models.memory import (
    MemoryNode, MemoryLayer, MemoryStatus,
)
from ..config import MemoryConfig
from .vector_store_base import VectorStoreBase

if TYPE_CHECKING:
    import zvec

logger = logging.getLogger(__name__)


def resolve_zvec_path(config: MemoryConfig) -> Path:
    """Resolve the physical zvec collection path from the base collection config."""
    from ...layout import home

    base = config.vector_store.collection_name or "agent_memories"
    dims = config.vector_store.embedding_dims or 1024
    suffix = f"_{dims}"
    if base.endswith(suffix):
        raise ValueError(
            "zvec config must use the base collection name; "
            f"got {base!r}, expected {base[:-len(suffix)]!r}"
        )
    return Path(home()) / "zvec" / f"{base}{suffix}"

_vdb_executor = None


def _get_executor():
    global _vdb_executor
    if _vdb_executor is None:
        import concurrent.futures
        _vdb_executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=32, thread_name_prefix="zvec-vdb"
        )
    return _vdb_executor


def _run_in_vdb_pool(func, *args, **kwargs):
    import functools
    loop = asyncio.get_event_loop()
    if args or kwargs:
        return loop.run_in_executor(_get_executor(), functools.partial(func, *args, **kwargs))
    return loop.run_in_executor(_get_executor(), func)


def _safe_topk(coll: Any, requested: int) -> int:
    """Clamp query topk to the collection's live doc count.

    zvec 0.5.1 logs ``ID is out or range: id[N] count[N]`` from
    ``doc_filter.cc`` when topk overshoots the live collection size.
    Cap requested topk at ``doc_count`` when known.
    """
    try:
        n = int(requested)
    except (TypeError, ValueError):
        n = 10
    if n < 1:
        n = 1
    try:
        count = int(getattr(getattr(coll, "stats", None), "doc_count", 0) or 0)
    except Exception:
        return n
    if count <= 0:
        return n
    if n > count:
        return count
    return n


def _quote(val: str) -> str:
    """SQL-escape a string value for zvec filter."""
    return '"' + val.replace('"', '\\"') + '"'


def _list_to_in(values: list[str]) -> str:
    """Build IN (...) clause from a list of strings."""
    quoted = ", ".join(_quote(v) for v in values)
    return f"IN ({quoted})"


def _build_filter(
    isolation_key: str = "",
    isolation_keys: list[str] | None = None,
    user_id: str | None = None,
    user_ids: list[str] | None = None,
    agent_ids: list[str] | None = None,
    layers: list[MemoryLayer] | None = None,
    status_filter: list[MemoryStatus] | None = None,
    only_latest: bool = True,
    tags_match_any: list[str] | None = None,
    created_after: float | None = None,
) -> str | None:
    """Build a zvec SQL-like filter string from HyAtlas search parameters."""
    parts = []

    if isolation_keys:
        parts.append(f"isolation_key {_list_to_in(isolation_keys)}")
    elif isolation_key:
        parts.append(f"isolation_key = {_quote(isolation_key)}")
    else:
        uids = user_ids if user_ids else ([user_id] if user_id else [])
        if uids:
            if len(uids) == 1:
                parts.append(f"user_id = {_quote(uids[0])}")
            else:
                parts.append(f"user_id {_list_to_in(uids)}")
            if agent_ids:
                if len(agent_ids) == 1:
                    parts.append(f"agent_id = {_quote(agent_ids[0])}")
                else:
                    parts.append(f"agent_id {_list_to_in(agent_ids)}")

    if layers:
        layer_vals = [l.value if hasattr(l, "value") else str(l) for l in layers]
        parts.append(f"layer {_list_to_in(layer_vals)}")

    if status_filter:
        sv = [s.value if hasattr(s, "value") else str(s) for s in status_filter]
        parts.append(f"status {_list_to_in(sv)}")
    else:
        parts.append(f"status = {_quote(MemoryStatus.ACTIVE.value)}")

    if only_latest:
        parts.append(f"is_latest = {_quote('true')}")

    if tags_match_any:
        tag_list = sorted({t for t in tags_match_any if t})
        if tag_list:
            parts.append(f"tags {_list_to_in(tag_list)}")

    if created_after:
        parts.append(f"gmt_created >= {int(created_after)}")

    if not parts:
        return None
    return " AND ".join(parts)


# zvec field schema — maps HyAtlas payload fields to zvec DataType
_FIELD_SCHEMA = [
    ("node_id", "STRING"),
    ("isolation_key", "STRING"),
    ("user_id", "STRING"),
    ("agent_id", "STRING"),
    ("session_id", "STRING"),
    ("owner", "STRING"),
    ("layer", "STRING"),
    ("content", "STRING"),
    ("search_text", "STRING"),
    ("status", "STRING"),
    ("confidence", "STRING"),
    ("source_type", "STRING"),
    ("emotional_valence", "STRING"),
    ("emotional_arousal", "STRING"),
    ("specificity_score", "STRING"),
    ("rarity_score", "STRING"),
    ("longtail_flag", "STRING"),
    ("meta_tags", "ARRAY_STRING"),
    ("source_session_id", "STRING"),
    ("memory_at", "STRING"),
    ("temporal_anchor", "STRING"),
    ("gmt_created", "STRING"),
    ("gmt_modified", "STRING"),
    ("valid_from", "STRING"),
    ("valid_until", "STRING"),
    ("access_count", "STRING"),
    ("last_accessed_at", "STRING"),
    ("supersedes", "ARRAY_STRING"),
    ("superseded_by", "ARRAY_STRING"),
    ("custom", "STRING"),
    ("meta_info", "STRING"),
    ("importance", "STRING"),
    ("content_type", "STRING"),
    ("tags", "ARRAY_STRING"),
    ("speculate", "STRING"),
    ("source_raw_memory_id", "STRING"),
    ("is_latest", "STRING"),  # BOOL not in zvec 0.5.1 schema
]


# Module-level cache: zvec is an in-process DB with file-level locking.
# Multiple pipelines each call initialize(), so we must open the collection
# exactly once and share the handle. A second zvec.open() on the same path
# raises "Can't lock read-write collection".
_open_collections: dict[str, "zvec.Collection"] = {}
_open_refs: dict[str, int] = {}


class ZvecVectorStore(VectorStoreBase):
    """
    Zvec in-process vector store.

    Uses zvec's native C++ HNSW index with IP metric.
    BM25 full-text search via zvec's built-in FTS index on content field.
    """

    def __init__(self, config: MemoryConfig):
        super().__init__(config)
        self._coll = None
        self._path = None
        # zvec BM25 returns normalized scores in [0,1] range
        self._keyword_score_normalized = True

    async def initialize(self) -> None:
        """Open or create the zvec collection (singleton per path)."""
        import zvec

        vs_config = self.config.vector_store
        dims = vs_config.embedding_dims or 1024

        # Collection path: zvec manages its own storage under HYATLAS_HOME/zvec/.
        # Ignore persist_directory (legacy Qdrant path) to avoid LOCK conflicts.
        self._path = str(resolve_zvec_path(self.config))

        os.makedirs(os.path.dirname(self._path), exist_ok=True)

        # Singleton: only open once per path
        if self._path in _open_collections:
            self._coll = _open_collections[self._path]
            _open_refs[self._path] = _open_refs.get(self._path, 0) + 1
            logger.info(f"[zvec] Reusing existing collection: {self._path}")
            return

        # Build schema
        fields = []
        for name, dtype_name in _FIELD_SCHEMA:
            dtype = getattr(zvec.DataType, dtype_name)
            fields.append(zvec.FieldSchema(name, dtype, nullable=True))

        vectors = [
            zvec.VectorSchema(
                name="embedding",
                dimension=dims,
                data_type=zvec.DataType.VECTOR_FP32,
            )
        ]

        schema = zvec.CollectionSchema(
            name=self._collection_name,
            fields=fields,
            vectors=vectors,
        )

        # Open existing or create new
        if os.path.exists(self._path):
            self._coll = zvec.open(self._path)
            logger.info(f"[zvec] Opened existing collection: {self._path}")
        else:
            self._coll = zvec.create_and_open(self._path, schema)
            logger.info(f"[zvec] Created new collection: {self._path} (dims={dims})")

        _open_collections[self._path] = self._coll
        _open_refs[self._path] = 1

        # Create FTS index on content field for BM25
        try:
            self._coll.create_index("content", zvec.FtsIndexParam())
            logger.info("[zvec] FTS index on content field ready")
        except Exception:
            # Index may already exist
            pass

        # Create FTS index on search_text for lemmatized BM25
        try:
            self._coll.create_index("search_text", zvec.FtsIndexParam())
        except Exception:
            pass

    async def close(self) -> None:
        """Close the collection."""
        if self._coll and self._path:
            self._coll.flush()
            refs = _open_refs.get(self._path, 1) - 1
            if refs > 0:
                _open_refs[self._path] = refs
            else:
                _open_refs.pop(self._path, None)
                _open_collections.pop(self._path, None)
                import gc
                self._coll = None
                gc.collect()
        self._coll = None
        logger.info("[zvec] Collection closed")

    def _node_to_doc(self, node: MemoryNode) -> Any:
        """Convert MemoryNode to zvec Doc.

        JSON-serialize dict fields since zvec lacks a native MAP type.
        All scalar fields are strings (zvec 0.5.1 schema).
        """
        import json
        import zvec
        payload = self._node_to_payload(node)

        # Coerce values to zvec schema types
        clean = {}
        for name, dtype_str in _FIELD_SCHEMA:
            val = payload.get(name)
            if val is None:
                continue
            if dtype_str == "ARRAY_STRING":
                if isinstance(val, list):
                    clean[name] = [str(v) for v in val]
                else:
                    clean[name] = [str(val)] if val else []
            elif name in ("custom", "meta_info") and isinstance(val, (dict, list)):
                clean[name] = json.dumps(val, ensure_ascii=False)
            elif name in ("memory_at", "gmt_created", "gmt_modified", "valid_from", "valid_until", "last_accessed_at") and isinstance(val, (int, float)) and val > 0:
                # Convert Unix timestamps to ISO format strings for parse_dt compatibility
                from datetime import datetime, timezone
                clean[name] = datetime.fromtimestamp(val, tz=timezone.utc).isoformat()
            elif isinstance(val, bool):
                clean[name] = "true" if val else "false"
            else:
                clean[name] = str(val) if val != "" else None

        point_id = self._node_id_to_point_id(node.node_id)
        return zvec.Doc(
            id=point_id,
            vectors={"embedding": node.embedding or []},
            fields=clean,
        )

    def _doc_to_node(self, doc: Any, include_vector: bool = False) -> MemoryNode:
        """Convert zvec Doc back to MemoryNode."""
        import json
        from datetime import datetime, timezone

        payload = {}
        for name, _ in _FIELD_SCHEMA:
            try:
                val = doc.field(name)
                if val is None:
                    continue
                # Deserialize JSON strings back to dicts
                if name in ("custom", "meta_info") and isinstance(val, str):
                    try:
                        val = json.loads(val)
                    except (json.JSONDecodeError, TypeError):
                        pass
                # Migrated Qdrant payloads store timestamps as unix epoch
                # strings (e.g. "1782866658"); normalize to ISO for parse_dt.
                if name in (
                    "memory_at",
                    "gmt_created",
                    "gmt_modified",
                    "valid_from",
                    "valid_until",
                    "last_accessed_at",
                ) and isinstance(val, str):
                    try:
                        f = float(val)
                        if f > 0:
                            val = datetime.fromtimestamp(f, tz=timezone.utc).isoformat()
                    except (ValueError, TypeError, OverflowError):
                        pass
                payload[name] = val
            except Exception:
                pass

        # zvec doesn't store None for missing nullable fields, so we need
        # to ensure required fields are present
        if "node_id" not in payload:
            payload["node_id"] = doc.id

        node = self._payload_to_node(payload)
        if include_vector:
            try:
                node.embedding = doc.vector("embedding")
            except Exception:
                pass
        return node

    # ================================================================
    # Write
    # ================================================================

    async def upsert(self, node: MemoryNode) -> str:
        """Insert or update a single MemoryNode."""
        import zvec
        doc = self._node_to_doc(node)
        def _upsert():
            self._coll.upsert(zvec.DocList([doc]))
            self._coll.flush()
        await _run_in_vdb_pool(_upsert)
        return node.node_id

    async def upsert_batch(self, nodes: list[MemoryNode]) -> list[str]:
        """Batch insert/update."""
        import zvec
        docs = [self._node_to_doc(n) for n in nodes]
        def _upsert():
            self._coll.upsert(zvec.DocList(docs))
            self._coll.flush()
        await _run_in_vdb_pool(_upsert)
        return [n.node_id for n in nodes]

    async def update_embedding(self, node_id: str, embedding: list[float]) -> bool:
        """Update only the vector (payload unchanged)."""
        import zvec
        point_id = self._node_id_to_point_id(node_id)
        try:
            def _update():
                self._coll.update(zvec.DocList([
                    zvec.Doc(id=point_id, vectors={"embedding": embedding})
                ]))
                self._coll.flush()
            await _run_in_vdb_pool(_update)
            return True
        except Exception as e:
            logger.warning(f"[zvec] Failed to update embedding for {node_id}: {e}")
            return False

    async def update_payload(self, node_id: str, updates: dict[str, Any]) -> bool:
        """Update only payload fields (vector unchanged).

        If the caller passes ``"embedding"`` in updates (as the reconciler
        does for in-place UPDATE ops), route it to ``update_embedding``
        separately — zvec treats ``embedding`` as a VECTOR field, so passing
        it as a scalar field triggers
        ``schema validate failed: embedding not found in collection schema``.
        """
        import json
        import zvec

        # Split vector updates from scalar updates.
        embedding_update = updates.get("embedding")
        scalar_updates = {k: v for k, v in updates.items() if k != "embedding"}

        # Apply the embedding update first (if any) via the dedicated path.
        if embedding_update is not None and isinstance(embedding_update, list):
            await self.update_embedding(node_id, embedding_update)

        if not scalar_updates:
            return True

        clean = {}
        for name, val in scalar_updates.items():
            if val is None:
                continue
            if name in ("custom", "meta_info") and isinstance(val, (dict, list)):
                clean[name] = json.dumps(val, ensure_ascii=False)
            elif name in ("meta_tags", "supersedes", "superseded_by", "tags"):
                clean[name] = [str(v) for v in val] if isinstance(val, list) else [str(val)]
            elif name in ("memory_at", "gmt_created", "gmt_modified", "valid_from", "valid_until", "last_accessed_at") and isinstance(val, (int, float)) and val > 0:
                from datetime import datetime, timezone
                clean[name] = datetime.fromtimestamp(val, tz=timezone.utc).isoformat()
            elif isinstance(val, bool):
                clean[name] = "true" if val else "false"
            else:
                clean[name] = str(val) if val != "" else None

        point_id = self._node_id_to_point_id(node_id)

        def _update_simple():
            # Payload-only update: pass only scalar fields, omit vectors
            # entirely. zvec's update() touches only the fields you specify,
            # so the existing embedding is preserved untouched on disk.
            self._coll.update(zvec.DocList([
                zvec.Doc(id=point_id, fields=clean, vectors=None)
            ]))
            self._coll.flush()

        def _update_with_vector():
            # Fallback: some zvec versions require the vector on update.
            # Fetch the existing embedding and re-pass it, but only if the
            # schema actually declares a vector named "embedding".
            schema = self._coll.schema
            if schema.vector("embedding") is None:
                _update_simple()
                return
            existing = self._coll.fetch([point_id], include_vector=True)
            vectors = None
            doc = existing.get(point_id) if existing else None
            if doc is not None and getattr(doc, "vectors", None):
                vec = doc.vectors.get("embedding")
                if vec is not None:
                    vectors = {"embedding": vec}
            self._coll.update(zvec.DocList([
                zvec.Doc(id=point_id, fields=clean, vectors=vectors)
            ]))
            self._coll.flush()

        try:
            await _run_in_vdb_pool(_update_simple)
            return True
        except Exception as e:
            logger.debug(f"[zvec] simple update_payload failed for {node_id}: {e}; trying vector-preserving path")
            try:
                await _run_in_vdb_pool(_update_with_vector)
                return True
            except Exception as e2:
                logger.warning(f"[zvec] Failed to update payload for {node_id}: {e2}")
                return False

    # ================================================================
    # Search
    # ================================================================

    async def search(
        self,
        query_embedding: list[float],
        isolation_key: str = "",
        isolation_keys: list[str] | None = None,
        user_id: str | None = None,
        user_ids: list[str] | None = None,
        agent_ids: list[str] | None = None,
        layers: list[MemoryLayer] | None = None,
        limit: int = 10,
        score_threshold: float = 0.0,
        status_filter: list[MemoryStatus] | None = None,
        only_latest: bool = True,
        tags_match_any: list[str] | None = None,
        created_after: float | None = None,
    ) -> list[dict[str, Any]]:
        """Semantic vector search with filtering."""
        import zvec

        filt = _build_filter(
            isolation_key=isolation_key,
            isolation_keys=isolation_keys,
            user_id=user_id,
            user_ids=user_ids,
            agent_ids=agent_ids,
            layers=layers,
            status_filter=status_filter,
            only_latest=only_latest,
            tags_match_any=tags_match_any,
            created_after=created_after,
        )

        def _search():
            return self._coll.query(
                queries=zvec.Query(field_name="embedding", vector=query_embedding),
                topk=_safe_topk(self._coll, limit),
                filter=filt,
            )

        try:
            results = await _run_in_vdb_pool(_search)
        except Exception as e:
            logger.error(f"[zvec] search failed: {e}", exc_info=True)
            return []

        output = []
        for doc in results:
            if doc.score < score_threshold:
                continue
            node = self._doc_to_node(doc, include_vector=False)
            output.append({
                "node_id": node.node_id,
                "score": doc.score,
                "node": node,
            })

        logger.debug(
            f"[zvec] search: user_id={user_id} layers={layers} "
            f"limit={limit} found={len(output)} filter={filt}"
        )
        return output

    async def get_by_id(self, node_id: str) -> MemoryNode | None:
        """Fetch by node ID."""
        point_id = self._node_id_to_point_id(node_id)
        try:
            def _fetch():
                return self._coll.fetch(ids=[point_id], include_vector=True)
            result = await _run_in_vdb_pool(_fetch)
            if point_id in result:
                return self._doc_to_node(result[point_id], include_vector=True)
        except Exception as e:
            logger.warning(f"[zvec] Failed to get {node_id}: {e}")
        return None

    async def get_by_ids(self, node_ids: list[str]) -> list[MemoryNode]:
        """Batch fetch by IDs."""
        if not node_ids:
            return []
        point_ids = [self._node_id_to_point_id(nid) for nid in node_ids]
        try:
            def _fetch():
                return self._coll.fetch(ids=point_ids, include_vector=False)
            result = await _run_in_vdb_pool(_fetch)
            nodes = []
            for pid in point_ids:
                if pid in result:
                    nodes.append(self._doc_to_node(result[pid], include_vector=False))
            return nodes
        except Exception as e:
            logger.warning(f"[zvec] batch get failed: {e}")
            return []

    async def get_embeddings(self, node_ids: list[str]) -> dict[str, list[float]]:
        """Batch fetch vectors for dedup cosine computation."""
        if not node_ids:
            return {}
        point_ids = [self._node_id_to_point_id(nid) for nid in node_ids]
        try:
            def _fetch():
                return self._coll.fetch(ids=point_ids, include_vector=True)
            result = await _run_in_vdb_pool(_fetch)
            out = {}
            for nid, pid in zip(node_ids, point_ids):
                if pid in result:
                    doc = result[pid]
                    try:
                        vec = doc.vector("embedding")
                        if vec:
                            out[nid] = list(vec)
                    except Exception:
                        pass
            return out
        except Exception as e:
            logger.warning(f"[zvec] get_embeddings failed: {e}")
            return {}

    # ================================================================
    # Delete
    # ================================================================

    async def delete(self, node_id: str) -> bool:
        """Delete a single point."""
        point_id = self._node_id_to_point_id(node_id)
        try:
            def _delete():
                self._coll.delete(ids=[point_id])
                self._coll.flush()
            await _run_in_vdb_pool(_delete)
            return True
        except Exception as e:
            logger.warning(f"[zvec] delete failed for {node_id}: {e}")
            return False

    async def delete_by_filter(self, filt: str) -> int:
        """Delete points matching a zvec filter string. Returns count deleted."""
        try:
            import zvec
            dims = self.config.vector_store.embedding_dims or 1024
            def _count():
                rows = self._coll.query(
                    queries=zvec.Query(field_name="embedding", vector=[0.0] * dims),
                    topk=_safe_topk(self._coll, 100_000), filter=filt,
                )
                return len(rows)
            n = await _run_in_vdb_pool(_count)
            def _delete():
                self._coll.delete_by_filter(filter=filt)
                self._coll.flush()
            await _run_in_vdb_pool(_delete)
            return n
        except Exception as e:
            logger.warning(f"[zvec] delete_by_filter failed: {e}")
            return 0

    async def delete_by_isolation_key(self, isolation_key: str) -> int:
        """Delete all points under an isolation key."""
        try:
            import zvec
            dims = self.config.vector_store.embedding_dims or 1024
            filt = f'isolation_key = {_quote(isolation_key)}'
            def _count():
                rows = self._coll.query(
                    queries=zvec.Query(field_name="embedding", vector=[0.0] * dims),
                    topk=_safe_topk(self._coll, 100_000), filter=filt,
                )
                return len(rows)
            n = await _run_in_vdb_pool(_count)
            def _delete():
                self._coll.delete_by_filter(filter=filt)
                self._coll.flush()
            await _run_in_vdb_pool(_delete)
            return n
        except Exception as e:
            logger.warning(f"[zvec] delete_by_isolation_key failed: {e}")
            return 0

    async def delete_by_metadata(
        self,
        user_id: str,
        agent_id: str | None = None,
        session_id: str | None = None,
    ) -> int:
        """Delete by metadata field combination. Returns count deleted."""
        parts = [f"user_id = {_quote(user_id)}"]
        if agent_id is not None:
            parts.append(f"agent_id = {_quote(agent_id)}")
        if session_id is not None:
            parts.append(f"session_id = {_quote(session_id)}")
        filt = " AND ".join(parts)

        try:
            import zvec
            dims = self.config.vector_store.embedding_dims or 1024
            def _count():
                rows = self._coll.query(
                    queries=zvec.Query(field_name="embedding", vector=[0.0] * dims),
                    topk=_safe_topk(self._coll, 100_000), filter=filt,
                )
                return len(rows)
            n = await _run_in_vdb_pool(_count)
            def _delete():
                self._coll.delete_by_filter(filter=filt)
                self._coll.flush()
            await _run_in_vdb_pool(_delete)
            return n
        except Exception as e:
            logger.warning(f"[zvec] delete_by_metadata failed: {e}")
            return 0

    # ================================================================
    # Enumerate
    # ================================================================

    async def list_by_user(
        self,
        user_id: str,
        agent_id: str | None = None,
        limit: int = 10000,
        status_filter: list | None = None,
        layers: list[MemoryLayer] | None = None,
    ) -> list[MemoryNode]:
        """List all memories for a user."""
        import zvec

        parts = [f'user_id = {_quote(user_id)}']
        if agent_id:
            parts.append(f'agent_id = {_quote(agent_id)}')
        if status_filter:
            sv = [s.value if hasattr(s, "value") else str(s) for s in status_filter]
            parts.append(f"status {_list_to_in(sv)}")
        if layers:
            lv = [l.value if hasattr(l, "value") else str(l) for l in layers]
            parts.append(f"layer {_list_to_in(lv)}")
        filt = " AND ".join(parts)

        # Use a zero vector to list all (zvec requires a query vector for search)
        # Better: use fetch with a filter if zvec supports it, otherwise use a
        # dummy vector with a very high topk and filter
        dims = self.config.vector_store.embedding_dims or 1024

        def _list():
            return self._coll.query(
                queries=zvec.Query(
                    field_name="embedding",
                    vector=[0.0] * dims,
                ),
                topk=_safe_topk(self._coll, limit),
                filter=filt,
                include_vector=True,
            )

        try:
            results = await _run_in_vdb_pool(_list)
            nodes = [self._doc_to_node(doc, include_vector=True) for doc in results]
            logger.info(f"[zvec] list_by_user: user_id={user_id} found={len(nodes)}")
            return nodes
        except Exception as e:
            logger.error(f"[zvec] list_by_user failed: {e}", exc_info=True)
            return []

    # ================================================================
    # Stats
    # ================================================================

    async def get_stats(self) -> dict[str, Any]:
        """Get collection statistics."""
        try:
            stats = self._coll.stats
            return {
                "collection": self._collection_name,
                "doc_count": stats.doc_count,
                "index_completeness": dict(stats.index_completeness) if stats.index_completeness else {},
                "path": self._path,
            }
        except Exception as e:
            return {"error": str(e)}

    async def count(self, isolation_key: str) -> int:
        """Count points under an isolation key."""
        import zvec
        dims = self.config.vector_store.embedding_dims or 1024
        try:
            def _count():
                result = self._coll.query(
                    queries=zvec.Query(
                        field_name="embedding",
                        vector=[0.0] * dims,
                    ),
                    topk=_safe_topk(self._coll, 100000),
                    filter=f'isolation_key = {_quote(isolation_key)}',
                )
                return len(result)
            return await _run_in_vdb_pool(_count)
        except Exception as e:
            logger.warning(f"[zvec] count failed: {e}")
            return -1

    # ================================================================
    # BM25 Full-Text Search (native zvec FTS)
    # ================================================================

    async def keyword_search(
        self,
        query: str,
        top_k: int = 10,
        user_id: str | None = None,
        agent_ids: list[str] | None = None,
        layers: list[MemoryLayer] | None = None,
        status_filter: list[MemoryStatus] | None = None,
        only_latest: bool = True,
    ) -> list[dict[str, Any]]:
        """BM25 full-text search via zvec's native FTS index."""
        import zvec

        parts = []
        if user_id:
            parts.append(f'user_id = {_quote(user_id)}')
        if agent_ids:
            if len(agent_ids) == 1:
                parts.append(f'agent_id = {_quote(agent_ids[0])}')
            else:
                parts.append(f"agent_id {_list_to_in(agent_ids)}")
        if layers:
            lv = [l.value if hasattr(l, "value") else str(l) for l in layers]
            parts.append(f"layer {_list_to_in(lv)}")
        if status_filter:
            sv = [s.value if hasattr(s, "value") else str(s) for s in status_filter]
            parts.append(f"status {_list_to_in(sv)}")
        else:
            parts.append(f"status = {_quote(MemoryStatus.ACTIVE.value)}")
        if only_latest:
            parts.append(f"is_latest = {_quote('true')}")

        filt = " AND ".join(parts) if parts else None

        def _search():
            return self._coll.query(
                queries=zvec.Query(
                    field_name="content",
                    fts=zvec.Fts(match_string=query),
                ),
                topk=_safe_topk(self._coll, top_k),
                filter=filt,
            )

        try:
            results = await _run_in_vdb_pool(_search)
        except Exception as e:
            logger.warning(f"[zvec] keyword_search failed: {e}")
            return []

        output = []
        for doc in results:
            node = self._doc_to_node(doc, include_vector=False)
            output.append({
                "node_id": node.node_id,
                "score": doc.score,
                "node": node,
            })
        logger.debug(f"[zvec] keyword_search: query={query!r} found={len(output)}")
        return output

    # ================================================================
    # Hybrid Search (vector + BM25 reranked)
    # ================================================================

    async def hybrid_search(
        self,
        query_embedding: list[float],
        query_text: str,
        top_k: int = 10,
        user_id: str | None = None,
        layers: list[MemoryLayer] | None = None,
        vector_weight: float = 0.7,
        bm25_weight: float = 0.3,
    ) -> list[dict[str, Any]]:
        """Hybrid search: vector + BM25 with weighted reranking."""
        import zvec

        parts = []
        if user_id:
            parts.append(f'user_id = {_quote(user_id)}')
        if layers:
            lv = [l.value if hasattr(l, "value") else str(l) for l in layers]
            parts.append(f"layer {_list_to_in(lv)}")
        parts.append(f"status = {_quote(MemoryStatus.ACTIVE.value)}")
        parts.append(f"is_latest = {_quote('true')}")
        filt = " AND ".join(parts)

        def _search():
            return self._coll.query(
                queries=[
                    zvec.Query(field_name="embedding", vector=query_embedding),
                    zvec.Query(field_name="content", fts=zvec.Fts(match_string=query_text)),
                ],
                topk=_safe_topk(self._coll, top_k),
                filter=filt,
                reranker=zvec.WeightedReRanker(
                    weights=[vector_weight, bm25_weight]
                ),
            )

        try:
            results = await _run_in_vdb_pool(_search)
        except Exception as e:
            logger.warning(f"[zvec] hybrid_search failed: {e}")
            return []

        output = []
        for doc in results:
            node = self._doc_to_node(doc, include_vector=False)
            output.append({
                "node_id": node.node_id,
                "score": doc.score,
                "node": node,
            })
        return output
