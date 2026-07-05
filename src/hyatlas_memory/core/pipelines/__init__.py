"""
HY Memory - Pipelines

pipelines/
├── base.py                # WritePipeline / ReadPipeline 抽象基类 + 统一协议
├── registry.py            # ComponentFactory — 组件工厂
├── writer.py              # MemoryWriter (System 1 写入)
├── reader.py              # MemoryReader (dispatcher → legacy/hybrid_tag/hybrid_v2/tencent_hybrid/mem0)
├── reader_legacy.py       # LegacyReadPipeline
├── reader_hybrid_tag.py   # HybridTagReadPipeline
├── system2_writer.py      # System2Writer (ultra mode: System 1 + System 2 调度)
├── system2_agent.py       # System 2 Agent (LLM + Graph tools)
├── system2_tools.py       # System 2 Tool definitions
├── cross_domain_sweeper.py # Cross-domain schema induction
└── _retrieval/            # 检索策略子模块 (BM25, RRF, tag_index, evolution)
"""

from .base import (
    WritePipeline,
    ReadPipeline,
    ChatMessage,
    ToolCall,
    WriteRequest,
    WriteResponse,
    ReadRequest,
    ReadResponse,
    PipelineContext,
)
from .registry import ComponentFactory, PipelineConfig

# 向后兼容别名
PipelineRegistry = ComponentFactory

__all__ = [
    # 抽象基类
    "WritePipeline",
    "ReadPipeline",
    # 统一协议
    "ChatMessage",
    "ToolCall",
    "WriteRequest",
    "WriteResponse",
    "ReadRequest",
    "ReadResponse",
    "PipelineContext",
    # 组件工厂
    "ComponentFactory",
    "PipelineRegistry",  # 向后兼容
    "PipelineConfig",    # 向后兼容
]
