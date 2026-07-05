"""
HY Memory - Summarizer (Lite+Agent Pipeline)

负责 agent 写入流程中的 L3_SUMMARY 生成。
仅供 MemAgent / MemoryWriter 调用。

System 2 pro pipeline 相关的摘要能力（SessionSummary / Schema / Profile）
保留在 abstractor.py 中。
"""

from typing import Dict, Any, Optional, List
from dataclasses import dataclass
import logging

from .llm_provider import LLMProvider
from ..config import LLMConfig as GlobalLLMConfig

logger = logging.getLogger(__name__)


# ================================================================
# 数据结构
# ================================================================

@dataclass
class SummaryResult:
    """摘要结果"""
    success: bool
    summary: Optional[str] = None          # 摘要文本，直接用于存储
    source_raw_memory_id: Optional[str] = None  # 对应的 L1_RAW 节点 ID（锚点）
    tokens_used: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    error: Optional[str] = None
    _actual_prompt: Optional[str] = None   # 实际发送给 LLM 的 prompt


# ================================================================
# Prompt
# ================================================================

SUMMARY_PROMPT = """Generate a concise summary of the following conversation content.

Content:
---
{content}
---

Memory date: {memory_date}
Current date: {current_date}

Requirements:
1. **Third-person voice**: Describe the user as "The user ..." — do not use pronouns without clear antecedent.
2. **Length**: 1-3 sentences, max 200 words.
3. **Priorities (when content exceeds the length budget)**:
   a) Changes, decisions, commitments (highest)
   b) Explicit preferences, attitudes, dislikes/likes
   c) Key events and facts
   d) Background context (lowest)
4. **Preserve preference signals**: Retain any expression of likes, dislikes, attitudes, or opinions — direct or implied — even if minor.
5. **Self-contained**: A reader should understand the summary without seeing the original conversation.
6. **No fabrication**: Do NOT add information not present in the original content.
7. **Language**: Output language MUST match input language (English → English, Chinese → Chinese).
8. **Time handling**:
   - **Memory date** is when the conversation actually took place. This is your ONLY temporal anchor for resolving relative time references in the conversation.
   - **Current date** is today's system date (may be years after Memory date). Do NOT use this to interpret user statements.
   - If Memory date is provided, resolve relative expressions against it ("last week" → the corresponding absolute date).
   - If Memory date is empty, rewrite sentences atemporally (avoid leaving raw "last week" / "yesterday" in the output).

## Output contract

Strict formatting rules:
1. Output the summary text ONLY — one paragraph of 1-3 sentences, plain prose.
2. Do NOT wrap the output in quotes, backticks, code fences, or any markdown.
3. Do NOT add a prefix or label (no "Summary:", "摘要：", "Here is ...", etc.).
4. Do NOT add trailing explanations or meta-commentary.
5. If the content is too trivial to summarize meaningfully, output a single sentence describing the most salient element — do not output an empty string.

Now produce the summary."""


# ================================================================
# Summarizer
# ================================================================

class Summarizer:
    """
    Lite+Agent Pipeline 摘要生成器。

    生成 L3_SUMMARY 层的会话摘要，直接存入向量库。
    摘要文本即 LLM 原始输出（已 strip），不加任何前缀/包装。
    """

    MIN_CONTENT_LENGTH = 500  # 低于此长度不生成摘要

    def __init__(
        self,
        llm_provider: LLMProvider,
        llm_config: Optional[GlobalLLMConfig] = None,
    ):
        self.llm = llm_provider
        self._llm_config = llm_config or GlobalLLMConfig()
        self._call_count = 0
        self._total_tokens = 0
        logger.debug("Summarizer initialized")

    async def summarize(
        self,
        content: str,
        current_time: str = "",
        source_raw_memory_id: Optional[str] = None,  # 对应 L1_RAW 节点 ID
    ) -> SummaryResult:
        """
        生成摘要。

        Args:
            content: 原始对话内容
            current_time: 记忆发生时间（ISO 格式字符串），不传则为空字符串
            source_raw_memory_id: 对应的 L1_RAW 节点 ID，存入 L3_SUMMARY 节点作为锚点

        Returns:
            SummaryResult.summary: 摘要文本（LLM 原始输出 strip 后），直接用于存入向量库
        """
        if len(content) < self.MIN_CONTENT_LENGTH:
            return SummaryResult(success=True, summary=None)

        try:
            # 构建日期字段（精确到日）
            from datetime import datetime as _dt_cls, date as _date_cls
            _current_date = _date_cls.today().isoformat()  # e.g. "2026-05-23"
            # memory_date: 转为日精度
            if current_time:
                try:
                    _memory_date = _dt_cls.fromisoformat(current_time).strftime("%Y-%m-%d")
                except (ValueError, TypeError):
                    _memory_date = _current_date
            else:
                _memory_date = _current_date

            # Select prompt based on input language
            from ..utils.lang_detect import is_chinese
            if is_chinese(content):
                from .prompts_zh import SUMMARY_PROMPT_ZH
                prompt = SUMMARY_PROMPT_ZH.format(
                    content=content,
                    memory_date=_memory_date,
                    current_date=_current_date,
                )
            else:
                prompt = SUMMARY_PROMPT.format(
                    content=content,
                    memory_date=_memory_date,
                    current_date=_current_date,
                )
            response = await self.llm.complete(
                prompt=prompt,
                max_tokens=self._llm_config.agent_max_tokens,
                temperature=self._llm_config.temperature,
            )
            self._call_count += 1
            self._total_tokens += response.tokens_used

            # 只做最小处理：去首尾空白。不剥离前缀、不加前缀。
            summary = response.content.strip()

            return SummaryResult(
                success=True,
                summary=summary,
                source_raw_memory_id=source_raw_memory_id,
                tokens_used=response.tokens_used,
                prompt_tokens=response.prompt_tokens,
                completion_tokens=response.completion_tokens,
                _actual_prompt=prompt,
            )

        except Exception as e:
            logger.error(f"Summarizer.summarize failed: {e}")
            return SummaryResult(success=False, error=str(e))

    def get_stats(self) -> Dict[str, Any]:
        return {
            "call_count": self._call_count,
            "total_tokens": self._total_tokens,
            "avg_tokens_per_call": (
                self._total_tokens / self._call_count
                if self._call_count > 0 else 0
            ),
        }
