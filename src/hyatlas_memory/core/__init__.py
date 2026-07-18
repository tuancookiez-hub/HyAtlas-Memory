"""
HY Memory - 核心 SDK

工业级智能体记忆系统核心框架。

快速开始:
    export OPENAI_API_KEY="sk-your-key-here"

    from hyatlas_memory.core.client import HyMemoryClient

    # 最简用法 — 只需设置 OPENAI_API_KEY 环境变量
    client = HyMemoryClient(user_id="test_user")
    client.add("用户喜欢科幻电影")
    results = client.search("用户喜欢什么？")
    client.close()

默认配置:
    - embedder: OpenAI text-embedding-3-small (1536 维)
    - llm: OpenAI gpt-4.1-nano
    - vector_store: Chroma 本地嵌入式 (零外部依赖)

    只需设置 OPENAI_API_KEY 环境变量即可运行。

    # 自定义配置
    client = HyMemoryClient.from_config({
        "vector_store": {"provider": "local"},
        "graph_store": {
            "provider": "kuzu",
            "path": "./data/graph",
        },
        "enable_graph": True,
    }, user_id="test_user")

数据隔离 (两级):
    - user_id:  一级 key — 每个用户唯一的记忆库
    - agent_id: 二级 key — 同一用户下不同 Agent 场景的隔离

安装方式:
    pip install hy-memory          # 核心依赖 (含 zvec，开箱即用)
"""

try:
    from hyatlas_memory._version import __version__
except Exception:
    __version__ = "3.0.0"

# ====== 用户级 API（推荐） ======
from .client import HyMemoryClient

# ====== 配置 ======
from .config import MemoryConfig
from .inspector import MemoryInspector

# ====== 数据模型 ======
from .models import (
    AddRequest,
    AddResponse,
    AgentProcessMode,
    AsyncAddResponse,
    BatchDeleteRequest,
    BatchDeleteResponse,
    DeleteRequest,
    DeleteResponse,
    DeleteScope,
    GetRequest,
    GetResponse,
    ListRequest,
    ListResponse,
    MemoryEntry,
    MemoryInputType,
    MemoryLayer,
    MemoryMetadata,
    MemoryNode,
    MemoryScore,
    QAPair,
    RecallRequest,
    RecallResponse,
    TaskStatus,
    UpdateRequest,
    UpdateResponse,
)

# ====== 高级 API（按需使用） ======
from .pipelines import (
    ChatMessage,
    ComponentFactory,
    PipelineConfig,
    ReadPipeline,
    ReadRequest,
    ReadResponse,
    WritePipeline,
    WriteRequest,
    WriteResponse,
)
from .runtime import SharedRuntime

__all__ = [
    "__version__",
    # 用户级 API
    "HyMemoryClient",
    "MemoryInspector",
    "SharedRuntime",
    # 配置
    "MemoryConfig",
    # 高级 API
    "ComponentFactory",
    "PipelineConfig",
    "WritePipeline",
    "ReadPipeline",
    "ChatMessage",
    "WriteRequest",
    "WriteResponse",
    "ReadRequest",
    "ReadResponse",
    # 数据模型
    "MemoryLayer",
    "MemoryNode",
    "MemoryEntry",
    "MemoryMetadata",
    "MemoryScore",
    "MemoryInputType",
    "AgentProcessMode",
    "TaskStatus",
    "DeleteScope",
    "QAPair",
    "AddRequest",
    "AddResponse",
    "AsyncAddResponse",
    "RecallRequest",
    "RecallResponse",
    "UpdateRequest",
    "UpdateResponse",
    "DeleteRequest",
    "DeleteResponse",
    "BatchDeleteRequest",
    "BatchDeleteResponse",
    "GetRequest",
    "GetResponse",
    "ListRequest",
    "ListResponse",
]
