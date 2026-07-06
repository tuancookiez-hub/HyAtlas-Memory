"""
Agent Memory V2 - 数据层 (Data Layer)

三引擎存储架构:
- VectorStore (Chroma/Qdrant/FAISS):  向量存储，L1 Raw / L2 Fact / Schema / Intention 语义检索
- GraphStore  (Kuzu/Neo4j): 图存储，实体关系 / 图遍历 / 时间线 / 知识缺口
- Cache       (SQLite/MySQL): 审计/观测落库（memory_operations / pipeline_logs / system_metrics）

缓存后端:
- SqliteCache: 零依赖本地（默认）
- MysqlCache:  腾讯云 MySQL 持久化（需 aiomysql 包）
- create_cache(): 工厂函数，根据配置选择后端
"""

from .vector_store import VectorStore, create_vector_store
from .vector_store_base import VectorStoreBase
from .graph_store import GraphStore, create_graph_store
from .graph_store_base import GraphStoreBase
from .cache_base import CacheBase
from .cache_sqlite import SqliteCache

from .cache import create_cache
from .cache_disabled import DisabledCache
from .history_store import HistoryStore

# 保留旧接口兼容
from .kv_store import KVStore, KVStoreConfig, KVBackend

__all__ = [
    # V2 核心
    "VectorStore",
    "VectorStoreBase",
    "create_vector_store",
    "GraphStore",
    "GraphStoreBase",
    "create_graph_store",
    "CacheBase",
    "SqliteCache",
    
    "create_cache",
    "DisabledCache",
    "HistoryStore",

    # V1 兼容
    "KVStore",
    "KVStoreConfig",
    "KVBackend",
]
