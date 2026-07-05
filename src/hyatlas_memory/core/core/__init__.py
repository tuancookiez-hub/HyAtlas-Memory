"""
Agent Memory - 核心层 (Core Layer)

核心组件：
- Scorer: 评分器，计算记忆综合评分
- Merger: 合并器，检测重复记忆并合并
- EmbedService: 向量化服务，文本转向量
"""

from .embed_service import EmbedService
from .merger import Merger, MergerConfig, MergeResult
from .scorer import MemoryScorer as Scorer

__all__ = [
    "Scorer",
    "Merger",
    "MergeResult",
    "MergerConfig",
    "EmbedService",
]
