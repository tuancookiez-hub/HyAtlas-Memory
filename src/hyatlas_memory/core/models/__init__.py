"""
Agent Memory V2 - 数据模型

核心数据结构，包含 V2 新模型和 V1 兼容类型。
"""

# === V2 核心模型 ===
from .memory import (
    ActivatedSchema,
    ContentType,
    GapType,
    IntentionNode,
    IntentionPriority,
    # 辅助模型
    KnowledgeGap,
    LifeStage,
    MemoryContextPackage,
    # V1 兼容
    MemoryEntry,
    MemoryIndexEntry,
    # 枚举类型
    MemoryLayer,
    MemoryMetadata,
    # 核心节点
    MemoryNode,
    MemoryScore,
    MemoryStatus,
    MemorySummaryEntry,
    MetaCognitionReport,
    MetaCognitionTag,
    # 输出协议
    ProfileSummary,
    SchemaNode,
    SchemaStatus,
    SourceType,
    TemporalContext,
    TemporalEvent,
    TriggeredIntention,
    TriggerType,
    UpdateType,
    UserTimeline,
    VersionedFact,
)

# === 请求/响应模型 ===
from .requests import (
    # 添加记忆
    AddRequest,
    AddResponse,
    AgentProcessMode,
    AsyncAddResponse,
    AsyncTask,
    BatchDeleteRequest,
    BatchDeleteResponse,
    CancelTaskRequest,
    CancelTaskResponse,
    # 删除记忆
    DeleteRequest,
    DeleteResponse,
    # 枚举
    DeleteScope,
    GetProfileRequest,
    GetProfileResponse,
    # 获取/列出记忆
    GetRequest,
    GetResponse,
    GetTaskRequest,
    GetTaskResponse,
    ListRequest,
    ListResponse,
    ListTasksRequest,
    ListTasksResponse,
    MemoryInputType,
    # QA 对
    QAPair,
    RebuildProfileRequest,
    RebuildProfileResponse,
    # 召回记忆
    RecallRequest,
    RecallResponse,
    # 异步任务管理
    SubmitTaskRequest,
    SubmitTaskResponse,
    TaskStatus,
    TaskStatusRequest,
    TaskStatusResponse,
    UpdateProfileRequest,
    UpdateProfileResponse,
    # 更新记忆
    UpdateRequest,
    UpdateResponse,
    # 用户画像
    UserProfile,
)

__all__ = [
    # === V2 枚举 ===
    "MemoryLayer",
    "ContentType",
    "MemoryStatus",
    "SourceType",
    "UpdateType",
    "SchemaStatus",
    "TriggerType",
    "IntentionPriority",
    "MetaCognitionTag",
    "GapType",
    # === V2 核心节点 ===
    "MemoryNode",
    "VersionedFact",
    "SchemaNode",
    "IntentionNode",
    # === V2 辅助模型 ===
    "KnowledgeGap",
    "LifeStage",
    "TemporalEvent",
    "UserTimeline",
    # === V2 输出协议 ===
    "ProfileSummary",
    "MemoryIndexEntry",
    "MemorySummaryEntry",
    "MetaCognitionReport",
    "TriggeredIntention",
    "ActivatedSchema",
    "TemporalContext",
    "MemoryContextPackage",
    # === V1 兼容 ===
    "MemoryEntry",
    "MemoryMetadata",
    "MemoryScore",
    # === 请求/响应枚举 ===
    "DeleteScope",
    "MemoryInputType",
    "TaskStatus",
    "AgentProcessMode",
    # === QA ===
    "QAPair",
    # === 添加 ===
    "AddRequest",
    "AddResponse",
    "AsyncAddResponse",
    "AsyncTask",
    "TaskStatusRequest",
    "TaskStatusResponse",
    # === 召回 ===
    "RecallRequest",
    "RecallResponse",
    # === 更新 ===
    "UpdateRequest",
    "UpdateResponse",
    # === 删除 ===
    "DeleteRequest",
    "DeleteResponse",
    "BatchDeleteRequest",
    "BatchDeleteResponse",
    # === 获取/列出 ===
    "GetRequest",
    "GetResponse",
    "ListRequest",
    "ListResponse",
    # === 用户画像 ===
    "UserProfile",
    "GetProfileRequest",
    "GetProfileResponse",
    "UpdateProfileRequest",
    "UpdateProfileResponse",
    "RebuildProfileRequest",
    "RebuildProfileResponse",
    # === 异步任务 ===
    "SubmitTaskRequest",
    "SubmitTaskResponse",
    "GetTaskRequest",
    "GetTaskResponse",
    "CancelTaskRequest",
    "CancelTaskResponse",
    "ListTasksRequest",
    "ListTasksResponse",
]
