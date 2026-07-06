"""
Agent Memory V2 - GraphStore Provider 工厂

根据配置选择图数据库后端 (Kuzu / Neo4j)。

用法:
    from .graph_store import create_graph_store, GraphStore

    # 工厂函数 (推荐)
    graph = create_graph_store(config)

    # 向后兼容别名
    graph = GraphStore(config)
"""

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from ..config import MemoryConfig
from .graph_store_base import GraphStoreBase

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


def graph_data_may_exist(config: MemoryConfig) -> bool:
    """
    判断是否存在需要 purge 的图数据（与 MEMORY_MODE 无关）。

    - Kuzu：仅当 db_path 上已有数据库文件/目录时返回 True，避免 pro 模式误建空库。
    - Neo4j/Memgraph：外部服务，始终尝试连接并 purge。
    """
    provider_raw = getattr(config.graph_store, "provider", None)
    if not provider_raw or not str(provider_raw).strip():
        raise ValueError(
            "config.graph_store.provider is required "
            "(one of: kuzu / neo4j / memgraph)"
        )
    provider = str(provider_raw).lower().strip()
    if provider != "kuzu":
        raise ValueError(
            f"Unknown graph_store provider {provider!r}; "
            f"must be one of: kuzu / neo4j / memgraph"
        )

    db_path = Path(config.graph_store.db_path)
    if not db_path.exists():
        return False
    if db_path.is_file():
        return True
    if db_path.is_dir():
        try:
            return any(db_path.iterdir())
        except OSError:
            return False
    return False


def create_graph_store(config: MemoryConfig) -> GraphStoreBase:
    """
    工厂函数: 根据 config.graph_store.provider 创建对应的 GraphStore 实现。

    - "kuzu"     → KuzuGraphStore     (嵌入式)
    - "neo4j"    → Neo4jGraphStore    (客户端-服务端)
    - "memgraph" → Neo4jGraphStore    (兼容 bolt 协议, 复用 Neo4j 驱动)

    未配置或配置未知 provider 直接抛错；不做任何静默 fallback 到 kuzu。
    """
    provider = getattr(config.graph_store, 'provider', None)
    if not provider or not str(provider).strip():
        raise ValueError(
            "config.graph_store.provider is required "
            "(one of: kuzu / neo4j / memgraph)"
        )
    provider = str(provider).lower().strip()

    if provider == "kuzu":
        from .graph_store_kuzu import KuzuGraphStore
        logger.debug("GraphStore provider: kuzu")
        return KuzuGraphStore(config)
    else:
        raise ValueError(
            f"Unknown graph_store provider {provider!r}; must be: kuzu"
        )


# 向后兼容: `from .graph_store import GraphStore` 仍然可用
# GraphStore 现在是工厂函数的别名，调用 GraphStore(config) 等价于 create_graph_store(config)
def GraphStore(config: MemoryConfig) -> GraphStoreBase:
    """向后兼容别名 — 等价于 create_graph_store(config)"""
    return create_graph_store(config)
