"""BM25-lite for hybrid retrieval and reconcile candidate scoring.

Used by reader_hybrid_tag, reconcile_retrieval, and related paths. Documents
and queries are expected to be preprocessed with ``lemmatize_for_bm25`` where
Chinese text is concerned; ``tokenize`` splits lemmatized or raw text into terms.
"""
from __future__ import annotations

import math
import re
from collections.abc import Sequence

# Align with intent.py: ASCII tokens + CJK segments from jieba/lemmatize.
_TOKEN_RE = re.compile(r"[a-zA-Z0-9]+|[\u4e00-\u9fff]+")

_K1 = 1.5
_B = 0.75


def tokenize(text: str) -> list[str]:
    """Split text into BM25 terms (lowercased ASCII; CJK segments preserved)."""
    if not text or not str(text).strip():
        return []
    raw = str(text).strip()
    if " " in raw:
        terms: list[str] = []
        for chunk in raw.split():
            chunk = chunk.strip()
            if not chunk:
                continue
            if chunk.isascii():
                terms.extend(t.lower() for t in _TOKEN_RE.findall(chunk))
            else:
                terms.extend(_TOKEN_RE.findall(chunk))
        return [t for t in terms if t]
    return [t.lower() if t.isascii() else t for t in _TOKEN_RE.findall(raw)]


def _doc_freqs(doc_tokens: list[list[str]]) -> tuple[list[dict[str, int]], dict[str, int], list[int]]:
    freqs: list[dict[str, int]] = []
    df: dict[str, int] = {}
    lengths: list[int] = []
    for tokens in doc_tokens:
        tf: dict[str, int] = {}
        for t in tokens:
            tf[t] = tf.get(t, 0) + 1
        freqs.append(tf)
        lengths.append(len(tokens))
        for t in tf:
            df[t] = df.get(t, 0) + 1
    return freqs, df, lengths


def compute_bm25_scores(
    query_terms: Sequence[str],
    documents: Sequence[str],
    *,
    k1: float = _K1,
    b: float = _B,
) -> list[float]:
    """Score each document against pre-tokenized query terms (Okapi BM25)."""
    n = len(documents)
    if n == 0:
        return []
    q_terms = [t for t in query_terms if t]
    if not q_terms:
        return [0.0] * n

    doc_tokens = [tokenize(d) if d else [] for d in documents]
    freqs, df, lengths = _doc_freqs(doc_tokens)
    avgdl = sum(lengths) / max(n, 1)
    scores = [0.0] * n

    for i in range(n):
        tf_map = freqs[i]
        dl = lengths[i]
        norm = 1.0 - b + b * (dl / max(avgdl, 1.0))
        for qt in q_terms:
            if qt not in tf_map:
                continue
            tf = tf_map[qt]
            n_qi = df.get(qt, 0)
            idf = math.log(1.0 + (n - n_qi + 0.5) / (n_qi + 0.5))
            scores[i] += idf * (tf * (k1 + 1.0)) / (tf + k1 * norm)
    return scores


class BM25Scorer:
    """In-memory BM25 scorer for a fixed corpus (one-shot reconcile helper)."""

    def __init__(self, k1: float = _K1, b: float = _B):
        self.k1 = k1
        self.b = b
        self._documents: list[str] = []

    def fit(self, documents: list[str]) -> None:
        self._documents = list(documents)

    def score(self, query: str) -> list[float]:
        q_terms = tokenize(query)
        return compute_bm25_scores(q_terms, self._documents, k1=self.k1, b=self.b)

    def top_k(self, query: str, k: int = 5) -> list[tuple[int, float]]:
        scores = self.score(query)
        ranked = sorted(enumerate(scores), key=lambda x: -x[1])
        return ranked[:k]


def score_candidates(
    query: str,
    candidates: list[str],
    k1: float = _K1,
    b: float = _B,
) -> list[float]:
    scorer = BM25Scorer(k1=k1, b=b)
    scorer.fit(candidates)
    return scorer.score(query)
