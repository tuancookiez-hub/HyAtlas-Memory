"""
HY Memory - Read (Dispatcher)

对外统一入口 `MemoryReader`，根据环境变量 `HY_MEMORY_READER` 选择并代理
真正的实现：

  - "legacy"     → LegacyReadPipeline      (默认)
  - "hybrid_tag" → HybridTagReadPipeline    (vec + tag-bridge + BM25, RRF 3-channel)
  - "hybrid_v2"  → HybridV2ReadPipeline     (Embed + Keyword + Graph Evidence)

cache 参数从 Registry 注入，用于 read 侧的 trace 写入；传 None 时
trace 写入静默跳过。
"""

import logging
from typing import Any

from ..config import MemoryConfig
from ..core.embed_service import EmbedService
from ..data.graph_store_base import GraphStoreBase
from ..data.vector_store_base import VectorStoreBase
from ..utils.tracer import PipelineTracer
from ._retrieval import config as _retrieval_config
from .base import PipelineContext, ReadPipeline, ReadRequest, ReadResponse

logger = logging.getLogger(__name__)


def _build_impl(
    config: MemoryConfig,
    embed_service: EmbedService | None,
    vector_store: VectorStoreBase | None,
    graph_store: GraphStoreBase | None = None,
    cache: Any = None,
) -> ReadPipeline:
    """按环境变量选择内部实现类并构造实例。"""
    name = _retrieval_config.resolve_reader_name()
    if name == _retrieval_config.READER_HYBRID_TAG:
        try:
            from .reader_hybrid_tag import HybridTagReadPipeline
            return HybridTagReadPipeline(config, embed_service, vector_store, cache=cache)
        except ImportError as e:
            logger.warning(f"[reader-dispatch] hybrid_tag import failed: {e}; fallback to legacy")
    elif name == _retrieval_config.READER_HYBRID_V2:
        try:
            from .reader_hybrid_v2 import HybridV2ReadPipeline
            return HybridV2ReadPipeline(config, embed_service, vector_store, graph_store=graph_store, cache=cache)
        except ImportError as e:
            logger.warning(f"[reader-dispatch] hybrid_v2 import failed: {e}; fallback to legacy")
    elif name == _retrieval_config.READER_TENCENT_HYBRID:
        try:
            from .reader_tencent_hybrid import TencentHybridReadPipeline
            return TencentHybridReadPipeline(config, embed_service, vector_store, graph_store=graph_store, cache=cache)
        except ImportError as e:
            logger.warning(f"[reader-dispatch] tencent_hybrid import failed: {e}; fallback to legacy")
    elif name == _retrieval_config.READER_MEM0:
        try:
            from .reader_mem0 import Mem0ReadPipeline
            return Mem0ReadPipeline(config, embed_service, vector_store, graph_store=graph_store, cache=cache)
        except ImportError as e:
            logger.warning(f"[reader-dispatch] mem0 import failed: {e}; fallback to legacy")

    # 默认 / fallback
    from .reader_legacy import LegacyReadPipeline
    return LegacyReadPipeline(config, embed_service, vector_store, graph_store=graph_store, cache=cache)


class MemoryReader(ReadPipeline):
    """
    读取器（对外入口）。

    按环境变量 `HY_MEMORY_READER` 分发到不同实现：
      - legacy / hybrid_tag / hybrid_v2 / tencent_hybrid / mem0
    默认 legacy，行为与 post13 之前一致。
    """

    VERSION = "reader"

    def __init__(
        self,
        config: MemoryConfig,
        embed_service: EmbedService | None = None,
        vector_store: VectorStoreBase | None = None,
        graph_store: GraphStoreBase | None = None,
        cache: Any = None,
    ):
        self.config = config
        self._impl: ReadPipeline = _build_impl(
            config, embed_service, vector_store, graph_store=graph_store, cache=cache,
        )
        logger.info(
            f"[reader-dispatch] active impl = {self._impl.__class__.__name__} "
            f"(HY_MEMORY_READER={_retrieval_config.resolve_reader_name()}) "
            f"graph_store={'yes' if graph_store else 'no'}"
        )

    async def initialize(self) -> None:
        await self._impl.initialize()

    async def read(
        self,
        request: ReadRequest,
        ctx: PipelineContext | None = None,
        tracer: PipelineTracer | None = None,
    ) -> ReadResponse:
        return await self._impl.read(request, ctx=ctx, tracer=tracer)

    async def close(self) -> None:
        await self._impl.close()

    # ---- 便捷属性：暴露内部实现给诊断脚本 ----
    @property
    def impl(self) -> ReadPipeline:
        return self._impl

    @property
    def embed_service(self) -> EmbedService:
        return getattr(self._impl, "embed_service", None)

