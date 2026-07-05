"""
Reconcile 候选召回：向量池 (limit×3) + 池内 BM25，按 hybrid_v2 权重融合。

用于 MemoryReconciler 在关闭 LLM search-query 扩写时，
用每条新 memory 文本直接检索相关旧记忆。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, TYPE_CHECKING

from ...models.memory import MemoryLayer, MemoryStatus
from ...utils.log_setup import get_request_id
from . import bm25 as rbm25
from .lemmatize import get_bm25_params, lemmatize_for_bm25
from .scoring import normalize_bm25, score_vdb_node

if TYPE_CHECKING:
    from ...data.vector_store_base import VectorStoreBase

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ReconcileRetrievalConfig:
    """与 hybrid_v2 VDB 路一致的默认权重与阈值。"""

    limit: int = 20
    vec_pool_multiplier: int = 3
    min_score: float = 0.4
    w_sem: float = 0.6
    w_bm25: float = 0.4


class ReconcileHybridRetriever:
    """
    对单条 query 文本做 hybrid 召回：先向量扩池，再在池内 BM25 + 语义融合。
    """

    def __init__(self, config: Optional[ReconcileRetrievalConfig] = None):
        self._cfg = config or ReconcileRetrievalConfig()

    @property
    def vec_pool_limit(self) -> int:
        return self._cfg.limit * self._cfg.vec_pool_multiplier

    def _log_bm25_diagnostics(
        self,
        *,
        query_text: str,
        query_lemmatized: str,
        q_terms: List[str],
        pool_size: int,
        raw_bm25: List[float],
        sem_scores: List[float],
        bm25_norms: List[float],
        fused_all: List[float],
        kept: int,
    ) -> None:
        """BM25 全零或融合后候选被滤空时打 WARNING/ERROR，便于排查 token 不匹配等问题。"""
        req = get_request_id() or "-"
        q_preview = (query_text or "")[:120].replace("\n", " ")
        lem_preview = (query_lemmatized or "")[:120].replace("\n", " ")

        raw_max = max(raw_bm25) if raw_bm25 else 0.0
        raw_nonzero = sum(1 for s in raw_bm25 if s > 0)
        norm_max = max(bm25_norms) if bm25_norms else 0.0
        norm_nonzero = sum(1 for s in bm25_norms if s > 0)
        sem_max = max(sem_scores) if sem_scores else 0.0
        fused_max = max(fused_all) if fused_all else 0.0

        base = (
            f"[reconcile-hybrid][{req}] pool={pool_size} q_terms={q_terms!r} "
            f"raw_bm25 max={raw_max:.4f} nonzero={raw_nonzero}/{len(raw_bm25)} "
            f"bm25_norm max={norm_max:.4f} nonzero={norm_nonzero}/{len(bm25_norms)} "
            f"sem_max={sem_max:.4f} fused_max={fused_max:.4f} "
            f"kept={kept}/{len(fused_all)} min_score={self._cfg.min_score} "
            f"query={q_preview!r} lemmatized={lem_preview!r}"
        )

        if not q_terms:
            logger.error(
                f"{base} | BM25_SKIP: empty query tokens after tokenize "
                f"(bm25 weight {self._cfg.w_bm25} wasted, fused≈sem×{self._cfg.w_sem})"
            )
            return

        if pool_size > 0 and raw_nonzero == 0:
            level = logger.warning if kept > 0 else logger.error
            level(
                f"{base} | BM25_ALL_ZERO: no term overlap between query and vec pool — "
                f"check tokenization/language; effective score ≈ semantic×{self._cfg.w_sem} only"
                + (f" ({kept} candidates still passed via semantic alone)" if kept > 0 else "")
            )
            return

        if pool_size > 0 and raw_nonzero > 0 and norm_nonzero == 0:
            logger.error(
                f"{base} | BM25_NORM_ALL_ZERO: raw scores exist but sigmoid normalized to 0 "
                f"(midpoint/steepness may be miscalibrated for this query length)"
            )
            return

        if pool_size > 0 and kept == 0 and fused_all:
            below = sum(1 for f in fused_all if f < self._cfg.min_score)
            logger.warning(
                f"{base} | MIN_SCORE_FILTER: all {below} fused candidates below "
                f"min_score={self._cfg.min_score} "
                f"(if bm25_norm≈0, max fused≈{sem_max * self._cfg.w_sem:.4f})"
            )
            return

        if pool_size > 0 and kept == 0:
            logger.warning(f"{base} | NO_CANDIDATES: empty vec pool or no nodes with content")

    async def search_candidates(
        self,
        query_text: str,
        query_embedding: List[float],
        *,
        vector_store: "VectorStoreBase",
        user_id: str,
        agent_id: Optional[str],
        layers: List[MemoryLayer],
    ) -> List[Dict[str, Any]]:
        """
        Returns:
            [{"node": MemoryNode, "score": float, "_sem": float, "_bm25": float}, ...]
            按融合分降序，长度 ≤ limit，且 score >= min_score。
        """
        if not query_text.strip() or not query_embedding:
            return []

        vec_limit = self.vec_pool_limit
        vec_results = await vector_store.search(
            query_embedding=query_embedding,
            user_id=user_id,
            agent_ids=[agent_id] if agent_id else None,
            layers=layers,
            limit=vec_limit,
            score_threshold=0.0,
            # 纳入 SUPERSEDED：被取代的旧节点也可命中，命中后由 reconciler
            # 用 _trace_full_chain 补全整链。SHADOW（逻辑删除）不纳入。
            status_filter=[MemoryStatus.ACTIVE, MemoryStatus.SUPERSEDED],
            only_latest=False,
        )

        if not vec_results:
            return []

        nodes = []
        sem_scores: List[float] = []
        contents: List[str] = []
        for r in vec_results:
            node = r.get("node")
            if node is None:
                continue
            nodes.append(node)
            sem_scores.append(float(r.get("score", 0.0)))
            contents.append(node.content or "")

        if not nodes:
            return []

        query_lemmatized = lemmatize_for_bm25(query_text)
        q_terms = rbm25.tokenize(query_lemmatized or query_text)
        # 候选端也必须过 lemmatize_for_bm25，否则中文 candidate 会被 _tokenize 当
        # 一整段处理（_TOKEN_RE 把连续 一-鿿 当一个 token），
        # 跟 jieba 切过的 query terms 完全无法匹配 → raw_bm25 全零。
        contents_lemmatized = [lemmatize_for_bm25(c) if c else "" for c in contents]
        raw_bm25 = rbm25.compute_bm25_scores(q_terms, contents_lemmatized)
        midpoint, steepness = get_bm25_params(query_text, query_lemmatized)

        bm25_norms: List[float] = []
        fused_all: List[float] = []
        fused_hits: List[Dict[str, Any]] = []
        for i, node in enumerate(nodes):
            sem = sem_scores[i]
            bm25_norm = normalize_bm25(raw_bm25[i], midpoint, steepness)
            bm25_norms.append(bm25_norm)
            fused = score_vdb_node(
                sem, bm25_norm, self._cfg.w_sem, self._cfg.w_bm25,
            )
            fused_all.append(fused)
            if fused < self._cfg.min_score:
                continue
            fused_hits.append({
                "node": node,
                "score": fused,
                "_sem": sem,
                "_bm25": bm25_norm,
            })

        self._log_bm25_diagnostics(
            query_text=query_text,
            query_lemmatized=query_lemmatized,
            q_terms=q_terms,
            pool_size=len(nodes),
            raw_bm25=raw_bm25,
            sem_scores=sem_scores,
            bm25_norms=bm25_norms,
            fused_all=fused_all,
            kept=len(fused_hits),
        )

        if fused_hits and logger.isEnabledFor(logging.DEBUG):
            req = get_request_id() or "-"
            top = fused_hits[0]
            logger.debug(
                f"[reconcile-hybrid][{req}] ok pool={len(nodes)} kept={len(fused_hits)} "
                f"top_fused={top['score']:.4f} sem={top['_sem']:.4f} bm25={top['_bm25']:.4f}"
            )

        fused_hits.sort(key=lambda x: x["score"], reverse=True)
        return fused_hits[: self._cfg.limit]
