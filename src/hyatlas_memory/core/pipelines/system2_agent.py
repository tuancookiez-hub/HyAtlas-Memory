"""
System 2 Agent — 统一认知加工引擎

替代旧的 SchemaInductor + IntentionDetector，改为单一 Agent + Tools 架构。

流程:
  Phase 1 (硬编码预处理):
    ① VDB 取未处理的 L2_FACT + L4_IDENTITY
    ② DBSCAN 聚类 → clusters
    ③ Graph 正向搜索: 按 cluster 主题 query Graph
    ④ Graph 反向查找: VDB node_id → find_referencing_memories

  Phase 2 (Agent LLM，仅当 clusters_found > 0):
    输入: 聚类结果为主；unprocessed_facts = 未进任一 cluster 的 fresh facts（加菜，有上限）
    无聚类时不触发 Agent（减轻冷启动/散事实场景的 LLM 负担）

  工具: 8 个 tools — 演化高级认知 (Schema + Intent + 关系)
"""

import json
import logging
import os
import time
from typing import Any

from ..config import MemoryConfig
from ..core.embed_service import EmbedService
from ..data.graph_store_base import GraphStoreBase
from ..data.vector_store_base import VectorStoreBase
from ..models.memory import MemoryLayer, MemoryNode, MemoryStatus
from .system2_tools import System2ToolExecutor

logger = logging.getLogger(__name__)

_S2_SUMMARY_ENABLED = os.getenv("MEMORY_SUMMARY_ENABLED_IN_SYS2", "false").lower() == "true"
# 有聚类时，未聚类事实作为加菜；超过此数量截断，避免 1 cluster + 30 条 noise
_S2_MAX_UNCLUSTERED_FACTS = int(os.getenv("MEMORY_S2_MAX_UNCLUSTERED_FACTS", "15"))
# 簇内 embedding 余弦相似度 ≥ 此阈值视为重复，只保留一条（防系统重复写入闯关）
_S2_CLUSTER_DEDUP_COSINE = float(os.getenv("MEMORY_S2_CLUSTER_DEDUP_COSINE", "0.92"))
# 量控制：单次 LLM 调用最多处理多少个 cluster，超出则分多批循环（防 prompt 上下文爆炸）
_S2_MAX_CLUSTERS_PER_CALL = int(os.getenv("MEMORY_S2_MAX_CLUSTERS_PER_CALL", "8"))
# 量控制：一个认知周期最多处理多少个 cluster（0 = 不限）；超出留到下个周期重新聚类
_S2_MAX_CLUSTERS_PER_RUN = int(os.getenv("MEMORY_S2_MAX_CLUSTERS_PER_RUN", "0"))


def _cosine_sim_vec(a, b) -> float:
    import numpy as np
    na = float(np.linalg.norm(a))
    nb = float(np.linalg.norm(b))
    if na < 1e-9 or nb < 1e-9:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


def _dedupe_group_indices(
    group_indices: list[int],
    X,
    valid_facts: list,
    sim_threshold: float,
) -> tuple:
    """
    簇内去重：与已保留条目的 embedding 相似度 ≥ threshold 则丢弃。
    Returns:
        (deduped_indices, removed_count)
    """
    if len(group_indices) <= 1:
        return list(group_indices), 0

    kept: list[int] = []
    removed = 0
    for idx in group_indices:
        if not kept:
            kept.append(idx)
            continue
        dup_of = None
        max_sim = 0.0
        for k in kept:
            sim = _cosine_sim_vec(X[idx], X[k])
            if sim > max_sim:
                max_sim = sim
                dup_of = k
        if max_sim >= sim_threshold:
            removed += 1
            logger.info(
                f"[S2-preprocess] cluster dedup: drop {valid_facts[idx].node_id} "
                f"(sim={max_sim:.3f} ≈ {valid_facts[dup_of].node_id})"
            )
        else:
            kept.append(idx)
    return kept, removed


def s2_agent_skip_reason(materials: dict[str, Any]) -> str | None:
    """
    返回跳过 S2 Agent 的原因；None 表示应运行 Agent。

    - no_facts: 聚类池为空（全已被 schema 引用或 VDB 无 fact）
    - no_clusters: DBSCAN 未形成任何 cluster（散事实，不触发 Agent）
    """
    stats = materials.get("stats") or {}
    if stats.get("total_facts_pool", 0) == 0:
        return "no_facts"
    if stats.get("clusters_found", 0) == 0:
        return "no_clusters"
    return None

# ====================================================================
# System Prompt (dual language)
# ====================================================================

SYSTEM2_AGENT_PROMPT_ZH = """你是一个认知加工 Agent，负责从用户的记忆数据中演化高层认知结构。

## 记忆分层架构

用户的记忆存储在两个系统中：

### VDB（向量数据库）— 你只能读取，不能修改
| 层级 | 说明 |
|------|------|
| L0 BASIC_INFO | 用户基础属性（姓名、年龄、职业等）|
| L1 RAW | 原始对话记录 |
| L2 FACT | 原子事实（从对话中提取的客观事实）|
| L3 SUMMARY | 会话摘要 |
| L4 IDENTITY | 身份画像（性格特征、偏好、习惯）|
| L5 KNOWLEDGE | 领域知识 |

### Graph（知识图谱）— 你是唯一的写入者
| 层级 | 说明 |
|------|------|
| L6 SCHEMA | 行为模式 — 域内、原子化、创建后不可变 |

## L6 Schema 是什么？

Schema 捕获用户在**特定领域**内的**一个**行为模式，包含三个要素：
- **Circumstance（场景）**：该模式发生的领域/话题/场景。Schema 必须限定在其场景内，不要跨域泛化。
- **Pattern（模式）**：用户在该场景下的惯常行为、思维方式或行动倾向。
- **Insight（洞察）**：底层心理驱动力或心智模型。

### 内容格式
用一句话组合三要素：
"当[场景]时，用户[模式]——反映了[洞察]。"

### 规则
- **原子化**：一个 Schema 只包含一个模式。两个不同模式 → 两个 Schema。
- **不可变**：Schema 创建后内容永远不变。不要修改已有 Schema 的内容。
- **累积证据**：新事实支持已有 Schema → 调 `add_evidence` 添加证据，不要重新创建。
- **域内约束**：不要跨域。跨域抽象由系统的其他流程处理。

## 工作流程

输入材料分两部分（仅当已形成聚类时才会调用你）：
- **聚类结果（Cluster Results）**：主输入，按语义相似度归组的事实
- **未聚类事实（补充）**：未进入任一聚类的事实，优先级低于聚类结果，酌情审视即可

对于每组事实（无论聚类还是未聚类）：
1. **搜索已有 Schema**：如果已有 Schema 覆盖了该模式，调 `add_evidence` 链接新事实——不要重新创建。
2. **创建新 Schema**：仅当没有已有 Schema 覆盖时。一句话，三要素。
3. **建立关系**：两个 Schema 主题相关 → 用 `add_edge`（RELATED_TO）。

## 原则

- 所有 Graph 操作必须通过 tools 执行
- Schema 内容创建后不可变——绝不重新创建已存在的 Schema
- 优先 `add_evidence` 而非创建重复节点
- 宁可不创建，也不要创建低质量节点
- 一个 Schema = 一个域内原子模式
- 打标签时优先使用已有标签列表中的标签"""


SYSTEM2_AGENT_PROMPT_EN = """You are a cognitive processing Agent. Your job is to evolve higher-order cognitive structures from a user's raw memory data.

## Memory Architecture

### VDB (Vector Database) — read-only
| Layer | Description |
|-------|-------------|
| L0 BASIC_INFO | User attributes (name, age, occupation) |
| L1 RAW | Original conversation transcripts |
| L2 FACT | Atomic facts extracted from conversations |
| L3 SUMMARY | Session summaries |
| L4 IDENTITY | Identity traits, preferences, habits |
| L5 KNOWLEDGE | Domain knowledge |

### Graph (Knowledge Graph) — you are the sole writer
| Layer | Description |
|-------|-------------|
| L6 SCHEMA | Behavioral patterns — domain-bound, atomic, immutable once created |

## What is an L6 Schema?

A Schema captures ONE behavioral pattern in a SPECIFIC domain. Three components:

- **Circumstance**: The domain/topic/situation where this pattern is observed. A Schema MUST stay within its circumstance — do NOT generalize across domains. Cross-domain abstraction is handled separately by the system.
- **Pattern**: The user's habitual behavior, thinking style, or action tendency in this circumstance.
- **Insight**: The underlying psychological driver or mental model.

### Content format
Single sentence combining all three:
"When [circumstance], the user [pattern] — reflecting [insight]."

### Rules
- **Atomic**: One pattern per Schema. Two distinct patterns → two Schemas.
- **Immutable**: Once created, content NEVER changes.
- **Evidence only**: New facts support existing Schema → call `add_evidence`. Do NOT recreate.
- **Domain-bound**: Stay within the observed domain. Cross-domain abstraction is handled by a separate system process.

### Good vs Bad examples
✅ "When cooking, the user strictly follows recipe steps and precisely measures ingredients — reflecting a need for external structure to manage uncertainty."
✅ "When gaming, the user always checks walkthroughs to achieve 100% completion — driven by low tolerance for incompleteness."
❌ "The user approaches music as a medium for creative synthesis and cultural bridge-building, consistently blending diverse musical traditions..." (multiple patterns merged, too broad)
❌ "The user is passionate about many things and values quality." (no circumstance, too vague)

## Your Workflow

Input materials (you are only invoked when at least one cluster exists):
- **Cluster Results**: Primary input — facts grouped by semantic similarity
- **Unclustered Facts (supplemental)**: Facts not in any cluster; lower priority than cluster results

For each group of facts (clustered or unclustered):
1. **Search existing Schemas**: If an existing Schema already captures this pattern, call `add_evidence` — do NOT recreate.
2. **Create new Schema**: Only if no existing Schema covers this pattern. One sentence, three components.
3. **Build relationships**: If two Schemas are thematically related, use `add_edge` (RELATED_TO).

## Principles

- All Graph operations MUST go through tools
- Schema content is IMMUTABLE — never recreate what already exists
- Prefer `add_evidence` over creating duplicates
- Prefer not creating over creating low-quality nodes
- One Schema = one atomic pattern in one domain
- When tagging, prefer reusing tags from the existing tags list"""

# backward compat alias
SYSTEM2_AGENT_PROMPT = SYSTEM2_AGENT_PROMPT_ZH


# ====================================================================
# Preprocessor: materials preparation (hardcoded, no LLM)
# ====================================================================

async def prepare_materials(
    user_id: str,
    agent_id: str,
    vector_store: VectorStoreBase,
    graph_store: GraphStoreBase,
    embed_service: EmbedService,
    config: MemoryConfig,
) -> dict[str, Any]:
    """
    Phase 1: 硬编码预处理 — 聚类 + Graph 正反向搜索 → materials

    Returns:
        {
            "clusters": [{ids, centroid_text, facts: [{node_id, content, layer}]}],
            "graph_forward": [{node_id, content, layer, confidence, evidence_count}],
            "graph_reverse": [{node_id, content, layer, confidence, evidence_vdb_id}],
            "unprocessed_facts": [{node_id, content, layer}],
            "stats": {total_facts, clusters_found, ...}
        }
    """
    # Clustering config（min_samples=4）
    _MIN_FACTS_FOR_INDUCTION = int(os.getenv("MEMORY_S2_MIN_CLUSTER_SIZE", "4"))
    _CLUSTERING_THRESHOLD = float(os.getenv("MEMORY_S2_CLUSTER_THRESHOLD", "0.55"))
    # Stage 2：大簇（>12）内再切；阈值比 Stage1 略严即可，0.75 过严难拆出合法子簇
    _REFINE_THRESHOLD = float(os.getenv("MEMORY_S2_REFINE_CLUSTER_THRESHOLD", "0.65"))
    _MAX_CLUSTER_SIZE = 12             # 超过此 size 的 cluster 触发二阶段细分
    _REFINE_MIN_SAMPLES = _MIN_FACTS_FOR_INDUCTION

    # Graph 正向搜索 config
    _GRAPH_SEARCH_TOPK_PER_CLUSTER = 8  # 每个 cluster centroid 召回的 Graph 节点数

    isolation_key = MemoryNode.build_isolation_key(user_id, agent_id or "default")

    # ① VDB: 取 L2_FACT + L4_IDENTITY（可选 L3_SUMMARY）
    _s2_layers = [MemoryLayer.L2_FACT, MemoryLayer.L4_IDENTITY]
    if _S2_SUMMARY_ENABLED:
        _s2_layers.append(MemoryLayer.L3_SUMMARY)
    all_facts = await vector_store.list_by_user(
        user_id=user_id,
        agent_id=agent_id,
        status_filter=[MemoryStatus.ACTIVE],
        layers=_s2_layers,
    )

    if not all_facts:
        return {
            "clusters": [],
            "graph_forward": [],
            "graph_reverse": [],
            "unprocessed_facts": [],
            "stats": {"total_facts": 0, "clusters_found": 0},
        }

    # ①b 按 s2_evidence_count 过滤：只要被任何 Schema 引用过（>=1），就不再参与聚类
    fresh_facts = []       # evidence_count = 0 → 全新，参与聚类
    evidenced_facts = []   # evidence_count >= 1 → 已被引用，排除
    for fact in all_facts:
        ec = 0
        if hasattr(fact, "custom") and isinstance(fact.custom, dict):
            ec = fact.custom.get("s2_evidence_count", 0)
        if ec >= 1:
            evidenced_facts.append(fact)
        else:
            fresh_facts.append(fact)

    # 聚类池 = 仅全新 facts
    clustering_pool = fresh_facts

    logger.info(
        f"[S2-preprocess] Facts triage: fresh={len(fresh_facts)} "
        f"evidenced_excluded={len(evidenced_facts)} "
        f"(pool={len(clustering_pool)})"
    )

    # ② 两阶段聚类（只对 clustering_pool 做聚类，排除已充分消化的 facts）
    import numpy as np
    clusters = []
    try:
        from sklearn.cluster import DBSCAN
        from sklearn.metrics.pairwise import cosine_distances

        # Collect embeddings from clustering_pool (not all_facts)
        embeddings = []
        valid_facts = []
        for fact in clustering_pool:
            emb = fact.embedding
            if isinstance(emb, dict):
                emb = list(emb.values())[0] if emb else None
            if emb and isinstance(emb, (list, np.ndarray)) and len(emb) > 0:
                embeddings.append(emb)
                valid_facts.append(fact)
            else:
                try:
                    emb = await embed_service.embed_queued(fact.content)
                    embeddings.append(emb)
                    valid_facts.append(fact)
                except Exception:
                    pass

        if len(embeddings) >= _MIN_FACTS_FOR_INDUCTION:
            X = np.array(embeddings)

            # --- Stage 1: 粗分 ---
            dist_matrix = cosine_distances(X)
            db = DBSCAN(eps=1.0 - _CLUSTERING_THRESHOLD, min_samples=_MIN_FACTS_FOR_INDUCTION, metric="precomputed")
            labels = db.fit_predict(dist_matrix)

            stage1_clusters = {}  # label → [indices]
            for i, lbl in enumerate(labels):
                if lbl == -1:
                    continue
                stage1_clusters.setdefault(lbl, []).append(i)

            # --- Stage 2: 对大 cluster 细分 ---
            final_index_groups = []
            for lbl, indices in stage1_clusters.items():
                if len(indices) <= _MAX_CLUSTER_SIZE:
                    final_index_groups.append(indices)
                else:
                    # 子矩阵上用更高 threshold 再跑一次 DBSCAN
                    sub_X = X[indices]
                    sub_dist = cosine_distances(sub_X)
                    sub_db = DBSCAN(
                        eps=1.0 - _REFINE_THRESHOLD,
                        min_samples=_REFINE_MIN_SAMPLES,
                        metric="precomputed",
                    )
                    sub_labels = sub_db.fit_predict(sub_dist)

                    sub_groups = {}
                    noise_indices = []
                    for si, sl in enumerate(sub_labels):
                        if sl == -1:
                            noise_indices.append(indices[si])
                        else:
                            sub_groups.setdefault(sl, []).append(indices[si])

                    for sg in sub_groups.values():
                        final_index_groups.append(sg)

                    # noise 归入最近的子 cluster（或保留为独立 cluster）
                    if noise_indices and sub_groups:
                        # 计算每个 noise 到各子 cluster centroid 的距离，归入最近的
                        sub_centroids = {
                            sl: np.mean(X[[indices[si] for si, sll in enumerate(sub_labels) if sll == sl]], axis=0)
                            for sl in sub_groups
                        }
                        for ni in noise_indices:
                            best_sl = min(sub_centroids, key=lambda sl: float(cosine_distances(
                                X[ni:ni+1], sub_centroids[sl].reshape(1, -1)
                            )[0, 0]))
                            # 找到对应的 final group 并 append
                            for fg in final_index_groups:
                                if any(indices[si] in fg for si, sl in enumerate(sub_labels) if sl == best_sl):
                                    fg.append(ni)
                                    break
                    elif noise_indices:
                        final_index_groups.append(noise_indices)

                    logger.info(
                        f"[S2-preprocess] Refined cluster (size={len(indices)}) "
                        f"→ {len(sub_groups)} sub-clusters + {len(noise_indices)} noise"
                    )

            # --- 构建 cluster 输出（簇内去重 + 最小 N 条，N=_MIN_FACTS_FOR_INDUCTION）---
            dedup_removed_total = 0
            for group_indices in final_index_groups:
                if len(group_indices) < _REFINE_MIN_SAMPLES:
                    continue
                group_indices, n_rm = _dedupe_group_indices(
                    group_indices, X, valid_facts, _S2_CLUSTER_DEDUP_COSINE,
                )
                dedup_removed_total += n_rm
                if len(group_indices) < _MIN_FACTS_FOR_INDUCTION:
                    logger.info(
                        f"[S2-preprocess] cluster dropped after dedup: "
                        f"size={len(group_indices)} < {_MIN_FACTS_FOR_INDUCTION}"
                    )
                    continue
                cluster_facts = [valid_facts[i] for i in group_indices]
                centroid = np.mean(X[group_indices], axis=0)
                dists_to_centroid = [np.linalg.norm(X[i] - centroid) for i in group_indices]
                rep_idx = group_indices[np.argmin(dists_to_centroid)]

                clusters.append({
                    "ids": [f.node_id for f in cluster_facts],
                    "centroid_text": valid_facts[rep_idx].content[:100],
                    "centroid_embedding": centroid.tolist(),
                    "facts": [
                        {"node_id": f.node_id, "content": f.content, "layer": f.layer.value}
                        for f in cluster_facts
                    ],
                })

            logger.info(
                f"[S2-preprocess] Clustering: {len(valid_facts)} facts → "
                f"{len(stage1_clusters)} stage1 → {len(clusters)} final clusters "
                f"(dedup_removed={dedup_removed_total})"
            )

    except Exception as e:
        logger.warning(f"[S2-preprocess] clustering failed: {e}")

    # ③ Graph 正向搜索: 用 cluster centroid 向量召回相关 Schema
    #    不再全列所有 Graph 节点 — 只召回与当前 clusters 语义相关的
    graph_forward = []
    seen_node_ids = set()
    try:
        if clusters and graph_store:
            for c in clusters:
                centroid_emb = c.get("centroid_embedding")
                if not centroid_emb:
                    continue
                # 搜索 L6_SCHEMA
                hits = await graph_store.vector_search(
                    query_embedding=centroid_emb,
                    isolation_key=isolation_key,
                    layers=["l6_schema"],
                    limit=_GRAPH_SEARCH_TOPK_PER_CLUSTER,
                    score_threshold=0.3,
                )
                for h in hits:
                    nid = h.get("node_id", "")
                    if nid in seen_node_ids:
                        continue
                    seen_node_ids.add(nid)
                    evidence_refs = await graph_store.get_evidence_vdbrefs(nid)
                    graph_forward.append({
                        "node_id": nid,
                        "content": h.get("content", ""),
                        "layer": h.get("layer", ""),
                        "confidence": h.get("confidence", 0),
                        "evidence_count": len(evidence_refs),
                        "similarity": round(h.get("score", 0), 4),
                    })
        logger.info(f"[S2-preprocess] Graph forward: {len(graph_forward)} nodes via centroid search")
    except Exception as e:
        logger.warning(f"[S2-preprocess] graph forward search failed: {e}")

    # ③b 查 Graph 真实节点总数（不依赖 vector_search 结果判断 Graph 是否为空）
    graph_total_schemas = 0
    try:
        if graph_store:
            _all_schemas = await graph_store.get_all_nodes(
                isolation_key=isolation_key,
                layer=MemoryLayer.L6_SCHEMA,
                status=MemoryStatus.ACTIVE,
                limit=1000,
            )
            graph_total_schemas = len(_all_schemas)
            logger.info(
                f"[S2-preprocess] Graph actual count: "
                f"schemas={graph_total_schemas} "
                f"(isolation_key={isolation_key})"
            )
    except Exception as e:
        logger.warning(f"[S2-preprocess] graph count query failed: {e}")

    # ④ Graph 反向查找: 从 VDB node_id 找到引用它的 Graph 节点
    graph_reverse = []
    try:
        vdb_ids = [f.node_id for f in valid_facts[:100]]
        graph_reverse = await graph_store.find_referencing_memories(vdb_ids, limit=50)
    except Exception as e:
        logger.warning(f"[S2-preprocess] graph reverse search failed: {e}")

    # ⑤ 已有 Topic 列表（供 Agent 复用 tag，减少发散）
    existing_tags = []
    try:
        topics = await graph_store.get_all_topics(isolation_key)
        existing_tags = [t["name"] for t in topics if t.get("name")]
    except Exception as e:
        logger.debug(f"[S2-preprocess] get_all_topics failed: {e}")

    clustered_node_ids: set = set()
    for c in clusters:
        for f in c.get("facts", []):
            clustered_node_ids.add(f["node_id"])

    unprocessed: list[dict[str, Any]] = []
    if clusters:
        unprocessed = [
            {"node_id": f.node_id, "content": f.content, "layer": f.layer.value}
            for f in valid_facts
            if f.node_id not in clustered_node_ids
        ]
        if len(unprocessed) > _S2_MAX_UNCLUSTERED_FACTS:
            logger.info(
                f"[S2-preprocess] unclustered facts capped: {len(unprocessed)} → "
                f"{_S2_MAX_UNCLUSTERED_FACTS} (clustered={len(clustered_node_ids)})"
            )
            unprocessed = unprocessed[:_S2_MAX_UNCLUSTERED_FACTS]

    if clusters:
        logger.info(
            f"[S2-preprocess] clusters={len(clusters)} clustered_facts={len(clustered_node_ids)} "
            f"unclustered_supplement={len(unprocessed)}"
        )
    else:
        logger.info(
            f"[S2-preprocess] no clusters → skip S2 agent "
            f"(fresh_pool={len(valid_facts)}, will not send unprocessed_facts to LLM)"
        )

    return {
        "clusters": clusters,
        "graph_forward": graph_forward,
        "graph_reverse": graph_reverse,
        "existing_tags": existing_tags,
        "unprocessed_facts": unprocessed,
        "stats": {
            "total_facts_all": len(all_facts),
            "total_facts_pool": len(valid_facts),
            "fresh_facts": len(fresh_facts),
            "evidenced_excluded": len(evidenced_facts),
            "clusters_found": len(clusters),
            "clustered_facts_count": len(clustered_node_ids),
            "unclustered_facts_count": len(unprocessed),
            "graph_schemas_recalled": len([g for g in graph_forward if g["layer"] == "l6_schema"]),
            "graph_total_schemas": graph_total_schemas,
            "graph_reverse_hits": len(graph_reverse),
            "existing_tags_count": len(existing_tags),
        },
    }


# ====================================================================
# System 2 Agent: LLM + tools
# ====================================================================

async def run_system2_agent(
    materials: dict[str, Any],
    tool_executor: System2ToolExecutor,
    config: MemoryConfig,
    max_iterations: int = 10,  # kept for backward compat, unused in single-call mode
) -> dict[str, Any]:
    """
    Phase 2: System 2 Agent — Single LLM call + JSON output + post-process execution.

    Instead of multi-turn tool-calling loop, this version:
    1. Puts all materials (facts + existing graph) into the prompt
    2. Asks LLM to output a JSON array of operations
    3. Parses and executes operations via tool_executor

    Returns:
        {
            "success": bool,
            "tool_calls": [...],
            "agent_reasoning": str,
            "total_tokens": int,
            "elapsed_ms": float,
        }
    """
    from ..agent.llm_provider import LLMProvider

    start = time.time()

    # Build LLMProvider from config
    llm_provider = LLMProvider(config)

    # Detect language from materials content
    lang = _detect_language(materials)
    system_prompt = _build_single_call_system_prompt(lang)
    user_msg = _build_materials_message(materials, lang=lang)

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_msg},
    ]

    # Single LLM call (no tools, just JSON output)
    try:
        response = await llm_provider.complete_messages(
            messages=messages,
            max_tokens=config.llm.agent_max_tokens or 4000,
            temperature=config.llm.temperature,
        )
    except Exception as e:
        logger.error(f"[S2-agent] LLM call failed: {e}")
        return {
            "success": False,
            "error": f"LLM call failed: {e}",
            "tool_calls": [],
            "elapsed_ms": (time.time() - start) * 1000,
        }

    total_prompt_tokens = response.prompt_tokens or 0
    total_completion_tokens = response.completion_tokens or 0
    agent_reasoning = response.content or ""

    # Parse JSON operations from response
    operations = _parse_operations_json(agent_reasoning)
    if operations is None:
        logger.warning("[S2-agent] Failed to parse operations JSON from response")
        return {
            "success": True,
            "tool_calls": [],
            "tool_call_log": [],
            "agent_reasoning": agent_reasoning,
            "messages": messages + [{"role": "assistant", "content": agent_reasoning}],
            "total_prompt_tokens": total_prompt_tokens,
            "total_completion_tokens": total_completion_tokens,
            "total_tokens": total_prompt_tokens + total_completion_tokens,
            "elapsed_ms": (time.time() - start) * 1000,
            "iterations": 1,
        }

    # Execute operations via tool_executor
    all_tool_calls = []
    for op in operations:
        op_type = op.get("op", "")
        try:
            if op_type == "create_schema":
                result = await tool_executor.execute("create_graph_node", {
                    "layer": "l6_schema",
                    "content": op.get("content", ""),
                    "evidence_list": op.get("evidence_list", []),
                    "tags": op.get("tags", []),
                    "confidence": op.get("confidence", 0.8),
                })
                all_tool_calls.append({"tool": "create_graph_node", "args": op, "result": result})

            elif op_type == "add_evidence":
                result = await tool_executor.execute("add_evidence", {
                    "node_id": op.get("node_id", ""),
                    "evidence_list": op.get("evidence_list", []),
                })
                all_tool_calls.append({"tool": "add_evidence", "args": op, "result": result})

            elif op_type == "add_edge":
                result = await tool_executor.execute("add_edge", {
                    "source_id": op.get("source_id", ""),
                    "target_id": op.get("target_id", ""),
                    "reason": op.get("reason", ""),
                })
                all_tool_calls.append({"tool": "add_edge", "args": op, "result": result})

            else:
                logger.warning(f"[S2-agent] Unknown operation type: {op_type}")

        except Exception as e:
            logger.warning(f"[S2-agent] Failed to execute op {op_type}: {e}")
            all_tool_calls.append({"tool": op_type, "args": op, "error": str(e)})

    elapsed_ms = (time.time() - start) * 1000

    logger.info(
        f"[S2-agent] Single-call done: {len(operations)} ops parsed, "
        f"{len(all_tool_calls)} executed, elapsed={elapsed_ms:.0f}ms"
    )

    return {
        "success": True,
        "tool_calls": all_tool_calls,
        "tool_call_log": tool_executor.tool_call_log,
        "agent_reasoning": agent_reasoning.strip(),
        "messages": messages + [{"role": "assistant", "content": agent_reasoning}],
        "total_prompt_tokens": total_prompt_tokens,
        "total_completion_tokens": total_completion_tokens,
        "total_tokens": total_prompt_tokens + total_completion_tokens,
        "elapsed_ms": elapsed_ms,
        "iterations": 1,
    }


async def run_system2_agent_batched(
    materials: dict[str, Any],
    tool_executor: System2ToolExecutor,
    config: MemoryConfig,
    max_clusters_per_call: int | None = None,
    max_clusters_per_run: int | None = None,
) -> dict[str, Any]:
    """
    量控制版 System 2 Agent：把 materials 的 clusters 按 max_clusters_per_call
    切成多批，每批一次 LLM 调用，循环处理，最后聚合各批结果。

    防止「一次性把所有 cluster 塞进单个 prompt 导致上下文爆炸」。
    返回结构与 run_system2_agent 完全一致（外层调用方无感）。

    - clusters 数 ≤ per_call → 直接走一次 run_system2_agent（含空 cluster，保留
      digest「空也跑一轮完整周期」语义）
    - max_clusters_per_run > 0 → 一个周期最多处理这么多 cluster，超出留到下个周期
      （fresh facts 未被引用，下次 prepare_materials 会重新聚类到它们）
    - graph_forward / graph_reverse / existing_tags 在每批中共享
    """
    per_call = max_clusters_per_call if max_clusters_per_call and max_clusters_per_call > 0 \
        else _S2_MAX_CLUSTERS_PER_CALL
    per_run = max_clusters_per_run if max_clusters_per_run is not None \
        else _S2_MAX_CLUSTERS_PER_RUN

    clusters = materials.get("clusters", []) or []

    # cluster 数不超过单次上限：单次（保持原语义）
    if len(clusters) <= per_call:
        result = await run_system2_agent(materials, tool_executor, config)
        result["batches"] = 1
        result["clusters_total"] = len(clusters)
        result["clusters_processed"] = len(clusters)
        return result

    # 本周期处理上限
    if per_run > 0 and len(clusters) > per_run:
        logger.info(
            f"[S2-agent] clusters={len(clusters)} > per_run={per_run}, "
            f"processing first {per_run} this cycle, rest deferred to next cycle"
        )
        clusters = clusters[:per_run]

    # 聚合容器
    agg_tool_calls: list[dict[str, Any]] = []
    agg_tool_call_log: list[dict[str, Any]] = []
    agg_messages: list[dict[str, Any]] = []
    agg_reasoning_parts: list[str] = []
    total_prompt_tokens = 0
    total_completion_tokens = 0
    total_elapsed_ms = 0.0
    batches = 0
    any_success = False

    n_batches = (len(clusters) + per_call - 1) // per_call
    logger.info(
        f"[S2-agent] batched mode: {len(clusters)} clusters → "
        f"{n_batches} batches × {per_call} clusters/call"
    )

    for bi in range(0, len(clusters), per_call):
        batch_clusters = clusters[bi:bi + per_call]
        batches += 1

        # 子 materials：本批 clusters + 共享 graph/tags；
        # unprocessed_facts 只保留不在本批 cluster 内的（避免重复，仍受 _S2_MAX_UNCLUSTERED_FACTS 限制）
        batch_node_ids = {f["node_id"] for c in batch_clusters for f in c.get("facts", [])}
        all_unclustered = materials.get("unprocessed_facts", []) or []
        batch_unclustered = [f for f in all_unclustered if f.get("node_id") not in batch_node_ids]

        sub_materials = dict(materials)
        sub_materials["clusters"] = batch_clusters
        sub_materials["unprocessed_facts"] = batch_unclustered

        batch_result = await run_system2_agent(sub_materials, tool_executor, config)

        agg_tool_calls.extend(batch_result.get("tool_calls", []) or [])
        agg_tool_call_log.extend(batch_result.get("tool_call_log", []) or [])
        if batch_result.get("messages"):
            agg_messages.extend(batch_result["messages"])
        if batch_result.get("agent_reasoning"):
            agg_reasoning_parts.append(
                f"[batch {batches}/{n_batches}] {batch_result['agent_reasoning']}"
            )
        total_prompt_tokens += batch_result.get("total_prompt_tokens", 0) or 0
        total_completion_tokens += batch_result.get("total_completion_tokens", 0) or 0
        total_elapsed_ms += batch_result.get("elapsed_ms", 0) or 0
        any_success = any_success or bool(batch_result.get("success"))

    logger.info(
        f"[S2-agent] batched done: {batches} batches, "
        f"{len(agg_tool_calls)} ops executed, elapsed={total_elapsed_ms:.0f}ms"
    )

    return {
        "success": any_success,
        "tool_calls": agg_tool_calls,
        "tool_call_log": agg_tool_call_log,
        "agent_reasoning": "\n\n".join(agg_reasoning_parts),
        "messages": agg_messages,
        "total_prompt_tokens": total_prompt_tokens,
        "total_completion_tokens": total_completion_tokens,
        "total_tokens": total_prompt_tokens + total_completion_tokens,
        "elapsed_ms": total_elapsed_ms,
        "iterations": batches,
        "batches": batches,
        "clusters_total": len(materials.get("clusters", []) or []),
        "clusters_processed": len(clusters),
    }


def _build_single_call_system_prompt(lang: str) -> str:
    """Build system prompt for single-call mode (includes output format instructions)."""
    base = SYSTEM2_AGENT_PROMPT_ZH if lang == "zh" else SYSTEM2_AGENT_PROMPT_EN

    if lang == "zh":
        output_instructions = """

## 输出格式

不要调用 tools。直接输出一个 JSON 数组，包含你决定执行的操作。用 ```json 代码块包裹。

可用操作：

1. **create_schema** — 创建新的 L6 Schema
   ```{"op": "create_schema", "content": "当[场景]时，用户[模式]——反映了[洞察]。", "evidence_list": ["fact_node_id_1", "fact_node_id_2"], "tags": ["tag1"]}```

2. **add_evidence** — 给已有 Schema 追加证据（不修改内容）
   ```{"op": "add_evidence", "node_id": "existing_schema_node_id", "evidence_list": ["new_fact_id_1"]}```

3. **add_edge** — 建立两个 Schema 之间的关系
   ```{"op": "add_edge", "source_id": "schema_id_1", "target_id": "schema_id_2", "reason": "关系原因"}```

## 输出约定

1. 输出必须是 JSON 数组，用 ```json 代码块包裹。
2. 如果数据不足以得出可靠结论，输出 `[]`。
3. 代码块外不要有任何其他文字。
4. evidence_list 中的 id 必须来自材料中提供的 fact node_id。
5. add_evidence 的 node_id 必须来自已有 Graph 节点的 id。"""
    else:
        output_instructions = """

## Output Format

Do NOT call tools. Output a JSON array of operations directly, wrapped in a ```json code block.

Available operations:

1. **create_schema** — Create a new L6 Schema
   ```{"op": "create_schema", "content": "When [circumstance], the user [pattern] — reflecting [insight].", "evidence_list": ["fact_node_id_1", "fact_node_id_2"], "tags": ["tag1"]}```

2. **add_evidence** — Add evidence to an existing Schema (content stays immutable)
   ```{"op": "add_evidence", "node_id": "existing_schema_node_id", "evidence_list": ["new_fact_id_1"]}```

3. **add_edge** — Create a relationship between two Schemas
   ```{"op": "add_edge", "source_id": "schema_id_1", "target_id": "schema_id_2", "reason": "relationship reason"}```

## Output Contract

1. Output MUST be a JSON array wrapped in a ```json code block.
2. If data is insufficient for reliable conclusions, output `[]`.
3. NO text outside the code block.
4. evidence_list IDs must come from the fact node_ids provided in the materials.
5. add_evidence node_id must come from existing Graph node IDs."""

    # Remove the old "All Graph operations MUST go through tools" principle
    base = base.replace("- 所有 Graph 操作必须通过 tools 执行\n", "")
    base = base.replace("- All Graph operations MUST go through tools\n", "")
    # Remove old tool-calling instructions from workflow
    base = base.replace("调 `add_evidence` 添加证据", "使用 add_evidence 操作")
    base = base.replace("call `add_evidence`", "use add_evidence operation")
    base = base.replace("调 `add_evidence`", "使用 add_evidence")
    base = base.replace("用 `add_edge`（RELATED_TO）", "使用 add_edge 操作")
    base = base.replace("use `add_edge` (RELATED_TO)", "use add_edge operation")

    return base + output_instructions


def _parse_operations_json(text: str) -> list[dict[str, Any]] | None:
    """Parse JSON array of operations from LLM response text."""
    import re

    # Try to find ```json ... ``` block
    match = re.search(r'```json\s*([\s\S]*?)\s*```', text)
    if match:
        json_str = match.group(1).strip()
    else:
        # Try raw JSON array
        match = re.search(r'\[\s*\{[\s\S]*\}\s*\]', text)
        if match:
            json_str = match.group(0)
        elif '[]' in text:
            return []
        else:
            return None

    try:
        result = json.loads(json_str)
        if isinstance(result, list):
            return result
        return None
    except json.JSONDecodeError:
        logger.warning(f"[S2-agent] JSON parse failed: {json_str[:200]}")
        return None


def _detect_language(materials: dict[str, Any]) -> str:
    """
    从 materials 的 fact content 检测语言。
    
    扫描所有聚类中的 fact content，计算中文字符占比。
    中文比例 >= 70% → "zh"，否则 → "en"。
    """
    import re
    all_text = ""
    for cluster in materials.get("clusters", []):
        for f in cluster.get("facts", []):
            all_text += f.get("content", "") + " "
    for f in materials.get("unprocessed_facts", []):
        all_text += f.get("content", "") + " "

    if not all_text.strip():
        return "en"

    # 统计中文字符（CJK Unified Ideographs）
    zh_chars = len(re.findall(r'[\u4e00-\u9fff]', all_text))
    # 统计所有非空白字符
    total_chars = len(re.findall(r'\S', all_text))

    if total_chars == 0:
        return "en"

    zh_ratio = zh_chars / total_chars
    lang = "zh" if zh_ratio >= 0.7 else "en"
    logger.debug(f"[S2-agent] language detection: zh_chars={zh_chars} total={total_chars} ratio={zh_ratio:.2f} → {lang}")
    return lang


def _build_materials_message(materials: dict[str, Any], lang: str = "zh") -> str:
    """将预处理好的 materials 格式化为 Agent 能理解的 user message"""
    is_zh = (lang == "zh")

    parts = [("## 预处理材料\n" if is_zh else "## Preprocessed Materials\n")]

    stats = materials.get("stats", {})
    pool = stats.get("total_facts_pool", 0)
    fresh = stats.get("fresh_facts", 0)
    evidenced = stats.get("evidenced_excluded", 0)
    # 用真实 Graph 节点数判断是否为空（不依赖 vector_search 召回结果）
    graph_total = stats.get("graph_total_schemas", 0)
    recalled_schemas = stats.get("graph_schemas_recalled", 0)

    if is_zh:
        parts.append(f"### 统计\n- VDB 事实总数: {stats.get('total_facts_all', 0)}")
        parts.append(f"- 新鲜事实（待处理）: {fresh}（已被引用排除={evidenced}）")
        parts.append(f"- 聚类数: {stats.get('clusters_found', 0)}")
        parts.append(
            f"- 聚类内事实: {stats.get('clustered_facts_count', 0)}，"
            f"未聚类补充: {stats.get('unclustered_facts_count', 0)}"
        )
        parts.append(f"- Graph Schema 总数: {stats.get('graph_total_schemas', 0)}（本次召回={recalled_schemas}）")
        if graph_total == 0:
            parts.append("- Graph 状态: **空**（首次认知加工）\n")
        else:
            parts.append("")
    else:
        parts.append(f"### Statistics\n- Total VDB facts: {stats.get('total_facts_all', 0)}")
        parts.append(f"- Fresh facts (to process): {fresh} (evidenced_excluded={evidenced})")
        parts.append(f"- Clusters found: {stats.get('clusters_found', 0)}")
        parts.append(
            f"- Facts in clusters: {stats.get('clustered_facts_count', 0)}, "
            f"unclustered supplement: {stats.get('unclustered_facts_count', 0)}"
        )
        parts.append(f"- Graph Schemas total: {stats.get('graph_total_schemas', 0)} (recalled={recalled_schemas})")
        if graph_total == 0:
            parts.append("- Graph status: **EMPTY** (first cognitive processing run)\n")
        else:
            parts.append("")

    # Clusters
    clusters = materials.get("clusters", [])
    clustered_ids = set()
    if clusters:
        parts.append("### 聚类结果\n" if is_zh else "### Cluster Results\n")
        for i, c in enumerate(clusters):
            n_facts = len(c['facts'])
            label = f"{n_facts} 条，主题" if is_zh else f"{n_facts} facts, topic"
            parts.append(f"**Cluster {i}** ({label}: {c['centroid_text']})")
            for f in c["facts"]:
                parts.append(f"  - [{f['layer']}] {f['content']}  (id={f['node_id']})")
                clustered_ids.add(f['node_id'])
            parts.append("")
    # 无聚类时不应进入 Agent；此处不输出「未聚类」大块（避免误导读成主输入）
    # Unclustered facts — 仅在有聚类时作为加菜
    all_facts = materials.get("unprocessed_facts", [])
    unclustered = [f for f in all_facts if f["node_id"] not in clustered_ids]
    if clusters and unclustered:
        header = "### 未聚类事实（补充，次要）\n" if is_zh else "### Unclustered Facts (supplemental)\n"
        hint = (
            "以下事实未进入上述任一聚类，可在完成聚类主题加工后酌情审视；优先级低于聚类结果。\n"
            if is_zh
            else "Facts below were not grouped into any cluster above. Review after cluster themes; lower priority than cluster results.\n"
        )
        parts.append(header)
        parts.append(hint)
        for f in unclustered:
            parts.append(f"  - [{f['layer']}] {f['content']}  (id={f['node_id']})")
        parts.append("")

    # Existing graph nodes
    forward = materials.get("graph_forward", [])
    if forward:
        if is_zh:
            parts.append(f"### 相关 Graph 节点（通过 cluster centroid 向量召回，共 {len(forward)} 条）\n")
        else:
            parts.append(f"### Related Graph Nodes (retrieved via cluster centroid search, {len(forward)} hits)\n")
        for g in forward:
            sim = g.get("similarity", "")
            sim_str = f", sim={sim}" if sim else ""
            parts.append(
                f"  - [{g['layer']}] {g['content']} "
                f"(id={g['node_id']}, evidence={g['evidence_count']}{sim_str})"
            )
        parts.append("")
    elif graph_total == 0:
        if is_zh:
            parts.append("### 已有 Graph 节点\n当前 Graph 为空，这是首次认知加工。无需搜索 Graph，直接根据聚类结果创建 Schema。\n")
        else:
            parts.append("### Existing Graph Nodes\nThe Graph is currently EMPTY — this is the first cognitive processing run. Do NOT search the Graph (it will return nothing). Proceed directly to create Schemas from the cluster results.\n")
    else:
        # Graph 有节点但 centroid search 没召回（可能 threshold 太高或语义距离远）
        if is_zh:
            parts.append(
                f"### 已有 Graph 节点\n"
                f"Graph 中共有 {stats.get('graph_total_schemas', 0)} 个 Schema，"
                f"但 centroid 向量召回未命中相关节点。请根据聚类结果判断是否需要创建新 Schema。\n"
            )
        else:
            parts.append(
                f"### Existing Graph Nodes\n"
                f"The Graph has {stats.get('graph_total_schemas', 0)} Schemas, "
                f"but centroid-based vector recall found no matches. Decide based on cluster results whether new Schemas are needed.\n"
            )

    # Reverse hits
    reverse = materials.get("graph_reverse", [])
    if reverse:
        parts.append("### Graph 反向引用（已有 Schema 引用的 VDB 事实）\n" if is_zh
                       else "### Graph Reverse References (VDB facts referenced by existing Schemas)\n")
        for r in reverse:
            parts.append(
                f"  - [{r['layer']}] {r['content']} "
                f"(schema_id={r['node_id']}, refs vdb={r.get('evidence_vdb_id', '')})"
            )
        parts.append("")

    # Existing tags（指导 Agent 复用已有 tag，减少发散）
    existing_tags = materials.get("existing_tags", [])
    if existing_tags:
        if is_zh:
            parts.append("### 已有标签（请优先从中选择，避免创建同义标签）\n")
            parts.append(f"  {', '.join(existing_tags)}\n")
        else:
            parts.append("### Existing Tags (prefer reusing these over creating synonyms)\n")
            parts.append(f"  {', '.join(existing_tags)}\n")

    if is_zh:
        parts.append(
            "请分析以上材料，输出操作 JSON 数组。"
            "决定是否需要创建新的 Schema，或为已有 Schema 追加 evidence。"
            "如果数据不足以得出可靠结论，输出空数组 `[]`。"
            "打标签时请优先使用已有标签列表中的标签。"
        )
    else:
        parts.append(
            "Analyze the materials above and output a JSON array of operations. "
            "Decide whether to create new Schemas, or add evidence to existing ones. "
            "If the data is insufficient for reliable conclusions, output an empty array `[]`. "
            "When tagging, prefer reusing tags from the existing tags list above."
        )

    return "\n".join(parts)
