"""
HybridTag Read Pipeline —— 路 A ∪ 路 B + 路 C + RRF(3).

  query → 正则分词 → batch embed keywords
       → 对每个 keyword 向量在 per-user tag_index 里查 topK 命中 tag
       → 用命中 tag 做 `tags MatchAny` filter 再发一次向量召回 → 路 B 候选池
  pool_for_bm25 = dedupe(vec_hits ∪ tag_hits)
  BM25 对合并池重排 → bm25_hits
  RRF(vec, tag, bm25) → 意图权重 3 路融合
  演化链合成 / profile 优先并入 / 弃权信号

向量召回 + BM25-lite（池内内存打分）+ tag 桥接，后端无关。

Backend 能力探测：
  - 如果 vector_store 未实现 tag_index 接口（_supports_tag_index=False）
    → init 时 warning 一次；read 阶段路 B 返回空，RRF 退化为「向量 + BM25」两路。
"""

from typing import Any, Dict, List, Optional
from datetime import datetime
import asyncio
import logging

from .base import ReadPipeline, ReadRequest, ReadResponse, PipelineContext
from ..config import MemoryConfig
from ..core.embed_service import EmbedService
from ..models.memory import MemoryNode, MemoryLayer
from ..data.vector_store import create_vector_store
from ..data.vector_store_base import VectorStoreBase
from ..utils.tracer import PipelineTracer, create_tracer
from ._retrieval import config as rconf
from ._retrieval import intent as rintent
from ._retrieval import bm25 as rbm25
from ._retrieval import rrf as rrrf
from ._retrieval import tag_index as rtag
from ._retrieval.evolution import expand_evolution_chains
from ._retrieval.intention import recall_intentions
from ._retrieval.strength import apply_strength_to_normal
from ._retrieval.lemmatize import lemmatize_for_bm25
from ._retrieval.trace import ReadTraceLogger

logger = logging.getLogger(__name__)


# 向量 / tag / BM25 主路覆盖全层（profile 层独立召回，避免被 RRF 挤出）
_PROFILE_LAYERS = [MemoryLayer.L0_BASIC_INFO, MemoryLayer.L6_SCHEMA]


class HybridTagReadPipeline(ReadPipeline):
    """Hybrid-Tag 读取器（路 A + 路 B + 路 C + RRF 3 路）。"""

    VERSION = "hybrid_tag"

    def __init__(
        self,
        config: MemoryConfig,
        embed_service: Optional[EmbedService] = None,
        vector_store: Optional[VectorStoreBase] = None,
        cache: Any = None,
    ):
        self.config = config
        self._embed_service = embed_service
        self._external_vector_store = vector_store
        self._cache = cache
        self._vector_store: Optional[VectorStoreBase] = None
        self._vector_store_initialized = False
        self._initialized = False
        # 能力探测延到 initialize 里做，避免构造时 vector_store 还没就绪
        self._tag_backend_supported: Optional[bool] = None

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

    async def initialize(self) -> None:
        if self._initialized:
            return
        if self._embed_service is None:
            self._embed_service = EmbedService(self.config)
        self._vector_store = self._external_vector_store or create_vector_store(self.config)
        if self._external_vector_store and getattr(self._external_vector_store, "_client", None):
            self._vector_store_initialized = True
        self._initialized = True

        supported = rtag.backend_supports_tag_index(self._vector_store)
        self._tag_backend_supported = supported
        if not supported:
            logger.warning(
                "[reader-hybrid-tag] backend %s does not implement tag_index; "
                "route B will return empty (fallback to vec+bm25)",
                type(self._vector_store).__name__,
            )
        else:
            logger.debug(
                "[reader-hybrid-tag] backend %s supports tag_index",
                type(self._vector_store).__name__,
            )

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
                pipeline_version="hybrid_tag",
                uid=request.user_id,
                agent_id=request.agent_id,
                content_preview=request.query,
            )

        # Read-side pipeline_logs tracer
        trace_log = ReadTraceLogger(
            cache=self._cache,
            request_id=request.request_id,
            user_id=request.user_id or (request.user_ids[0] if request.user_ids else ""),
            agent_id=request.agent_id or "default_agent",
            reader_version=self.VERSION,
        )

        # 预声明以便异常分支下 summary 安全
        vec_hits: List[Dict[str, Any]] = []
        tag_hits: List[Dict[str, Any]] = []
        bm25_hits: List[Dict[str, Any]] = []
        profile_hits: List[Dict[str, Any]] = []
        hit_tags: List[str] = []

        try:
            if not request.query:
                response.error_code = 400
                response.error_message = "query is required"
                return response

            # 入参快照
            await trace_log.log_request(
                query=request.query,
                limit=request.limit,
                layers=list(request.layers) if request.layers else None,
                profile_min_score=request.profile_min_score,
                profile_limit=request.profile_limit,
                user_ids=list(request.user_ids or []),
                agent_ids=list(request.agent_ids or []),
                session_ids=list(request.session_ids or []),
            )

            # 1. embed query
            _t_embed = datetime.now()
            with tracer.span("embed_query") as s:
                query_embedding = request.query_embedding
                cache_hit = False
                if not query_embedding:
                    query_embedding = await self.embed_service.embed(request.query)
                else:
                    cache_hit = True
                s.set_output({"dims": len(query_embedding)})
            _embed_ms = (datetime.now() - _t_embed).total_seconds() * 1000
            await trace_log.log_embed_query(
                query=request.query,
                dims=len(query_embedding),
                cache_hit=cache_hit,
                elapsed_ms=_embed_ms,
            )

            # 2. 隔离参数
            isolation_params = self._build_isolation_params(request)
            if isolation_params.get("error_msg"):
                response.error_code = 400
                response.error_message = isolation_params["error_msg"]
                return response

            # 3. 意图分类
            intent = rintent.classify_intent(request.query)
            await trace_log.log_intent(
                query=request.query,
                intent=intent,
                keywords=rintent.extract_keywords(request.query),
            )

            # 4. keyword 提取 & 并行 batch embed（只在 backend 支持时做）
            keywords: List[str] = []
            kw_vecs: List[List[float]] = []
            if self._tag_backend_supported:
                keywords = rintent.extract_keywords(request.query)
                if keywords:
                    _t_kw = datetime.now()
                    with tracer.span("embed_keywords") as s:
                        try:
                            kw_vecs = await self.embed_service.embed_batch(keywords)
                        except Exception as e:
                            logger.debug(f"[reader-hybrid-tag] embed_batch failed: {e}")
                            kw_vecs = []
                        s.set_output({"keyword_count": len(keywords), "vec_count": len(kw_vecs)})
                    _kw_ms = (datetime.now() - _t_kw).total_seconds() * 1000
                    await trace_log.log_keyword_embed(
                        keywords=keywords,
                        vec_count=len(kw_vecs),
                        elapsed_ms=_kw_ms,
                    )

            # 5. 并行：路 A（主向量）+ 路 B 预备（tag 检索） + profile 路
            final_limit = request.limit if request.limit > 0 else 10
            vec_pool_size = max(rconf.VEC_POOL_SIZE, final_limit)
            vector_store = self._vector_store
            layers_filter = self._parse_layers(request)

            profile_limit = request.profile_limit
            profile_min_score = request.profile_min_score

            _t_stage1 = datetime.now()
            with tracer.span("recall_stage_1") as s:
                vec_hits, hit_tags, profile_hits = await asyncio.gather(
                    self._recall_main(
                        vector_store=vector_store,
                        query_embedding=query_embedding,
                        isolation_params=isolation_params,
                        layers_filter=layers_filter,
                        pool_size=vec_pool_size,
                    ),
                    self._find_matching_tags(kw_vecs, request.user_id),
                    self._recall_profile(
                        vector_store=vector_store,
                        query_embedding=query_embedding,
                        isolation_params=isolation_params,
                        layers_filter=layers_filter,
                        profile_min_score=profile_min_score,
                        profile_limit=profile_limit,
                    ),
                )
                s.set_output({
                    "vec_pool_size": len(vec_hits),
                    "profile_size": len(profile_hits),
                    "hit_tag_count": len(hit_tags),
                    "hit_tags": hit_tags[:10],
                    "intent": intent,
                })
            _stage1_ms = (datetime.now() - _t_stage1).total_seconds() * 1000
            await trace_log.log_recall_vec(
                pool_size=vec_pool_size,
                hits=vec_hits,
                elapsed_ms=_stage1_ms,
            )
            await trace_log.log_recall_profile(
                profile_min_score=profile_min_score,
                profile_limit=profile_limit,
                hits=profile_hits,
            )
            await trace_log.log_tag_match(
                keywords=keywords,
                hit_tags=hit_tags,
                topk=rconf.TAG_MATCH_TOPK,
                min_score=rconf.TAG_MATCH_MIN_SCORE,
            )

            # 6. 路 B：用命中 tag 做 filter 再发一次向量召回（共用 query_embedding）
            if hit_tags:
                _t_tagr = datetime.now()
                with tracer.span("recall_tag_filter") as s:
                    try:
                        tag_hits = await self._recall_tag_filtered(
                            vector_store=vector_store,
                            query_embedding=query_embedding,
                            isolation_params=isolation_params,
                            layers_filter=layers_filter,
                            hit_tags=hit_tags,
                            pool_size=rconf.TAG_POOL_SIZE,
                        )
                    except Exception as e:
                        logger.warning(f"[reader-hybrid-tag] tag-filtered search failed: {e}")
                        tag_hits = []
                    s.set_output({"tag_hits": len(tag_hits)})
                _tagr_ms = (datetime.now() - _t_tagr).total_seconds() * 1000
                await trace_log.log_recall_tag(
                    hit_tags=hit_tags,
                    pool_size=rconf.TAG_POOL_SIZE,
                    hits=tag_hits,
                    elapsed_ms=_tagr_ms,
                )

            # 7. 合并池子（A ∪ B）去重后跑 BM25
            merged_pool = self._merge_dedup(vec_hits, tag_hits)
            _t_bm = datetime.now()
            with tracer.span("bm25") as s:
                bm25_hits = self._run_bm25(request.query, merged_pool)
                s.set_output({
                    "pool_size": len(merged_pool),
                    "bm25_size": len(bm25_hits),
                })
            _bm_ms = (datetime.now() - _t_bm).total_seconds() * 1000
            await trace_log.log_bm25(
                pool_size=len(merged_pool),
                query_terms=rbm25.tokenize(request.query),
                hits=bm25_hits,
                elapsed_ms=_bm_ms,
            )

            # 8. RRF 融合（3 路：vec / tag / bm25）
            weights = rconf.INTENT_WEIGHTS_3CHANNEL.get(intent, rconf.INTENT_WEIGHTS_3CHANNEL["FACTUAL"])
            fused = rrrf.rrf_fuse(
                channels={"vec": vec_hits, "tag": tag_hits, "bm25": bm25_hits},
                weights=weights,
            )
            confidence = rrrf.compute_confidence(fused, top_n=3)
            is_low_confidence = confidence < rconf.ABSTAIN_THRESHOLD
            await trace_log.log_rrf(
                channels={"vec": len(vec_hits), "tag": len(tag_hits), "bm25": len(bm25_hits)},
                weights=weights,
                fused=fused,
                confidence=confidence,
                is_low_confidence=is_low_confidence,
            )

            # 8.5 Memory Strength（默认关闭）：normal 通道 idle 衰减 × 频次（fused 为 normal-only）
            if getattr(self.config.recall, "strength_enabled", False):
                _tau = getattr(self.config.recall, "strength_tau", 180.0)
                apply_strength_to_normal(fused, score_key="rrf_score", tau=_tau)
                fused.sort(key=lambda x: x.get("rrf_score", 0.0), reverse=True)

            # 9. profile 并入 + 截断 + 演化链合成
            merged = self._merge_with_profile(fused, profile_hits, profile_limit)
            truncated = merged[:final_limit]
            await trace_log.log_merge_profile(
                fused_size=len(fused),
                profile_size=len(profile_hits),
                merged_size=len(merged),
                final_limit=final_limit,
            )

            _t_evo = datetime.now()
            final_results = await expand_evolution_chains(vector_store, truncated)
            _evo_ms = (datetime.now() - _t_evo).total_seconds() * 1000
            await trace_log.log_evolution(
                input_size=len(truncated),
                evolved_count=sum(1 for r in final_results if r.get("is_evolved")),
                elapsed_ms=_evo_ms,
            )

            # 9b. Proactive 路：召回未过期 intention（L7），过期惰性转 L2_FACT。
            intention_hits: List[Dict[str, Any]] = []
            if request.intention_limit > 0:
                intention_hits = await recall_intentions(
                    vector_store,
                    query_embedding,
                    user_ids=isolation_params.get("user_ids"),
                    agent_ids=isolation_params.get("agent_ids"),
                    limit=request.intention_limit,
                )

            # 10. 填充响应
            for item in final_results + intention_hits:
                node: Optional[MemoryNode] = item.get("node")
                node_id = item.get("node_id", "")
                content = node.content if node else ""
                is_evolved = bool(item.get("is_evolved"))
                display_score = item.get("rrf_score", item.get("score", 0.0))
                speculate = None if is_evolved else (getattr(node, "speculate", None) if node else None)
                source_raw_memory_id = getattr(node, "source_raw_memory_id", None) if node else None
                tags = list(node.tags) if (node and getattr(node, "tags", None)) else []

                response.memories.append({
                    "memory_id": node_id,
                    "content": content,
                    "layer": item.get("layer") or (node.layer.value if node else ""),
                    "score": float(display_score),
                    "access_count": getattr(node, "access_count", 0) if node else 0,
                    "owner": getattr(node, "owner", None) if node else None,
                    "speculate": speculate,
                    "source_raw_memory_id": source_raw_memory_id,
                    "tags": tags,
                    "memory_at": int(node.memory_at.timestamp()) if (node and node.memory_at) else None,
                    "gmt_created": int(node.gmt_created.timestamp()) if (node and node.gmt_created) else None,
                })

            response.total_found = len(response.memories)
            response.extra.update({
                "intent": intent,
                "confidence": round(float(confidence), 4),
                "is_low_confidence": is_low_confidence,
                "reader": self.VERSION,
                "hit_tag_count": len(hit_tags),
                "tag_backend_supported": bool(self._tag_backend_supported),
            })

            tracer.set_output({
                "success": True,
                "total_found": response.total_found,
                "pipeline_ms": (datetime.now() - start_time).total_seconds() * 1000,
                "intent": intent,
                "confidence": round(float(confidence), 4),
                "is_low_confidence": is_low_confidence,
                "vec_pool_size": len(vec_hits),
                "tag_hits_size": len(tag_hits),
                "bm25_pool_size": len(bm25_hits),
                "profile_size": len(profile_hits),
                "hit_tags": hit_tags[:10],
                "returned_memories": [
                    {
                        "memory_id": m["memory_id"],
                        "content": m["content"],
                        "layer": m["layer"],
                        "score": round(m["score"], 6),
                        "speculate": m.get("speculate"),
                        "tags": m.get("tags") or [],
                    }
                    for m in response.memories
                ],
            })

            logger.info(
                f"[lite-read/hybrid_tag] intent={intent} conf={confidence:.3f} "
                f"vec={len(vec_hits)} tag={len(tag_hits)} bm25={len(bm25_hits)} "
                f"profile={len(profile_hits)} hit_tags={len(hit_tags)} "
                f"returned={len(final_results)} low_conf={is_low_confidence} "
                f"for query='{request.query}'"
            )

            response.success = True

        except Exception as e:
            logger.error(f"HybridTagReadPipeline.read failed: {e}", exc_info=True)
            response.error_code = 500
            response.error_message = str(e)
            tracer.set_error(str(e))

        response.elapsed_ms = (datetime.now() - start_time).total_seconds() * 1000

        # 读路径总览
        await trace_log.log_summary(
            query=request.query,
            intent=response.extra.get("intent"),
            confidence=response.extra.get("confidence"),
            is_low_confidence=response.extra.get("is_low_confidence"),
            channels={
                "vec": len(vec_hits),
                "tag": len(tag_hits),
                "bm25": len(bm25_hits),
                "profile": len(profile_hits),
                "hit_tag_count": len(hit_tags),
            },
            total_found=response.total_found,
            elapsed_ms=response.elapsed_ms,
            returned_memories=response.memories,
        )

        return response

    async def close(self) -> None:
        logger.debug("HybridTagReadPipeline closed")

    # ==================================================================
    # Route B helpers
    # ==================================================================

    async def _find_matching_tags(
        self,
        keyword_embeddings: List[List[float]],
        user_id: str,
    ) -> List[str]:
        """通过 tag_index 查找与 keyword embedding 语义接近的 tag 集合。"""
        if not keyword_embeddings or not user_id or not self._tag_backend_supported:
            return []
        return await rtag.search_matching_tags(
            vector_store=self._vector_store,
            user_id=user_id,
            keyword_embeddings=keyword_embeddings,
            topk=rconf.TAG_MATCH_TOPK,
            min_score=rconf.TAG_MATCH_MIN_SCORE,
        )

    async def _recall_tag_filtered(
        self,
        vector_store,
        query_embedding: List[float],
        isolation_params: Dict[str, Any],
        layers_filter: Optional[List[MemoryLayer]],
        hit_tags: List[str],
        pool_size: int,
    ) -> List[Dict[str, Any]]:
        """
        路 B 召回：带 tags MatchAny filter 的向量召回。
        搜索 layer = 所有非 profile 层（与路 A 一致），再叠加 tag 过滤。
        """
        if layers_filter is not None:
            layers = [l for l in layers_filter if l not in _PROFILE_LAYERS]
            if not layers:
                return []
        else:
            layers = [l for l in MemoryLayer.all_layers() if l not in _PROFILE_LAYERS]

        return await vector_store.search(
            query_embedding=query_embedding,
            isolation_key=isolation_params.get("isolation_key", ""),
            isolation_keys=isolation_params.get("isolation_keys"),
            user_ids=isolation_params.get("user_ids"),
            agent_ids=isolation_params.get("agent_ids"),
            layers=layers,
            limit=pool_size,
            tags_match_any=hit_tags,
        )

    @staticmethod
    def _merge_dedup(*hit_lists: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        把多路向量召回结果合并去重（按 node_id）。保留每路中 node_id 第一次出现的
        hit（含 node 对象，供 BM25 取 content 用）。
        """
        seen = set()
        merged: List[Dict[str, Any]] = []
        for lst in hit_lists:
            for h in lst or []:
                nid = h.get("node_id")
                if not nid or nid in seen:
                    continue
                seen.add(nid)
                merged.append(h)
        return merged

    # ==================================================================
    # 通用 helpers（向量召回 / 隔离 / BM25 / profile 合并）
    # ==================================================================

    def _parse_layers(self, request: ReadRequest) -> Optional[List[MemoryLayer]]:
        if not request.layers:
            return None
        try:
            return [MemoryLayer.from_string(l) for l in request.layers]
        except Exception:
            return None

    def _build_isolation_params(self, request: ReadRequest) -> Dict[str, Any]:
        """隔离参数构建，额外返回 error_msg 字段表示参数非法。"""
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

    async def _recall_main(
        self,
        vector_store: VectorStoreBase,
        query_embedding: List[float],
        isolation_params: Dict[str, Any],
        layers_filter: Optional[List[MemoryLayer]],
        pool_size: int,
    ) -> List[Dict[str, Any]]:
        """路 A：主向量召回。搜索全 layer（减去 profile layer）。"""
        if layers_filter is not None:
            layers = [l for l in layers_filter if l not in _PROFILE_LAYERS]
            if not layers:
                return []
        else:
            layers = [l for l in MemoryLayer.all_layers() if l not in _PROFILE_LAYERS]

        return await vector_store.search(
            query_embedding=query_embedding,
            isolation_key=isolation_params.get("isolation_key", ""),
            isolation_keys=isolation_params.get("isolation_keys"),
            user_ids=isolation_params.get("user_ids"),
            agent_ids=isolation_params.get("agent_ids"),
            layers=layers,
            limit=pool_size,
        )

    async def _recall_profile(
        self,
        vector_store: VectorStoreBase,
        query_embedding: List[float],
        isolation_params: Dict[str, Any],
        layers_filter: Optional[List[MemoryLayer]],
        profile_min_score: float,
        profile_limit: int,
    ) -> List[Dict[str, Any]]:
        """Profile 路：L0_BASIC_INFO + L6_SCHEMA，按 profile_min_score 过滤。"""
        if layers_filter is not None and not any(l in layers_filter for l in _PROFILE_LAYERS):
            return []
        effective = (
            [l for l in _PROFILE_LAYERS if l in layers_filter]
            if layers_filter is not None
            else _PROFILE_LAYERS
        )
        p_limit = profile_limit if profile_limit > 0 else 100
        hits = await vector_store.search(
            query_embedding=query_embedding,
            isolation_key=isolation_params.get("isolation_key", ""),
            isolation_keys=isolation_params.get("isolation_keys"),
            user_ids=isolation_params.get("user_ids"),
            agent_ids=isolation_params.get("agent_ids"),
            limit=p_limit,
            layers=effective,
            score_threshold=profile_min_score,
        )
        if profile_limit > 0:
            hits = hits[:profile_limit]
        return hits

    @staticmethod
    def _run_bm25(
        query: str,
        vec_hits: List[Dict[str, Any]],
    ) -> List[Dict[str, Any]]:
        """对向量召回池跑 BM25-lite，返回按 BM25 分数降序的 hits。"""
        if not vec_hits:
            return []
        query_lemmatized = lemmatize_for_bm25(query)
        q_terms = rbm25.tokenize(query_lemmatized or query)
        if not q_terms:
            return []
        contents: List[str] = []
        for h in vec_hits:
            node: Optional[MemoryNode] = h.get("node")
            raw = node.content if node else ""
            contents.append(lemmatize_for_bm25(raw) if raw else "")
        raw = rbm25.compute_bm25_scores(q_terms, contents)
        max_s = max(raw) if raw else 0.0
        if max_s <= 0:
            return []
        scored: List[Dict[str, Any]] = []
        for i, h in enumerate(vec_hits):
            s = raw[i] / max_s
            if s <= 0:
                continue
            scored.append({
                "node_id": h.get("node_id"),
                "node": h.get("node"),
                "score": float(s),
            })
        scored.sort(key=lambda x: x["score"], reverse=True)
        return scored

    @staticmethod
    def _merge_with_profile(
        fused: List[Dict[str, Any]],
        profile_hits: List[Dict[str, Any]],
        profile_limit: int,
    ) -> List[Dict[str, Any]]:
        """把 profile 路结果插到 fused 前面（按 node_id 去重）。"""
        seen = set()
        merged: List[Dict[str, Any]] = []
        for p in profile_hits:
            nid = p.get("node_id")
            if not nid or nid in seen:
                continue
            seen.add(nid)
            merged.append({
                "node_id": nid,
                "node": p.get("node"),
                "score": float(p.get("score", 0.0)),
                "from_profile": True,
            })
        for f in fused:
            nid = f.get("node_id")
            if not nid or nid in seen:
                continue
            seen.add(nid)
            enriched = dict(f)
            enriched.setdefault("score", enriched.get("rrf_score", 0.0))
            merged.append(enriched)
        return merged
