"""
Per-user tag embedding index —— 为 reader_hybrid_tag 的路 B 提供语义桥接召回。

核心设计：
  - 存储：和 memories 同一套 VDB（通过 VectorStoreBase 接口抽象），独立 collection
    （命名 `{memories_collection}_tag_index`）
  - 写入：writer 侧惰性维护。每次 memory 写入成功后，对节点的每个 tag 调用
    `ensure_tag_embedding`，已存在则跳过（uuid5 保证幂等），否则 embed + upsert
  - 删除：client.delete 删除单条 memory 后，对每个 tag 扫主 collection 验证
    引用数，为 0 才从 tag_index 中删除
  - 查询：reader_hybrid_tag 用 `search_matching_tags` 批量查匹配 tag
  - 失败策略：全链路 try-except 静默降级——tag_index 失败不影响主流程；
    reader 侧拿不到 tag 匹配时，路 B 返回空，RRF 自动降级

限制：
  - 非 Qdrant 后端（Chroma / Faiss / Tencent VDB）需要在 VectorStoreBase 的
    tag_index 方法中先实现或抛 NotImplementedError；reader_hybrid_tag 在
    初始化时会探测 backend 能力，不支持则 warning + 降级到 hybrid 行为
"""

from typing import Any, Dict, List, Optional
import logging

from . import config

logger = logging.getLogger(__name__)


async def ensure_tag_embedding(
    vector_store,
    embed_service,
    user_id: str,
    tag: str,
) -> bool:
    """
    惰性 upsert：若该 (user_id, tag) 的 embedding 不存在则计算并写入；
    已存在则直接返回 True。

    返回 True 表示"tag 在 tag_index 里是可用的"，False 表示失败。
    异常一律吞掉并 log warning，不阻塞主流程。
    """
    if not config.TAG_INDEX_WRITE_ENABLED:
        return False
    if not user_id or not tag:
        return False
    try:
        exists = await vector_store.has_tag_embedding(user_id, tag)
        if exists:
            return True
    except NotImplementedError:
        return False
    except Exception as e:
        logger.debug(f"[tag-index] has_tag_embedding failed ({user_id}, {tag}): {e}")
        # 尝试直接 upsert（exists 检查失败不代表不能写）

    try:
        # 复用 search 路径的 embed 接口（search 路径低延迟优先，不走 write 攒批队列）
        vec = await embed_service.embed_queued(tag)
    except Exception as e:
        logger.warning(f"[tag-index] embed tag failed ({user_id}, {tag}): {e}")
        return False

    try:
        await vector_store.upsert_tag_embedding(user_id=user_id, tag=tag, embedding=vec)
        return True
    except NotImplementedError:
        return False
    except Exception as e:
        logger.warning(f"[tag-index] upsert tag failed ({user_id}, {tag}): {e}")
        return False


async def ensure_tag_embeddings_for_node(
    vector_store,
    embed_service,
    user_id: str,
    tags: List[str],
) -> int:
    """
    批量确保一组 tag 的 embedding 在 tag_index 中就位。返回成功数量。
    串行调用（每个 tag 有独立 try-except），不让单个 tag 失败影响其他。
    """
    if not tags:
        return 0
    ok = 0
    for tag in tags:
        if not tag:
            continue
        success = await ensure_tag_embedding(
            vector_store=vector_store,
            embed_service=embed_service,
            user_id=user_id,
            tag=tag,
        )
        if success:
            ok += 1
    return ok


async def cleanup_tags_on_delete(
    vector_store,
    user_id: str,
    tags: List[str],
    isolation_key: str = "",
) -> int:
    """
    删除 memory 后，对其每个 tag 检查引用数。无引用则从 tag_index 删除。
    返回实际删除的 tag 条目数。
    """
    if not config.TAG_INDEX_WRITE_ENABLED or not user_id or not tags:
        return 0
    removed = 0
    for tag in tags:
        if not tag:
            continue
        try:
            remaining = await vector_store.count_memories_with_tag(
                user_id=user_id, tag=tag, isolation_key=isolation_key,
            )
        except NotImplementedError:
            return 0  # backend 不支持，整体放弃
        except Exception as e:
            logger.debug(f"[tag-index] count_memories_with_tag failed ({user_id}, {tag}): {e}")
            continue

        if remaining > 0:
            continue

        try:
            await vector_store.delete_tag_embedding(user_id=user_id, tag=tag)
            removed += 1
        except NotImplementedError:
            return removed
        except Exception as e:
            logger.debug(f"[tag-index] delete_tag_embedding failed ({user_id}, {tag}): {e}")
    return removed


async def search_matching_tags(
    vector_store,
    user_id: str,
    keyword_embeddings: List[List[float]],
    topk: Optional[int] = None,
    min_score: Optional[float] = None,
) -> List[str]:
    """
    对每个 keyword embedding 在 per-user tag_index 里检索 topk 最相近的 tag。
    合并去重后返回。

    返回的 tag 列表已去重，但不保证排序（reader 只关心 MatchAny 集合）。
    任何失败静默返回空列表（reader 会把路 B 视为空）。
    """
    if not keyword_embeddings or not user_id:
        return []
    topk = topk if topk is not None else config.TAG_MATCH_TOPK
    min_score = min_score if min_score is not None else config.TAG_MATCH_MIN_SCORE

    seen = set()
    result: List[str] = []
    for vec in keyword_embeddings:
        try:
            hits = await vector_store.search_tag_embeddings(
                user_id=user_id,
                query_embedding=vec,
                topk=topk,
                min_score=min_score,
            )
        except NotImplementedError:
            return []
        except Exception as e:
            logger.debug(f"[tag-index] search_tag_embeddings failed: {e}")
            continue
        for hit in hits or []:
            tag = hit.get("tag") if isinstance(hit, dict) else None
            if tag and tag not in seen:
                seen.add(tag)
                result.append(tag)
    return result


def backend_supports_tag_index(vector_store) -> bool:
    """
    探测 vector_store 是否实现了 tag_index 接口族。
    reader_hybrid_tag 初始化时用此决定是否降级到 hybrid 行为。
    """
    required = [
        "upsert_tag_embedding",
        "search_tag_embeddings",
        "delete_tag_embedding",
        "has_tag_embedding",
        "count_memories_with_tag",
    ]
    for name in required:
        fn = getattr(vector_store, name, None)
        if fn is None:
            return False
    # 通过 `_supports_tag_index` 私有布尔快速 opt-in，避免子类需要真调一次
    flag = getattr(vector_store, "_supports_tag_index", None)
    return bool(flag) if flag is not None else True
