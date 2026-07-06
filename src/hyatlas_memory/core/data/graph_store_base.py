"""
Agent Memory V2 - GraphStore 抽象基类

定义图存储层的统一接口（Provider 模式），允许切换不同的图数据库后端。

当前支持:
- Kuzu (嵌入式)
- Neo4j (客户端-服务端)

图 Schema:
  节点类型:
    User    — 用户根节点
    Memory  — 记忆节点 (L6_SCHEMA / L7_INTENTION)
                embedding FLOAT[dims]      — 内容向量 V_con (HNSW 索引)
                beh_embedding FLOAT[dims]  — 行为向量 V_beh (HNSW 索引, 可 NULL)
    Topic   — 主题标签
    VdbRef  — VDB 影子节点 (evidence 引用)

  边类型:
    HAS_MEMORY          : User → Memory
    TAGGED_WITH         : Memory → Topic
    RELATED_TO          : Memory ↔ Memory (双向)
    DERIVED_FROM        : Memory → VdbRef (evidence 关系)
    CROSS_ABSTRACTS_TO  : Memory → Memory (单向, L6 basic → L6 core)
"""

from abc import ABC, abstractmethod
from typing import Optional, List, Dict, Any

from ..models.memory import MemoryNode, MemoryLayer, MemoryStatus
from ..config import MemoryConfig


class GraphStoreBase(ABC):
    """
    图存储抽象基类

    所有图数据库后端必须实现此接口。
    """

    def __init__(self, config: MemoryConfig):
        self.config = config

    # ================================================================
    # 生命周期
    # ================================================================

    @abstractmethod
    async def initialize(self) -> None:
        """初始化图数据库连接和 Schema"""
        ...

    @abstractmethod
    async def close(self) -> None:
        """关闭连接"""
        ...

    @abstractmethod
    async def get_stats(self) -> Dict[str, Any]:
        """获取图统计信息"""
        ...

    # ================================================================
    # User 节点管理
    # ================================================================

    @abstractmethod
    async def ensure_user_node(
        self,
        isolation_key: str,
        user_id: str = "",
        agent_id: str = "",
    ) -> None:
        """确保 User 节点存在"""
        ...

    # ================================================================
    # Memory 节点 CRUD
    # ================================================================

    @abstractmethod
    async def upsert_memory_node(self, node: MemoryNode) -> str:
        """创建或更新 Memory 节点 + User→Memory 边，返回 node_id"""
        ...

    @abstractmethod
    async def get_node(self, node_id: str) -> Optional[MemoryNode]:
        """获取单个 Memory 节点"""
        ...

    @abstractmethod
    async def get_nodes_by_ids(self, node_ids: List[str]) -> List[MemoryNode]:
        """批量获取 Memory 节点"""
        ...

    @abstractmethod
    async def get_all_nodes(
        self,
        isolation_key: str,
        layer: Optional[MemoryLayer] = None,
        status: Optional[MemoryStatus] = None,
        limit: int = 100,
    ) -> List[MemoryNode]:
        """按条件获取节点列表"""
        ...

    @abstractmethod
    async def get_profile(self, isolation_key: str) -> Optional[MemoryNode]:
        """获取 L5 Identity Profile 节点"""
        ...

    @abstractmethod
    async def update_node(self, node_id: str, updates: Dict[str, Any]) -> bool:
        """更新节点属性"""
        ...

    @abstractmethod
    async def delete_node(self, node_id: str) -> bool:
        """删除 Memory 节点及其关联边"""
        ...

    @abstractmethod
    async def delete_all_nodes(self, isolation_key: str) -> int:
        """删除某隔离键下所有 Memory 节点，返回删除数量"""
        ...

    @abstractmethod
    async def delete_by_metadata(
        self,
        user_id: str,
        agent_id: Optional[str] = None,
        session_id: Optional[str] = None,
    ) -> int:
        """
        按 metadata 字段组合删除 Memory 节点。

        粒度:
        - 仅 user_id          → 删除该用户所有记忆
        - user_id + agent_id   → 删除该用户指定 agent 下所有记忆
        - user_id + agent_id + session_id → 删除精确 session 的记忆

        Returns:
            删除的节点数量
        """
        ...

    # ================================================================
    # 边操作
    # ================================================================

    @abstractmethod
    async def add_edge(
        self,
        source_id: str,
        target_id: str,
        edge_type: str,
        properties: Optional[Dict[str, Any]] = None,
    ) -> bool:
        """添加 Memory→Memory 关系边 (RELATED_TO)"""
        ...

    @abstractmethod
    async def add_topic_tag(
        self,
        memory_node_id: str,
        topic_name: str,
        isolation_key: str,
    ) -> str:
        """记忆节点关联主题 (Memory→Topic)，返回 topic_id"""
        ...

    # ================================================================
    # Topic 检索 (Tag 收敛 + Tag 桥接召回)
    # ================================================================

    async def get_all_topics(
        self,
        isolation_key: str,
    ) -> List[Dict[str, Any]]:
        """获取某 isolation_key 下的所有 Topic 节点

        返回 [{topic_id, name, embedding}]，embedding 可能为 None（旧数据）
        """
        return []  # 默认 no-op，子类覆盖

    async def tag_bridge_search(
        self,
        topic_ids: List[str],
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """Tag 桥接召回：从 Topic 节点反向查找关联的 Memory 节点

        即 MATCH (t:Topic)<-[:TAGGED_WITH]-(m:Memory) WHERE m.status = 'active'
        返回 [{node_id, content, layer, confidence, topic_name, custom_json}]
        """
        return []  # 默认 no-op，子类覆盖

    # ================================================================
    # V3: VdbRef (shadow node) + DERIVED_FROM edge
    # ================================================================

    async def ensure_vdbref(self, node_id: str, layer: str) -> None:
        """确保 VdbRef 影子节点存在（不存在则创建，已存在则跳过）"""
        ...  # 默认 no-op，子类覆盖

    async def add_derived_from(self, memory_node_id: str, vdbref_node_id: str) -> bool:
        """添加 DERIVED_FROM 边: Memory → VdbRef (evidence 关系)"""
        return False  # 默认 no-op

    async def find_referencing_memories(
        self,
        vdb_node_ids: List[str],
        limit: int = 50,
    ) -> List[Dict[str, Any]]:
        """反向查找: 给定 VDB node_id 列表，找到 Graph 中引用它们的 Memory 节点

        即 MATCH (v:VdbRef)<-[:DERIVED_FROM]-(m:Memory)
        返回 [{node_id, content, layer, confidence, evidence_vdb_id}]
        """
        return []  # 默认 no-op

    async def get_evidence_vdbrefs(self, memory_node_id: str) -> List[Dict[str, Any]]:
        """获取一个 Graph Memory 节点的所有 DERIVED_FROM evidence VdbRef

        返回 [{node_id, layer}]
        """
        return []  # 默认 no-op

    # ================================================================
    # 图检索
    # ================================================================

    @abstractmethod
    async def expand_from_anchors(
        self,
        anchor_ids: List[str],
        hop: int = 1,
        max_nodes: int = 50,
    ) -> List[Dict[str, Any]]:
        """从锚点节点出发，沿图边扩展 N 跳"""
        ...

    async def expand_with_tags(
        self,
        anchor_ids: List[str],
        hop: int = 2,
        max_nodes: int = 500,
        isolation_key: str = "",
    ) -> List[Dict[str, Any]]:
        """从锚点出发，沿 RELATED_TO + TAGGED_WITH(双向) 展开 N 跳。

        路径包括:
        - Memory -[RELATED_TO]- Memory（双向）
        - Memory -[TAGGED_WITH]-> Topic <-[TAGGED_WITH]- Memory（经 Topic 桥接）

        isolation_key 非空时只展开属于同一 isolation_key 的节点。

        返回 [{node_id, content, layer, confidence, hop, source}]
        """
        return []  # 默认 no-op，子类覆盖

    # ================================================================
    # 向量检索 (Graph HNSW)
    # ================================================================

    async def vector_search(
        self,
        query_embedding: List[float],
        isolation_key: str,
        layers: Optional[List[str]] = None,
        limit: int = 10,
        score_threshold: float = 0.0,
        user_id: str = "",
    ) -> List[Dict[str, Any]]:
        """V_con 向量检索 Graph Memory 节点

        使用 embedding 列的 HNSW 索引做 ANN 检索。
        
        当 isolation_key 为空但 user_id 非空时，执行跨 agent 搜索
        （匹配 isolation_key 以 user_id + "::" 为前缀的所有节点）。
        
        返回 [{node_id, content, layer, confidence, score}]
        """
        return []  # 默认 no-op，子类覆盖

    async def beh_vector_search(
        self,
        query_beh_embedding: List[float],
        isolation_key: str,
        limit: int = 10,
    ) -> List[Dict[str, Any]]:
        """V_beh 向量检索 L6 basic 节点 (sweeper 用)

        使用 beh_embedding 列的 HNSW 索引做 ANN 检索。
        返回 [{node_id, content, embedding, beh_distance}]
        """
        return []  # 默认 no-op，子类覆盖

    async def update_embedding(
        self,
        node_id: str,
        embedding: Optional[List[float]] = None,
        beh_embedding: Optional[List[float]] = None,
    ) -> bool:
        """更新节点的 embedding / beh_embedding 向量属性"""
        return False  # 默认 no-op

    # ================================================================
    # 跨域归纳 (Cross-Domain Schema Induction)
    # ================================================================

    async def add_cross_abstracts_to(
        self,
        basic_id: str,
        core_id: str,
    ) -> bool:
        """添加 CROSS_ABSTRACTS_TO 边: L6 basic → L6 core (单向)"""
        return False  # 默认 no-op

    async def find_cores_from_basics(
        self,
        basic_ids: List[str],
        max_hops: int = 2,
    ) -> List[Dict[str, Any]]:
        """从 L6 basic 出发，沿 RELATED_TO / CROSS_ABSTRACTS_TO 找 L6 core 节点

        返回 [{node_id, content, confidence}]，已去重
        """
        return []  # 默认 no-op

    async def get_cross_abstracts_targets(
        self,
        basic_id: str,
    ) -> List[str]:
        """查询某个 L6 basic 已有的 CROSS_ABSTRACTS_TO 边指向的 core node_id 列表"""
        return []  # 默认 no-op
