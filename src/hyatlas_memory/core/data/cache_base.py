"""
Agent Memory V2 - CacheBase

审计/观测落库层的抽象基类。
定义所有后端（SQLite / MySQL）必须实现的公共接口：
memory_operations（变动日志）、pipeline_logs（LLM 调用链）、system_metrics（指标）。
"""

from abc import ABC, abstractmethod
from typing import Optional, List, Dict, Any


class CacheBase(ABC):
    """
    审计/观测落库抽象基类。

    所有后端（SqliteCache / MysqlCache）均继承此类，
    上层代码只依赖 CacheBase 类型。
    """

    # ================================================================
    # 生命周期
    # ================================================================

    @abstractmethod
    async def initialize(self) -> None:
        """初始化后端连接/数据库。"""
        ...

    @abstractmethod
    async def close(self) -> None:
        """关闭连接，释放资源。"""
        ...

    # ================================================================
    # 统计
    # ================================================================

    @abstractmethod
    async def get_stats(self) -> Dict[str, Any]:
        ...

    # ================================================================
    # Memory Operations Log
    # ================================================================

    @abstractmethod
    async def store_memory_operation(
        self,
        request_id: str,
        user_id: str,
        agent_id: str,
        op: str,
        memory_id: str,
        content: str,
        layer: str = "",
        old_memory_id: Optional[str] = None,
        reason: str = "",
        supersedes: Optional[List[str]] = None,
    ) -> bool:
        """记录一条知识库变动操作（ADD / EVOLVE）"""
        ...

    @abstractmethod
    async def get_memory_operations(
        self,
        request_id: Optional[str] = None,
        memory_id: Optional[str] = None,
        user_id: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """查询知识库变动记录，支持按 request_id / memory_id / user_id 过滤"""
        ...

    # ================================================================
    # Pipeline Logs (LLM 调用链中间结果)
    # ================================================================

    @abstractmethod
    async def store_pipeline_log(
        self,
        request_id: str,
        user_id: str,
        agent_id: str,
        step: str,
        prompt: str,
        response: str,
        parsed: str = "",
        memory_ids: Optional[List[str]] = None,
        elapsed_ms: float = 0,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
    ) -> bool:
        """记录一条 pipeline 中间结果（EXTRACT / SEARCH_QUERY / RECONCILE / SUMMARY）"""
        ...

    @abstractmethod
    async def get_pipeline_logs(
        self,
        request_id: Optional[str] = None,
        user_id: Optional[str] = None,
        step: Optional[str] = None,
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """查询 pipeline 中间结果日志"""
        ...
