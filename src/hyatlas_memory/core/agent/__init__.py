"""
Agent Memory - 智能层 (Agent Layer)

基于 LLM 的智能处理能力，提供语义理解、信息提取、推理判断等功能。
该层是可选的，关闭后系统仍能正常工作。

包含模块：
- MemAgent: 智能体协调器，统一入口
- Summarizer: Lite+Agent pipeline 摘要生成器 (L3_SUMMARY)
- Abstractor: System 2 全量摘要智能体 (Session摘要/Schema归纳/Profile摘要)
- Extractor: 提取智能体，提取结构化信息 (V2: 轻量提取/深度提取双模式)
- Reflector: 反思智能体，检测冲突和更新 (V2: UpdateType分类/冲突检测/隐式推断)
- EmotionAnalyzer: 情绪分析智能体，valence + arousal 双维标注
- IntentionDetector: 意图检测智能体，前瞻性记忆 + 主动关怀
- LLMProvider: LLM 提供器，统一的模型调用接口
"""

# Lite+Agent pipeline
from .mem_agent import MemAgent, AgentResult, ProcessMode
from .summarizer import Summarizer, SummaryResult
from .extractor import Extractor, ExtractResult
from .reflector import Reflector, ReflectResult, ConflictType, ConflictAction
from .llm_provider import LLMProvider, LLMResponse, LLMConfig, LLMBackend

# V2 Extractor
from .extractor import ExtractMode, V2ExtractResult

# System 2 / Pro Pipeline Abstractor
from .abstractor import Abstractor, SessionSummaryResult, SchemaAbstractResult, ProfileSummaryResult

# V2 Reflector
from .reflector import (
    V2ConflictType,
    UpdateTypeResult,
    ConflictDetectionResult,
    ImplicitSignal,
    ImplicitInferenceResult,
)

# V2 Emotion Analyzer


# V2 Intention Detector
from .intention_detector import (
    IntentionDetector,
    IntentionDetectorConfig,
    DetectedIntention,
    IntentionDetectResult,
    TriggeredIntentionItem,
    IntentionTriggerResult,
)

__all__ = [
    # MemAgent
    "MemAgent",
    "AgentResult",
    "ProcessMode",
    # Summarizer (Lite+Agent)
    "Summarizer",
    "SummaryResult",
    # Abstractor (System 2)
    "Abstractor",
    "SessionSummaryResult",
    "SchemaAbstractResult",
    "ProfileSummaryResult",
    # Extractor (V1)
    "Extractor",
    "ExtractResult",
    # Extractor (V2)
    "ExtractMode",
    "V2ExtractResult",
    # Reflector (V1)
    "Reflector",
    "ReflectResult",
    "ConflictType",
    "ConflictAction",
    # Reflector (V2)
    "V2ConflictType",
    "UpdateTypeResult",
    "ConflictDetectionResult",
    "ImplicitSignal",
    "ImplicitInferenceResult",
    # Emotion Analyzer (V2)
    
    
    
    # Intention Detector (V2)
    "IntentionDetector",
    "IntentionDetectorConfig",
    "DetectedIntention",
    "IntentionDetectResult",
    "TriggeredIntentionItem",
    "IntentionTriggerResult",
    # LLMProvider
    "LLMProvider",
    "LLMResponse",
    "LLMConfig",
    "LLMBackend",
]
