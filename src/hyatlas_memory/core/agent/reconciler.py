"""
Agent Memory - MemoryReconciler 记忆协调器

在 Lite pipeline 中，LLM 提取完新 facts/identity 后、入库前，
与向量库中的现有记忆做 reconcile：

流程:
  1. （可选）Pre-process: LLM 生成 search queries（MEMORY_ENABLE_SEARCH_QUERY=true，默认关）
  2. 候选搜索: 默认每条新 memory 做 hybrid 检索（向量池 limit×3 + BM25，min_score=0.4）；
     开启 search query 时额外用扩写 query 做向量搜索
  3. 一次 LLM 调用（Reconcile），输入新记忆 + 候选旧记忆
  4. 返回操作指令列表（ADD / DELETE，按顺序执行）
  5. 调用方按指令执行入库操作并更新旧节点状态

操作 Schema（两种 op 自由组合表达三种语义）:
  ADD    → 新增节点
           supersedes=[]         → 场景 A：全新信息（与任何旧记忆无关）
           supersedes=[id, ...]  → 场景 B：矛盾/取代（旧节点不再成立，进演化链）
  DELETE → 逻辑删除旧节点（status=SHADOW, is_latest=False，不进演化链）
           场景 C：合并吸收 — 配合 ADD(supersedes=[]) 使用，表达
                   "这条 ADD 吸收了某几条旧记忆的内容，旧记忆不再召回"

  例子：
    场景 B 矛盾：ADD(content="喝茶", supersedes=[id_coffee])
    场景 C 合并：ADD(content="有 PI 血统并在音乐中运用", supersedes=[]) + DELETE(id_heritage)
"""

import json
import logging
import os
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from ..config import MemoryConfig
from .llm_provider import LLMProvider

logger = logging.getLogger(__name__)


def _log_ops_detail(ops: list["ReconcileOp"]) -> None:
    """以单条 INFO（JSON list）输出 reconcile 的逐条结果，便于线上 debug。"""
    if not ops:
        return
    view = [
        {
            "op": o.op,
            "memory_id": o.memory_id or None,
            "content": o.content,
            "tags": list(o.tags or []),
            "reason": o.supersede_reason or o.reason or "",
        }
        for o in ops
    ]
    logger.info(
        f"[reconciler] ops detail: {json.dumps(view, ensure_ascii=False, default=str)}"
    )


# ================================================================
# 操作指令
# ================================================================


@dataclass
class ReconcileOp:
    """
    单条操作指令（v2 flat 格式）。

    op = "ADD"：纯新增节点，信息完全新颖，不引用任何现有记忆。

    op = "SUPERSEDE"：状态演变/矛盾链。新记忆取代旧记忆成为链头部。
      - memory_id 指向被取代的旧节点
      - content 仅描述新/变更的信息（旧节点自动保留在链中）
      - supersede_reason 描述矛盾原因
      - 执行：旧节点 → SHADOW + is_latest=False；新节点 supersedes=[old_id]（进演化链）

    op = "UPDATE"：合并与精炼。新记忆与旧记忆描述同一主题且不矛盾，合并为高密度节点。
      - memory_id 指向被更新的旧节点
      - content 是合并后的完整文本
      - 执行：旧节点 → SHADOW + is_latest=False；新节点独立创建（不进演化链，无 supersedes）
      - 旧节点作为历史保留在 VDB 中（status=SHADOW），但不形成链关系
    """

    op: str = "ADD"
    # --- 共用字段 ---
    content: str | None = None  # 最终存储内容
    layer: str | None = None  # "L2_FACT" / "L4_IDENTITY"
    owner: str | None = None  # 'user' / 'agent'（仅 L2_FACT 有意义）
    tags: list[str] = field(default_factory=list)  # 1-3 topic keywords
    speculate: str | None = None  # 隐性偏好推导（仅 identity）
    # --- SUPERSEDE / UPDATE 字段 ---
    memory_id: str | None = None  # 目标旧节点 ID（单节点；多节点链时为链上最新的那个）
    memory_ids: list[str] = field(default_factory=list)  # SUPERSEDE 多节点链：有序（旧→新）的现有节点 ID 列表；用于把多条同维度旧记忆折叠进同一条演化链
    supersede_reason: str = ""  # SUPERSEDE 的矛盾描述
    # --- 通用 ---
    reason: str = ""


@dataclass
class ReconcileResult:
    """Reconcile 结果"""

    ops: list[ReconcileOp] = field(default_factory=list)
    success: bool = True
    error: str | None = None
    # reconcile 链路 LLM token 消耗（search-query + reconcile 两次 LLM 调用之和）
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


# ================================================================
# Prompts
# ================================================================

SEARCH_QUERY_PROMPT = """You are a search query generator. Given a list of newly extracted memories, generate a set of short search queries that can be used to find related existing memories in a vector database.

The goal is to maximize recall — find existing memories that are semantically related to the new memories, even if the wording is very different.

## New memories:
{new_memories}

## Instructions

Generate search queries that cover:
- Key topics, entities, and themes mentioned in the memories
- Rephrased or abstracted versions of the core concepts
- Related concepts that might exist in the user's memory store

Output a JSON array of query strings (5-15 queries, short and focused):

["query1", "query2", "query3", ...]

Output JSON array only, no other text."""

RECONCILE_PROMPT = """You are a memory management system.

Your task is to merge new memories into an existing memory base while keeping it:

* lossless
* non-contradictory
* compact
* highly retrievable

---

# Operations

## ADD

Use when the information is entirely new.

```json
{{
  "op": "ADD",
  "content": "...",
  "owner": "user",
  "tags": ["..."]
}}
```

---

## UPDATE

Use when:

* the new memory is compatible with an existing one
* they describe the same topic
* merging improves completeness or retrievability

Rules:

* merge losslessly
* preserve all useful information
* do NOT create duplicates

```json
{{
  "op": "UPDATE",
  "memory_id": "...",
  "content": "...",
  "owner": "user",
  "tags": ["..."]
}}
```

---

## SUPERSEDE

Use ONLY when:

* the new memory contradicts an existing memory
* both cannot be true at the same time
* the old memory would now give the wrong current-state answer

Examples:

* city changed
* employer changed
* relationship status changed

Do NOT use for:

* added detail
* compatible preferences
* wording differences
* semantic equivalence

Rules:

* content must contain ONLY the new state
* do NOT copy unchanged information
* the system keeps the old node and links it onto the evolution chain automatically; you don't need to handle history

```json
{{
  "op": "SUPERSEDE",
  "memory_id": "...",
  "content": "...",
  "supersede_reason": "...",
  "owner": "user",
  "tags": ["..."]
}}
```

### Folding several existing memories into one chain

If TWO OR MORE existing memories track the SAME dimension and the new memory is
the latest change on that dimension, you may fold them into a single evolution
chain. Use `memory_ids` (an ordered list, OLDEST → NEWEST) instead of a single
`memory_id`:

```json
{{
  "op": "SUPERSEDE",
  "memory_ids": ["<oldest_id>", "<newer_id>"],
  "content": "<only the newest state>",
  "supersede_reason": "...",
  "owner": "user",
  "tags": ["..."]
}}
```

The listed memories are chained oldest → newest and the new memory becomes the
chain head. `content` still describes ONLY the newest state — never copy the
older memories' text into it; they remain queryable in the chain history.

---

# Rules

* Preserve all meaningful information.
* Skip exact duplicates.
* Prefer merging related fragments.
* Keep each memory focused on one coherent topic.
* Do not invent chronology.
* `memory_at: null` means unknown time.
* `owner` (`user`/`agent`): copy it from the memory being written. NEVER merge or supersede across different owners — a user's fact and the assistant's fact are distinct even on the same topic.
* A SUPERSEDE either targets ONE memory (`memory_id`) or folds several memories on the same dimension into one chain (`memory_ids`, oldest→newest). An UPDATE targets exactly ONE memory.
* Never touch the same existing memory from more than one op.
* Output language matches input memory language.

---
{few_shot_section}
###################
Input Data
###################

Current date: {current_date}

## Existing memories

{existing_memories}

## New memories

{new_memories}

---

# Output

Return ONLY a JSON array inside a fenced ```json code block.

If no changes are needed:

```json
[]
```

Now produce the JSON array."""


# Reconcile few-shot examples (injected into {few_shot_section} only when
# few_shot_enabled). Each shows the existing/new input and the expected ops,
# with a one-line takeaway. IDs are shortened for readability.
RECONCILE_FEW_SHOT_EN = """
###################
Examples
###################

## Example 1 — One new memory: part conflicts with an existing chain, part is unrelated

Existing memories:
[
  {"memory_id": "m1", "content": "The user works as a product manager at Stripe.", "owner": "user", "history_versions": [{"content": "The user works as a product manager at a fintech startup."}]}
]
New memories:
[
  {"content": "The user has left Stripe and is now a product lead at Notion, and has recently taken up pottery on weekends.", "owner": "user"}
]

Output:
```json
[
  {"op": "SUPERSEDE", "memory_id": "m1", "content": "The user works as a product lead at Notion.", "supersede_reason": "employer changed from Stripe to Notion", "owner": "user", "tags": ["work"]},
  {"op": "ADD", "content": "The user has recently taken up pottery on weekends.", "owner": "user", "tags": ["hobby"]}
]
```
A SINGLE new memory carries two things: the job change (same dimension as the existing employment chain m1, which already has history → SUPERSEDE, becomes the new chain head) and an unrelated detail (pottery → ADD). Split the one memory into the right op per part — the new head describes ONLY the employment state, never the pottery.

## Example 2 — Fold two unlinked existing memories + the new change into one 3-node chain

Existing memories:
[
  {"memory_id": "a1", "content": "The user is learning to play the guitar.", "owner": "user", "memory_at": "2024-02-10 09:00"},
  {"memory_id": "a2", "content": "The user can now play a few full songs on the guitar.", "owner": "user", "memory_at": "2024-09-05 09:00"}
]
New memories:
[
  {"content": "The user now performs guitar at local open-mic nights.", "owner": "user"}
]

Output:
```json
[
  {"op": "SUPERSEDE", "memory_ids": ["a1", "a2"], "content": "The user performs guitar at local open-mic nights.", "supersede_reason": "guitar skill progressed: learning → playing songs → performing live", "owner": "user", "tags": ["music", "hobby"]}
]
```
a1 and a2 were never linked, but they track the same dimension (guitar progress) and the new memory is the latest step. Fold them with `memory_ids` ordered oldest→newest; they become a 3-node chain (a1 ← a2 ← new head). The head `content` describes ONLY the current state — it does not restate "learning" or "a few songs".

## Example 3 — New head must NOT copy the old memory's content

Existing memories:
[
  {"memory_id": "c1", "content": "The user lives in a rented one-bedroom apartment in Shibuya, Tokyo, pays ¥180,000/month, loves the 5-minute walk to the station, but complains the place is noisy at night and has no parking.", "owner": "user"}
]
New memories:
[
  {"content": "The user just bought and moved into a three-bedroom house in the suburbs of Yokohama with a garden and a two-car garage.", "owner": "user"}
]

Output:
```json
[
  {"op": "SUPERSEDE", "memory_id": "c1", "content": "The user bought and moved into a three-bedroom house in suburban Yokohama, with a garden and a two-car garage.", "supersede_reason": "residence changed from a rented Shibuya apartment to an owned Yokohama house", "owner": "user", "tags": ["housing"]}
]
```
The new memory SUPERSEDEs c1 and becomes the chain head. The head `content` describes ONLY the new Yokohama house. Do NOT drag the old apartment's distinctive details (Shibuya, ¥180,000 rent, the station walk, the noise/parking complaints) into the new node — those clearly belong to the old residence, not the new one. They stay in chain history and remain retrievable; copying them would make the head self-contradictory (renting in Shibuya AND owning in Yokohama at once).
"""


# ================================================================
# MemoryReconciler
# ================================================================


class MemoryReconciler:
    """
    记忆协调器

    在 lite pipeline 中，提取完新 facts/identity 后，
    与向量库现有记忆做 reconcile，再决定入库操作。
    """

    # 每条新 memory hybrid 融合后保留的候选数
    SEARCH_TOPK = 20
    # hybrid 向量扩池倍数（召回 limit × 3 再在池内 BM25 融合）
    VEC_POOL_MULTIPLIER = 3
    # 向量 + BM25 融合分阈值（与 hybrid_v2 一致）
    HYBRID_MIN_SCORE = 0.4
    # search query 扩写路向量 topk（仅 MEMORY_ENABLE_SEARCH_QUERY=true）
    QUERY_SEARCH_TOPK = 20
    SEARCH_THRESHOLD = 0.3
    # 链条合并后最终保留的候选数（每路）
    FINAL_TOPK = 10
    # max_tokens for reconcile LLM call（基础值 + 动态扩展）
    # 每条 ADD 输出大约 200-300 tokens（content + supersedes + reason）
    # 推理模型（MiniMax-M3 等）的 reasoning tokens 也计入 max_tokens 预算，
    # 基础值必须留足推理开销，否则 reasoning 耗尽预算后 content 为空。
    # 总上限 16000 tokens，防止单次调用失控
    MAX_TOKENS = 4096
    PER_OP_TOKENS = 400
    BASE_OVERHEAD_TOKENS = 2048  # reasoning model thinking budget
    MAX_TOKENS_UPPER_BOUND = 16000

    def __init__(self, config: MemoryConfig):
        self.config = config
        self._llm: LLMProvider | None = None
        # 消融开关：关闭时只用新 memory 直接搜索，不生成 search queries
        self._enable_search_query = self._read_enable_search_query()

    @staticmethod
    def _read_enable_search_query() -> bool:
        v = os.getenv("MEMORY_ENABLE_SEARCH_QUERY", "false").lower().strip()
        return v not in ("false", "0", "no", "off")

    def _get_llm(self) -> LLMProvider:
        if self._llm is None:
            self._llm = LLMProvider(self.config)
        return self._llm

    async def _generate_search_queries(self, new_memories: list[str]) -> tuple:
        """
        Pre-process: 从全部新 memory 中提取 search queries，
        用于补充向量搜索的召回能力。

        Returns:
            (queries: List[str], llm_response: Optional[LLMResponse])
        """
        mem_lines = "\n".join(f"{i+1}. {m}" for i, m in enumerate(new_memories))

        # Select prompt based on input language
        from ..utils.lang_detect import is_chinese

        if is_chinese("\n".join(new_memories)):
            from .prompts_zh import SEARCH_QUERY_PROMPT_ZH

            prompt = SEARCH_QUERY_PROMPT_ZH.format(new_memories=mem_lines)
        else:
            prompt = SEARCH_QUERY_PROMPT.format(new_memories=mem_lines)

        try:
            llm = self._get_llm()
            response = await llm.complete(
                prompt=prompt,
                max_tokens=500,
                temperature=self.config.llm.temperature,
            )
            text = response.content.strip()
            if "```json" in text:
                text = text.split("```json")[1].split("```")[0].strip()
            elif "```" in text:
                text = text.split("```")[1].split("```")[0].strip()

            queries = json.loads(text)
            if isinstance(queries, list):
                queries = [q for q in queries if isinstance(q, str) and q.strip()]
                logger.debug(
                    f"[reconciler] generated {len(queries)} search queries: {queries}"
                )
                return queries, response
        except Exception as e:
            logger.warning(f"[reconciler] search query generation failed: {e}")

        return [], None

    async def reconcile(
        self,
        new_memories: list[str],
        user_id: str,
        agent_id: str,
        vector_store,  # VectorStoreBase
        embed_service,  # EmbedService
        layers=None,  # Optional[List[MemoryLayer]]
        cache=None,  # Optional[CacheBase], 用于写 pipeline_logs
        request_id: str = "",
        current_time: str = "",
        new_memories_with_meta: list[dict] | None = None,  # 含 tags 的完整 meta
    ) -> ReconcileResult:
        """
        对 new_memories 做 reconcile。

        Args:
            new_memories: 新记忆内容列表（content only，用于 embed 搜索）
            new_memories_with_meta: 可选，含 tags 的完整 meta
        """
        if not new_memories:
            return ReconcileResult(ops=[], success=True)

        import asyncio

        from ..models.memory import MemoryLayer, MemoryNode, MemoryStatus

        if layers is None:
            layers = [MemoryLayer.L2_FACT, MemoryLayer.L4_IDENTITY]

        try:
            import time as _time

            # ── Step 1: Search Query 生成（可消融）──
            search_queries: list[str] = []
            sq_response = None
            t_sq = _time.perf_counter()

            if self._enable_search_query:
                search_queries, sq_response = await self._generate_search_queries(
                    new_memories
                )
            sq_elapsed = (_time.perf_counter() - t_sq) * 1000

            # token 累计：search-query 这次 LLM 调用的消耗（reconcile 调用稍后追加）
            _tok_pt = sq_response.prompt_tokens if sq_response else 0
            _tok_ct = sq_response.completion_tokens if sq_response else 0
            _tok_total = sq_response.tokens_used if sq_response else 0

            # 写 SEARCH_QUERY pipeline log
            if cache and request_id:
                mem_lines = "\n".join(f"{i+1}. {m}" for i, m in enumerate(new_memories))
                if self._enable_search_query:
                    from ..utils.lang_detect import is_chinese as _is_zh

                    if _is_zh("\n".join(new_memories)):
                        from .prompts_zh import SEARCH_QUERY_PROMPT_ZH

                        sq_prompt = SEARCH_QUERY_PROMPT_ZH.format(
                            new_memories=mem_lines
                        )
                    else:
                        sq_prompt = SEARCH_QUERY_PROMPT.format(new_memories=mem_lines)
                else:
                    sq_prompt = "[search query disabled — hybrid retrieval per new memory]"
                try:
                    await cache.store_pipeline_log(
                        request_id=request_id,
                        user_id=user_id,
                        agent_id=agent_id,
                        step="SEARCH_QUERY",
                        prompt=sq_prompt,
                        response=json.dumps(search_queries, ensure_ascii=False),
                        parsed=json.dumps(search_queries, ensure_ascii=False),
                        elapsed_ms=sq_elapsed,
                        prompt_tokens=sq_response.prompt_tokens if sq_response else 0,
                        completion_tokens=(
                            sq_response.completion_tokens if sq_response else 0
                        ),
                        total_tokens=sq_response.tokens_used if sq_response else 0,
                    )
                except Exception as e:
                    logger.warning(f"[reconciler] store SEARCH_QUERY log failed: {e}")

            # ── Step 2: 候选搜索，合并候选池 ──
            candidate_map: dict[str, MemoryNode] = {}
            candidate_scores: dict[str, float] = {}

            all_search_texts = list(new_memories) + list(search_queries)
            all_embeddings = (
                await embed_service.embed_batch(all_search_texts)
                if all_search_texts
                else []
            )

            def _merge_hits(hits: list[dict[str, Any]], source: str) -> None:
                logger.debug(f"[reconciler] search {source}: {len(hits)} results")
                for r in hits:
                    node = r.get("node")
                    score = float(r.get("score", 0))
                    if not node:
                        continue
                    nid = node.node_id
                    if nid not in candidate_map:
                        candidate_map[nid] = node
                        candidate_scores[nid] = score
                    else:
                        candidate_scores[nid] = max(candidate_scores[nid], score)

            from ..pipelines._retrieval.reconcile_retrieval import (
                ReconcileHybridRetriever,
                ReconcileRetrievalConfig,
            )

            hybrid = ReconcileHybridRetriever(
                ReconcileRetrievalConfig(
                    limit=self.SEARCH_TOPK,
                    vec_pool_multiplier=self.VEC_POOL_MULTIPLIER,
                    min_score=self.HYBRID_MIN_SCORE,
                )
            )

            mem_embeddings = all_embeddings[: len(new_memories)]
            if not mem_embeddings and new_memories:
                mem_embeddings = await embed_service.embed_batch(new_memories)

            hybrid_tasks = [
                hybrid.search_candidates(
                    new_memories[i],
                    mem_embeddings[i],
                    vector_store=vector_store,
                    user_id=user_id,
                    agent_id=agent_id,
                    layers=layers,
                )
                for i in range(len(new_memories))
                if i < len(mem_embeddings)
            ]

            async def _search_by_embedding(embedding: list[float], topk: int):
                return await vector_store.search(
                    query_embedding=embedding,
                    user_id=user_id,
                    agent_ids=[agent_id] if agent_id else None,
                    layers=layers,
                    limit=topk,
                    score_threshold=self.SEARCH_THRESHOLD,
                    # 纳入 SUPERSEDED：旧节点也可命中，命中后补全整链。
                    status_filter=[MemoryStatus.ACTIVE, MemoryStatus.SUPERSEDED],
                    only_latest=False,
                )

            query_tasks = []
            if self._enable_search_query and search_queries:
                query_tasks = [
                    _search_by_embedding(
                        all_embeddings[len(new_memories) + i],
                        self.QUERY_SEARCH_TOPK,
                    )
                    for i in range(len(search_queries))
                    if (len(new_memories) + i) < len(all_embeddings)
                ]

            all_results = await asyncio.gather(
                *hybrid_tasks, *query_tasks, return_exceptions=True,
            )
            n_hybrid = len(hybrid_tasks)
            for i, result in enumerate(all_results):
                if isinstance(result, Exception):
                    logger.warning(f"[reconciler] search task [{i}] failed: {result}")
                    continue
                if i < n_hybrid:
                    _merge_hits(result, f"memory_hybrid[{i}]")
                else:
                    _merge_hits(
                        [
                            {"node": r.get("node"), "score": r.get("score", 0)}
                            for r in result
                        ],
                        f"query[{i - n_hybrid}]",
                    )

            logger.debug(
                f"[reconciler] search: {len(new_memories)} memories (hybrid) + "
                f"{len(search_queries)} queries → {len(candidate_map)} raw candidates"
            )

            # ── Step 2.5: 按演化链合并（复用 reader 侧 _trace_full_chain）──
            # 召回池可能含 SUPERSEDED 旧节点（链身）。对每个候选节点双向追溯
            # （supersedes / superseded_by）补全整条链，以链头（is_latest）为代表
            # 去重：同一条链的多个命中（链头+链身）合并为一个候选，链头作代表，
            # 链头分数取该链所有命中的最高分。
            # 注：祖先节点仅用于去重/避免重复候选，**不进 prompt**（见 Step 4，
            # supersede 时只暴露 head 的 id+content，history 不提供，防止 LLM 误操作）。
            from ..pipelines._retrieval.evolution import _trace_full_chain

            chain_head_node: dict[str, MemoryNode] = {}   # head_id → head node
            chain_best_score: dict[str, float] = {}       # head_id → 链上最高命中分
            node_to_head: dict[str, str] = {}             # 任一命中 node_id → 其链头 id
            chain_members: dict[str, list[str]] = {}      # head_id → 整条链 node_id（head+bodies）

            for nid, node in candidate_map.items():
                if nid in node_to_head:
                    continue  # 已被某条已展开的链覆盖
                try:
                    chain = await _trace_full_chain(vector_store, node)
                except Exception as e:
                    logger.warning(f"[reconciler] trace chain for {nid} failed: {e}")
                    chain = [node]
                head = chain[0]  # _trace_full_chain 保证 [0] 为链头（is_latest 优先）
                head_id = head.node_id
                chain_head_node.setdefault(head_id, head)
                chain_members[head_id] = [cn.node_id for cn in chain]
                # 该链上所有“恰好被召回”的节点，分数并入链头候选分
                for cn in chain:
                    node_to_head[cn.node_id] = head_id
                    if cn.node_id in candidate_scores:
                        chain_best_score[head_id] = max(
                            chain_best_score.get(head_id, 0.0),
                            candidate_scores[cn.node_id],
                        )

            # 按链头候选分排序，取 top（留足余量）
            sorted_chains = sorted(
                chain_head_node.keys(),
                key=lambda hid: chain_best_score.get(hid, 0.0),
                reverse=True,
            )[: self.FINAL_TOPK * 2]

            # 最终候选：以链头为代表（prompt 只展示 head）
            final_candidates: dict[str, MemoryNode] = {}
            for head_id in sorted_chains:
                final_candidates[head_id] = chain_head_node[head_id]
                candidate_scores[head_id] = chain_best_score.get(head_id, 0.0)

            candidate_map = final_candidates
            logger.debug(
                f"[reconciler] after chain merge: {len(candidate_map)} chain groups "
                f"(traced from {len(node_to_head)} recalled chain nodes)"
            )

            # ── Step 2.6: 候选去重（pre-search）──
            # 候选都是链头（is_latest）。只对 L2_FACT + L4_IDENTITY（重复重灾区）
            # 判重，其他层不参与（与 search 链路一致）。命中即删库（被删链头连带
            # 删其历史链），并从 candidate_map 移除，避免 LLM 看到重复候选。
            try:
                from ..pipelines._retrieval.dedup import DedupItem, execute_dedup

                _DEDUP_LAYERS = {MemoryLayer.L2_FACT, MemoryLayer.L4_IDENTITY}
                head_ids = [
                    hid for hid, node in candidate_map.items()
                    if node.layer in _DEDUP_LAYERS
                ]
                head_embs = await vector_store.get_embeddings(head_ids)
                dedup_items = []
                for hid in head_ids:
                    node = candidate_map[hid]
                    emb = head_embs.get(hid)
                    if not emb:
                        continue
                    dedup_items.append(DedupItem(
                        node_id=hid,
                        embedding=emb,
                        content=node.content or "",
                        is_latest=True,
                        is_chain_head=True,
                        gmt_created=(node.gmt_created.timestamp() if node.gmt_created else None),
                        chain_node_ids=chain_members.get(hid, [hid]),
                    ))
                if len(dedup_items) >= 2:
                    plan = await execute_dedup(
                        dedup_items,
                        vector_store=vector_store,
                        cache=cache,
                        trigger="reconcile",
                        request_id=request_id,
                        user_id=user_id,
                        agent_id=agent_id,
                        delete_from_store=True,
                    )
                    for did in plan.get("delete_ids", []):
                        candidate_map.pop(did, None)
            except Exception as e:
                logger.warning(f"[reconciler] candidate dedup failed: {e}")

            # ── Step 3: 如果没有任何候选，全部 ADD（supersedes=[]）──
            if not candidate_map:
                logger.debug(
                    f"[reconciler] no candidates found, all ADD ({len(new_memories)} items)"
                )
                all_add_ops = []
                if new_memories_with_meta:
                    for meta in new_memories_with_meta:
                        all_add_ops.append(
                            ReconcileOp(
                                op="ADD",
                                content=meta.get("content", ""),
                                layer=meta.get("layer"),
                                tags=list(meta.get("tags") or []),
                                speculate=meta.get("speculate"),
                            )
                        )
                else:
                    all_add_ops = [
                        ReconcileOp(op="ADD", content=m) for m in new_memories
                    ]

                if cache and request_id:
                    try:
                        await cache.store_pipeline_log(
                            request_id=request_id,
                            user_id=user_id,
                            agent_id=agent_id,
                            step="RECONCILE",
                            prompt="[skipped - no candidates found in search]",
                            response=json.dumps(
                                [{"op": "ADD", "content": m} for m in new_memories],
                                ensure_ascii=False,
                            ),
                            parsed=json.dumps(
                                [
                                    {"op": o.op, "content": o.content}
                                    for o in all_add_ops
                                ],
                                ensure_ascii=False,
                            ),
                            elapsed_ms=0,
                        )
                    except Exception as e:
                        logger.warning(
                            f"[reconciler] store skipped RECONCILE log failed: {e}"
                        )
                logger.info(
                    f"[reconciler] no candidates → all ADD: "
                    f"{len(new_memories)} new memories → {len(all_add_ops)} ops"
                )
                _log_ops_detail(all_add_ops)
                return ReconcileResult(
                    ops=all_add_ops,
                    success=True,
                    prompt_tokens=_tok_pt,
                    completion_tokens=_tok_ct,
                    total_tokens=_tok_total,
                )

            # ── Step 4: 构造 prompt ──

            # 收集该 user 下所有已有 tags（去重，供 LLM 复用，减少发散）
            _existing_tags_set: set = set()
            try:
                for node in candidate_map.values():
                    if getattr(node, "tags", None):
                        _existing_tags_set.update(node.tags)
            except Exception:
                pass
            existing_tags_line = (
                ", ".join(sorted(_existing_tags_set)) if _existing_tags_set else ""
            )

            # Helper: format memory_at as date string to minute precision
            def _format_memory_at(val) -> str | None:
                if val is None:
                    return None
                if isinstance(val, datetime):
                    return val.strftime("%Y-%m-%d %H:%M")
                if isinstance(val, str) and val:
                    try:
                        dt = datetime.fromisoformat(val)
                        return dt.strftime("%Y-%m-%d %H:%M")
                    except (ValueError, TypeError):
                        return val
                return None

            # new_memories 展示（JSON list 格式，与 existing_memories 对齐）
            if new_memories_with_meta:
                new_mem_list = []
                for meta in new_memories_with_meta:
                    item: dict[str, Any] = {
                        "content": meta.get("content", ""),
                        "owner": meta.get("owner") or "user",
                        "memory_at": _format_memory_at(current_time) if current_time else None,
                        "tags": list(meta.get("tags") or []),
                    }
                    new_mem_list.append(item)
                new_mem_lines = json.dumps(new_mem_list, ensure_ascii=False, indent=2)
            else:
                new_mem_list = [{"content": m, "owner": "user", "memory_at": _format_memory_at(current_time) if current_time else None, "tags": []} for m in new_memories]
                new_mem_lines = json.dumps(new_mem_list, ensure_ascii=False, indent=2)

            def _sort_key(n):
                t = n.memory_at or n.gmt_created
                return t.isoformat() if t else ""

            sorted_candidates = sorted(candidate_map.values(), key=_sort_key)

            existing_mem_list = []
            for node in sorted_candidates:
                item: dict[str, Any] = {
                    "memory_id": node.node_id,
                    "content": node.content,
                    "owner": getattr(node, "owner", None) or "user",
                    "memory_at": _format_memory_at(node.memory_at),
                }
                if getattr(node, "tags", None):
                    item["tags"] = list(node.tags)
                # 每条演化链只暴露链头（head）的 memory_id + content，**不提供
                # history_versions**：reconcile 只应针对当前有效版本（head）做
                # ADD/UPDATE/SUPERSEDE，暴露历史旧节点会诱导 LLM 误操作（如去
                # supersede 一个已经被取代的旧节点）。链的完整展开只发生在 reader 侧。
                existing_mem_list.append(item)
            existing_lines = json.dumps(existing_mem_list, ensure_ascii=False, indent=2)

            # 构建 current_date（精确到分钟）
            _current_date_str = datetime.now().strftime("%Y-%m-%d %H:%M")

            # Select prompt based on input language
            from ..utils.lang_detect import is_chinese

            _few_shot_on = getattr(self.config.llm, "few_shot_enabled", False)

            if is_chinese("\n".join(new_memories)):
                from .prompts_zh import RECONCILE_FEW_SHOT_ZH, RECONCILE_PROMPT_ZH

                _fs = ("\n" + RECONCILE_FEW_SHOT_ZH + "\n") if _few_shot_on else ""
                prompt = RECONCILE_PROMPT_ZH.format(
                    current_date=_current_date_str,
                    new_memories=new_mem_lines,
                    existing_memories=existing_lines,
                    few_shot_section=_fs,
                )
            else:
                _fs = ("\n" + RECONCILE_FEW_SHOT_EN + "\n") if _few_shot_on else ""
                prompt = RECONCILE_PROMPT.format(
                    current_date=_current_date_str,
                    new_memories=new_mem_lines,
                    existing_memories=existing_lines,
                    few_shot_section=_fs,
                )

            # ── Step 5: LLM 调用 + 重试 ──
            logger.debug(f"[reconciler] full prompt: {prompt!r}")
            # 动态估算 max_tokens
            # 取基础值与动态值的较大者，并夹在上限以内。
            dyn_max_tokens = min(
                self.MAX_TOKENS_UPPER_BOUND,
                max(
                    self.MAX_TOKENS,
                    len(new_memories) * self.PER_OP_TOKENS + self.BASE_OVERHEAD_TOKENS,
                ),
            )
            t_recon = _time.perf_counter()
            llm = self._get_llm()
            response = None
            raw_response = ""
            ops = []
            _parse_ok = False
            _base_temp = self.config.llm.temperature
            # 逐次升温 + 逐次加宽 token 预算（推理模型可能在低温下过度思考）
            _temperatures = [_base_temp, max(_base_temp, 0.3), 0.7]
            _token_scales = [1.0, 1.5, 2.0]

            for _attempt, (_temp, _tscale) in enumerate(zip(_temperatures, _token_scales)):
                _attempt_tokens = min(self.MAX_TOKENS_UPPER_BOUND, int(dyn_max_tokens * _tscale))
                response = await llm.complete(
                    prompt=prompt,
                    max_tokens=_attempt_tokens,
                    temperature=_temp,
                )
                # 每次 reconcile LLM 调用（含重试）都计入 token 消耗
                _tok_pt += response.prompt_tokens
                _tok_ct += response.completion_tokens
                _tok_total += response.tokens_used
                raw_response = response.content
                ops = self._parse_ops(raw_response)

                _is_empty_array = self._strip_code_fence(raw_response) in ("[]", "")
                if ops or _is_empty_array:
                    _parse_ok = True
                    break
                logger.warning(
                    f"[reconciler] RECONCILE JSON parse failed (attempt {_attempt+1}/3), "
                    f"retrying: raw={raw_response[:200]}"
                )

            recon_elapsed = (_time.perf_counter() - t_recon) * 1000

            # ── Step 6: 解析仍失败 ──
            if not _parse_ok:
                error_msg = (
                    f"RECONCILE JSON parse failed after 3 attempts: {raw_response[:200]}"
                    if raw_response.strip()
                    else "RECONCILE LLM returned empty response after 3 attempts"
                )
                error_code = (
                    "EMPTY_RESPONSE"
                    if not raw_response.strip()
                    else "JSON_PARSE_FAILED"
                )
                logger.warning(f"[reconciler] {error_msg}")

                if cache and request_id:
                    try:
                        await cache.store_pipeline_log(
                            request_id=request_id,
                            user_id=user_id,
                            agent_id=agent_id,
                            step="RECONCILE",
                            prompt=prompt,
                            response=raw_response,
                            parsed=json.dumps(
                                {"error": error_code, "message": error_msg},
                                ensure_ascii=False,
                            ),
                            elapsed_ms=recon_elapsed,
                            prompt_tokens=response.prompt_tokens,
                            completion_tokens=response.completion_tokens,
                            total_tokens=response.tokens_used,
                        )
                    except Exception as e:
                        logger.warning(
                            f"[reconciler] store failed RECONCILE log failed: {e}"
                        )

                return ReconcileResult(
                    ops=[],
                    success=False,
                    error=error_msg,
                    prompt_tokens=_tok_pt,
                    completion_tokens=_tok_ct,
                    total_tokens=_tok_total,
                )

            # 写 RECONCILE pipeline log
            if cache and request_id:
                related_ids = []
                for op in ops:
                    if op.op in ("SUPERSEDE", "UPDATE") and op.memory_id:
                        related_ids.append(op.memory_id)
                try:
                    await cache.store_pipeline_log(
                        request_id=request_id,
                        user_id=user_id,
                        agent_id=agent_id,
                        step="RECONCILE",
                        prompt=prompt,
                        response=response.content,
                        parsed=json.dumps(
                            [
                                {
                                    "op": o.op,
                                    "content": o.content,
                                    "layer": o.layer,
                                    "memory_id": o.memory_id,
                                    "tags": o.tags,
                                    "speculate": o.speculate,
                                    "supersede_reason": o.supersede_reason,
                                    "reason": o.reason,
                                }
                                for o in ops
                            ],
                            ensure_ascii=False,
                        ),
                        memory_ids=related_ids,
                        elapsed_ms=recon_elapsed,
                        prompt_tokens=response.prompt_tokens,
                        completion_tokens=response.completion_tokens,
                        total_tokens=response.tokens_used,
                    )
                except Exception as e:
                    logger.warning(f"[reconciler] store RECONCILE log failed: {e}")

            logger.info(
                f"[reconciler] reconciled {len(new_memories)} new memories, "
                f"{len(candidate_map)} candidates → {len(ops)} ops"
            )
            _log_ops_detail(ops)
            return ReconcileResult(
                ops=ops,
                success=True,
                prompt_tokens=_tok_pt,
                completion_tokens=_tok_ct,
                total_tokens=_tok_total,
            )

        except Exception as e:
            logger.error(f"[reconciler] failed: {e}", exc_info=True)
            return ReconcileResult(ops=[], success=False, error=str(e))

    @staticmethod
    def _strip_code_fence(text: str) -> str:
        """剥掉 ```json ... ``` / ``` ... ``` markdown 围栏，返回内层内容。

        同时剥掉推理模型（如 MiniMax-M3）可能混入 content 的 think 块，
        避免思考过程污染 JSON 解析。
        """
        text = (text or "").strip()
        # 先剥掉 think 推理块（reasoning models 可能把它塞进 content）
        text = re.sub(r"\<think\>.*?\</think\>", "", text, flags=re.DOTALL).strip()
        if "```json" in text:
            return text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            return text.split("```")[1].split("```")[0].strip()
        return text

    @staticmethod
    def _loads(text: str):
        """json.loads with a repair fallback for common LLM output artifacts.

        LLMs frequently emit trailing commas (``[{"op": "ADD",},]``) or a
        stray comma before a closing bracket. Strict ``json.loads`` rejects
        these and the reconcile op is silently dropped (data loss). We try
        strict parse first, then strip trailing commas and retry. Returns the
        parsed object or raises ``json.JSONDecodeError`` if unrecoverable.
        """
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            # Drop commas that sit directly before a closing ] or } (with
            # optional whitespace/newlines between). This is the single most
            # common LLM JSON defect and is safe: a trailing comma is never
            # valid JSON, so removing it cannot corrupt well-formed input.
            repaired = re.sub(r",\s*([}\]])", r"\1", text)
            return json.loads(repaired)

    def _parse_ops(self, text: str) -> list[ReconcileOp]:
        """
        解析 LLM 返回的 RECONCILE 输出。

        支持格式：
        - Flat list（推荐）：[{op, content, ...}, {op, content, ...}]
        - Group 格式（向后兼容）：[{"reason": ..., "ops": [...]}]

        支持的 op 类型：ADD, SUPERSEDE, UPDATE
        """
        text = self._strip_code_fence(text)

        try:
            data = self._loads(text)
        except json.JSONDecodeError:
            match = re.search(r"\[.*\]", text, re.DOTALL)
            if match:
                try:
                    data = self._loads(match.group())
                except json.JSONDecodeError:
                    logger.warning(
                        f"[reconciler] failed to parse ops JSON: {text[:200]}"
                    )
                    return []
            else:
                logger.warning(f"[reconciler] no JSON array found: {text[:200]}")
                return []

        if not isinstance(data, list):
            data = [data]

        # ── 展平：把 (op_dict, group_reason) 二元组放到 flat 列表里 ──
        flat_items: list[tuple] = []  # (op_dict, group_reason)
        for entry in data:
            if not isinstance(entry, dict):
                continue
            # 分支 A：group 结构（向后兼容）— 带 reason + ops 子列表
            if "ops" in entry and isinstance(entry.get("ops"), list):
                group_reason = str(entry.get("reason") or "").strip()
                for op_dict in entry["ops"]:
                    if isinstance(op_dict, dict):
                        flat_items.append((op_dict, group_reason))
            # 分支 B：扁平 op 结构（推荐）
            elif "op" in entry:
                flat_items.append((entry, ""))
            else:
                logger.warning(
                    f"[reconciler] unrecognized entry shape, skipped: {entry}"
                )

        # Pass 1: 结构解析
        parsed: list[ReconcileOp] = []
        for op_dict, group_reason in flat_items:
            op_type = str(op_dict.get("op", "")).upper()
            op_reason = str(op_dict.get("reason") or "").strip() or group_reason

            # 通用字段提取
            raw_tags = op_dict.get("tags", []) or []
            if not isinstance(raw_tags, list):
                raw_tags = [raw_tags]
            tags = [str(t).strip().lower() for t in raw_tags if t and str(t).strip()][
                :3
            ]
            speculate = op_dict.get("speculate") or None
            if speculate and isinstance(speculate, str):
                speculate = speculate.strip() or None

            # owner：仅接受 user/agent（assistant→agent），否则 None
            _raw_owner = op_dict.get("owner")
            owner = None
            if _raw_owner:
                _ov = str(_raw_owner).strip().lower()
                if _ov in ("user", "agent"):
                    owner = _ov
                elif _ov == "assistant":
                    owner = "agent"

            if op_type == "ADD":
                parsed.append(
                    ReconcileOp(
                        op="ADD",
                        content=op_dict.get("content"),
                        layer=op_dict.get("layer"),
                        owner=owner,
                        tags=tags,
                        speculate=speculate,
                        reason=op_reason,
                    )
                )

            elif op_type == "SUPERSEDE":
                # 接受单 id（memory_id）或多 id 链（memory_ids，有序 旧→新）
                raw_ids = op_dict.get("memory_ids")
                id_list: list[str] = []
                if isinstance(raw_ids, list):
                    id_list = [str(x).strip() for x in raw_ids if x and str(x).strip()]
                mid = str(op_dict.get("memory_id") or "").strip()
                if mid and mid not in id_list:
                    # memory_id 视为链上最新的目标，追加到末尾
                    id_list.append(mid)
                if not id_list:
                    logger.warning(
                        f"[reconciler] SUPERSEDE op missing memory_id(s), skipped: {op_dict}"
                    )
                    continue
                # memory_id 始终指向链上最新的现有节点（新 head 直接 supersede 它）
                parsed.append(
                    ReconcileOp(
                        op="SUPERSEDE",
                        memory_id=id_list[-1],
                        memory_ids=id_list,
                        content=op_dict.get("content"),
                        layer=op_dict.get("layer"),
                        owner=owner,
                        tags=tags,
                        speculate=speculate,
                        supersede_reason=str(
                            op_dict.get("supersede_reason") or ""
                        ).strip(),
                        reason=op_reason,
                    )
                )

            elif op_type == "UPDATE":
                mid = str(op_dict.get("memory_id") or "").strip()
                if not mid:
                    logger.warning(
                        f"[reconciler] UPDATE op missing memory_id, skipped: {op_dict}"
                    )
                    continue
                parsed.append(
                    ReconcileOp(
                        op="UPDATE",
                        memory_id=mid,
                        content=op_dict.get("content"),
                        layer=op_dict.get("layer"),
                        owner=owner,
                        tags=tags,
                        speculate=speculate,
                        reason=op_reason,
                    )
                )

            elif op_type == "DELETE":
                # 向后兼容：旧 prompt 可能仍返回 DELETE，映射为 SHADOW 旧节点
                mid = str(op_dict.get("memory_id") or "").strip()
                if not mid:
                    logger.warning(
                        f"[reconciler] DELETE op missing memory_id, skipped: {op_dict}"
                    )
                    continue
                logger.info(
                    f"[reconciler] legacy DELETE op mapped (memory_id={mid}), "
                    f"will shadow the node"
                )
                parsed.append(
                    ReconcileOp(
                        op="UPDATE",
                        memory_id=mid,
                        content=None,  # 标记为无 content 的 shadow-only op
                        reason=op_reason,
                    )
                )

            else:
                logger.warning(
                    f"[reconciler] unknown op '{op_type}', skipped: {op_dict}"
                )
                continue

        # Pass 2: 校验"同一 existing memory 不得被多处 touch"
        touched: dict[str, str] = {}  # memory_id → first-seen op description
        validated: list[ReconcileOp] = []
        for op in parsed:
            # SUPERSEDE 多节点链：登记链上所有 id；UPDATE/单节点 SUPERSEDE 登记 memory_id
            if op.op == "SUPERSEDE" and op.memory_ids:
                target_ids = list(op.memory_ids)
            elif op.op in ("SUPERSEDE", "UPDATE"):
                target_ids = [op.memory_id] if op.memory_id else []
            else:
                target_ids = []
            if target_ids:
                clash = next((t for t in target_ids if t in touched), None)
                if clash is not None:
                    logger.warning(
                        f"[reconciler] memory_id {clash} already touched by '{touched[clash]}', "
                        f"current op '{op.op} ({op.reason[:60]})' dropped"
                    )
                    continue
                for t in target_ids:
                    touched[t] = f"{op.op}({op.reason[:40]})"
            validated.append(op)

        return validated
