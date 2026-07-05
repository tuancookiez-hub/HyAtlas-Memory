"""
Agent Memory V2 - 数据模型

核心数据结构，包含 V2 新模型和 V1 兼容类型。
"""

# === V2 核心模型 ===
from .memory import (
    # 枚举类型
    MemoryLayer,
    ContentType,
    MemoryStatus,
    SourceType,
    UpdateType,
    SchemaStatus,
    TriggerType,
    IntentionPriority,
    MetaCognitionTag,
    GapType,
    # 核心节点
    MemoryNode,
    VersionedFact,
    SchemaNode,
    IntentionNode,
    # 辅助模型
    KnowledgeGap,
    LifeStage,
    TemporalEvent,
    UserTimeline,
    # 输出协议
    ProfileSummary,
    MemoryIndexEntry,
    MemorySummaryEntry,
    MetaCognitionReport,
    TriggeredIntention,
    ActivatedSchema,
    TemporalContext,
    MemoryContextPackage,
    # V1 兼容
    MemoryEntry,
    MemoryMetadata,
    MemoryScore,
)

# === 请求/响应模型 ===
from .requests import (
    # 枚举
    DeleteScope,
    MemoryInputType,
    TaskStatus,
    AgentProcessMode,
    # QA 对
    QAPair,
    # 添加记忆
    AddRequest,
    AddResponse,
    AsyncAddResponse,
    AsyncTask,
    TaskStatusRequest,
    TaskStatusResponse,
    # 召回记忆
    RecallRequest,
    RecallResponse,
    # 更新记忆
    UpdateRequest,
    UpdateResponse,
    # 删除记忆
    DeleteRequest,
    DeleteResponse,
    BatchDeleteRequest,
    BatchDeleteResponse,
    # 获取/列出记忆
    GetRequest,
    GetResponse,
    ListRequest,
    ListResponse,
    # 用户画像
    UserProfile,
    GetProfileRequest,
    GetProfileResponse,
    UpdateProfileRequest,
    UpdateProfileResponse,
    RebuildProfileRequest,
    RebuildProfileResponse,
    # 异步任务管理
    SubmitTaskRequest,
    SubmitTaskResponse,
    GetTaskRequest,
    GetTaskResponse,
    CancelTaskRequest,
    CancelTaskResponse,
    ListTasksRequest,
    ListTasksResponse,
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
