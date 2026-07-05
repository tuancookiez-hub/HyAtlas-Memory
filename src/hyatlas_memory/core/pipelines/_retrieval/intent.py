"""
意图分类 + query keyword 提取。

设计参考 OMEGA v1.4.9 的 `_is_keyword_sufficient` / 意图检测逻辑（见
docs/write-read-pipeline-analysis.md §4.15）。

三种意图：
- NAVIGATIONAL：query 含精确标识符（CamelCase/snake_case/引号短语/路径/ID/URL
  等），应关掉向量通道、加重 BM25
- CONCEPTUAL：query 问倾向/模式/态度（how/why/explain/tend to/怎么/为什么 等），
  应加重 tag 通道和向量通道
- FACTUAL：其余默认，向量主导 + tag 次之

正则只识别 NAVIGATIONAL 高置信度的字面信号，其余回退 CONCEPTUAL / FACTUAL 的
关键词触发表。FACTUAL 与 CONCEPTUAL 分错的权重差异很小，对结果影响不大；
NAVIGATIONAL 分错的代价较高（应字面匹配却被向量稀释），因此 NAV 检测写得严。
"""

import re
from typing import List

from . import config


# ─── NAVIGATIONAL patterns ───

_NAV_PATTERNS = [
    r"`[^`]+`",
    r'"[^"]{2,}"',
    r"'[^']{2,}'",
    r"[/\\][\w.\-]+[/\\]",
    r"\b[a-z][a-z0-9]*_[a-z0-9_]+\b",
    r"\b[A-Za-z][a-z0-9]+[A-Z][A-Za-z0-9]*\b",
    r"\bmem-[a-f0-9]{8,}\b",
    r"\b[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}\b",
    r"\bv?\d+\.\d+(?:\.\d+)?\b",
    r"\b[a-f0-9]{16,}\b",
    r"https?://\S+",
]

_NAV_REGEX = re.compile("|".join(_NAV_PATTERNS))


# ─── CONCEPTUAL triggers ───

_CONCEPTUAL_TRIGGERS = {
    "how", "why", "explain", "approach", "strategy", "tend", "overall",
    "architecture", "design", "philosophy", "pattern", "style",
    "in general", "generally",
    "怎么", "为什么", "如何", "倾向", "风格", "整体", "一般", "通常",
    "总体", "模式",
}


# ─── Keyword extraction stopwords ───

_STOPWORDS_EN = {
    "the", "a", "an", "is", "are", "was", "were", "be", "been", "being",
    "do", "does", "did", "have", "has", "had", "having",
    "my", "mine", "your", "yours", "our", "ours", "their", "theirs",
    "i", "you", "we", "they", "he", "she", "it", "me", "him", "her", "us", "them",
    "this", "that", "these", "those",
    "what", "when", "where", "who", "whom", "how", "why", "which",
    "and", "or", "but", "not", "no", "nor",
    "for", "to", "of", "in", "on", "at", "by", "from", "with", "about",
    "as", "if", "then", "than", "so", "too", "very",
    "can", "could", "would", "should", "may", "might", "will", "shall",
    "am",
}


def is_navigational(query: str) -> bool:
    return bool(_NAV_REGEX.search(query or ""))


def is_conceptual(query: str) -> bool:
    if not query:
        return False
    q = query.lower()
    return any(t in q for t in _CONCEPTUAL_TRIGGERS)


def classify_intent(query: str) -> str:
    if config.INTENT_OVERRIDE in ("NAVIGATIONAL", "FACTUAL", "CONCEPTUAL"):
        return config.INTENT_OVERRIDE
    if is_navigational(query):
        return "NAVIGATIONAL"
    if is_conceptual(query):
        return "CONCEPTUAL"
    return "FACTUAL"


_TOKEN_RE = re.compile(r"[a-zA-Z0-9]+|[\u4e00-\u9fff]+")


def extract_keywords(query: str) -> List[str]:
    if not query:
        return []
    raw = _TOKEN_RE.findall(query)
    seen = set()
    result: List[str] = []
    for tok in raw:
        t = tok.lower() if tok.isascii() else tok
        if tok.isascii():
            if len(t) < 3 or t in _STOPWORDS_EN:
                continue
        else:
            if len(t) < 2:
                continue
        if t in seen:
            continue
        seen.add(t)
        result.append(t)
        if len(result) >= config.KEYWORD_MAX_COUNT:
            break
    return result
