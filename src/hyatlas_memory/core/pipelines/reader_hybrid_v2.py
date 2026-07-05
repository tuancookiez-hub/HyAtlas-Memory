"""
Hybrid V2 Read Pipeline — Embed + Keyword Search + Graph Evidence.

Multi-signal fusion architecture:
  - Channel 1: VDB Semantic Search (vector cosine similarity)
  - Channel 2: VDB Keyword Search (Qdrant text index, independent retrieval)
  - Channel 3: Graph Semantic Search (L6_SCHEMA + L7_INTENTION)

Scoring:
  - VDB nodes: semantic × 0.6 + bm25_norm × 0.4 → [0, 1.0]
  - Graph nodes: raw semantic for display; evidence_boost for intra-pool ranking
  - Graph quota guarantees schema/intention representation in results

Key differences from hybrid reader:
  - Independent keyword retrieval channel (not just in-memory BM25 reranking)
  - Graph nodes with evidence boost (but same [0,1] display scale as VDB)
  - Graph quota merge strategy (guaranteed representation)
  - Tags participate via search_text (content + tags indexed together)
"""

from typing import Any, Dict, List, Optional
from datetime import datetime
import asyncio
import os
import logging

from .base import ReadPipeline, ReadRequest, ReadResponse, PipelineContext
from ..config import MemoryConfig
from ..core.embed_service import EmbedService
from ..models.memory import MemoryNode, MemoryLayer, MemoryStatus
from ..data.vector_store import create_vector_store
from ..data.vector_store_base import VectorStoreBase
from ..data.graph_store_base import GraphStoreBase
from ..utils.tracer import PipelineTracer, create_tracer
from ._retrieval import config as rconf
from ._retrieval.lemmatize import lemmatize_for_bm25, get_bm25_params
from ._retrieval.scoring import (
    normalize_bm25,
    compute_evidence_boost,
    score_vdb_node,
)
from ._retrieval.rrf import rrf_fuse
from ._retrieval.profile_evidence import reverse_lookup_l6
from ._retrieval.evolution import expand_evolution_chains
from ._retrieval.intention import recall_intentions
from ._retrieval.strength import apply_strength_to_normal
from ._retrieval.trace import ReadTraceLogger

logger = logging.getLogger(__name__)

# Profile 路覆盖的层：L0（基础属性，VDB）+ L6_SCHEMA（心智模型，graph 专属）。
# 用于 client 分桶判断 / strength 排除。L4_IDENTITY 不在此——走主 VDB 路归 normal。
_PROFILE_LAYERS = [MemoryLayer.L0_BASIC_INFO, MemoryLayer.L6_SCHEMA]

# profile 路在 VDB 里实际只搜 L0（basic_info）。L6 是 graph 专属，由 graph 正反路提供：
#   正路 = graph vector_search（query 语义相近的 L6，过 profile_min_score + evidence boost）
#   反路 = 从 normal 命中的 VDB 节点反查支撑它们的 L6（按支撑度排序，不卡阈值）
# 正反 RRF 融合 + L0 置顶 = profile 路输出。
_VDB_PROFILE_LAYERS = [MemoryLayer.L0_BASIC_INFO]

# L0 召回候选上限（basic_info 是单条演化链，少量即可覆盖）。最终 profile 条数
# 由 Stage 11 按 profile_limit 截断，故召回上限与 profile_limit 解耦。
_PROFILE_RECALL_LIMIT = 10

# 主 VDB 召回（语义 + BM25 关键词）覆盖的记忆层。
# 覆盖 L2/L3/L4：fact / summary / identity。identity（L4）按 VDB 普通记忆处理，
# 参与语义+BM25 融合排序，最终归入 normal 通道（不再算 profile）。
# 与 _PROFILE_LAYERS 互斥（L0/L6 只走 profile 路），杜绝双重召回。
# 不含 L1_RAW（原始对话，append-only 不召回）、L5_KNOWLEDGE、L7（graph 专属通道）。
_VDB_RECALL_LAYERS = [
    MemoryLayer.L2_FACT,
    MemoryLayer.L3_SUMMARY,
    MemoryLayer.L4_IDENTITY,
]


# ========================================================================
# Config helpers
# ========================================================================

def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


# ========================================================================
# Pipeline
# ========================================================================

class HybridV2ReadPipeline(ReadPipeline):
    """
    Hybrid V2 reader: Embed + Keyword + Graph Evidence + Quota Merge.
    """

    VERSION = "hybrid_v2"

    def __init__(
        self,
        config: MemoryConfig,
        embed_service: Optional[EmbedService] = None,
        vector_store: Optional[VectorStoreBase] = None,
        graph_store: Optional[GraphStoreBase] = None,
        cache: Any = None,
    ):
        self.config = config
        self._embed_service = embed_service
        self._external_vector_store = vector_store
        self._graph_store = graph_store
        self._cache = cache
        self._vector_store: Optional[VectorStoreBase] = None
        self._vector_store_initialized = False
        self._initialized = False

    async def initialize(self) -> None:
        if self._initialized:
            return
        if self._embed_service is None:
            self._embed_service = EmbedService(self.config)
        self._vector_store = self._external_vector_store or create_vector_store(self.config)
        if self._external_vector_store and getattr(self._external_vector_store, "_client", None):
            self._vector_store_initialized = True
        self._initialized = True
        logger.debug("HybridV2ReadPipeline initialized")

    @property
    def embed_service(self) -> EmbedService:
        if self._embed_service is None:
            self._embed_service = EmbedService(self.config)
        return self._embed_service

    async def _get_vector_store(self) -> VectorStoreBase:
        if self._vector_store is None:
            self._vector_store = self._external_vector_store or create_vector_store(self.config)
        if not self._vector_store_initialized:
            await self._vector_store.initialize()
            self._vector_store_initialized = True
        return self._vector_store

    # ------------------------------------------------------------------
    # Main pipeline
    # ------------------------------------------------------------------

    async def read(
        self,
        request: ReadRequest,
        ctx: Optional[PipelineContext] = None,
        tracer: Optional[PipelineTracer] = None,
    ) -> ReadResponse:
        start_time = datetime.now()
        response = ReadResponse()

        if tracer is None:
            tracer = create_tracer(
                operation="read",
                pipeline_version="hybrid_v2",
                uid=request.user_id,
                agent_id=request.agent_id,
                content_preview=request.query,
            )

        # Read-side trace logger
        trace_log = ReadTraceLogger(
            cache=self._cache,
            request_id=request.request_id,
            user_id=request.user_id or (request.user_ids[0] if request.user_ids else ""),
            agent_id=request.agent_id or "default_agent",
            reader_version=self.VERSION,
        )

        # Pre-declare for exception safety
        vdb_semantic_hits: List[Dict[str, Any]] = []
        vdb_keyword_hits: List[Dict[str, Any]] = []
        profile_hits: List[Dict[str, Any]] = []
        graph_schema_hits: List[Dict[str, Any]] = []
        graph_intention_hits: List[Dict[str, Any]] = []

        try:
            if not request.query:
                response.error_code = 400
                response.error_message = "query is required"
                return response

            # --- Load config ---
            W_SEM = _float_env("MEMORY_HYBRID_V2_VDB_WEIGHT_SEM", 0.6)
            W_BM25 = _float_env("MEMORY_HYBRID_V2_VDB_WEIGHT_BM25", 0.4)
            EV_BOOST_MAX = _float_env("MEMORY_HYBRID_V2_EVIDENCE_BOOST_MAX", 0.3)
            EV_SATURATE = _int_env("MEMORY_HYBRID_V2_EVIDENCE_SATURATE", 5)
            MIN_SCORE = request.min_score if request.min_score > 0 else 0.3

            final_limit = request.limit if request.limit > 0 else 10
            vdb_sem_limit = max(final_limit * 3, 30)
            vdb_kw_limit = max(final_limit * 4, 60)
            graph_limit = max(final_limit * 2, 20)

            # Log request
            await trace_log.log_request(
                query=request.query,
                limit=request.limit,
                layers=list(request.layers) if request.layers else None,
                min_score=MIN_SCORE,
                profile_min_score=request.profile_min_score,
                profile_limit=request.profile_limit,
                user_ids=list(request.user_ids or []),
                agent_ids=list(request.agent_ids or []),
                session_ids=list(request.session_ids or []),
            )

            # =========================================================
            # Stage 1: Embed query + Lemmatize query
            # =========================================================
            _t_embed = datetime.now()
            query_embedding = request.query_embedding
            cache_hit = False
            if not query_embedding:
                query_embedding = await self.embed_service.embed(request.query)
            else:
                cache_hit = True

            query_lemmatized = lemmatize_for_bm25(request.query)
            _embed_ms = (datetime.now() - _t_embed).total_seconds() * 1000

            await trace_log.log_embed_query(
                query=request.query,
                dims=len(query_embedding),
                cache_hit=cache_hit,
                elapsed_ms=_embed_ms,
            )

            # =========================================================
            # Stage 2: Build isolation params
            # =========================================================
            isolation_params = self._build_isolation_params(request)
            if isolation_params.get("error_msg"):
                response.error_code = 400
                response.error_message = isolation_params["error_msg"]
                return response

            isolation_key = self._get_isolation_key(request)

            # =========================================================
            # Stage 3: Parallel multi-channel recall
            # =========================================================
            _t_recall = datetime.now()
            vector_store = await self._get_vector_store()

            profile_limit = request.profile_limit
            profile_min_score = request.profile_min_score

            # Build common search params
            search_kwargs = {
                "isolation_key": isolation_params.get("isolation_key", ""),
                "isolation_keys": isolation_params.get("isolation_keys"),
                "user_ids": isolation_params.get("user_ids"),
                "agent_ids": isolation_params.get("agent_ids"),
            }

            # Determine user_id and agent_ids for keyword search
            kw_user_id = request.user_id or (request.user_ids[0] if request.user_ids else "")
            kw_agent_ids = request.agent_ids if request.agent_ids else (
                [request.agent_id] if request.agent_id else None
            )

            # Parallel recall tasks
            # 召回池纳入 SUPERSEDED（被取代的旧节点）+ only_latest=False：
            # 命中旧节点后由 expand_evolution_chains 从命中点双向展开整链；
            # 同链多节点命中会在 expand 阶段去重为一条（以链头为代表）。
            # SHADOW（逻辑删除）不纳入。profile 路保持只 ACTIVE。
            _recall_status = [MemoryStatus.ACTIVE, MemoryStatus.SUPERSEDED]
            recall_tasks = [
                # Channel 1: VDB Semantic (L0/L2/L3/L4)
                vector_store.search(
                    query_embedding=query_embedding,
                    layers=_VDB_RECALL_LAYERS,
                    limit=vdb_sem_limit,
                    status_filter=_recall_status,
                    only_latest=False,
                    **search_kwargs,
                ),
                # Channel 2: VDB Keyword (BM25, L0/L2/L3/L4)
                vector_store.keyword_search(
                    query=query_lemmatized,
                    top_k=vdb_kw_limit,
                    user_id=kw_user_id,
                    agent_ids=kw_agent_ids,
                    layers=_VDB_RECALL_LAYERS,
                    status_filter=_recall_status,
                    only_latest=False,
                ),
                # Profile VDB recall：只搜 L0（basic_info）。L6 由 graph 正反路提供。
                # L0 是 basic_info 演化链，召回候选用固定上限即可；最终条数由 Stage 11
                # 按 profile_limit 截断（0→0 条、<0→不限、>0→截断）。
                vector_store.search(
                    query_embedding=query_embedding,
                    layers=_VDB_PROFILE_LAYERS,
                    limit=_PROFILE_RECALL_LIMIT,
                    score_threshold=profile_min_score,
                    **search_kwargs,
                ),
            ]

            # Channel 3: Graph 正路（only if graph_store available）
            has_graph = self._graph_store is not None
            graph_user_id = request.user_id or (request.user_ids[0] if request.user_ids else "")
            if has_graph:
                # Graph search: use user_id prefix match (not exact isolation_key)
                # because isolation_key may have wrong agent/session when agent_ids list is used.
                # L6 属于 profile 路，阈值用 profile_min_score（与 L0 一致），而非 MIN_SCORE。
                recall_tasks.append(
                    self._graph_store.vector_search(
                        query_embedding=query_embedding,
                        isolation_key="",
                        layers=["l6_schema"],
                        limit=graph_limit,
                        score_threshold=profile_min_score,
                        user_id=graph_user_id,
                    )
                )

            results = await asyncio.gather(*recall_tasks, return_exceptions=True)

            # Unpack results (handle exceptions gracefully)
            # 固定前 3 路: vdb_semantic, vdb_keyword, profile
            vdb_semantic_hits = results[0] if not isinstance(results[0], Exception) else []
            vdb_keyword_hits = results[1] if not isinstance(results[1], Exception) else []
            profile_hits = results[2] if not isinstance(results[2], Exception) else []

            graph_schema_hits = []
            if has_graph:
                # graph schema 是第 4 路
                graph_schema_hits = results[3] if not isinstance(results[3], Exception) else []
                if isinstance(results[3], Exception):
                    logger.warning(f"[hybrid_v2] graph schema search failed: {results[3]}")

            # Proactive 路：intention（L7）从 VDB 召回（与 graph 解耦，全模式可用），
            # 过期惰性转 L2_FACT。仅当 intention_limit > 0 时启用。
            graph_intention_hits: List[Dict[str, Any]] = []
            if request.intention_limit > 0:
                graph_intention_hits = await recall_intentions(
                    vector_store,
                    query_embedding,
                    user_ids=isolation_params.get("user_ids"),
                    agent_ids=isolation_params.get("agent_ids"),
                    limit=request.intention_limit,
                )

            if isinstance(results[0], Exception):
                logger.warning(f"[hybrid_v2] VDB semantic search failed: {results[0]}")
            if isinstance(results[1], Exception):
                logger.warning(f"[hybrid_v2] VDB keyword search failed: {results[1]}")

            _recall_ms = (datetime.now() - _t_recall).total_seconds() * 1000

            # Trace: recall results
            await trace_log.log_recall_vec(
                pool_size=vdb_sem_limit,
                hits=vdb_semantic_hits,
                elapsed_ms=_recall_ms,
            )
            await trace_log.log_recall_profile(
                profile_min_score=profile_min_score,
                profile_limit=profile_limit,
                hits=profile_hits,
            )

            # =========================================================
            # Stage 4: Semantic gate on VDB
            # =========================================================
            vdb_semantic_hits = [r for r in vdb_semantic_hits if r["score"] >= MIN_SCORE]

            # =========================================================
            # Stage 5: BM25 score normalization
            # =========================================================
            # 后端的 keyword_search 分两类：
            #   - keyword_score_normalized=True（tencent sparse IP / qdrant binary）：
            #     分已在 [0,1]，直接用，不能再过 normalize_bm25（那个 sigmoid 为
            #     "经典 BM25 原始分 0~20" 标定，会把 ~1 的分压成 ~0.03）。
            #   - False（经典 BM25 原始分）：用 sigmoid 归一化。
            kw_normalized = getattr(vector_store, "keyword_score_normalized", False)
            midpoint, steepness = get_bm25_params(request.query, query_lemmatized)
            keyword_scores: Dict[str, float] = {}
            for r in vdb_keyword_hits:
                nid = r["node_id"]
                raw = r["score"]
                if kw_normalized:
                    keyword_scores[nid] = max(0.0, min(float(raw), 1.0))
                else:
                    keyword_scores[nid] = normalize_bm25(raw, midpoint, steepness)

            # =========================================================
            # Stage 6: VDB fusion (semantic + keyword, deduplicated)
            # =========================================================
            # 权重是否启用为「整个 query」级别的全局决策（对齐 mem0 的 has_bm25）：
            #   - keyword 通道全池无命中（keyword_scores 为空）→ 不加权，
            #     final = semantic（满权重 1.0）。否则一条 bm25=0 的 mem 会被
            #     sem*0.6 平白拉低，甚至低于「纯语义模式」的分。
            #   - keyword 通道有任意命中 → 全池统一走 sem*W_SEM + bm25*W_BM25，
            #     不会出现同一 query 里两条相近 mem 因一条有 bm25、一条没有而走
            #     不同权重的情况。
            has_bm25 = bool(keyword_scores)
            seen_vdb: Dict[str, int] = {}
            vdb_scored: List[Dict[str, Any]] = []

            # Process semantic hits first
            for r in vdb_semantic_hits:
                nid = r["node_id"]
                sem_score = r["score"]
                bm25_score = keyword_scores.get(nid, 0.0)
                if has_bm25:
                    final = score_vdb_node(sem_score, bm25_score, W_SEM, W_BM25)
                else:
                    final = sem_score  # 全池无 bm25 → 纯语义满权重
                seen_vdb[nid] = len(vdb_scored)
                vdb_scored.append({
                    "node_id": nid,
                    "node": r["node"],
                    "score": final,
                    "source": "vdb",
                    "_semantic": sem_score,
                    "_bm25": bm25_score,
                })

            # Add keyword-only hits (not found by semantic)
            # 仅当 keyword 通道有命中时才可能有 keyword-only（has_bm25 必为 True）
            for r in vdb_keyword_hits:
                nid = r["node_id"]
                if nid not in seen_vdb:
                    bm25_score = keyword_scores.get(nid, 0.0)
                    final = score_vdb_node(0.0, bm25_score, W_SEM, W_BM25)
                    if final > 0:  # Only include if contributes score
                        seen_vdb[nid] = len(vdb_scored)
                        vdb_scored.append({
                            "node_id": nid,
                            "node": r["node"],
                            "score": final,
                            "source": "vdb_keyword_only",
                            "_semantic": 0.0,
                            "_bm25": bm25_score,
                        })

            # Memory Strength（默认关闭）：normal 通道 idle 衰减 × 频次（profile 层
            # L0/L4 不参与），在 sort 前乘进 score。
            if getattr(self.config.recall, "strength_enabled", False):
                _tau = getattr(self.config.recall, "strength_tau", 180.0)
                _profile_vals = {l.value for l in _PROFILE_LAYERS}
                apply_strength_to_normal(vdb_scored, profile_layers=_profile_vals, score_key="score", tau=_tau)

            # Sort VDB by final score
            vdb_scored.sort(key=lambda x: x["score"], reverse=True)

            # Trace: BM25/keyword fusion
            await trace_log.log_bm25(
                pool_size=len(vdb_semantic_hits),
                query_terms=query_lemmatized.split() if query_lemmatized else [],
                hits=vdb_scored[:20],
                raw_hits=vdb_keyword_hits,
                bm25_scores=keyword_scores,
                normalize_method="passthrough" if kw_normalized else "sigmoid",
                sigmoid_midpoint=None if kw_normalized else midpoint,
                sigmoid_steepness=None if kw_normalized else steepness,
                has_bm25=has_bm25,
                elapsed_ms=0,
            )

            # =========================================================
            # Stage 7: Profile 正路 —— graph L6 evidence boost
            # =========================================================
            # 正路：graph vector_search 命中的 L6（已过 profile_min_score），按
            # 证据数加成排序。每项 score = raw 语义分；_internal 仅用于正路内部排序。
            forward_l6: List[Dict[str, Any]] = []
            if has_graph and graph_schema_hits:
                for r in graph_schema_hits:
                    ev_count = 0
                    try:
                        ev_refs = await self._graph_store.get_evidence_vdbrefs(r["node_id"])
                        ev_count = len(ev_refs) if ev_refs else 0
                    except Exception:
                        pass
                    ev_boost = compute_evidence_boost(ev_count, EV_SATURATE, EV_BOOST_MAX)
                    internal_score = r["score"] * (1.0 + ev_boost)
                    forward_l6.append({
                        "node_id": r["node_id"],
                        "node": None,
                        "content": r.get("content", ""),
                        "score": r["score"],
                        "source": "profile_forward",
                        "layer": r.get("layer", "l6_schema"),
                        "confidence": r.get("confidence", r["score"]),
                        "_internal": internal_score,
                        "_evidence_count": ev_count,
                    })
                forward_l6.sort(key=lambda x: x["_internal"], reverse=True)

            # =========================================================
            # Stage 8: Profile 反路 + RRF 融合
            # =========================================================
            # 反路：从 normal 命中的 VDB 节点反查支撑它们的 L6（按支撑度排序，不卡阈值）。
            reverse_l6: List[Dict[str, Any]] = []
            if has_graph and vdb_scored:
                reverse_l6 = await reverse_lookup_l6(
                    self._graph_store,
                    [v["node_id"] for v in vdb_scored],
                    limit=graph_limit,
                )

            # 正反 RRF 融合（rank 级，规避正路语义分 vs 反路支撑度不可比）。
            # RRF 输出只保留 node_id/node，content/layer 需从 node_id→item map 重新 hydrate。
            fused_l6: List[Dict[str, Any]] = []
            if forward_l6 or reverse_l6:
                _l6_by_id: Dict[str, Dict[str, Any]] = {}
                for it in forward_l6 + reverse_l6:
                    _l6_by_id.setdefault(it["node_id"], it)  # 正路优先保留（先填）
                fused = rrf_fuse({"forward": forward_l6, "reverse": reverse_l6})
                for f in fused:
                    src = _l6_by_id.get(f["node_id"], {})
                    fused_l6.append({
                        "node_id": f["node_id"],
                        "node": None,
                        "content": src.get("content", ""),
                        "layer": src.get("layer", "l6_schema"),
                        "score": float(f.get("rrf_score", 0.0)),
                        "source": "profile_l6",
                        "confidence": src.get("confidence"),
                    })

            # =========================================================
            # Stage 9: Intention（不占 limit 配额，末尾追加）
            # =========================================================
            intention_results = list(graph_intention_hits)

            # =========================================================
            # Stage 10: 演化链展开（normal VDB + profile L0；均有 node 对象）
            # =========================================================
            _t_evo = datetime.now()
            expandable = vdb_scored + list(profile_hits)
            if expandable:
                expanded = await expand_evolution_chains(vector_store, expandable)
            else:
                expanded = []
            _evo_ms = (datetime.now() - _t_evo).total_seconds() * 1000
            await trace_log.log_evolution(
                input_size=len(expandable),
                evolved_count=sum(1 for r in expanded if r.get("is_evolved")),
                elapsed_ms=_evo_ms,
            )

            # =========================================================
            # Stage 11: 组装 —— normal（截断 + 出口阈值）+ profile（L0 置顶 + 融合 L6）
            # =========================================================
            _profile_layer_vals = {l.value for l in _PROFILE_LAYERS}

            def _layer_of(item: Dict[str, Any]) -> str:
                nd = item.get("node")
                return nd.layer.value if (nd and getattr(nd, "layer", None)) else (item.get("layer") or "")

            # normal：演化展开后的 VDB 项（排除 profile 层），按分排序 + 截断 + MIN_SCORE 出口 gate
            normal_results = [it for it in expanded if _layer_of(it) not in _profile_layer_vals]
            normal_results.sort(key=lambda x: x.get("score", 0), reverse=True)
            normal_results = normal_results[:final_limit]
            _NORMAL_SOURCES = {"vdb", "vdb_keyword_only"}
            normal_results = [
                it for it in normal_results
                if it.get("source") not in _NORMAL_SOURCES
                or it.get("score", 0.0) >= MIN_SCORE
            ]

            # profile：L0（演化展开后置顶）+ 融合 L6。profile_min_score 只作用于正路 threshold，
            # 这里不再卡阈值（反路/RRF 融合分可能低于它，照常返回）。
            # profile_limit 语义：0 → 返回 0 条；<0 → 不限制；>0 → 截断（L0 置顶优先保留）。
            l0_results = [it for it in expanded if _layer_of(it) in _profile_layer_vals]
            l0_results.sort(key=lambda x: x.get("score", 0), reverse=True)
            profile_results = l0_results + fused_l6
            if profile_limit == 0:
                profile_results = []
            elif profile_limit > 0:
                profile_results = profile_results[:profile_limit]

            await trace_log.log_merge_profile(
                fused_size=len(vdb_scored),
                profile_size=len(l0_results) + len(fused_l6),
                merged_size=len(normal_results) + len(profile_results),
                final_limit=final_limit,
            )

            # 最终结果：normal + profile（L0/L6 由 client 按 layer 分桶进 profile）+ intention
            final_results = normal_results + profile_results
            if intention_results:
                final_results = final_results + intention_results

            # =========================================================
            # Stage 12: Build response
            # =========================================================
            for item in final_results:
                node: Optional[MemoryNode] = item.get("node")
                node_id = item.get("node_id", "")
                is_evolved = bool(item.get("is_evolved"))

                if node:
                    # VDB node
                    content = node.content
                    layer = node.layer.value
                    speculate = getattr(node, "speculate", None)
                    source_raw_memory_id = getattr(node, "source_raw_memory_id", None)
                    tags = list(node.tags) if getattr(node, "tags", None) else []
                    memory_at = int(node.memory_at.timestamp()) if node.memory_at else None
                    gmt_created = int(node.gmt_created.timestamp()) if node.gmt_created else None
                else:
                    # Graph node
                    content = item.get("content", "")
                    layer = item.get("layer", "l6_schema")
                    speculate = None
                    source_raw_memory_id = None
                    tags = []
                    memory_at = None
                    gmt_created = None

                display_score = item.get("score", 0.0)

                mem_entry = {
                    "memory_id": node_id,
                    "content": content,
                    "layer": layer,
                    "score": float(display_score),
                    "access_count": getattr(node, "access_count", 0) if node else 0,
                    "owner": getattr(node, "owner", None) if node else None,
                    "speculate": speculate,
                    "source_raw_memory_id": source_raw_memory_id,
                    "tags": tags,
                    "memory_at": memory_at,
                    "gmt_created": gmt_created,
                    "source": item.get("source", ""),
                }
                if is_evolved:
                    mem_entry["evolution_chain"] = item.get("evolution_chain", [])

                response.memories.append(mem_entry)

            response.total_found = len(final_results)
            response.extra["reader"] = self.VERSION
            response.extra["channels"] = {
                "vdb_semantic": len(vdb_semantic_hits),
                "vdb_keyword": len(vdb_keyword_hits),
                "profile_forward_l6": len(forward_l6),
                "profile_reverse_l6": len(reverse_l6),
                "profile_fused_l6": len(fused_l6),
                "graph_intention": len(graph_intention_hits),
                "profile_l0": len(profile_hits),
            }
            response.extra["config"] = {
                "w_sem": W_SEM,
                "w_bm25": W_BM25,
                "ev_boost_max": EV_BOOST_MAX,
                "ev_saturate": EV_SATURATE,
                "min_score": MIN_SCORE,
            }

            tracer.set_output({
                "success": True,
                "total_found": response.total_found,
                "pipeline_ms": (datetime.now() - start_time).total_seconds() * 1000,
                "channels": response.extra["channels"],
            })

            logger.info(
                f"[read/hybrid_v2] "
                f"vdb_sem={len(vdb_semantic_hits)} vdb_kw={len(vdb_keyword_hits)} "
                f"profile_l0={len(profile_hits)} fwd_l6={len(forward_l6)} rev_l6={len(reverse_l6)} "
                f"fused_l6={len(fused_l6)} graph_intent={len(graph_intention_hits)} "
                f"returned={len(final_results)} "
                f"query='{request.query[:80]}'"
            )

            response.success = True

        except Exception as e:
            logger.error(f"HybridV2ReadPipeline.read failed: {e}", exc_info=True)
            response.error_code = 500
            response.error_message = str(e)
            tracer.set_error(str(e))

        response.elapsed_ms = (datetime.now() - start_time).total_seconds() * 1000

        # Summary trace log
        await trace_log.log_summary(
            query=request.query,
            intent=None,
            confidence=None,
            is_low_confidence=None,
            channels={
                "vdb_semantic": len(vdb_semantic_hits),
                "vdb_keyword": len(vdb_keyword_hits),
                "graph_schema": len(graph_schema_hits),
                "graph_intention": len(graph_intention_hits),
                "profile": len(profile_hits),
            },
            total_found=response.total_found,
            elapsed_ms=response.elapsed_ms,
            returned_memories=response.memories,
        )

        return response

    async def close(self) -> None:
        logger.debug("HybridV2ReadPipeline closed")

    # ==================================================================
    # Helpers
    # ==================================================================

    def _get_isolation_key(self, request: ReadRequest) -> str:
        """Build isolation key for graph store queries."""
        user_id = request.user_id or (request.user_ids[0] if request.user_ids else "")
        agent_id = request.agent_id or "default"
        return MemoryNode.build_isolation_key(user_id, agent_id)

    def _build_isolation_params(self, request: ReadRequest) -> Dict[str, Any]:
        """
        Build isolation parameters (same logic as hybrid reader).
        """
        user_ids = request.user_ids if request.user_ids else ([request.user_id] if request.user_id else [])
        agent_ids = request.agent_ids
        session_ids = request.session_ids

        if len(agent_ids) > 1 and len(session_ids) > 1:
            return {
                "error_msg": (
                    "Cannot specify multiple agent_ids and multiple session_ids simultaneously. "
                    "Use single agent_id with multiple session_ids, or multiple agent_ids without session_ids."
                )
            }

        isolation_key = ""
        isolation_keys = None
        search_user_ids = None
        search_agent_ids = None

        if not agent_ids and not session_ids:
            if request.agent_id:
                keys = [
                    MemoryNode.build_isolation_key(u, request.agent_id or "default")
                    for u in user_ids
                ] if user_ids else None
                if keys and len(keys) == 1:
                    isolation_key = keys[0]
                else:
                    isolation_keys = keys
            else:
                search_user_ids = user_ids if user_ids else None
        elif agent_ids and not session_ids:
            search_user_ids = user_ids if user_ids else None
            search_agent_ids = agent_ids
        else:
            effective_agent_ids = agent_ids if agent_ids else [request.agent_id or "default"]
            isolation_keys = [
                MemoryNode.build_isolation_key(u, a, s)
                for u in (user_ids or ["default"])
                for a in effective_agent_ids
                for s in session_ids
            ]

        return {
            "isolation_key": isolation_key,
            "isolation_keys": isolation_keys,
            "user_ids": search_user_ids,
            "agent_ids": search_agent_ids,
        }

