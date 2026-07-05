"""
Agent Memory V2 - 请求/响应模型

定义 API 接口的请求和响应数据结构。
适配 V2 数据模型 (MemoryNode, MemoryContextPackage 等)。
"""

from typing import Optional, Dict, Any, List, Union
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
import uuid

from .memory import (
    MemoryLayer,
    MemoryNode,
    MemoryEntry,
    MemoryContextPackage,
    MemoryScore,
    MemoryMetadata,
    ContentType,
    SourceType,
)


# ============================================================
# 枚举
# ============================================================

class DeleteScope(str, Enum):
    """删除范围"""
    MEMORY = "memory"
    SCENE = "scene"
    USER = "user"
    APP = "app"


class MemoryInputType(str, Enum):
    """记忆输入类型"""
    TEXT = "text"
    QA = "qa"
    DIALOGUE = "dialogue"


class TaskStatus(str, Enum):
    """异步任务状态"""
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class AgentProcessMode(str, Enum):
    """Agent 处理模式"""
    DISABLED = "disabled"    # 关闭 Agent 处理
    FULL = "full"            # 完整处理 (提取+摘要+冲突检测)


# ============================================================
# 添加记忆
# ============================================================

@dataclass
class QAPair:
    """问答对"""
    question: str = ""
    answer: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"question": self.question, "answer": self.answer}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "QAPair":
        return cls(question=data.get("question", ""), answer=data.get("answer", ""))

    def to_content(self) -> str:
        return f"Q: {self.question}\nA: {self.answer}"


@dataclass
class AddRequest:
    """
    添加记忆请求

    参数职责分离：
    - agent_mode: 控制 MemAgent 处理深度 (disabled / full)
    """
    # 必填 - 身份信息
    uid: str = ""
    agent_id: str = ""

    # 会话信息 (V2 新增)
    session_id: str = ""

    # 输入内容
    input_type: MemoryInputType = MemoryInputType.TEXT
    content: Optional[str] = None
    qa_pair: Optional[QAPair] = None

    # 记忆层
    layer: Optional[Union[MemoryLayer, str]] = None

    # 时间
    event_time: Optional[datetime] = None
    ttl_seconds: Optional[int] = None

    # 权重
    importance: float = 1.0

    # 扩展
    tags: List[str] = field(default_factory=list)
    custom: Dict[str, Any] = field(default_factory=dict)
    source: Optional[str] = None

    # Agent 处理选项
    agent_mode: AgentProcessMode = AgentProcessMode.FULL
    async_process: bool = False

    # V1 兼容
    auto_extract: bool = False
    store_raw: bool = True
    infer: bool = False

    def __post_init__(self):
        if isinstance(self.layer, str):
            self.layer = MemoryLayer.from_string(self.layer)
        if isinstance(self.input_type, str):
            self.input_type = MemoryInputType(self.input_type)
        if isinstance(self.agent_mode, str):
            self.agent_mode = AgentProcessMode(self.agent_mode)
        if self.auto_extract and self.agent_mode == AgentProcessMode.DISABLED:
            self.agent_mode = AgentProcessMode.FULL
        if not self.session_id:
            self.session_id = str(uuid.uuid4())
        self._validate()

    def _validate(self):
        if self.input_type == MemoryInputType.TEXT:
            if not self.content:
                raise ValueError("content is required when input_type is 'text'")
        elif self.input_type == MemoryInputType.QA:
            if not self.qa_pair:
                raise ValueError("qa_pair is required when input_type is 'qa'")

    def get_content(self) -> str:
        if self.input_type == MemoryInputType.TEXT:
            return self.content or ""
        elif self.input_type == MemoryInputType.QA:
            return self.qa_pair.to_content() if self.qa_pair else ""
        return ""

    def to_dict(self) -> Dict[str, Any]:
        result = {
            "uid": self.uid,
            "agent_id": self.agent_id,
            "session_id": self.session_id,
            "input_type": self.input_type.value,
            "layer": self.layer.value if self.layer else None,
            "importance": self.importance,
            "tags": self.tags,
            "agent_mode": self.agent_mode.value,
            "async_process": self.async_process,
        }
        if self.content:
            result["content"] = self.content
        if self.qa_pair:
            result["qa_pair"] = self.qa_pair.to_dict()
        return result


@dataclass
class AddResponse:
    """添加记忆响应（同步模式）"""
    success: bool = False
    memory_id: str = ""
    node_ids: List[str] = field(default_factory=list)
    layer: Optional[MemoryLayer] = None
    message: str = ""

    # System 1 提取的额外节点
    extracted_fact_ids: List[str] = field(default_factory=list)
    extracted_intention_ids: List[str] = field(default_factory=list)

    # V1 兼容
    extracted_ids: List[str] = field(default_factory=list)
    abstract_key_ids: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "memory_id": self.memory_id,
            "node_ids": self.node_ids,
            "layer": self.layer.value if self.layer else None,
            "message": self.message,
            "extracted_fact_ids": self.extracted_fact_ids,
            "extracted_intention_ids": self.extracted_intention_ids,
        }


@dataclass
class AsyncAddResponse:
    """异步添加记忆响应"""
    success: bool = False
    task_id: str = ""
    status: TaskStatus = TaskStatus.PENDING
    message: str = ""
    estimated_time: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "task_id": self.task_id,
            "status": self.status.value,
            "message": self.message,
            "estimated_time": self.estimated_time,
        }


@dataclass
class TaskStatusRequest:
    """查询任务状态请求"""
    task_id: str = ""


@dataclass
class TaskStatusResponse:
    """任务状态响应"""
    success: bool = False
    task_id: str = ""
    status: TaskStatus = TaskStatus.PENDING
    message: str = ""
    progress: int = 0
    result: Optional[AddResponse] = None
    error: Optional[str] = None
    created_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "task_id": self.task_id,
            "status": self.status.value,
            "message": self.message,
            "progress": self.progress,
            "result": self.result.to_dict() if self.result else None,
            "error": self.error,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }


@dataclass
class AsyncTask:
    """异步任务"""
    task_id: str = ""
    uid: str = ""
    agent_id: str = ""
    request: Optional[AddRequest] = None
    status: TaskStatus = TaskStatus.PENDING
    progress: int = 0
    result: Optional[AddResponse] = None
    error: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.now)
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    @classmethod
    def create(cls, request: AddRequest) -> "AsyncTask":
        return cls(
            task_id=str(uuid.uuid4()),
            uid=request.uid,
            agent_id=request.agent_id,
            request=request,
        )


# ============================================================
# 召回记忆
# ============================================================

@dataclass
class RecallRequest:
    """
    召回记忆请求

    V2 适配：
    - 新增 session_id (用于 reconsolidation)
    - 新增 include_context_package (默认 True，返回 MemoryContextPackage)
    - 新增 token_budget (Token 预算)
    """
    query: str = ""
    uid: str = ""

    # 会话信息
    session_id: Optional[str] = None
    agent_id: Optional[str] = None
    agent_ids: List[str] = field(default_factory=list)
    layers: List[MemoryLayer] = field(default_factory=list)

    # 数量限制
    limit: int = 10
    limit_per_layer: Optional[int] = None

    # 跨场景
    cross_scene: bool = False

    # 过滤条件
    tags: List[str] = field(default_factory=list)
    time_range_start: Optional[datetime] = None
    time_range_end: Optional[datetime] = None
    min_importance: float = 0.0
    min_confidence: float = 0.0

    # V2: Context Package 选项
    include_context_package: bool = True
    token_budget: int = 2000
    include_meta_cognition: bool = True
    include_intentions: bool = True
    include_schemas: bool = True
    include_temporal: bool = True

    # V1 兼容: 评分权重
    semantic_weight: float = 0.5
    recency_weight: float = 0.3
    importance_weight: float = 0.15
    access_weight: float = 0.05

    # 返回选项
    include_score: bool = True
    include_metadata: bool = True


@dataclass
class RecallResponse:
    """
    召回记忆响应

    V2: 核心返回 MemoryContextPackage。memories 保留用于 V1 兼容。
    """
    success: bool = False
    context_package: Optional[MemoryContextPackage] = None
    memories: List[MemoryNode] = field(default_factory=list)
    total_count: int = 0
    message: str = ""
    by_category: Dict[str, List[MemoryNode]] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        result = {
            "success": self.success,
            "total_count": self.total_count,
            "message": self.message,
            "memories": [m.to_dict() for m in self.memories],
            "by_category": {
                k: [m.to_dict() for m in v]
                for k, v in self.by_category.items()
            },
        }
        if self.context_package:
            result["context_package"] = self.context_package.to_dict()
        return result


# ============================================================
# 获取记忆
# ============================================================

@dataclass
class GetRequest:
    """获取单条记忆请求"""
    memory_id: str = ""


@dataclass
class GetResponse:
    """获取单条记忆响应"""
    success: bool = False
    memory: Optional[MemoryNode] = None
    message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "memory": self.memory.to_dict() if self.memory else None,
            "message": self.message,
        }


# ============================================================
# 列出记忆
# ============================================================

@dataclass
class ListRequest:
    """列出记忆请求"""
    uid: str = ""
    agent_id: Optional[str] = None
    layers: List[MemoryLayer] = field(default_factory=list)
    offset: int = 0
    limit: int = 100
    order_by: str = "created_at"
    order_desc: bool = True


@dataclass
class ListResponse:
    """列出记忆响应"""
    success: bool = False
    memories: List[MemoryNode] = field(default_factory=list)
    total_count: int = 0
    offset: int = 0
    limit: int = 100
    message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "memories": [m.to_dict() for m in self.memories],
            "total_count": self.total_count,
            "offset": self.offset,
            "limit": self.limit,
            "message": self.message,
        }


# ============================================================
# 更新记忆
# ============================================================

@dataclass
class UpdateRequest:
    """更新记忆请求"""
    memory_id: str = ""
    content: Optional[str] = None
    importance: Optional[float] = None
    tags: Optional[List[str]] = None
    custom: Optional[Dict[str, Any]] = None
    conflict_strategy: str = "update"


@dataclass
class UpdateResponse:
    """更新记忆响应"""
    success: bool = False
    memory_id: str = ""
    message: str = ""
    conflict_resolved: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "memory_id": self.memory_id,
            "message": self.message,
            "conflict_resolved": self.conflict_resolved,
        }


# ============================================================
# 删除记忆
# ============================================================

@dataclass
class DeleteRequest:
    """删除单条记忆请求"""
    memory_id: str = ""


@dataclass
class DeleteResponse:
    """删除记忆响应"""
    success: bool = False
    deleted_count: int = 0
    message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "deleted_count": self.deleted_count,
            "message": self.message,
        }


@dataclass
class BatchDeleteRequest:
    """批量删除记忆请求"""
    scope: DeleteScope = DeleteScope.MEMORY
    memory_ids: List[str] = field(default_factory=list)
    uid: Optional[str] = None
    agent_id: Optional[str] = None
    layers: List[MemoryLayer] = field(default_factory=list)
    before_time: Optional[datetime] = None
    confirm: bool = False


@dataclass
class BatchDeleteResponse:
    """批量删除响应"""
    success: bool = False
    deleted_count: int = 0
    message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "deleted_count": self.deleted_count,
            "message": self.message,
        }


# ============================================================
# 用户画像
# ============================================================

@dataclass
class UserProfile:
    """用户画像"""
    uid: str = ""
    basic_info: Dict[str, Any] = field(default_factory=dict)
    preferences: Dict[str, Any] = field(default_factory=dict)
    relationships: List[Dict[str, Any]] = field(default_factory=list)
    tags: List[str] = field(default_factory=list)
    created_at: datetime = field(default_factory=datetime.now)
    updated_at: datetime = field(default_factory=datetime.now)
    version: int = 1

    # V2 新增字段
    schemas: List[str] = field(default_factory=list)
    gotchas: List[str] = field(default_factory=list)
    personality: str = ""
    hypotheses: Dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "uid": self.uid,
            "basic_info": self.basic_info,
            "preferences": self.preferences,
            "relationships": self.relationships,
            "tags": self.tags,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
            "version": self.version,
            "schemas": self.schemas,
            "gotchas": self.gotchas,
            "personality": self.personality,
            "hypotheses": self.hypotheses,
        }


@dataclass
class GetProfileRequest:
    """获取用户画像请求"""
    uid: str = ""
    include_history: bool = False


@dataclass
class GetProfileResponse:
    """获取用户画像响应"""
    success: bool = False
    profile: Optional[UserProfile] = None
    message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "profile": self.profile.to_dict() if self.profile else None,
            "message": self.message,
        }


@dataclass
class UpdateProfileRequest:
    """更新用户画像请求"""
    uid: str = ""
    updates: Dict[str, Any] = field(default_factory=dict)
    merge: bool = True


@dataclass
class UpdateProfileResponse:
    """更新用户画像响应"""
    success: bool = False
    profile: Optional[UserProfile] = None
    message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "profile": self.profile.to_dict() if self.profile else None,
            "message": self.message,
        }


@dataclass
class RebuildProfileRequest:
    """重建用户画像请求"""
    uid: str = ""
    force: bool = False


@dataclass
class RebuildProfileResponse:
    """重建用户画像响应"""
    success: bool = False
    profile: Optional[UserProfile] = None
    memories_processed: int = 0
    message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "profile": self.profile.to_dict() if self.profile else None,
            "memories_processed": self.memories_processed,
            "message": self.message,
        }


# ============================================================
# 异步任务管理
# ============================================================

@dataclass
class SubmitTaskRequest:
    """提交异步任务请求"""
    task_type: str = ""
    payload: Dict[str, Any] = field(default_factory=dict)
    priority: int = 0
    callback_url: Optional[str] = None


@dataclass
class SubmitTaskResponse:
    """提交异步任务响应"""
    success: bool = False
    task_id: str = ""
    status: str = "pending"
    message: str = ""
    estimated_time: Optional[int] = None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "task_id": self.task_id,
            "status": self.status,
            "message": self.message,
            "estimated_time": self.estimated_time,
        }


@dataclass
class GetTaskRequest:
    """查询任务状态请求"""
    task_id: str = ""


@dataclass
class GetTaskResponse:
    """查询任务状态响应"""
    success: bool = False
    task_id: str = ""
    task_type: str = ""
    status: str = "pending"
    progress: int = 0
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    message: str = ""
    created_at: Optional[datetime] = None
    started_at: Optional[datetime] = None
    completed_at: Optional[datetime] = None

    @property
    def is_completed(self) -> bool:
        return self.status == "completed"

    @property
    def is_failed(self) -> bool:
        return self.status == "failed"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "task_id": self.task_id,
            "task_type": self.task_type,
            "status": self.status,
            "progress": self.progress,
            "result": self.result,
            "error": self.error,
            "message": self.message,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "started_at": self.started_at.isoformat() if self.started_at else None,
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
        }


@dataclass
class CancelTaskRequest:
    """取消任务请求"""
    task_id: str = ""


@dataclass
class CancelTaskResponse:
    """取消任务响应"""
    success: bool = False
    task_id: str = ""
    message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "task_id": self.task_id,
            "message": self.message,
        }


@dataclass
class ListTasksRequest:
    """列出任务请求"""
    uid: Optional[str] = None
    task_type: Optional[str] = None
    status: Optional[str] = None
    offset: int = 0
    limit: int = 20


@dataclass
class ListTasksResponse:
    """列出任务响应"""
    success: bool = False
    tasks: List[Dict[str, Any]] = field(default_factory=list)
    total_count: int = 0
    offset: int = 0
    limit: int = 20
    message: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "success": self.success,
            "tasks": self.tasks,
            "total_count": self.total_count,
            "offset": self.offset,
            "limit": self.limit,
            "message": self.message,
        }
