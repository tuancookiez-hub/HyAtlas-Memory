"""Lightweight in-memory BM25 for reconcile retrieval.

This is the classic BM25 implementation used by the reconciler when
doing candidate scoring during write-time dedup. It does NOT require
fastembed or Qdrant sparse vectors — it's a pure Python in-memory scorer.
"""
from __future__ import annotations

import math
import re
from typing import Dict, List, Set, Tuple


def _tokenize(text: str) -> List[str]:
    """Simple whitespace + punctuation tokenizer."""
    return re.findall(r"\w+", text.lower())


class BM25Scorer:
    """In-memory BM25 scorer for a fixed corpus of documents."""

    def __init__(self, k1: float = 1.5, b: float = 0.75):
        self.k1 = k1
        self.b = b
        self._docs: List[List[str]] = []
        self._doc_freqs: List[Dict[str, int]] = []
        self._doc_len: List[int] = []
        self._avgdl: float = 0.0
        self._df: Dict[str, int] = {}
        self._n: int = 0

    def fit(self, documents: List[str]) -> None:
        """Index a corpus of documents."""
        self._docs = []
        self._doc_freqs = []
        self._doc_len = []
        self._df = {}
        for doc in documents:
            tokens = _tokenize(doc)
            self._docs.append(tokens)
            freq: Dict[str, int] = {}
            for t in tokens:
                freq[t] = freq.get(t, 0) + 1
            self._doc_freqs.append(freq)
            self._doc_len.append(len(tokens))
            for t in freq:
                self._df[t] = self._df.get(t, 0) + 1
        self._n = len(documents)
        self._avgdl = sum(self._doc_len) / max(self._n, 1)

    def score(self, query: str) -> List[float]:
        """Score all documents against a query. Returns list of scores."""
        query_tokens = _tokenize(query)
        scores = [0.0] * self._n
        for i in range(self._n):
            doc_len = self._doc_len[i]
            freq = self._doc_freqs[i]
            for qt in query_tokens:
                if qt not in freq:
                    continue
                tf = freq[qt]
                df = self._df.get(qt, 0)
                idf = math.log(1 + (self._n - df + 0.5) / (df + 0.5))
                norm = 1 - self.b + self.b * (doc_len / max(self._avgdl, 1))
                scores[i] += idf * (tf * (self.k1 + 1)) / (tf + self.k1 * norm)
        return scores

    def top_k(self, query: str, k: int = 5) -> List[Tuple[int, float]]:
        """Return top-k (index, score) pairs."""
        scores = self.score(query)
        ranked = sorted(enumerate(scores), key=lambda x: -x[1])
        return ranked[:k]


def score_candidates(
    query: str,
    candidates: List[str],
    k1: float = 1.5,
    b: float = 0.75,
) -> List[float]:
    """One-shot BM25 scoring: score each candidate against the query.

    Returns a list of BM25 scores, one per candidate.
    """
    scorer = BM25Scorer(k1=k1, b=b)
    scorer.fit(candidates)
    return scorer.score(query)
