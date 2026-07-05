"""
Entity store 写入 helper（供 writer 落库时 + client 批量迁移共用）。

职责：给定一条 memory 的 (memory_id, content)，抽取 entity → embed → upsert
到 vector_store 的 entity store（独立 {collection}_entities collection）。

全部 best-effort：任何异常都吞掉并记 debug，绝不影响主写入/迁移流程。
spaCy 不可用或抽不到 entity 时静默跳过（与 mem0 行为一致）。
"""

from typing import Any, List, Optional
import logging

logger = logging.getLogger(__name__)

# 单条 memory 最多刷入的 entity 数（对齐 mem0 query 侧上限）
MAX_ENTITIES_PER_MEMORY = 8


async def index_memory_entities(
    *,
    vector_store: Any,
    embed_service: Any,
    memory_id: str,
    content: str,
    user_id: str,
    agent_id: str = "",
    merge_threshold: float = 0.95,
) -> int:
    """对一条 memory 抽 entity 并刷入 entity store。返回写入/合并的 entity 数。

    best-effort：失败返回 0，不抛异常。
    """
    if not content or not memory_id or not user_id:
        return 0
    try:
        from .entities import extract_entities
    except Exception as e:
        logger.debug(f"[entity-index] entities module unavailable: {e}")
        return 0

    try:
        entities = extract_entities(content)
    except Exception as e:
        logger.debug(f"[entity-index] extract failed: {e}")
        return 0

    if not entities:
        return 0
    entities = entities[:MAX_ENTITIES_PER_MEMORY]

    texts = [t for (_etype, t) in entities if t and t.strip()]
    if not texts:
        return 0

    try:
        embeddings = await embed_service.embed_batch(texts)
    except Exception as e:
        logger.debug(f"[entity-index] embed_batch failed: {e}")
        return 0
    if not embeddings or len(embeddings) != len(texts):
        return 0

    done = 0
    for (etype, etext), emb in zip(entities, embeddings):
        try:
            await vector_store.upsert_entity(
                entity_text=etext,
                entity_type=etype,
                embedding=emb,
                memory_id=memory_id,
                user_id=user_id,
                agent_id=agent_id or "",
                merge_threshold=merge_threshold,
            )
            done += 1
        except NotImplementedError:
            # 后端不支持 entity store（非 chroma）→ 整体跳过
            logger.debug("[entity-index] backend has no entity store; skipping")
            return done
        except Exception as e:
            logger.debug(f"[entity-index] upsert_entity '{etext}' failed: {e}")
    return done
