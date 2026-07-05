"""
HY Memory - Write (System 1)

写入流程:
  1. 参数校验
  2. 向量化 (EmbedService)
  3. Qdrant 持久化 (VectorStore) - 原始内容始终存为 L1_RAW
  4. [可选] MemAgent 智能处理 (提取实体、生成摘要、冲突检测)
     - Agent 提取的高层信息存为 L2_FACT/L3_SUMMARY（不再区分 L4_IDENTITY，统一 L2_FACT）
     - intentions（前瞻意图）存为 L7_INTENTION（带 valid_until，过期由 reader 惰性转 L2）
     - 提取成功后，L1_RAW 降级为 SHADOW 状态（不被召回）
  5. [可选] 合并检测 (Merger)

mode 行为差异:
  - lite:  只存 L1_RAW，不调 LLM，最简 embed 入库
  - pro:   L1_RAW + MemAgent 提取高层信息 → reconcile → L2/L4/L3
  - ultra: 同 pro，但 System2Writer 会在之后异步执行 System 2 认知加工
"""

import json
import logging
import os
from datetime import datetime
from typing import Any

from ..agent.mem_agent import MemAgent, ProcessMode
from ..agent.reconciler import MemoryReconciler
from ..agent.tools.basic_profile import upsert_basic_profile
from ..config import MemoryConfig
from ..core.embed_service import EmbedService
from ..core.merger import Merger
from ..data.vector_store import create_vector_store
from ..data.vector_store_base import VectorStoreBase
from ..models.memory import MemoryLayer, MemoryNode, MemoryStatus, SourceType
from ..utils.log_setup import get_request_id
from ..utils.pipeline_observability import is_pipeline_trace_enabled
from ..utils.tracer import PipelineTracer, create_tracer
from ._retrieval import tag_index as _tag_index_helper
from .base import PipelineContext, WritePipeline, WriteRequest, WriteResponse

logger = logging.getLogger(__name__)

_RECONCILE_ENABLED = os.getenv("RECONCILE_ENABLED", "true").lower() == "true"


def _norm_owner(value: Any) -> str | None:
    """归一化 extractor/reconcile 给出的 owner：仅接受 'user' / 'agent'，否则 None。"""
    if not value:
        return None
    v = str(value).strip().lower()
    if v in ("user", "agent"):
        return v
    # 兼容 mem0 风格的 'assistant' → 'agent'
    if v == "assistant":
        return "agent"
    return None


class MemoryWriter(WritePipeline):
    """
    核心写入器 (System 1)

    写入流程: 原始内容存 L1_RAW + 单路 Qdrant 存储 + 可选 MemAgent LLM 提取高层信息
    lite 模式不调 LLM，pro/ultra 模式调 MemAgent。
    """

    VERSION = "writer"

    def __init__(
        self,
        config: MemoryConfig,
        embed_service: EmbedService | None = None,
        vector_store: VectorStoreBase | None = None,
        cache=None,
    ):
        self.config = config
        self._embed_service = embed_service
        self._external_vector_store = vector_store
        self._cache = cache

        self._merger: Merger | None = None
        self._mem_agent: MemAgent | None = None
        self._reconciler: MemoryReconciler | None = None
        self._vector_store: VectorStoreBase | None = None
        self._vector_store_initialized = False

        self._initialized = False

    async def initialize(self) -> None:
        if self._initialized:
            return
        if self._embed_service is None:
            self._embed_service = EmbedService(self.config)
        self._merger = Merger()
        self._mem_agent = MemAgent(self.config)
        self._reconciler = MemoryReconciler(self.config)
        self._vector_store = self._external_vector_store or create_vector_store(self.config)
        if self._external_vector_store and getattr(self._external_vector_store, '_client', None):
            self._vector_store_initialized = True
        self._initialized = True
        logger.debug("MemoryWriter initialized")

    @property
    def embed_service(self) -> EmbedService:
        if self._embed_service is None:
            self._embed_service = EmbedService(self.config)
        return self._embed_service

    @property
    def merger(self) -> Merger:
        if self._merger is None:
            self._merger = Merger()
        return self._merger

    @property
    def mem_agent(self) -> MemAgent:
        if self._mem_agent is None:
            self._mem_agent = MemAgent(self.config)
        return self._mem_agent

    @property
    def reconciler(self) -> MemoryReconciler:
        if self._reconciler is None:
            self._reconciler = MemoryReconciler(self.config)
        return self._reconciler

    async def _get_vector_store(self) -> VectorStoreBase:
        if self._vector_store is None:
            self._vector_store = self._external_vector_store or create_vector_store(self.config)
        if not self._vector_store_initialized:
            await self._vector_store.initialize()
            self._vector_store_initialized = True
        return self._vector_store

    # ================================================================
    # 私有辅助方法
    # ================================================================

    @staticmethod
    def _build_custom(op, request_id: str) -> dict[str, Any]:
        """构造 VDB payload 中的 custom 字段。始终包含 request_id 以便追溯。"""
        custom: dict[str, Any] = {}
        if request_id:
            custom["request_id"] = request_id
        if op.supersede_reason:
            custom["supersede_reason"] = op.supersede_reason
        return custom

    async def _maybe_index_entities(
        self, vector_store, node, request,
    ) -> None:
        """若 entity_store 开关开启且为 L2_FACT，落库后刷 entity store（best-effort）。

        覆盖 reconcile（ADD/UPDATE/SUPERSEDE）与首写（_direct_store）两条路。
        """
        try:
            if not getattr(self.config.recall, "entity_store_enabled", False):
                return
            if node is None or node.layer != MemoryLayer.L2_FACT:
                return
            from ._retrieval.entity_store import index_memory_entities
            await index_memory_entities(
                vector_store=vector_store,
                embed_service=self.embed_service,
                memory_id=node.node_id,
                content=node.content or "",
                user_id=request.user_id,
                agent_id=request.agent_id or "default_agent",
            )
        except Exception as e:
            logger.debug(f"[entity-index] skipped (non-fatal): {e}")

    @staticmethod
    def _collect_new_memories(
        extracted_info: dict[str, Any],
    ) -> tuple[list[str], list[dict]]:
        """
        从 extract 结果中收集新 memory 文本列表和完整 meta 列表。

        注意：basic_info 字段由 extractor 在 JSON 输出中返回，writer 单独处理
        （走 upsert_basic_profile() 落 L0_BASIC_INFO 演化链），这里不再收集。

        Returns:
            (new_memory_texts, new_memories_meta)
            每条 meta: {"content", "layer", "tags"}
        """
        new_memory_texts: list[str] = []
        new_memories_meta: list[dict] = []

        # 1) memory → 每条独立 memory（统一 L2_FACT）
        #    新版 extractor 输出 `memory`；兼容旧版 `facts` 字段。
        for item in (extracted_info.get("memory") or extracted_info.get("facts") or []):
            if not isinstance(item, dict):
                continue
            content = item.get("content", "")
            if not content:
                continue
            new_memory_texts.append(content)
            new_memories_meta.append({
                "content": content,
                "layer": "L2_FACT",
                "tags": item.get("tags") or [],
                "owner": _norm_owner(item.get("owner")),
            })

        # 2) 向后兼容：旧 extractor 输出的 identity 也并入 L2_FACT
        #    （新版 extractor 不再产出 identity；L4_IDENTITY 不再写入，仅读历史数据）
        for item in (extracted_info.get("identity") or []):
            if not isinstance(item, dict):
                continue
            content = item.get("content", "")
            if not content:
                continue
            new_memory_texts.append(content)
            new_memories_meta.append({
                "content": content,
                "layer": "L2_FACT",
                "tags": item.get("tags") or [],
                "owner": _norm_owner(item.get("owner")),
            })

        # 3) 兼容旧版 extract 输出（profile + facts）— 不处理 basic_info
        if not new_memory_texts:
            profile = extracted_info.get("profile", {})
            if profile and isinstance(profile, dict):
                profile_items = []
                for k, v in profile.items():
                    if k == "preferences":
                        continue
                    if v and str(v).lower() not in ("null", "none", ""):
                        profile_items.append(f"{k}: {v}")
                prefs = profile.get("preferences", [])
                if prefs and isinstance(prefs, list):
                    pref_strs = [str(p) for p in prefs if p]
                    if pref_strs:
                        profile_items.append(f"preferences: {', '.join(pref_strs)}")
                if profile_items:
                    t = "; ".join(profile_items)
                    new_memory_texts.append(t)
                    new_memories_meta.append({"content": t, "layer": "L2_FACT", "tags": []})
            for f in (extracted_info.get("facts") or []):
                if isinstance(f, dict) and f.get("content"):
                    new_memory_texts.append(f["content"])
                    new_memories_meta.append({"content": f["content"], "layer": "L2_FACT", "tags": f.get("tags") or []})

        return new_memory_texts, new_memories_meta

    async def _dedup_extracted(
        self,
        new_memory_texts: list[str],
        new_memories_meta: list[dict],
        request: "WriteRequest",
        req_id: str,
    ) -> tuple[list[str], list[dict]]:
        """对 extractor 新抽取的多条 memory 互相去重（入库前）。

        这些条目还没落库、无 node_id、无演化链 → 全按非链处理，额外 embed 一次，
        delete_from_store=False（只丢弃重复项，不删库），并记 DEDUP log。
        保留：用列表下标做确定性 gmt（越靠前越优先保留，等价 extractor 输出顺序）。
        """
        from ..pipelines._retrieval.dedup import DedupItem, execute_dedup

        embeds = await self.embed_service.embed_batch(list(new_memory_texts))
        items: list[DedupItem] = []
        for i, (text, emb) in enumerate(zip(new_memory_texts, embeds)):
            if not emb:
                continue
            items.append(DedupItem(
                node_id=str(i),               # 用下标作临时 id
                embedding=emb,
                content=text,
                is_latest=True,
                is_chain_head=False,
                gmt_created=float(i),         # 越靠前 gmt 越小 → 优先保留
                chain_node_ids=[str(i)],
            ))
        if len(items) < 2:
            return new_memory_texts, new_memories_meta

        plan = await execute_dedup(
            items,
            vector_store=None,                # 还没入库
            cache=self._cache,
            trigger="extractor",
            request_id=req_id,
            user_id=request.user_id,
            agent_id=request.agent_id or "default_agent",
            delete_from_store=False,
        )
        drop_idx = {int(d) for d in plan.get("delete_ids", [])}
        if not drop_idx:
            return new_memory_texts, new_memories_meta

        kept_texts, kept_meta = [], []
        for i, (t, m) in enumerate(zip(new_memory_texts, new_memories_meta)):
            if i in drop_idx:
                continue
            kept_texts.append(t)
            kept_meta.append(m)
        logger.info(
            f"[write] extractor dedup dropped {len(drop_idx)} of {len(new_memory_texts)} extracted"
        )
        return kept_texts, kept_meta

    @staticmethod
    def _collect_intentions(extracted_info: dict[str, Any]) -> list[dict[str, Any]]:
        """
        从 extract 结果中收集 intentions（前瞻意图，存 L7_INTENTION）。

        每条 intention: {"content", "tags", "valid_until"}
        valid_until 是 extractor 输出的 ISO 日期串（或 null）；这里解析为
        datetime（当天 23:59:59，宽松到日末），解析失败/缺失 → None。
        """
        out: list[dict[str, Any]] = []
        for item in (extracted_info.get("intentions") or []):
            if not isinstance(item, dict):
                continue
            content = (item.get("content") or "").strip()
            if not content:
                continue
            valid_until: datetime | None = None
            raw_vu = item.get("valid_until")
            if raw_vu and str(raw_vu).lower() not in ("null", "none", ""):
                try:
                    # 支持 "YYYY-MM-DD" 或完整 ISO；统一拉到当天日末
                    d = datetime.fromisoformat(str(raw_vu).strip())
                    valid_until = d.replace(hour=23, minute=59, second=59, microsecond=0)
                except (ValueError, TypeError):
                    valid_until = None
            out.append({
                "content": content,
                "tags": item.get("tags") or [],
                "valid_until": valid_until,
                "owner": _norm_owner(item.get("owner")),
            })
        return out

    async def _store_intentions(
        self,
        intentions: list[dict[str, Any]],
        request: WriteRequest,
        vector_store: VectorStoreBase,
        req_id: str,
    ) -> list[str]:
        """
        把 intentions 直接 upsert 为 L7_INTENTION 节点（不走 reconcile）。

        意图是 point-in-time 信号，不与已有 fact 合并；过期后由 reader 惰性
        转成 L2_FACT。返回新建节点 id 列表。
        """
        if not intentions:
            return []

        stored: list[str] = []
        # 批量 embed
        contents = [it["content"] for it in intentions]
        embeddings: list[list[float] | None] = [None] * len(contents)
        try:
            batch = await self.embed_service.embed_batch(contents)
            for i, emb in enumerate(batch):
                embeddings[i] = emb
        except Exception as e:
            logger.warning(f"[intention] batch embed failed, falling back to sequential: {e}")

        for i, it in enumerate(intentions):
            emb = embeddings[i]
            if emb is None:
                try:
                    emb = await self.embed_service.embed_queued(it["content"])
                except Exception as e:
                    logger.error(f"[intention] embed failed, skip: {e}")
                    continue
            node = MemoryNode(
                user_id=request.user_id,
                agent_id=request.agent_id or "default_agent",
                session_id=request.session_id or "default_session",
                layer=MemoryLayer.L7_INTENTION,
                content=it["content"],
                owner=it.get("owner") or "user",
                supersedes=None,
                is_latest=True,
                source_type=SourceType.INFERRED,
                status=MemoryStatus.ACTIVE,
                embedding=emb,
                memory_at=request.memory_at,
                valid_until=it.get("valid_until"),
                tags=list(it.get("tags") or []),
                custom={"request_id": req_id} if req_id else {},
            )
            try:
                nid = await vector_store.upsert(node)
                stored.append(nid)
                logger.debug(
                    f"[intention] L7 upsert: {it['content'][:80]} → node_id={nid} "
                    f"valid_until={it.get('valid_until')}"
                )
            except Exception as e:
                logger.error(f"[intention] upsert failed: {e}")
        return stored

    async def _reconcile_and_store(
        self,
        new_memory_texts: list[str],
        new_memories_meta: list[dict],
        request: WriteRequest,
        vector_store: VectorStoreBase,
        req_id: str,
    ) -> tuple[list[str], str | None, dict[str, int], dict[str, int]]:
        """
        对新 memories 做 reconcile，执行 ADD（含 EVOLVE），写 DIGEST_SUMMARY log。

        Returns:
            (stored_ids, error_message, ops_counts, recon_tokens)
            error_message=None 表示成功；ops_counts 含 add/supersede/update/total；
            recon_tokens 含 prompt/completion/total（reconcile 链路 LLM 消耗）
        """
        stored_ids: list[str] = []
        current_time = request.memory_at.isoformat(timespec="seconds") if request.memory_at else ""

        recon_result = await self.reconciler.reconcile(
            new_memories=new_memory_texts,
            user_id=request.user_id,
            agent_id=request.agent_id or "default_agent",
            vector_store=vector_store,
            embed_service=self.embed_service,
            layers=[MemoryLayer.L2_FACT, MemoryLayer.L4_IDENTITY],
            cache=self._cache,
            request_id=req_id,
            current_time=current_time,
            new_memories_with_meta=new_memories_meta,
        )

        if not recon_result.success:
            return [], recon_result.error, {"add": 0, "supersede": 0, "update": 0, "total": 0}, {"prompt": 0, "completion": 0, "total": 0}

        # 分类统计
        _op_counts = {}
        for op in recon_result.ops:
            _op_counts[op.op] = _op_counts.get(op.op, 0) + 1
        _op_counts_str = " ".join(f"{k}={v}" for k, v in sorted(_op_counts.items()))

        logger.info(
            f"TRACE_PERF [{req_id}] S1_RECONCILE_DONE "
            f"ops={len(recon_result.ops)} candidates={len(new_memory_texts)} | {_op_counts_str}"
        )
        # 逐条 op 详情由 reconciler 的 "[reconciler] ops detail" 单条 JSON list 输出，
        # 这里不再逐条打印（避免重复刷屏）。

        # ── 批量 embed：收集所有需要 embed 的 content，一次性 batch 调用 ──
        contents_to_embed: list[str] = []
        content_indices: list[int] = []  # 对应 ops 索引
        for i, op in enumerate(recon_result.ops):
            if op.op in ("SUPERSEDE", "UPDATE"):
                if op.op == "UPDATE" and not op.content:
                    continue  # shadow-only, no embed needed
                if not op.memory_id:
                    continue
                contents_to_embed.append(op.content or "")
                content_indices.append(i)
            elif op.op == "ADD":
                contents_to_embed.append(op.content or "")
                content_indices.append(i)

        # 一次 batch embed（不逐个串行）
        embeddings_map: dict[int, list[float]] = {}
        if contents_to_embed:
            try:
                batch_embeddings = await self.embed_service.embed_batch(contents_to_embed)
                for idx, emb in zip(content_indices, batch_embeddings):
                    embeddings_map[idx] = emb
            except Exception as e:
                logger.warning(f"[reconciler] batch embed failed, falling back to sequential: {e}")
                # fallback: 逐个 embed
                for idx, content in zip(content_indices, contents_to_embed):
                    try:
                        embeddings_map[idx] = await self.embed_service.embed_queued(content)
                    except Exception as e2:
                        logger.error(f"[reconciler] embed failed for op {idx}: {e2}")

        # 统计
        add_cnt = 0           # ADD（全新）
        supersede_cnt = 0     # SUPERSEDE（矛盾演化）
        update_cnt = 0        # UPDATE（合并精炼）

        for op_idx, op in enumerate(recon_result.ops):
            # ------------------------------------------------
            # SUPERSEDE / UPDATE：标记旧节点 → 创建新节点
            # SUPERSEDE: 旧节点 status=SUPERSEDED（进演化链，仍可召回+展开），
            #            新节点 supersedes=[old_id]
            # UPDATE: 旧节点 status=SHADOW（逻辑删除，不召回），
            #         新节点 supersedes=None（不进链）
            # ------------------------------------------------
            if op.op in ("SUPERSEDE", "UPDATE"):
                target_id = op.memory_id
                if not target_id:
                    logger.warning(f"[reconciler] {op.op} op missing memory_id, skipped")
                    continue

                # UPDATE with no content = legacy DELETE mapping (shadow-only)
                if op.op == "UPDATE" and not op.content:
                    try:
                        await vector_store.update_payload(
                            target_id,
                            {
                                "is_latest": False,
                                "status": MemoryStatus.SHADOW.value,
                            },
                        )
                        logger.debug(f"[reconciler] shadow-only UPDATE: memory_id={target_id}")
                    except Exception as e:
                        logger.warning(f"[reconciler] failed to shadow node {target_id}: {e}")
                    continue

                content = op.content or ""
                layer = MemoryLayer.from_string(op.layer) if op.layer else MemoryLayer.L2_FACT

                # SUPERSEDE → 旧节点进演化链，status=SUPERSEDED（仍可被召回，
                #   命中后双向展开整链）；新节点 supersedes=[old_id]。
                # UPDATE → 旧节点不进链，status=SHADOW（等价逻辑删除，不召回）；
                #   新节点 supersedes=None，独立。
                is_supersede = op.op == "SUPERSEDE"
                old_status = (
                    MemoryStatus.SUPERSEDED.value if is_supersede
                    else MemoryStatus.SHADOW.value
                )

                # ------------------------------------------------
                # 多节点链折叠（仅 SUPERSEDE）：
                # op.memory_ids = [E0, E1, ...]（有序，旧→新）。把这些原本
                # 未成链的旧节点先连成一条链（E0 ← E1 ← ...），再让新 head
                # 节点 supersede 最新的那个（target_id = memory_ids[-1]）。
                # 每条旧节点都被标 SUPERSEDED（仍可召回+展开）；只有 head（新节点）
                # 保持 is_latest=True。
                # ------------------------------------------------
                chain_ids = list(getattr(op, "memory_ids", None) or [])
                if is_supersede and len(chain_ids) > 1:
                    for _prev_id, _cur_id in zip(chain_ids, chain_ids[1:]):
                        # _cur_id supersedes _prev_id：建立 supersedes / superseded_by 双向链
                        try:
                            _cur_node = await vector_store.get_by_id(_cur_id)
                            if _cur_node is not None:
                                _sup = list(_cur_node.supersedes or [])
                                if _prev_id not in _sup:
                                    _sup.append(_prev_id)
                                await vector_store.update_payload(
                                    _cur_id, {"supersedes": _sup}
                                )
                        except Exception as e:
                            logger.warning(
                                f"[reconciler] chain-link supersedes on {_cur_id} failed: {e}"
                            )
                        try:
                            _prev_node = await vector_store.get_by_id(_prev_id)
                            _sb = list(_prev_node.superseded_by or []) if _prev_node else []
                            if _cur_id not in _sb:
                                _sb.append(_cur_id)
                            await vector_store.update_payload(
                                _prev_id,
                                {
                                    "superseded_by": _sb,
                                    "is_latest": False,
                                    "status": MemoryStatus.SUPERSEDED.value,
                                },
                            )
                        except Exception as e:
                            logger.warning(
                                f"[reconciler] chain-link superseded_by on {_prev_id} failed: {e}"
                            )
                    logger.debug(
                        f"[reconciler] SUPERSEDE chain fold: {chain_ids} "
                        f"(head target={target_id})"
                    )

                # 标记链上最新的 target_id（新 head 直接取代它）：
                # SUPERSEDE → SUPERSEDED（进链，可召回）；UPDATE → SHADOW（逻辑删除）
                # ------------------------------------------------
                # UPDATE（有 content，非 SUPERSEDE）：原地更新旧节点
                # ------------------------------------------------
                # UPDATE 语义是「合并精炼同一主题」，本质是同一条记忆的延续，因此
                # 原地更新 target_id：换 content + embedding + tags + memory_at，
                # 天然保留 access_count / last_accessed_at（不打回冷启动），
                # 也不产生 SHADOW 垃圾节点。SUPERSEDE 仍走下面的 shadow+建新链。
                if not is_supersede:
                    new_emb = embeddings_map.get(op_idx) or await self.embed_service.embed_queued(content)
                    try:
                        await vector_store.update_payload(
                            target_id,
                            {
                                "content": content,
                                "embedding": new_emb,
                                "tags": list(op.tags or []),
                                "layer": layer.value,
                                "memory_at": (int(request.memory_at.timestamp())
                                              if request.memory_at else None),
                                "gmt_modified": int(datetime.now().timestamp()),
                                "is_latest": True,
                                "status": MemoryStatus.ACTIVE.value,
                            },
                        )
                    except Exception as e:
                        logger.warning(f"[reconciler] in-place UPDATE failed on {target_id}: {e}")
                        continue
                    stored_ids.append(target_id)
                    update_cnt += 1
                    logger.debug(
                        f"[reconciler] UPDATE(in-place): {content[:80]} → node_id={target_id} "
                        f"layer={layer.value} reason={op.reason}"
                    )
                    # tag_index 惰性维护
                    if op.tags:
                        try:
                            await _tag_index_helper.ensure_tag_embeddings_for_node(
                                vector_store=vector_store,
                                embed_service=self.embed_service,
                                user_id=request.user_id,
                                tags=list(op.tags),
                            )
                        except Exception as e:
                            logger.debug(f"[tag-index] maintain failed (non-fatal): {e}")
                    # 写 memory_operations 记录
                    if self._cache:
                        try:
                            await self._cache.store_memory_operation(
                                request_id=req_id,
                                user_id=request.user_id,
                                agent_id=request.agent_id or "default_agent",
                                op=op.op,
                                memory_id=target_id,
                                content=content,
                                layer=layer.value,
                                reason=op.reason,
                            )
                        except Exception as e:
                            logger.warning(f"[reconciler] store_memory_operation(UPDATE) failed: {e}")
                    continue

                # ------------------------------------------------
                # SUPERSEDE：shadow（SUPERSEDED）旧节点 + 建新链头节点
                # ------------------------------------------------
                try:
                    await vector_store.update_payload(
                        target_id,
                        {
                            "is_latest": False,
                            "status": old_status,
                        },
                    )
                except Exception as e:
                    logger.warning(f"[reconciler] failed to mark old node {target_id} ({op.op}): {e}")
                    continue

                # 创建新 head 节点（此处只可能是 SUPERSEDE；UPDATE 已在上面原地更新并 continue）
                new_node = MemoryNode(
                    user_id=request.user_id,
                    agent_id=request.agent_id or "default_agent",
                    session_id=request.session_id or "default_session",
                    layer=layer,
                    content=content,
                    owner=getattr(op, "owner", None),
                    speculate=op.speculate,
                    supersedes=[target_id],
                    is_latest=True,
                    source_type=SourceType.INFERRED,
                    status=MemoryStatus.ACTIVE,
                    embedding=embeddings_map.get(op_idx) or await self.embed_service.embed_queued(content),
                    memory_at=request.memory_at,
                    tags=list(op.tags or []),
                    custom=self._build_custom(op, req_id),
                    emotional_valence=getattr(self, "_cur_valence", 0.0),
                    emotional_arousal=getattr(self, "_cur_arousal", 0.0),
                )
                nid = await vector_store.upsert(new_node)
                stored_ids.append(nid)
                await self._maybe_index_entities(vector_store, new_node, request)

                supersede_cnt += 1

                logger.debug(
                    f"[reconciler] SUPERSEDE: {content[:80]} → node_id={nid} "
                    f"layer={layer.value} supersedes=[{target_id}] reason={op.reason}"
                )

                # SUPERSEDE: 回写旧节点的 superseded_by（建立链关系）
                try:
                    old_node = await vector_store.get_by_id(target_id)
                    existing_superseded_by = (old_node.superseded_by or []) if old_node else []
                    if nid not in existing_superseded_by:
                        existing_superseded_by.append(nid)
                    await vector_store.update_payload(
                        target_id,
                        {"superseded_by": existing_superseded_by},
                    )
                except Exception as e:
                    logger.warning(f"[reconciler] failed to update superseded_by on {target_id}: {e}")

                # tag_index 惰性维护
                if new_node.tags:
                    try:
                        await _tag_index_helper.ensure_tag_embeddings_for_node(
                            vector_store=vector_store,
                            embed_service=self.embed_service,
                            user_id=new_node.user_id,
                            tags=list(new_node.tags),
                        )
                    except Exception as e:
                        logger.debug(f"[tag-index] maintain failed (non-fatal): {e}")

                # 写 memory_operations 记录
                if self._cache:
                    try:
                        await self._cache.store_memory_operation(
                            request_id=req_id,
                            user_id=request.user_id,
                            agent_id=request.agent_id or "default_agent",
                            op=op.op,
                            memory_id=nid,
                            content=content,
                            layer=layer.value,
                            reason=op.supersede_reason or op.reason,
                            supersedes=[target_id] if is_supersede else [],
                        )
                    except Exception as e:
                        logger.warning(f"[reconciler] store_memory_operation({op.op}) failed: {e}")
                continue

            # ------------------------------------------------
            # ADD op：纯新增节点
            # ------------------------------------------------
            if op.op != "ADD":
                logger.warning(f"[reconciler] unknown op '{op.op}', skipped")
                continue

            content = op.content or ""
            layer = MemoryLayer.from_string(op.layer) if op.layer else MemoryLayer.L2_FACT

            new_node = MemoryNode(
                user_id=request.user_id,
                agent_id=request.agent_id or "default_agent",
                session_id=request.session_id or "default_session",
                layer=layer,
                content=content,
                owner=getattr(op, "owner", None),
                speculate=op.speculate,
                supersedes=None,
                is_latest=True,
                source_type=SourceType.INFERRED,
                status=MemoryStatus.ACTIVE,
                embedding=embeddings_map.get(op_idx) or await self.embed_service.embed_queued(content),
                memory_at=request.memory_at,
                tags=list(op.tags or []),
                custom=self._build_custom(op, req_id),
                emotional_valence=getattr(self, "_cur_valence", 0.0),
                emotional_arousal=getattr(self, "_cur_arousal", 0.0),
            )
            nid = await vector_store.upsert(new_node)
            stored_ids.append(nid)
            add_cnt += 1
            await self._maybe_index_entities(vector_store, new_node, request)
            logger.debug(
                f"[reconciler] ADD: {content[:80]} → node_id={nid} "
                f"layer={layer.value} reason={op.reason}"
            )

            # tag_index 惰性维护（reader_hybrid_tag 路 B 依赖；失败静默降级）
            if new_node.tags:
                try:
                    await _tag_index_helper.ensure_tag_embeddings_for_node(
                        vector_store=vector_store,
                        embed_service=self.embed_service,
                        user_id=new_node.user_id,
                        tags=list(new_node.tags),
                    )
                except Exception as e:
                    logger.debug(f"[tag-index] maintain failed (non-fatal): {e}")

            if self._cache:
                try:
                    await self._cache.store_memory_operation(
                        request_id=req_id,
                        user_id=request.user_id,
                        agent_id=request.agent_id or "default_agent",
                        op="ADD",
                        memory_id=nid,
                        content=content,
                        layer=layer.value,
                        reason=op.reason,
                        supersedes=[],
                    )
                except Exception as e:
                    logger.warning(f"[reconciler] store_memory_operation failed: {e}")

        # DIGEST_SUMMARY log
        if self._cache and recon_result.ops:
            try:
                import json as _json2
                total_ops = len(recon_result.ops)
                summary_data = {
                    "add_count": add_cnt,
                    "supersede_count": supersede_cnt,
                    "update_count": update_cnt,
                    "total_ops": total_ops,
                    "new_memories_input": len(new_memory_texts),
                }
                await self._cache.store_pipeline_log(
                    request_id=req_id,
                    user_id=request.user_id,
                    agent_id=request.agent_id or "default_agent",
                    step="DIGEST_SUMMARY",
                    prompt="",
                    response=_json2.dumps(summary_data, ensure_ascii=False),
                    parsed=_json2.dumps(summary_data, ensure_ascii=False),
                    memory_ids=stored_ids,
                )
            except Exception as e:
                logger.warning(f"[write] store DIGEST_SUMMARY failed: {e}")

        ops_counts = {
            "add": add_cnt,
            "supersede": supersede_cnt,
            "update": update_cnt,
            "total": add_cnt + supersede_cnt + update_cnt,
        }
        recon_tokens = {
            "prompt": recon_result.prompt_tokens,
            "completion": recon_result.completion_tokens,
            "total": recon_result.total_tokens,
        }
        return stored_ids, None, ops_counts, recon_tokens

    async def _direct_store(
        self,
        new_memories_meta: list[dict],
        request: WriteRequest,
        vector_store: VectorStoreBase,
        req_id: str,
    ) -> tuple[list[str], str | None, dict[str, int], dict[str, int]]:
        """
        跳过 reconcile，直接把 extractor 提取的 memories 插入 VDB。

        通过 RECONCILE_ENABLED=false 激活。适用于 eval 场景：
        不做去重/合并/演化，保留所有提取结果。
        """
        stored_ids: list[str] = []

        # 批量 embed
        contents = [m.get("content", "") for m in new_memories_meta if m.get("content")]
        if contents:
            batch_embeddings = await self.embed_service.embed_batch(contents)
        else:
            batch_embeddings = []

        emb_idx = 0
        for meta in new_memories_meta:
            content = meta.get("content", "")
            if not content:
                continue

            layer = MemoryLayer.from_string(meta.get("layer", "L2_FACT"))
            tags = meta.get("tags") or []
            speculate = meta.get("speculate")

            new_node = MemoryNode(
                user_id=request.user_id,
                agent_id=request.agent_id or "default_agent",
                session_id=request.session_id or "default_session",
                layer=layer,
                content=content,
                owner=_norm_owner(meta.get("owner")),
                speculate=speculate,
                is_latest=True,
                source_type=SourceType.INFERRED,
                status=MemoryStatus.ACTIVE,
                embedding=batch_embeddings[emb_idx] if emb_idx < len(batch_embeddings) else await self.embed_service.embed_queued(content),
                memory_at=request.memory_at,
                tags=list(tags),
                emotional_valence=getattr(self, "_cur_valence", 0.0),
                emotional_arousal=getattr(self, "_cur_arousal", 0.0),
            )
            emb_idx += 1
            nid = await vector_store.upsert(new_node)
            stored_ids.append(nid)
            await self._maybe_index_entities(vector_store, new_node, request)

        logger.info(f"[direct_store] stored {len(stored_ids)} nodes (reconcile disabled)")
        n = len(stored_ids)
        return stored_ids, None, {"add": n, "supersede": 0, "update": 0, "total": n}, {"prompt": 0, "completion": 0, "total": 0}

    async def _store_summary(
        self,
        summary_content: str,
        source_raw_memory_id: str,
        request: WriteRequest,
        vector_store: VectorStoreBase,
    ) -> str:
        """存储 L3_SUMMARY 节点，返回 node_id。"""
        summary_node = MemoryNode(
            user_id=request.user_id,
            agent_id=request.agent_id or "default_agent",
            session_id=request.session_id or "default_session",
            layer=MemoryLayer.L3_SUMMARY,
            content=summary_content,
            source_type=SourceType.INFERRED,
            status=MemoryStatus.ACTIVE,
            is_latest=True,
            source_raw_memory_id=source_raw_memory_id,
            embedding=await self.embed_service.embed_queued(summary_content),
            memory_at=request.memory_at,
        )
        return await vector_store.upsert(summary_node)

    async def _emit_pipeline_step(
        self,
        request: WriteRequest,
        *,
        step: str,
        parsed: str | dict[str, Any] | list[Any],
        elapsed_ms: float = 0,
        response: str = "",
        prompt: str = "",
        memory_ids: list[str] | None = None,
        **kwargs: Any,
    ) -> None:
        """环节级 log/trace（经 client hook：文件始终 + DB 可关）。"""
        if not self._cache:
            return
        parsed_str = (
            parsed
            if isinstance(parsed, str)
            else json.dumps(parsed, ensure_ascii=False, default=str)
        )
        try:
            await self._cache.store_pipeline_log(
                request_id=request.request_id or get_request_id(),
                user_id=request.user_id,
                agent_id=request.agent_id or "default_agent",
                step=step,
                prompt=prompt,
                response=response,
                parsed=parsed_str,
                memory_ids=memory_ids,
                elapsed_ms=elapsed_ms,
                **kwargs,
            )
        except Exception as e:
            logger.debug(f"[write] pipeline step {step} failed: {e}")

    async def _emit_write_timeline(
        self,
        request: WriteRequest,
        tracer: PipelineTracer,
    ) -> None:
        """请求级 timeline 写入 Trace（SQLite），供 Inspector。"""
        if not self._cache or not is_pipeline_trace_enabled():
            return
        try:
            await self._cache.store_pipeline_log(
                request_id=request.request_id or get_request_id() or tracer.request_id,
                user_id=request.user_id,
                agent_id=request.agent_id or "default_agent",
                step="WRITE_TIMELINE",
                prompt="",
                response=tracer.to_summary_line(),
                parsed=json.dumps(tracer.to_dict(), ensure_ascii=False, default=str),
                elapsed_ms=tracer.total_ms,
            )
        except Exception as e:
            logger.debug(f"[write] WRITE_TIMELINE failed: {e}")

    # ================================================================
    # 主写入流程
    # ================================================================

    async def write(
        self,
        request: WriteRequest,
        ctx: PipelineContext | None = None,
        tracer: PipelineTracer | None = None,
    ) -> WriteResponse:
        """执行 Lite 写入流程。"""
        start_time = datetime.now()
        response = WriteResponse()

        # request_id 归一化：优先用 client 显式透传的 request.request_id（contextvar-immune），
        # 兜底用 contextvar（保护直接调用 write() 的测试 / 其它 caller）。
        # 此后整条链路落库统一读 request.request_id，不再依赖 contextvar。
        if not request.request_id:
            request.request_id = get_request_id()

        if tracer is None:
            tracer = create_tracer(
                operation="write",
                pipeline_version="system1",
                uid=request.user_id,
                agent_id=request.agent_id,
                request_id=request.request_id,
                content_preview=request.content,
            )

        try:
            # 参数校验
            content = request.content
            if not content and request.has_messages():
                content = request.get_flat_content()
                request.content = content

            if not content:
                response.error_code = 400
                response.error_message = "content or messages is required"
                return response
            if not request.user_id:
                response.error_code = 400
                response.error_message = "user_id is required"
                return response

            # ── 确保 memory_at 有值：未传则用接收到请求的时间 ──
            if request.memory_at is None:
                request.memory_at = start_time

            total_tokens = 0

            # 1. Layer 分配
            with tracer.span("layer_assign") as s:
                layer_str = request.extra.get("layer", "")
                if layer_str:
                    suggested_layer = layer_str
                    s.set_output({"layer": suggested_layer, "source": "explicit"})
                else:
                    suggested_layer = MemoryLayer.L1_RAW.value
                    s.set_output({"layer": suggested_layer, "source": "default_raw"})

            response.layer = suggested_layer

            # ── Timing: sys1_waiting_ms ──
            # start_time 是进入 write() 的时间，现在开始实际 I/O
            _t_process_start = datetime.now()
            _sys1_waiting_ms = (_t_process_start - start_time).total_seconds() * 1000
            _req_id_perf = request.request_id or get_request_id()
            logger.info(f"TRACE_PERF [{_req_id_perf}] S1_START waiting={_sys1_waiting_ms:.0f}ms user={request.user_id}")

            # Metrics: S1 开始
            from ..metrics import MetricsCollector
            MetricsCollector.get().sys1_start()

            # 2. 向量化 + 持久化（L1_RAW）
            _t_embed = datetime.now()
            with tracer.span("embed") as s:
                embedding = await self.embed_service.embed_queued(request.content)
                s.set_output({"dims": len(embedding), "content_len": len(request.content)})
            _embed_ms = (datetime.now() - _t_embed).total_seconds() * 1000
            response.extra["embedding"] = embedding
            await self._emit_pipeline_step(
                request,
                step="S1_EMBED",
                parsed={"dims": len(embedding), "content_len": len(request.content)},
                elapsed_ms=_embed_ms,
            )

            # 3. 持久化到向量库（L1_RAW）
            _t_l1 = datetime.now()
            memory_id = ""
            _l1_error: str | None = None
            vector_store = None
            mem_node = None
            with tracer.span("qdrant_upsert") as s:
                try:
                    vector_store = await self._get_vector_store()
                    layer_enum = MemoryLayer.from_string(suggested_layer)
                    mem_node = MemoryNode(
                        user_id=request.user_id,
                        agent_id=request.agent_id or "default_agent",
                        session_id=request.session_id or "default_session",
                        layer=layer_enum,
                        content=request.content,
                        source_type=SourceType.EXPLICIT,
                        status=MemoryStatus.ACTIVE,
                        is_latest=True,
                        embedding=embedding,
                        memory_at=request.memory_at,
                    )
                    memory_id = await vector_store.upsert(mem_node)
                    response.memory_id = memory_id
                    s.set_output({"memory_id": memory_id, "layer": suggested_layer})
                    logger.debug(f"[write] persisted: id={memory_id} layer={suggested_layer}")
                except Exception as persist_err:
                    _l1_error = str(persist_err)
                    s.set_error(_l1_error)
                    logger.error(f"V1 Write: Persist failed (non-fatal): {persist_err}", exc_info=True)
            _l1_ms = (datetime.now() - _t_l1).total_seconds() * 1000
            # sparse 全文向量是否随本次 upsert 写入（tencent + BM25 可用时为 True）
            _sparse_enabled = bool(getattr(vector_store, "supports_fulltext", False))
            await self._emit_pipeline_step(
                request,
                step="S1_L1_UPSERT",
                parsed={
                    "memory_id": memory_id or None,
                    "layer": suggested_layer,
                    "content": (mem_node.content if mem_node else request.content),
                    "tags": list(mem_node.tags) if (mem_node and mem_node.tags) else [],
                    "sparse_enabled": _sparse_enabled,
                    "error": _l1_error,
                },
                elapsed_ms=_l1_ms,
                memory_ids=[memory_id] if memory_id else None,
            )

            # ── Timing: sys1_l1_process_ms ──
            _sys1_l1_process_ms = (datetime.now() - _t_process_start).total_seconds() * 1000
            logger.info(f"TRACE_PERF [{_req_id_perf}] S1_L1_DONE l1_ms={_sys1_l1_process_ms:.0f}ms")

            # 4. MemAgent 处理（可选）
            agent_mode = request.extra.get("agent_mode", "disabled")
            response.extra["agent_mode"] = agent_mode

            _t_workflow = datetime.now()
            if agent_mode == "full" and vector_store is not None and mem_node is not None:
                # ── 获取历史上下文（最近 20 条 messages）供 extractor 使用 ──
                _history_context = ""
                try:
                    _history_context = await self._get_recent_history(
                        vector_store, request.user_id, request.agent_id,
                        exclude_memory_id=memory_id,
                    )
                except Exception as _hist_err:
                    logger.warning(f"[write] get_recent_history failed: {_hist_err}")

                with tracer.span("mem_agent") as s:
                    await self._run_agent(
                        request=request,
                        response=response,
                        vector_store=vector_store,
                        mem_node=mem_node,
                        memory_id=memory_id,
                        tracer_span=s,
                        history_context=_history_context,
                    )
                    total_tokens += response.extra.get("_agent_tokens", 0)
                if response.extra.get("agent_status") == "failed":
                    await self._emit_pipeline_step(
                        request,
                        step="S1_AGENT_FAILED",
                        parsed={
                            "error_code": response.extra.get("agent_error_code", ""),
                            "error": response.extra.get("agent_error", ""),
                        },
                        elapsed_ms=(datetime.now() - _t_workflow).total_seconds() * 1000,
                    )
            elif agent_mode == "full":
                await self._emit_pipeline_step(
                    request,
                    step="S1_AGENT_SKIPPED",
                    parsed={"reason": "l1_upsert_failed_or_no_vector_store"},
                )

            # ── Timing: sys1_workflow_ms ──
            _sys1_workflow_ms = (datetime.now() - _t_workflow).total_seconds() * 1000
            logger.info(f"TRACE_PERF [{_req_id_perf}] S1_WORKFLOW_DONE workflow_ms={_sys1_workflow_ms:.0f}ms")

            # 5. 合并检测（可选）
            enable_merge_check = request.extra.get("enable_merge_check", False)
            should_merge = response.extra.get("should_merge", False)

            if enable_merge_check and not should_merge and request.existing_memories:
                with tracer.span("merge_check") as s:
                    existing_for_merge = [
                        {"memory_id": mem.get("memory_id", ""), "content": mem.get("content", "")}
                        for mem in request.existing_memories
                    ]
                    merge_result = self.merger.check_merge(
                        new_content=request.content,
                        existing_memories=existing_for_merge,
                    )
                    response.extra["should_merge"] = merge_result.should_merge
                    response.extra["merge_target_id"] = merge_result.target_memory_id or ""
                    s.set_output({
                        "should_merge": merge_result.should_merge,
                        "target_id": merge_result.target_memory_id or "",
                    })

            response.success = True
            response.tokens_used = total_tokens

            # ── 汇总 sys1 timing ──
            _ops_count = len(response.extra.get("agent_stored_ids", []))
            _ops_avg_ms = _sys1_workflow_ms / _ops_count if _ops_count > 0 else 0
            response.extra["timing"] = {
                "sys1_waiting_ms": round(_sys1_waiting_ms, 1),
                "sys1_l1_process_ms": round(_sys1_l1_process_ms, 1),
                "sys1_workflow_ms": round(_sys1_workflow_ms, 1),
                "sys1_ops_avg_ms": round(_ops_avg_ms, 1),
            }

            # Metrics: S1 完成 + VDB ops
            _mc = MetricsCollector.get()
            _mc.sys1_end(response.extra["timing"], success=True)
            if _ops_count > 0:
                for _ in range(_ops_count):
                    _mc.record_vdb_op(_ops_avg_ms)

        except Exception as e:
            logger.error(f"MemoryWriter.write failed: {e}", exc_info=True)
            response.error_code = 500
            response.error_message = str(e)
            tracer.set_error(str(e))
            # Metrics: S1 失败
            try:
                MetricsCollector.get().sys1_end({}, success=False)
            except Exception:
                pass
        finally:
            response.elapsed_ms = (datetime.now() - start_time).total_seconds() * 1000
            tracer.set_output({
                "success": response.success,
                "layer": response.layer,
                "memory_id": response.memory_id,
                "entities_count": len(response.entities),
                "entities": [
                    {"name": e.get("name", ""), "type": e.get("type", "")}
                    if isinstance(e, dict) else {"name": str(e)}
                    for e in response.entities
                ],
                "summary": response.extra.get("summary", ""),
                "content_stored": request.content,
                "tokens_used": response.tokens_used,
                "pipeline_ms": response.elapsed_ms,
                "agent_status": response.extra.get("agent_status", ""),
            })
            await self._emit_write_timeline(request, tracer)
            tracer.finish(write_file=False)

        return response

    async def _get_recent_history(
        self,
        vector_store: VectorStoreBase,
        user_id: str,
        agent_id: str | None = None,
        exclude_memory_id: str = "",
        max_turns: int = 20,
        max_chars_assistant: int = 500,
    ) -> str:
        """
        获取该用户最近的 L1_RAW 对话记录，按轮次拆分后拼成 history context。

        每条 L1_RAW 的 content 是多轮对话（[user]: ...\n[assistant]: ...）。
        拆成单条 message 后，user 消息完整保留，assistant 消息截取前 max_chars_assistant 字符。
        最终取最近 max_turns 轮。

        Returns:
            格式化的历史上下文字符串，空字符串表示无历史。
        """
        try:
            nodes = await vector_store.list_by_user(
                user_id=user_id,
                agent_id=agent_id,
                layers=[MemoryLayer.L1_RAW],
                status_filter=[MemoryStatus.ACTIVE, MemoryStatus.SHADOW],
                limit=50,
            )
            if not nodes:
                return ""

            # 排除当前正在处理的 memory（避免自引用）
            nodes = [n for n in nodes if n.node_id != exclude_memory_id]

            # 按 gmt_created 升序（时间正序）
            nodes.sort(key=lambda n: n.gmt_created or datetime.min)

            # 拆分每条 L1_RAW 内容为单条 messages。
            # L1_RAW content 格式: "[user]: xxx\n[assistant]: yyy\n[user]: zzz..."
            # 关键：单条 message 的 content 本身可能含换行（多行 user 输入），
            # 因此必须按「[role]: 前缀」识别消息边界，无前缀的行视为上一条消息的续行
            # 归属其下，而不是逐行当成独立 message——否则续行会被当成裸消息，且后续
            # 按行数截断时可能从一条多行消息中间切开，导致前缀行丢失、续行裸露在开头。
            import re as _re
            _role_prefix = _re.compile(r"^\[?(user|assistant|system|tool)\]?:\s?")

            # (role, content) 元组列表
            parsed: list[tuple[str, str]] = []
            for node in nodes:
                content = node.content or ""
                for line in content.split("\n"):
                    m = _role_prefix.match(line)
                    if m:
                        role = m.group(1)
                        body = line[m.end():]
                        parsed.append((role, body))
                    elif parsed:
                        # 续行：拼回上一条 message（保留换行）
                        prev_role, prev_body = parsed[-1]
                        parsed[-1] = (prev_role, prev_body + "\n" + line)
                    else:
                        # 开头就是无前缀行（异常/老数据）：当成 user 消息兜底
                        parsed.append(("user", line))

            # 取最近 max_turns 条 message（按 message 截断，绝不从消息中间切开）
            recent = parsed[-max_turns:] if len(parsed) > max_turns else parsed
            if not recent:
                return ""

            # 格式化：assistant 整条截断，user 完整保留
            all_messages: list[str] = []
            for role, body in recent:
                body = body.strip()
                if role == "assistant" and len(body) > max_chars_assistant:
                    body = body[:max_chars_assistant] + "..."
                all_messages.append(f"[{role}]: {body}")

            return "\n".join(all_messages)
        except Exception as e:
            logger.debug(f"[write] _get_recent_history error: {e}")
            return ""

    async def _collect_existing_tags(
        self,
        vector_store: VectorStoreBase,
        user_id: str,
        agent_id: str | None = None,
    ) -> list[str]:
        """
        收集该用户在 VDB 中已有的所有 tags（去重）。

        从 vector_store.list_by_user 的结果中提取所有 tags 字段，
        合并为唯一集合，供 extract prompt 引导 LLM 优先复用已有标签。
        失败静默返回空列表（不阻塞主流程）。
        """
        try:
            nodes = await vector_store.list_by_user(
                user_id=user_id,
                agent_id=agent_id,
                limit=10000,
            )
            tag_set: set = set()
            for node in nodes:
                if hasattr(node, "tags") and node.tags:
                    tag_set.update(node.tags)
            tags = sorted(tag_set)
            if tags:
                logger.debug(f"[write] collected {len(tags)} existing tags for user={user_id}")
            return tags
        except Exception as e:
            logger.warning(f"[write] _collect_existing_tags failed (non-fatal): {e}")
            return []

    async def _run_agent(
        self,
        request: WriteRequest,
        response: WriteResponse,
        vector_store: VectorStoreBase,
        mem_node: MemoryNode,
        memory_id: str,
        tracer_span,
        history_context: str = "",
    ) -> None:
        """
        MemAgent 完整处理流程：extract → reconcile & store → summary。
        结果写回 response.extra。
        """
        mode = ProcessMode.FULL
        existing_memories = []
        for mem in (request.existing_memories or []):
            existing_memories.append({
                "memory_id": mem.get("memory_id", ""),
                "content": mem.get("content", ""),
                "layer": mem.get("layer", ""),
                "embedding": mem.get("embedding", []),
            })

        try:
            # 收集该用户已有的所有 tags（供 extractor prompt 引导复用）
            existing_tags = await self._collect_existing_tags(
                vector_store, request.user_id, request.agent_id
            )

            # 构建 extractor 输入内容：优先用原始 messages dump
            if request.messages:
                import json as _json_msgs
                _extract_content = _json_msgs.dumps(
                    [{"role": m.role, "content": m.content} for m in request.messages],
                    ensure_ascii=False,
                )
            else:
                _extract_content = request.content

            # 基础画像 schema 字段（{name: description}），由 extractor 渲染到 prompt
            _basic_profile_fields = self.config.basic_profile.effective_fields()

            agent_result = await self.mem_agent.process_add(
                content=_extract_content,
                context={"uid": request.user_id, "agent_id": request.agent_id},
                mode=mode,
                existing_memories=existing_memories,
                memory_at=request.memory_at,
                existing_tags=existing_tags,
                history_context=history_context,
                enable_summary=request.enable_summary,
                basic_profile_fields=_basic_profile_fields,
                extract_scene=request.extract_scene,
            )
        except Exception as agent_err:
            logger.error(f"[write] MemAgent process_add raised: {agent_err}", exc_info=True)
            response.extra["agent_status"] = "failed"
            response.extra["agent_error"] = str(agent_err)
            response.extra["agent_nodes"] = 0
            tracer_span.set_output({"success": False, "error": str(agent_err)})
            return

        if agent_result is None or not agent_result.success:
            await self._handle_agent_failure(request, response, agent_result, tracer_span)
            return

        # ── Emotional tagging: analyze valence/arousal on the conversation ──
        # Best-effort: failures default to 0.0 (neutral), never block writes.
        _valence = 0.0
        _arousal = 0.0
        if getattr(getattr(self.config, "extractor", None), "emotion_enabled", False):
            try:
                from ..agent.emotion_analyzer import EmotionAnalyzer
                _emotion = EmotionAnalyzer(self.mem_agent.extractor.llm)
                _emo_result = await _emotion.analyze(_extract_content[:2000])
                if _emo_result.success:
                    _valence = _emo_result.valence
                    _arousal = _emo_result.arousal
                    logger.info(
                        f"[write] emotion: valence={_valence:.2f} "
                        f"arousal={_arousal:.2f} emotion={_emo_result.dominant_emotion}"
                    )
            except Exception as emo_err:
                logger.debug(f"[write] emotion analysis skipped: {emo_err}")

        # ── 提取成功 ──
        _perf_req_id = request.request_id or get_request_id()
        logger.info(
            f"TRACE_PERF [{_perf_req_id}] S1_EXTRACT_DONE "
            f"extract_ms={agent_result.extract_elapsed_ms:.0f} "
            f"summary_ms={agent_result.summary_elapsed_ms:.0f} "
            f"memory={len((agent_result.extracted_info or {}).get('memory', (agent_result.extracted_info or {}).get('facts', [])))}"
        )

        # ── basic_info: prompt-driven schema, no LLM function-calling ──
        # extractor 把 basic_info dict 放进 extracted_info；writer 在此 upsert L0_BASIC_INFO
        # 演化链。失败/无效都不抛错，落 tool_results_summary 走原 TOOL_CALLS pipeline log。
        basic_info_raw = None
        if isinstance(agent_result.extracted_info, dict):
            basic_info_raw = agent_result.extracted_info.pop("basic_info", None)

        tool_calls_raw = None  # v0.1.5.13+ 不再有真实 LLM tool_calls；保留字段做兼容
        tool_results_summary: list[dict[str, Any]] = []

        if isinstance(basic_info_raw, dict) and basic_info_raw:
            try:
                _bp_result = await upsert_basic_profile(
                    user_id=request.user_id,
                    agent_id=request.agent_id or "default_agent",
                    session_id=request.session_id or "default_session",
                    kv=basic_info_raw,
                    vector_store=vector_store,
                    embed_service=self.embed_service,
                    allowed_fields=list(_basic_profile_fields.keys()),
                )
                # 把 upsert 结果包成原 tool_results 形态（向后兼容 pipeline log 解析）
                tool_results_summary.append({
                    "tool": "basic_profile_upsert",  # 不再是 LLM tool name
                    "round": 1,
                    "success": _bp_result.success,
                    "data": _bp_result.to_dict(),
                    "error": _bp_result.error,
                })
                # input 侧记录 LLM 给出的 basic_info 原始值（trace 可追溯）
                tool_calls_raw = [{
                    "function": {
                        "name": "basic_profile_upsert",
                        "arguments": json.dumps(basic_info_raw, ensure_ascii=False, default=str),
                    }
                }]
            except Exception as bp_err:
                logger.error(f"[write] upsert_basic_profile raised: {bp_err}", exc_info=True)
                tool_results_summary.append({
                    "tool": "basic_profile_upsert",
                    "round": 1,
                    "success": False,
                    "error": str(bp_err),
                })

        response.extra["tool_results"] = tool_results_summary

        # 写 TOOL_CALLS pipeline log（可观测 tool 调用链路）
        if self._cache and (tool_calls_raw or tool_results_summary):
            try:
                import json as _json_tc
                _tc_req_id = request.request_id or get_request_id()
                tc_data = {
                    "tool_calls_input": (
                        [{"name": tc.get("function", {}).get("name", ""), "arguments": tc.get("function", {}).get("arguments", "")}
                         for tc in tool_calls_raw]
                        if isinstance(tool_calls_raw, list) else str(tool_calls_raw)[:500]
                    ) if tool_calls_raw else [],
                    "tool_results": tool_results_summary,
                    "tool_calls_only": bool(
                        tool_calls_raw
                        and not (agent_result.extracted_info or {}).get("memory")
                        and not (agent_result.extracted_info or {}).get("facts")
                        and not (agent_result.extracted_info or {}).get("identity")
                    ),
                }
                await self._cache.store_pipeline_log(
                    request_id=_tc_req_id,
                    user_id=request.user_id,
                    agent_id=request.agent_id or "default_agent",
                    step="TOOL_CALLS",
                    prompt="",
                    response=_json_tc.dumps(tc_data, ensure_ascii=False, default=str),
                    parsed=_json_tc.dumps(tc_data, ensure_ascii=False, default=str),
                    memory_ids=[],
                )
            except Exception as e:
                logger.warning(f"[write] store TOOL_CALLS log failed: {e}")
        # 过滤实体
        _CATEGORY_BLACKLIST = {
            "locations", "location", "persons", "person",
            "organizations", "organization", "events", "event",
            "relations", "relation", "products", "product",
            "animals", "animal", "foods", "food",
            "technologies", "technology", "tech",
            "attributes", "attribute", "others", "other",
        }
        if agent_result.extracted_info and isinstance(agent_result.extracted_info, dict):
            for entity in (agent_result.extracted_info.get("entities") or []):
                if isinstance(entity, dict):
                    name = entity.get("name", "")
                    if name and name.lower() not in _CATEGORY_BLACKLIST:
                        response.entities.append(entity)
                elif isinstance(entity, str):
                    if entity.lower() not in _CATEGORY_BLACKLIST:
                        response.entities.append({"name": entity})

        response.extra["summary"] = agent_result.summary or ""
        if agent_result.suggested_layer:
            response.layer = agent_result.suggested_layer

        conflicts = []
        for conflict in (agent_result.conflicts or []):
            conflicts.append({
                "type": conflict.get("type", ""),
                "target_id": conflict.get("target_id", ""),
                "description": conflict.get("description", ""),
                "resolution": conflict.get("resolution", ""),
            })
        response.extra["conflicts"] = conflicts
        response.extra["should_merge"] = agent_result.should_merge
        response.extra["merge_target_id"] = agent_result.merge_target_id or ""
        response.extra["_agent_tokens"] = agent_result.tokens_used
        # extract 链路 LLM token 消耗（prompt/completion/total），供 client 透传给调用方
        response.extra["extract_tokens"] = {
            "prompt": getattr(agent_result, "extract_prompt_tokens", 0) or 0,
            "completion": getattr(agent_result, "extract_completion_tokens", 0) or 0,
            "total": getattr(agent_result, "extract_tokens_used", 0) or 0,
        }

        # 写 pipeline logs
        _req_id = request.request_id or get_request_id()
        await self._write_extract_log(request, agent_result, _req_id, tool_results=tool_results_summary)
        await self._write_summary_log(request, agent_result, _req_id)

        # Reconcile & store
        stored_ids: list[str] = []
        self._cur_valence = _valence
        self._cur_arousal = _arousal
        _vs_logger = logging.getLogger("hy_memory.data.vector_store_chroma")
        _vs_level = _vs_logger.level
        _vs_logger.setLevel(logging.INFO)
        try:
            new_memory_texts, new_memories_meta = self._collect_new_memories(
                agent_result.extracted_info or {}
            )

            # extractor 结果去重：新提取的多条之间互相判重（额外 embed 一次，
            # 还没入库故 delete_from_store=False，只丢弃重复项 + 记 DEDUP log）。
            if len(new_memory_texts) >= 2:
                try:
                    new_memory_texts, new_memories_meta = await self._dedup_extracted(
                        new_memory_texts, new_memories_meta, request, _req_id,
                    )
                except Exception as e:
                    logger.warning(f"[write] extractor dedup failed (non-fatal): {e}")

            if new_memory_texts:
                if _RECONCILE_ENABLED:
                    stored_ids, recon_error, ops_counts, recon_tokens = await self._reconcile_and_store(
                        new_memory_texts=new_memory_texts,
                        new_memories_meta=new_memories_meta,
                        request=request,
                        vector_store=vector_store,
                        req_id=_req_id,
                    )
                else:
                    stored_ids, recon_error, ops_counts, recon_tokens = await self._direct_store(
                        new_memories_meta=new_memories_meta,
                        request=request,
                        vector_store=vector_store,
                        req_id=_req_id,
                    )
                response.extra["reconcile_ops"] = ops_counts
                response.extra["reconcile_tokens"] = recon_tokens
                if recon_error is not None:
                    response.success = False
                    response.error_code = 502
                    response.error_message = f"[RECONCILE_FAILED] {recon_error}"
                    response.extra["agent_status"] = "failed"
                    response.extra["agent_error"] = recon_error
                    response.extra["agent_nodes"] = 0
                    logger.warning(f"[write] reconcile failed: {recon_error}")
                    return

            # Intentions → L7（直接 upsert，不走 reconcile）
            intentions = self._collect_intentions(agent_result.extracted_info or {})
            if intentions:
                intention_ids = await self._store_intentions(
                    intentions=intentions,
                    request=request,
                    vector_store=vector_store,
                    req_id=_req_id,
                )
                stored_ids.extend(intention_ids)
                logger.info(
                    f"TRACE_PERF [{_perf_req_id}] S1_INTENTION_DONE "
                    f"intentions={len(intention_ids)}"
                )

            # Summary → L3
            if agent_result.summary:
                sid = await self._store_summary(
                    summary_content=agent_result.summary,
                    source_raw_memory_id=memory_id,
                    request=request,
                    vector_store=vector_store,
                )
                stored_ids.append(sid)

            response.extra["agent_stored_ids"] = stored_ids
            response.extra["agent_status"] = "success"
            response.extra["agent_error"] = ""
            response.extra["agent_nodes"] = len(stored_ids)
            _vs_logger.setLevel(_vs_level)
            logger.info(
                f"TRACE_PERF [{_perf_req_id}] S1_PERSIST_DONE "
                f"ops={len(stored_ids)} nodes to vector store"
            )
            logger.debug(f"[agent] persisted {len(stored_ids)} nodes to vector store")

            # L1_RAW → SHADOW
            if stored_ids and memory_id:
                try:
                    mem_node.status = MemoryStatus.SHADOW
                    await vector_store.upsert(mem_node)
                    logger.debug(f"[agent] L1 raw {memory_id} status → SHADOW")
                except Exception as shadow_err:
                    logger.warning(f"[agent] failed to shadow L1 raw: {shadow_err}")

        except Exception as persist_err:
            _vs_logger.setLevel(_vs_level)
            logger.error(f"[agent] persist failed: {persist_err}", exc_info=True)
            response.extra["agent_stored_ids"] = stored_ids
            response.extra["agent_status"] = "failed"
            response.extra["agent_error"] = f"agent persist failed: {persist_err}"
            response.extra["agent_nodes"] = len(stored_ids)
            return

        tracer_span.set_output({
            "success": True,
            "entities_count": len(response.entities),
            "summary": agent_result.summary or "",
            "conflicts_count": len(conflicts),
            "tokens_used": agent_result.tokens_used,
            "should_merge": agent_result.should_merge,
            "agent_stored_count": len(stored_ids),
        })

    async def _handle_agent_failure(self, request, response, agent_result, tracer_span) -> None:
        """处理 agent extract 失败的情况。"""
        if agent_result is None:
            tracer_span.set_output({"success": False, "error": "agent processing failed"})
            return

        _error_code = getattr(agent_result, "error_code", "") or "AGENT_FAILED"
        _error_msg = agent_result.error or "agent processing failed"
        response.extra["agent_status"] = "failed"
        response.extra["agent_error"] = _error_msg
        response.extra["agent_error_code"] = _error_code
        response.extra["agent_nodes"] = 0
        response.success = False
        response.error_code = 502
        response.error_message = f"[{_error_code}] {_error_msg}"
        logger.warning(f"[write] agent extract failed: code={_error_code} error={_error_msg}")

        _raw_resp = getattr(agent_result, "extract_raw_response", "") or ""
        await self._emit_pipeline_step(
            request,
            step="EXTRACT",
            parsed={"error": _error_code, "message": _error_msg},
            response=_raw_resp,
            elapsed_ms=getattr(agent_result, "extract_elapsed_ms", 0) or 0,
            prompt_tokens=getattr(agent_result, "extract_prompt_tokens", 0) or 0,
            completion_tokens=getattr(agent_result, "extract_completion_tokens", 0) or 0,
            total_tokens=getattr(agent_result, "extract_tokens_used", 0) or 0,
        )

        tracer_span.set_output({"success": False, "error": _error_msg})

    async def _write_extract_log(self, request, agent_result, req_id: str, tool_results: list[dict[str, Any]] = None) -> None:
        """写 EXTRACT pipeline log。"""
        if not self._cache:
            return
        try:
            import json as _json

            # 使用 extractor 返回的真实 prompt（如果有的话）
            _extract_prompt = ""
            _extract_result = getattr(agent_result, '_extract_result', None)
            if _extract_result and hasattr(_extract_result, '_actual_prompt') and _extract_result._actual_prompt:
                _sys = _extract_result._actual_system_prompt or ""
                _usr = _extract_result._actual_prompt or ""
                _extract_prompt = f"[SYSTEM]\n{_sys}\n\n[USER]\n{_usr}"
            else:
                # fallback: 用旧方式构造近似 prompt（不完全准确）
                from ..agent.extractor import EXTRACT_PROMPT
                _current_time = request.memory_at.isoformat(timespec="seconds") if request.memory_at else ""
                _extract_prompt = EXTRACT_PROMPT.format(content=request.content, current_date="", memory_at=_current_time or "", existing_tags="(see actual prompt)", last_messages="")

            # response 字段：优先用 raw_response（LLM 原始输出）
            _raw_resp = agent_result.extract_raw_response or ""

            # parsed 字段：包含 extracted_info + tool_results 元信息
            _parsed_data = dict(agent_result.extracted_info) if agent_result.extracted_info else {}
            if tool_results:
                _parsed_data["_tool_results"] = tool_results

            await self._cache.store_pipeline_log(
                request_id=req_id,
                user_id=request.user_id,
                agent_id=request.agent_id or "default_agent",
                step="EXTRACT",
                prompt=_extract_prompt,
                response=_raw_resp if _raw_resp else _json.dumps(_parsed_data, ensure_ascii=False, default=str),
                parsed=_json.dumps(_parsed_data, ensure_ascii=False, default=str) if _parsed_data else "",
                elapsed_ms=agent_result.extract_elapsed_ms,
                prompt_tokens=agent_result.extract_prompt_tokens,
                completion_tokens=agent_result.extract_completion_tokens,
                total_tokens=agent_result.extract_tokens_used,
            )
        except Exception as e:
            logger.warning(f"[write] store EXTRACT log failed: {e}")

    async def _write_summary_log(self, request, agent_result, req_id: str) -> None:
        """写 SUMMARY pipeline log（若有摘要）。"""
        if not self._cache or not agent_result.summary or not agent_result.summary_tokens_used:
            return
        try:
            import json as _json_s

            # 使用 summarizer 返回的真实 prompt
            _summary_result = getattr(agent_result, '_summary_result', None)
            if _summary_result and hasattr(_summary_result, '_actual_prompt') and _summary_result._actual_prompt:
                _summary_prompt = _summary_result._actual_prompt
            else:
                # fallback
                from datetime import date as _date_now

                from ..agent.summarizer import SUMMARY_PROMPT
                _current_time = request.memory_at.isoformat(timespec="seconds") if request.memory_at else ""
                _memory_date = _current_time[:10] if _current_time and len(_current_time) >= 10 else _date_now.today().isoformat()
                _current_date = _date_now.today().isoformat()
                _summary_prompt = SUMMARY_PROMPT.format(
                    content=request.content or "",
                    memory_date=_memory_date,
                    current_date=_current_date,
                )
            await self._cache.store_pipeline_log(
                request_id=req_id,
                user_id=request.user_id,
                agent_id=request.agent_id or "default_agent",
                step="SUMMARY",
                prompt=_summary_prompt,
                response=agent_result.summary,
                parsed=_json_s.dumps({"summary": agent_result.summary}, ensure_ascii=False),
                elapsed_ms=agent_result.summary_elapsed_ms,
                prompt_tokens=agent_result.summary_prompt_tokens,
                completion_tokens=agent_result.summary_completion_tokens,
                total_tokens=agent_result.summary_tokens_used,
            )
        except Exception as e:
            logger.warning(f"[write] store SUMMARY log failed: {e}")

    async def close(self) -> None:
        logger.debug("MemoryWriter closed")
