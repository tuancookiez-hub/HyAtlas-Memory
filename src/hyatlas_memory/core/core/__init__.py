"""
Agent Memory - 核心层 (Core Layer)

核心组件：
- Scorer: 评分器，计算记忆综合评分
- Merger: 合并器，检测重复记忆并合并
- EmbedService: 向量化服务，文本转向量
"""

from .scorer import MemoryScorer as Scorer
from .merger import Merger, MergeResult, MergerConfig
from .embed_service import EmbedService

__all__ = [
    "Scorer",
    "Merger",
    "MergeResult",
    "MergerConfig",
    "EmbedService",
]
