"""
Scoring utilities for Hybrid Reader V2.

Pure functions for:
- BM25 score normalization (sigmoid)
- Evidence boost computation
- VDB node scoring (semantic + BM25)
"""

import math


def normalize_bm25(
    raw_score: float,
    midpoint: float = 8.0,
    steepness: float = 0.5,
) -> float:
    """
    Sigmoid normalization: raw BM25 score → [0, 1].

    Uses logistic function: 1 / (1 + exp(-steepness * (raw - midpoint)))

    Args:
        raw_score: Raw BM25/text-match score from keyword search
        midpoint: Score at which output = 0.5
        steepness: Controls curve sharpness

    Returns:
        Normalized score in [0, 1]
    """
    if raw_score <= 0:
        return 0.0
    try:
        return 1.0 / (1.0 + math.exp(-steepness * (raw_score - midpoint)))
    except OverflowError:
        # exp overflow means very negative exponent → score is ~0 or ~1
        return 1.0 if raw_score > midpoint else 0.0


def compute_evidence_boost(
    evidence_count: int,
    saturate: int = 5,
    max_boost: float = 0.3,
) -> float:
    """
    Graph node evidence boost (linear ramp, saturates at `saturate` count).

    Used only for intra-Graph ranking (which nodes win the quota slots).

    Args:
        evidence_count: Number of DERIVED_FROM edges (VDB facts supporting this schema)
        saturate: Number of evidence items for max boost
        max_boost: Maximum boost value

    Returns:
        Boost value in [0, max_boost]
    """
    if evidence_count <= 0 or saturate <= 0:
        return 0.0
    return min(evidence_count / float(saturate), 1.0) * max_boost


def score_vdb_node(
    semantic_score: float,
    bm25_score: float,
    w_sem: float = 0.6,
    w_bm25: float = 0.4,
) -> float:
    """
    Compute final VDB node score.

    final = semantic × w_sem + bm25 × w_bm25
    Range: [0, 1.0]

    Args:
        semantic_score: Cosine similarity from vector search [0, 1]
        bm25_score: Normalized BM25 score [0, 1]
        w_sem: Semantic weight (default 0.6)
        w_bm25: BM25 weight (default 0.4)

    Returns:
        Fused score in [0, 1.0]
    """
    return semantic_score * w_sem + bm25_score * w_bm25
