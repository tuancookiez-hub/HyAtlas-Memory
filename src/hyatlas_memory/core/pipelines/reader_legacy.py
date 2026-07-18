"""
Legacy Read Pipeline.

本实现 = post13 之前 `reader.py` 的原始行为，一字不差（仅两点工程性调整）：

1. class 名从 `LiteReadPipeline` 改为 `LegacyReadPipeline`；对外统一入口
   仍是 `reader.py::LiteReadPipeline`，由 dispatcher 按环境变量选择。
2. 演化链合成从内联函数改为调用 `_retrieval/evolution.py::expand_evolution_chains`，
   三个 reader 共享同一实现。
3. post15 新增：接受 cache 参数，把 read 阶段的关键步骤写入 pipeline_logs 表
   （step 前缀 `READ_*`），供 inspector 查询 search trace。

行为语义：profile 路（L0_BASIC_INFO + L6_SCHEMA）+ 其他路两路并行
向量召回，profile 按 `profile_min_score` / `profile_limit` 过滤，最后合并 +
演化链回溯。L4_IDENTITY 按普通记忆走 normal 路（不算 profile）。
"""

import logging
import os
from datetime import datetime
from typing import Any

from ..config import MemoryConfig
from ..core.embed_service import EmbedService
from ..data.graph_store_base import GraphStoreBase
from ..data.vector_store import create_vector_store
from ..data.vector_store_base import VectorStoreBase
from ..models.memory import MemoryLayer, MemoryNode
from ..utils.tracer import PipelineTracer, create_tracer
from ._retrieval.evolution import expand_evolution_chains
from ._retrieval.intention import recall_intentions
from ._retrieval.strength import apply_strength_to_normal
from ._retrieval.trace import ReadTraceLogger
from .base import PipelineContext, ReadPipeline, ReadRequest, ReadResponse

logger = logging.getLogger(__name__)

_READER_ENABLE_SUMMARY = os.getenv("MEMORY_READER_ENABLE_SUMMARY", "false").lower() == "true"


# ── Profile 配额模式 ─────────────────────────────────────────────────────────
# "score"      — 纯按 score 排序（默认）
# "quota"      — 4:4:2 identity/schema 配额
PROFILE_MODE = "score"


# ── Profile 配额选取辅助 ──────────────────────────────────────────
# identity 层（L0 basic_info）和 schema 层（L6）各保底 40%，剩余 20% 按 score 竞争。
# 若某侧不足配额，空槽归入竞争池。

def _profile_quota_select(
    items: list,
    total_limit: int,
    identity_layer_vals: set,
    schema_layer_vals: set,
) -> list:
    """从 profile 结果池中按 identity/schema 配额选取。

    items 须已按 score 降序排列。返回值也按 score 降序。
    当 PROFILE_MODE != "quota" 时直接截断返回。
    """
    if total_limit <= 0 or not items:
        return items

    if PROFILE_MODE != "quota":
        return items[:total_limit]

    if total_limit <= 0 or not items:
        return items

    id_pool = []
    sc_pool = []
    for it in items:
        nd = it.get("node")
        lv = nd.layer.value if (nd and nd.layer) else ""
        if lv in identity_layer_vals:
            id_pool.append(it)
        elif lv in schema_layer_vals:
            sc_pool.append(it)
        else:
            id_pool.append(it)  # 未知层归 identity 侧

    id_quota = max(1, int(total_limit * 0.4))
    sc_quota = max(1, int(total_limit * 0.4))

    id_take = id_pool[:id_quota]
    sc_take = sc_pool[:sc_quota]

    free_slots = total_limit - len(id_take) - len(sc_take)
    if free_slots > 0:
        free_pool = sorted(
            id_pool[id_quota:] + sc_pool[sc_quota:],
            key=lambda x: x.get("score", 0),
            reverse=True,
        )
        free_take = free_pool[:free_slots]
    else:
        free_take = []

    return sorted(
        id_take + sc_take + free_take,
        key=lambda x: x.get("score", 0),
        reverse=True,
    )


class LegacyReadPipeline(ReadPipeline):
    """
    Legacy 读取器（profile 两路并行 + 演化链）。

    与 post13 之前行为完全一致，作为默认 reader，用于向后兼容。
    新增：L6_SCHEMA / L7_INTENTION 走 graph_store.vector_search()（如果有 graph_store）。
    """

    VERSION = "legacy"

    def __init__(
        self,
        config: MemoryConfig,
        embed_service: EmbedService | None = None,
        vector_store: VectorStoreBase | None = None,
        graph_store: GraphStoreBase | None = None,
        cache: Any = None,
    ):
        self.config = config
        self._embed_service = embed_service
        self._external_vector_store = vector_store
        self._graph_store = graph_store
        self._cache = cache
        self._vector_store: VectorStoreBase | None = None
        self._vector_store_initialized = False
        self._initialized = False

    async def initialize(self) -> None:
        if self._initialized:
            return
        if self._embed_service is None:
            self._embed_service = EmbedService(self.config)
        self._vector_store = self._external_vector_store or create_vector_store(self.config)
        if self._external_vector_store and getattr(self._external_vector_store, '_client', None):
            self._vector_store_initialized = True
        self._initialized = True
        logger.debug("LegacyReadPipeline initialized")

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

    async def read(
        self,
        request: ReadRequest,
        ctx: PipelineContext | None = None,
        tracer: PipelineTracer | None = None,
    ) -> ReadResponse:
        start_time = datetime.now()
        response = ReadResponse()

        if tracer is None:
            tracer = create_tracer(
                operation="read",
                pipeline_version="legacy",
                uid=request.user_id,
                agent_id=request.agent_id,
                content_preview=request.query,
            )

        # Read-side pipeline_logs tracer（cache/request_id 任意为空时 no-op）
        trace_log = ReadTraceLogger(
            cache=self._cache,
            request_id=request.request_id,
            user_id=request.user_id or (request.user_ids[0] if request.user_ids else ""),
            agent_id=request.agent_id or "default_agent",
            reader_version=self.VERSION,
        )

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
                min_score=request.min_score,
                profile_min_score=request.profile_min_score,
                profile_limit=request.profile_limit,
                user_ids=list(request.user_ids or []),
                agent_ids=list(request.agent_ids or []),
                session_ids=list(request.session_ids or []),
            )

            # 1. query embed
            _t_embed = datetime.now()
            with tracer.span("embed_query") as s:
                query_embedding = request.query_embedding
                cache_hit = False
                if not query_embedding:
                    query_embedding = await self.embed_service.embed(request.query)
                    s.set_output({"dims": len(query_embedding), "cache_hit": False, "query_len": len(request.query)})
                else:
                    cache_hit = True
                    s.set_output({"dims": len(query_embedding), "cache_hit": True, "source": "pre_computed"})
            _embed_ms = (datetime.now() - _t_embed).total_seconds() * 1000
            await trace_log.log_embed_query(
                query=request.query,
                dims=len(query_embedding),
                cache_hit=cache_hit,
                elapsed_ms=_embed_ms,
            )

            # 2. 隔离参数
            user_ids = request.user_ids if request.user_ids else ([request.user_id] if request.user_id else [])
            agent_ids = request.agent_ids
            session_ids = request.session_ids

            if len(agent_ids) > 1 and len(session_ids) > 1:
                response.error_code = 400
                response.error_message = (
                    "Cannot specify multiple agent_ids and multiple session_ids simultaneously. "
                    "Use single agent_id with multiple session_ids, or multiple agent_ids without session_ids."
                )
                return response

            search_isolation_key = ""
            search_isolation_keys = None
            search_user_ids = None
            search_agent_ids = None
            # Graph 专用：当跨 agent 搜索时，Graph 不能用精确 isolation_key，
            # 而要用 user_id 前缀匹配。graph_search_user_ids 为非空时表示 Graph 走跨 agent 模式。
            graph_search_user_ids: list[str] | None = None

            if not agent_ids and not session_ids:
                if request.agent_id:
                    search_isolation_keys = [
                        MemoryNode.build_isolation_key(u, request.agent_id or "default")
                        for u in user_ids
                    ] if user_ids else None
                    if search_isolation_keys and len(search_isolation_keys) == 1:
                        search_isolation_key = search_isolation_keys[0]
                        search_isolation_keys = None
                else:
                    # 跨 agent 搜索：VDB 用 user_ids（不限 agent），Graph 用 user_id 前缀匹配
                    search_user_ids = user_ids if user_ids else None
                    # Graph 不用 isolation_keys（精确匹配搜不到），走 user_id 模式
                    graph_search_user_ids = user_ids if user_ids else None
            elif agent_ids and not session_ids:
                # VDB: 按 user_ids + agent_ids 字段级过滤（不构建 isolation_key，避免 session 不匹配）
                search_user_ids = user_ids if user_ids else None
                search_agent_ids = agent_ids
                # Graph: 用 user_id 前缀匹配（不用 isolation_key 精确匹配）
                graph_search_user_ids = user_ids if user_ids else None
            else:
                effective_agent_ids = agent_ids if agent_ids else [request.agent_id or "default"]
                search_isolation_keys = [
                    MemoryNode.build_isolation_key(u, a, s)
                    for u in (user_ids or ["default"])
                    for a in effective_agent_ids
                    for s in session_ids
                ]

            limit = request.limit if request.limit > 0 else 10
            profile_min_score = request.profile_min_score
            profile_limit = request.profile_limit

            with tracer.span("vector_search") as s:
                try:
                    vector_store = await self._get_vector_store()

                    layers_filter = None
                    if request.layers:
                        try:
                            layers_filter = [MemoryLayer.from_string(l) for l in request.layers]
                        except Exception:
                            pass

                    import asyncio
                    import math

                    # ========================================
                    # 三路分流定义
                    # ========================================

                    # Profile VDB 路：L0_BASIC_INFO（存在 VDB 中）。
                    # L4_IDENTITY 不再算 profile —— 按普通记忆走 normal 路（与 hybrid_v2/
                    # tencent_hybrid 对齐）。normal = all_layers - special，移除 L4 后
                    # 它自动落入 normal。
                    profile_vdb_layers = [
                        MemoryLayer.L0_BASIC_INFO,
                    ]

                    # Profile Graph 路：L6_SCHEMA（存在 Graph 中，走 graph vector_search）
                    profile_graph_layers = [MemoryLayer.L6_SCHEMA]

                    # 合并 profile layers 用于 skip 判断和 normal 排除
                    profile_layers = profile_vdb_layers + profile_graph_layers

                    # Proactive Graph 路：L7_INTENTION（存在 Graph 中）
                    proactive_layers = [MemoryLayer.L7_INTENTION]
                    intention_limit = request.intention_limit if request.intention_limit > 0 else 0

                    # Normal 路：L2_FACT + L5_KNOWLEDGE + L3_SUMMARY 等（VDB 中）
                    all_special = set(profile_layers) | set(proactive_layers) | {MemoryLayer.L1_RAW}
                    if not _READER_ENABLE_SUMMARY:
                        all_special.add(MemoryLayer.L3_SUMMARY)
                    normal_layers_default = [
                        l for l in MemoryLayer.all_layers()
                        if l not in all_special
                    ]

                    # Over-fetch 系数：召回 1.5x，演化链展开后去重再截取 topk
                    OVERFETCH = 1.5

                    # layers_filter 处理
                    # profile_limit 语义：0 → 返回 0 条（跳过 profile 召回）；
                    #   <0 → 不限制；>0 → 截断。
                    skip_profile = (profile_limit == 0) or (
                        layers_filter is not None
                        and not any(l in layers_filter for l in profile_layers)
                    )
                    skip_proactive = (intention_limit == 0) or (
                        layers_filter is not None
                        and not any(l in layers_filter for l in proactive_layers)
                    )

                    async def _search_profile():
                        """Profile 路：VDB(L0) + Graph(L6)"""
                        if skip_profile:
                            return []

                        # profile_limit>0 → 该值；<0（不限）→ 固定大上限召回。
                        p_limit = profile_limit if profile_limit > 0 else 1000
                        fetch_limit = math.ceil(p_limit * OVERFETCH)
                        results = []

                        # --- VDB 路：L0 ---
                        vdb_effective = (
                            [l for l in profile_vdb_layers if l in layers_filter]
                            if layers_filter is not None
                            else profile_vdb_layers
                        )
                        if vdb_effective:
                            vdb_results = await vector_store.search(
                                query_embedding=query_embedding,
                                isolation_key=search_isolation_key,
                                isolation_keys=search_isolation_keys,
                                user_ids=search_user_ids,
                                agent_ids=search_agent_ids,
                                limit=fetch_limit,
                                layers=vdb_effective,
                                score_threshold=profile_min_score,
                                only_latest=False,
                                created_after=request.created_after,
                            )
                            results.extend(vdb_results)

                        # --- Graph 路：L6_SCHEMA ---
                        if self._graph_store is not None:
                            graph_effective = (
                                [l for l in profile_graph_layers if l in layers_filter]
                                if layers_filter is not None
                                else profile_graph_layers
                            )
                            if graph_effective:
                                # 确定 Graph 搜索的 isolation_keys
                                if graph_search_user_ids:
                                    # 跨 agent 模式：用 user_id 前缀搜索
                                    for _uid in graph_search_user_ids:
                                        graph_results = await self._graph_store.vector_search(
                                            query_embedding=query_embedding,
                                            isolation_key="",
                                            user_id=_uid,
                                            layers=[l.value for l in graph_effective],
                                            limit=fetch_limit,
                                            score_threshold=profile_min_score,
                                        )
                                        for gr in graph_results:
                                            results.append(self._graph_result_to_memory_node(gr))
                                else:
                                    ik = search_isolation_key
                                    # 多 ik 模式下逐个搜
                                    iks = [ik] if ik else (search_isolation_keys or [])
                                    for _ik in iks:
                                        graph_results = await self._graph_store.vector_search(
                                            query_embedding=query_embedding,
                                            isolation_key=_ik,
                                            layers=[l.value for l in graph_effective],
                                            limit=fetch_limit,
                                            score_threshold=profile_min_score,
                                        )
                                        for gr in graph_results:
                                            results.append(self._graph_result_to_memory_node(gr))

                        # 按 score 降序排序（VDB L0/L4 和 Graph L6 混合后需要统一排序）
                        results.sort(key=lambda x: x.get("score", 0), reverse=True)
                        return results

                    async def _search_proactive():
                        """Proactive 路：L7_INTENTION（VDB），过期惰性转 L2_FACT。"""
                        if skip_proactive:
                            return []
                        effective = (
                            [l for l in proactive_layers if l in layers_filter]
                            if layers_filter is not None
                            else proactive_layers
                        )
                        if not effective:
                            return []
                        fetch_limit = math.ceil(intention_limit * OVERFETCH)
                        # 意图统一从 VDB 召回（与 graph 解耦，全模式可用）
                        return await recall_intentions(
                            vector_store,
                            query_embedding,
                            user_ids=search_user_ids,
                            agent_ids=search_agent_ids,
                            limit=fetch_limit,
                        )

                    async def _search_normal():
                        if layers_filter is not None:
                            normal_effective = [l for l in layers_filter if l not in profile_layers and l not in proactive_layers and l != MemoryLayer.L1_RAW]
                            if not normal_effective:
                                return []
                        else:
                            normal_effective = normal_layers_default
                        fetch_limit = math.ceil(limit * OVERFETCH)
                        return await vector_store.search(
                            query_embedding=query_embedding,
                            isolation_key=search_isolation_key,
                            isolation_keys=search_isolation_keys,
                            user_ids=search_user_ids,
                            agent_ids=search_agent_ids,
                            limit=fetch_limit,
                            layers=normal_effective,
                            score_threshold=request.min_score if request.min_score > 0 else None,
                            only_latest=False,
                            created_after=request.created_after,
                        )

                    profile_results, proactive_results, normal_results = await asyncio.gather(
                        _search_profile(),
                        _search_proactive(),
                        _search_normal(),
                    )

                    if profile_limit > 0:
                        # ── Profile 配额分配：identity 40% / schema 40% / 自由竞争 20% ──
                        # 不能简单按 score 截断，否则高分 L6 会挤掉全部 L4。
                        _p_lim = math.ceil(profile_limit * OVERFETCH)
                        profile_results = _profile_quota_select(
                            profile_results, _p_lim,
                            {l.value for l in profile_vdb_layers},
                            {l.value for l in profile_graph_layers},
                        )

                    # trace
                    await trace_log.log_recall_profile(
                        profile_min_score=profile_min_score,
                        profile_limit=profile_limit,
                        hits=profile_results,
                    )
                    await trace_log.log_recall_vec(
                        pool_size=limit,
                        hits=normal_results,
                    )

                    # 合并三路（profile 优先 > proactive > normal），node_id 去重
                    seen_ids = set()
                    merged_results = []
                    for item in profile_results:
                        nid = item.get("node_id", "")
                        if nid not in seen_ids:
                            seen_ids.add(nid)
                            merged_results.append(item)
                    for item in proactive_results:
                        nid = item.get("node_id", "")
                        if nid not in seen_ids:
                            seen_ids.add(nid)
                            merged_results.append(item)
                    for item in normal_results:
                        nid = item.get("node_id", "")
                        if nid not in seen_ids:
                            seen_ids.add(nid)
                            merged_results.append(item)

                    # Graph expand: 对命中的 L6 basic 查找关联 L6 core
                    if self._graph_store is not None:
                        l6_basic_ids = [
                            item.get("node_id", "")
                            for item in merged_results
                            if item.get("node") and getattr(item["node"], "layer", None) == MemoryLayer.L6_SCHEMA
                        ]
                        if l6_basic_ids:
                            try:
                                cores = await self._graph_store.find_cores_from_basics(l6_basic_ids)
                                for core_dict in cores:
                                    cid = core_dict.get("node_id", "")
                                    if cid not in seen_ids:
                                        seen_ids.add(cid)
                                        merged_results.append(self._graph_result_to_memory_node({
                                            **core_dict,
                                            "layer": "l6_schema",
                                            "score": 0.95,  # core 权重高
                                        }))
                                        logger.debug(f"[lite-read] Attached L6 core: {cid[:12]}")
                            except Exception as e:
                                logger.debug(f"[lite-read] find_cores_from_basics failed: {e}")

                    # Tag 桥接召回：query embed → cosine top-k Topic → TAGGED_WITH 反查
                    if self._graph_store is not None:
                        try:
                            import numpy as _np
                            ik = search_isolation_key
                            iks = [ik] if ik else (search_isolation_keys or [])
                            for _ik in iks:
                                all_topics = await self._graph_store.get_all_topics(_ik)
                                if not all_topics:
                                    continue
                                # 只对有 embedding 的 topic 做 cosine
                                topics_with_emb = [t for t in all_topics if t.get("embedding")]
                                if not topics_with_emb:
                                    continue
                                q_vec = _np.array(query_embedding, dtype=_np.float32)
                                q_norm = _np.linalg.norm(q_vec) + 1e-10
                                scored_topics = []
                                for t in topics_with_emb:
                                    t_vec = _np.array(t["embedding"], dtype=_np.float32)
                                    t_norm = _np.linalg.norm(t_vec) + 1e-10
                                    sim = float(_np.dot(q_vec, t_vec) / (q_norm * t_norm))
                                    scored_topics.append((sim, t))
                                scored_topics.sort(key=lambda x: x[0], reverse=True)
                                # 取 top-3 且 sim > 0.5 的 topic
                                top_topic_ids = [
                                    t["topic_id"] for sim, t in scored_topics[:3] if sim > 0.5
                                ]
                                if top_topic_ids:
                                    bridge_results = await self._graph_store.tag_bridge_search(
                                        top_topic_ids, limit=10,
                                    )
                                    for br in bridge_results:
                                        nid = br.get("node_id", "")
                                        if nid not in seen_ids:
                                            seen_ids.add(nid)
                                            merged_results.append(self._graph_result_to_memory_node({
                                                **br,
                                                "score": 0.70,  # tag 桥接的默认分
                                            }))
                                    if bridge_results:
                                        logger.debug(
                                            f"[lite-read] Tag bridge: {len(bridge_results)} hits "
                                            f"from topics {[t['topic_id'][:12] for _, t in scored_topics[:3]]}"
                                        )
                        except Exception as e:
                            logger.debug(f"[lite-read] tag_bridge_search failed: {e}")

                    # 演化链合成 + 去重（抽到公共 helper）
                    _t_evo = datetime.now()
                    deduped_results = await expand_evolution_chains(vector_store, merged_results)
                    _evo_ms = (datetime.now() - _t_evo).total_seconds() * 1000
                    _evolved_cnt = sum(1 for r in deduped_results if r.get("is_evolved"))
                    await trace_log.log_evolution(
                        input_size=len(merged_results),
                        evolved_count=_evolved_cnt,
                        elapsed_ms=_evo_ms,
                    )

                    # Topk 截取：按三路各自的 limit 截取
                    # （Profile 的 identity/schema 配额已在上方 profile_results 截断时完成）
                    profile_layer_set = {l.value for l in profile_layers}
                    proactive_layer_set = {l.value for l in proactive_layers}

                    final_profile = []
                    final_proactive = []
                    final_normal = []
                    for item in deduped_results:
                        node = item.get("node")
                        layer_val = node.layer.value if (node and node.layer) else ""
                        if layer_val in profile_layer_set:
                            final_profile.append(item)
                        elif layer_val in proactive_layer_set:
                            final_proactive.append(item)
                        else:
                            final_normal.append(item)

                    # 按各路 limit 截取
                    # profile_limit：>0 截断；<0 不限（_profile_quota_select 在
                    #   total_limit<=0 时返回全部）；0 已在 skip_profile 阶段清空。
                    # Profile 再做一次 identity/schema 配额（evolution chain 可能改变分布）
                    final_profile = _profile_quota_select(
                        final_profile, profile_limit,
                        {l.value for l in profile_vdb_layers},
                        {l.value for l in profile_graph_layers},
                    )
                    final_proactive = final_proactive[:intention_limit]
                    # Memory Strength（默认关闭）：normal 通道 idle 衰减 × 频次，在截断前乘进 score 并重排。
                    if getattr(self.config.recall, "strength_enabled", False):
                        _tau = getattr(self.config.recall, "strength_tau", 180.0)
                        apply_strength_to_normal(final_normal, score_key="score", tau=_tau)
                        final_normal.sort(key=lambda x: x.get("score", 0.0), reverse=True)
                    final_normal = final_normal[:limit]

                    final_results = final_profile + final_proactive + final_normal

                    # 填充结果
                    top_scores = []
                    for result_item in final_results:
                        node = result_item.get("node")
                        score = result_item.get("score", 0.0)
                        node_id = result_item.get("node_id", "")
                        is_evolved = bool(result_item.get("is_evolved"))

                        # evolved: content 用 head 原始内容，chain 原始数据单独返回
                        content = node.content if node else ""
                        speculate = getattr(node, "speculate", None) if node else None
                        source_raw_memory_id = getattr(node, "source_raw_memory_id", None) if node else None
                        tags = list(node.tags) if (node and getattr(node, "tags", None)) else []

                        mem_entry = {
                            "memory_id": node_id,
                            "content": content,
                            "layer": node.layer.value if node else "",
                            "score": score,
                            "access_count": getattr(node, "access_count", 0) if node else 0,
                            "owner": getattr(node, "owner", None) if node else None,
                            "speculate": speculate,
                            "source_raw_memory_id": source_raw_memory_id,
                            "tags": tags,
                            "memory_at": int(node.memory_at.timestamp()) if (node and node.memory_at) else None,
                            "gmt_created": int(node.gmt_created.timestamp()) if (node and node.gmt_created) else None,
                        }
                        # schema_type: basic/abstract (仅 L6_SCHEMA 有)
                        _custom = getattr(node, "custom", None) or {} if node else {}
                        _st = _custom.get("schema_type", "")
                        if _st:
                            mem_entry["schema_type"] = _st
                        if is_evolved:
                            mem_entry["evolution_chain"] = result_item.get("evolution_chain", [])

                        response.memories.append(mem_entry)
                        top_scores.append(round(score, 4))

                    response.total_found = len(final_results)
                    evolved_count = sum(1 for r in final_results if r.get("is_evolved"))
                    s.set_output({
                        "total_found": len(final_results),
                        "profile_count": len(final_profile),
                        "proactive_count": len(final_proactive),
                        "normal_count": len(final_normal),
                        "evolved_count": evolved_count,
                        "limit": limit,
                        "profile_min_score": profile_min_score,
                        "profile_limit": profile_limit,
                        "isolation_key": search_isolation_key,
                        "isolation_keys": search_isolation_keys,
                        "search_user_ids": search_user_ids,
                        "search_agent_ids": search_agent_ids,
                        "top_scores": top_scores[:5],
                        "returned_memories": [
                            {
                                "memory_id": m["memory_id"],
                                "content": m["content"],
                                "layer": m["layer"],
                                "score": round(m["score"], 4),
                                "speculate": m.get("speculate"),
                                "source_raw_memory_id": m.get("source_raw_memory_id"),
                                "tags": m.get("tags") or [],
                            }
                            for m in response.memories
                        ],
                    })
                    logger.info(
                        f"[lite-read/legacy] search: profile={len(final_profile)} "
                        f"proactive={len(final_proactive)} normal={len(final_normal)} "
                        f"total={len(final_results)} evolved={evolved_count} "
                        f"for query='{request.query}'"
                    )

                except Exception as search_err:
                    s.set_error(str(search_err))
                    logger.error(f"Legacy Read: vector search failed: {search_err}", exc_info=True)
                    response.total_found = 0

            response.success = True

        except Exception as e:
            logger.error(f"LegacyReadPipeline.read failed: {e}", exc_info=True)
            response.error_code = 500
            response.error_message = str(e)
            tracer.set_error(str(e))

        response.elapsed_ms = (datetime.now() - start_time).total_seconds() * 1000

        tracer.set_output({
            "success": response.success,
            "total_found": response.total_found,
            "pipeline_ms": response.elapsed_ms,
            "returned_memories": [
                {
                    "memory_id": m["memory_id"],
                    "content": m["content"],
                    "layer": m["layer"],
                    "score": round(m["score"], 4),
                    "speculate": m.get("speculate"),
                    "source_raw_memory_id": m.get("source_raw_memory_id"),
                    "tags": m.get("tags") or [],
                }
                for m in response.memories
            ],
        })

        # 读路径总览（对齐 write 侧 DIGEST_SUMMARY）
        await trace_log.log_summary(
            query=request.query,
            intent=None,                 # legacy 不做意图分类
            confidence=None,
            is_low_confidence=None,
            channels={"legacy_merged": response.total_found},
            total_found=response.total_found,
            elapsed_ms=response.elapsed_ms,
            returned_memories=response.memories,
        )

        return response

    # ================================================================
    # Graph vector_search 结果 → VDB search 兼容格式
    # ================================================================

    @staticmethod
    def _graph_result_to_memory_node(gr: dict) -> dict:
        """将 graph_store.vector_search() 返回的 dict 转为 VDB search 兼容格式"""
        from ..models.memory import SourceType as _ST

        node = MemoryNode(
            node_id=gr.get("node_id", ""),
            content=gr.get("content", ""),
            layer=MemoryLayer.from_string(gr.get("layer", "l6_schema")),
            confidence=gr.get("confidence", 0.8),
            source_type=_ST.INFERRED,
        )
        custom_json = gr.get("custom_json")
        if custom_json and isinstance(custom_json, str):
            try:
                import json as _json
                node.custom = _json.loads(custom_json)
            except Exception:
                pass

        return {
            "node_id": gr.get("node_id", ""),
            "node": node,
            "score": gr.get("score", 0.0),
        }

    async def close(self) -> None:
        logger.debug("LegacyReadPipeline closed")
