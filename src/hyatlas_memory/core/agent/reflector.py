"""
Agent Memory V2 - Reflector 反思智能体 (增强版)

V2 设计文档:

  反思智能体负责:
  1. UpdateType 分类: 判断新信息与已有信息的关系 (OVERRIDE/SUPPLEMENT/TEMPORAL/NEGATE/CONFLICT)
  2. 冲突检测:       识别矛盾、时间变化、粒度不匹配等冲突类型
  3. 隐式推断:       从行为/态度/选择信号中推断用户属性

  保留 V1 接口 (check_conflicts, suggest_merge)
  新增 V2 接口 (classify_update_type, detect_conflicts_v2, extract_implicit_signals)
"""

from typing import Dict, Any, Optional, List
from dataclasses import dataclass, field
from enum import Enum
import json
import re
import logging

from .llm_provider import LLMProvider
from ..config import LLMConfig as GlobalLLMConfig

logger = logging.getLogger(__name__)


# ================================================================
# 枚举与数据结构
# ================================================================

class ConflictType(Enum):
    """冲突类型 (V1 兼容)"""
    CONTRADICTION = "contradiction"
    UPDATE = "update"
    DUPLICATE = "duplicate"
    RELATED = "related"


class ConflictAction(Enum):
    """冲突处理建议 (V1 兼容)"""
    KEEP_NEW = "keep_new"
    KEEP_OLD = "keep_old"
    MERGE = "merge"
    BOTH = "both"


class V2ConflictType(str, Enum):
    """V2 冲突类型"""
    TEMPORAL_CHANGE = "temporal_change"
    CONTEXT_DEPENDENT = "context_dependent"
    CONTRADICTION = "contradiction"
    GRANULARITY_MISMATCH = "granularity_mismatch"


@dataclass
class ReflectResult:
    """反思结果 (V1 兼容)"""
    success: bool
    conflicts: List[Dict[str, Any]] = field(default_factory=list)
    should_merge: bool = False
    merge_target_id: Optional[str] = None
    merged_content: Optional[str] = None
    tokens_used: int = 0
    error: Optional[str] = None


@dataclass
class UpdateTypeResult:
    """UpdateType 分类结果 (V2)"""
    success: bool
    update_type: str = ""  # OVERRIDE/SUPPLEMENT/TEMPORAL/NEGATE/CONFLICT
    target_fact_id: Optional[str] = None
    confidence: float = 0.0
    reasoning: str = ""
    temporal_scope: Optional[str] = None
    tokens_used: int = 0
    error: Optional[str] = None


@dataclass
class ConflictDetectionResult:
    """冲突检测结果 (V2)"""
    success: bool
    conflicts: List[Dict[str, Any]] = field(default_factory=list)
    tokens_used: int = 0
    error: Optional[str] = None


@dataclass
class ImplicitSignal:
    """隐式信号"""
    signal_type: str = ""   # behavior / attitude / choice
    content: str = ""
    possible_traits: List[str] = field(default_factory=list)
    strength: str = "weak"  # weak / moderate / strong


@dataclass
class ImplicitInferenceResult:
    """隐式推断结果 (V2)"""
    success: bool
    signals: List[ImplicitSignal] = field(default_factory=list)
    tokens_used: int = 0
    error: Optional[str] = None


# ================================================================
# Prompt 模板
# ================================================================

UPDATE_TYPE_PROMPT = """分析新信息与已有信息的关系，判断更新类型。

新信息:
{new_fact}

已有的相关信息:
{existing_facts}

判断更新类型:
- OVERRIDE: 明确替代（年龄变化、搬家、换工作）
- SUPPLEMENT: 补充（新增爱好、新的经历）
- TEMPORAL: 临时性变化（"今天想吃..."、"最近在..."）
- NEGATE: 否定旧信息（"不再喜欢..."、"已经离开..."）
- CONFLICT: 无法判断的矛盾（需要人工/LLM进一步确认）

输出JSON:
{{
  "update_type": "类型",
  "target_fact_id": "被影响的旧信息ID(如有,否则null)",
  "confidence": 0.0-1.0,
  "reasoning": "判断理由",
  "temporal_scope": "TEMPORAL时的有效范围(如有,否则null)"
}}"""

CONFLICT_DETECT_PROMPT = """检测以下新信息与已有记忆之间的冲突。

新信息:
{new_content}

已有记忆:
{existing_memories}

冲突类型说明:
- TEMPORAL_CHANGE: 用户状态随时间真实变化 (搬家、换工作)
- CONTEXT_DEPENDENT: 不同场景下的不同表现 (工作时严肃，朋友间活泼)
- CONTRADICTION: 逻辑矛盾 (同一时间的矛盾陈述)
- GRANULARITY_MISMATCH: 粒度不同但可共存 ("喜欢水果" vs "喜欢苹果")

输出JSON:
{{
  "conflicts": [
    {{
      "existing_memory_id": "冲突记忆的ID",
      "conflict_type": "TEMPORAL_CHANGE/CONTEXT_DEPENDENT/CONTRADICTION/GRANULARITY_MISMATCH",
      "description": "冲突描述",
      "severity": "high/medium/low",
      "suggested_action": "override/contextualize/both_valid/flag_for_review"
    }}
  ]
}}

如无冲突返回: {{"conflicts": []}}"""

IMPLICIT_INFERENCE_PROMPT = """分析以下对话内容中的隐式信号，推断用户可能的属性或偏好。

对话内容:
---
{content}
---

隐式信号类型:
- behavior: 行为信号 (如 "每天跑步" → 自律/健康意识)
- attitude: 态度信号 (如 "这个太贵了" → 价格敏感)
- choice: 选择信号 (如 "还是选简约风格" → 简约主义)

输出JSON:
{{
  "signals": [
    {{
      "signal_type": "behavior/attitude/choice",
      "content": "原始信号内容",
      "possible_traits": ["可能推断出的特质1", "特质2"],
      "strength": "weak/moderate/strong"
    }}
  ]
}}

只输出有依据的推断，不要猜测。如无信号返回: {{"signals": []}}"""


# ================================================================
# 核心实现
# ================================================================

class Reflector:
    """
    反思智能体 (V2)

    保留 V1 冲突检测 + 合并建议能力，
    新增 V2 的 UpdateType 分类、V2 冲突检测、隐式推断。
    """

    # V1 Prompt
    CONFLICT_CHECK_PROMPT = """请分析新记忆与已有记忆之间是否存在冲突。

新记忆：
{new_content}

已有记忆：
{existing_memories}

请以 JSON 格式输出分析结果：
{{
    "conflicts": [
        {{
            "memory_id": "冲突的记忆ID",
            "conflict_type": "contradiction/update/duplicate/related",
            "description": "冲突描述",
            "suggestion": "keep_new/keep_old/merge/both"
        }}
    ],
    "should_merge": true/false,
    "merge_target_id": "建议合并到的记忆ID（如果需要合并）",
    "merged_content": "合并后的内容（如果需要合并）"
}}

只输出 JSON，不要其他解释："""

    def __init__(
        self,
        llm_provider: LLMProvider,
        llm_config: Optional[GlobalLLMConfig] = None,
    ):
        self.llm = llm_provider
        self._llm_config = llm_config or GlobalLLMConfig()
        self._call_count = 0
        self._total_tokens = 0
        logger.info("Reflector V2 initialized")

    # ================================================================
    # V1 兼容接口
    # ================================================================

    async def check_conflicts(
        self,
        new_content: str,
        existing_memories: List[Dict[str, Any]],
        context: Dict[str, Any] = None,
    ) -> ReflectResult:
        """V1 兼容: 检测冲突"""
        try:
            if not existing_memories:
                return ReflectResult(success=True)

            memories_text = "\n".join([
                f"[{m.get('id', 'unknown')}] {m.get('content', '')}"
                for m in existing_memories
            ])

            prompt = self.CONFLICT_CHECK_PROMPT.format(
                new_content=new_content, existing_memories=memories_text,
            )
            response = await self.llm.complete(
                prompt=prompt,
                max_tokens=self._llm_config.agent_max_tokens,
                temperature=0.1,
            )
            self._call_count += 1
            self._total_tokens += response.tokens_used

            data = self._parse_json(response.content)

            return ReflectResult(
                success=True,
                conflicts=data.get("conflicts", []),
                should_merge=data.get("should_merge", False),
                merge_target_id=data.get("merge_target_id"),
                merged_content=data.get("merged_content"),
                tokens_used=response.tokens_used,
            )
        except Exception as e:
            logger.error(f"Reflector.check_conflicts failed: {e}")
            return ReflectResult(success=False, error=str(e))

    async def suggest_merge(
        self, memory1: Dict[str, Any], memory2: Dict[str, Any],
    ) -> Optional[str]:
        """V1 兼容: 生成合并建议"""
        prompt = f"""请将以下两条记忆合并为一条，保留所有有用信息，解决冲突。

记忆1：
{memory1.get('content', '')}

记忆2：
{memory2.get('content', '')}

请直接输出合并后的内容："""

        try:
            response = await self.llm.complete(
                prompt=prompt,
                max_tokens=self._llm_config.agent_max_tokens,
                temperature=0.3,
            )
            self._call_count += 1
            self._total_tokens += response.tokens_used
            return response.content.strip()
        except Exception as e:
            logger.error(f"suggest_merge failed: {e}")
            return None

    # ================================================================
    # V2 UpdateType 分类
    # ================================================================

    async def classify_update_type(
        self,
        new_fact: str,
        existing_facts: List[Dict[str, Any]],
    ) -> UpdateTypeResult:
        """
        判断新事实与已有事实的更新关系。
        由 System 2 Deep Extraction / Write Path 调用。
        """
        try:
            facts_text = "\n".join([
                f"[{f.get('id', '?')}] {f.get('content', '')} "
                f"(时间: {f.get('created_at', '?')}, 置信度: {f.get('confidence', '?')})"
                for f in existing_facts
            ])

            prompt = UPDATE_TYPE_PROMPT.format(
                new_fact=new_fact,
                existing_facts=facts_text or "(无已有信息)",
            )
            response = await self.llm.complete(
                prompt=prompt,
                max_tokens=self._llm_config.agent_max_tokens,
                temperature=0.1,
            )
            self._call_count += 1
            self._total_tokens += response.tokens_used

            data = self._parse_json(response.content)

            return UpdateTypeResult(
                success=True,
                update_type=data.get("update_type", "SUPPLEMENT"),
                target_fact_id=data.get("target_fact_id"),
                confidence=float(data.get("confidence", 0.5)),
                reasoning=data.get("reasoning", ""),
                temporal_scope=data.get("temporal_scope"),
                tokens_used=response.tokens_used,
            )
        except Exception as e:
            logger.error(f"classify_update_type failed: {e}")
            return UpdateTypeResult(success=False, error=str(e))

    # ================================================================
    # V2 冲突检测
    # ================================================================

    async def detect_conflicts_v2(
        self,
        new_content: str,
        existing_memories: List[Dict[str, Any]],
    ) -> ConflictDetectionResult:
        """
        V2 冲突检测: 区分四种冲突类型。
        """
        try:
            if not existing_memories:
                return ConflictDetectionResult(success=True)

            memories_text = "\n".join([
                f"[{m.get('id', '?')}] {m.get('content', '')} "
                f"(时间: {m.get('created_at', '?')})"
                for m in existing_memories
            ])

            prompt = CONFLICT_DETECT_PROMPT.format(
                new_content=new_content,
                existing_memories=memories_text,
            )
            response = await self.llm.complete(
                prompt=prompt,
                max_tokens=self._llm_config.agent_max_tokens,
                temperature=0.1,
            )
            self._call_count += 1
            self._total_tokens += response.tokens_used

            data = self._parse_json(response.content)

            return ConflictDetectionResult(
                success=True,
                conflicts=data.get("conflicts", []),
                tokens_used=response.tokens_used,
            )
        except Exception as e:
            logger.error(f"detect_conflicts_v2 failed: {e}")
            return ConflictDetectionResult(success=False, error=str(e))

    # ================================================================
    # V2 隐式推断
    # ================================================================

    async def extract_implicit_signals(
        self,
        content: str,
    ) -> ImplicitInferenceResult:
        """
        从对话内容中提取隐式信号。
        由 System 2 Trait Inferencer 调用。
        """
        try:
            prompt = IMPLICIT_INFERENCE_PROMPT.format(content=content)

            response = await self.llm.complete(
                prompt=prompt,
                max_tokens=self._llm_config.agent_max_tokens,
                temperature=0.3,
            )
            self._call_count += 1
            self._total_tokens += response.tokens_used

            data = self._parse_json(response.content)

            signals = []
            for s in data.get("signals", []):
                signals.append(ImplicitSignal(
                    signal_type=s.get("signal_type", ""),
                    content=s.get("content", ""),
                    possible_traits=s.get("possible_traits", []),
                    strength=s.get("strength", "weak"),
                ))

            return ImplicitInferenceResult(
                success=True,
                signals=signals,
                tokens_used=response.tokens_used,
            )
        except Exception as e:
            logger.error(f"extract_implicit_signals failed: {e}")
            return ImplicitInferenceResult(success=False, error=str(e))

    # ================================================================
    # 工具方法
    # ================================================================

    @staticmethod
    def _parse_json(text: str) -> Dict[str, Any]:
        """从 LLM 输出中解析 JSON"""
        text = text.strip()
        if "```json" in text:
            text = text.split("```json")[1].split("```")[0].strip()
        elif "```" in text:
            text = text.split("```")[1].split("```")[0].strip()
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            match = re.search(r'\{[\s\S]*\}', text)
            if match:
                try:
                    return json.loads(match.group())
                except json.JSONDecodeError:
                    pass
        return {"conflicts": [], "should_merge": False}

    def get_stats(self) -> Dict[str, Any]:
        """获取统计信息"""
        return {
            "call_count": self._call_count,
            "total_tokens": self._total_tokens,
            "avg_tokens_per_call": (
                self._total_tokens / self._call_count
                if self._call_count > 0 else 0
            ),
        }
