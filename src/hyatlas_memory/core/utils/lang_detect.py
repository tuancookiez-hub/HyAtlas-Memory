"""
Language detection utility for prompt selection.

Uses a two-tier strategy:
1. Fast path: Unicode character-range heuristic (CJK ideographs → zh)
2. Fallback: langdetect (for non-CJK ambiguous cases)

This avoids langdetect's known weakness with short Chinese text.
"""

import logging
import re
from typing import List, Dict, Optional, Union

logger = logging.getLogger(__name__)

# CJK Unified Ideographs ranges
_CJK_RE = re.compile(
    r'[\u4e00-\u9fff'        # CJK Unified Ideographs
    r'\u3400-\u4dbf'         # CJK Extension A
    r'\u2e80-\u2eff'         # CJK Radicals Supplement
    r'\uf900-\ufaff'         # CJK Compatibility Ideographs
    r'\ufe30-\ufe4f'         # CJK Compatibility Forms
    r'\U00020000-\U0002a6df' # CJK Extension B
    r']'
)

# Lazy-load langdetect
_langdetect_initialized = False


def _ensure_langdetect():
    global _langdetect_initialized
    if _langdetect_initialized:
        return True
    try:
        from langdetect import DetectorFactory
        DetectorFactory.seed = 0
        _langdetect_initialized = True
        return True
    except ImportError:
        logger.warning(
            "langdetect not installed. Falling back to heuristic only. "
            "Install with: pip install langdetect"
        )
        return False


def _chinese_char_ratio(text: str) -> float:
    """Return the ratio of CJK characters in text."""
    if not text:
        return 0.0
    # Only count non-whitespace, non-punctuation chars
    chars = re.sub(r'\s+', '', text)
    if not chars:
        return 0.0
    cjk_count = len(_CJK_RE.findall(chars))
    return cjk_count / len(chars)


def detect_language(text: str) -> str:
    """
    Detect the language of text content.

    Strategy:
    1. If CJK character ratio >= 0.3 → "zh" (fast, reliable)
    2. If CJK ratio == 0 and text is ASCII-heavy → "en" (fast)
    3. Otherwise → langdetect fallback

    Returns:
        "zh" for Chinese, "en" for English, or other ISO 639-1 codes.
        Returns "en" as fallback if detection fails.
    """
    if not text or not text.strip():
        return "en"

    text = text.strip()

    # Fast path: CJK character ratio
    ratio = _chinese_char_ratio(text)
    if ratio >= 0.3:
        return "zh"
    if ratio == 0:
        # No CJK chars at all — likely English or other Latin-based
        # Use langdetect for accuracy if available, else default "en"
        if _ensure_langdetect():
            try:
                from langdetect import detect
                lang = detect(text)
                if lang.startswith("zh"):
                    return "zh"
                return lang
            except Exception:
                pass
        return "en"

    # Mixed content (0 < ratio < 0.3): use langdetect
    if _ensure_langdetect():
        try:
            from langdetect import detect
            lang = detect(text)
            if lang.startswith("zh"):
                return "zh"
            return lang
        except Exception:
            pass

    # If langdetect unavailable and some CJK present, lean toward zh
    return "zh" if ratio > 0.1 else "en"


def extract_content_for_detection(
    content: str = "",
    messages: Optional[List] = None,
) -> str:
    """
    Extract pure content text for language detection.

    For messages, only extracts the content field (not role/metadata)
    to avoid 'user', 'assistant' etc. biasing the detection toward English.

    Args:
        content: plain text content
        messages: list of {"role": ..., "content": ...} dicts or ChatMessage objects

    Returns:
        Concatenated content text suitable for language detection.
    """
    if content and content.strip():
        return content.strip()

    if messages:
        parts = []
        for msg in messages:
            if isinstance(msg, dict):
                c = msg.get("content", "")
            elif hasattr(msg, "content"):
                c = msg.content
            else:
                continue
            if c and c.strip():
                parts.append(c.strip())
        return "\n".join(parts)

    return ""


def is_chinese(
    content: str = "",
    messages: Optional[List] = None,
) -> bool:
    """
    Quick check: is the input content Chinese?

    Args:
        content: plain text content
        messages: optional messages list (only content fields are used)

    Returns:
        True if detected language is Chinese.
    """
    sample = extract_content_for_detection(content, messages)
    if not sample:
        return False
    return detect_language(sample) == "zh"
