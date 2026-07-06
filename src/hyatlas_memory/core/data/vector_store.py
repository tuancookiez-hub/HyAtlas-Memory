"""Zvec-only vector-store factory for HyAtlas runtime."""

import logging

from ..config import MemoryConfig
from .vector_store_base import VectorStoreBase

logger = logging.getLogger(__name__)


def create_vector_store(config: MemoryConfig) -> VectorStoreBase:
    """Create the runtime vector store.

    Zvec is the only supported runtime backend. Qdrant remains only as an
    archived migration source for `scripts/migrate_qdrant_to_zvec.py` and
    `hyatlas archive qdrant`, not a live provider.
    """
    provider = str(getattr(config.vector_store, "provider", "zvec") or "zvec").lower().strip()
    if provider != "zvec":
        raise ValueError(f"Unsupported vector_store provider {provider!r}; HyAtlas runtime uses zvec")

    from .vector_store_zvec import ZvecVectorStore

    logger.debug("VectorStore provider: zvec")
    return ZvecVectorStore(config)


# Backward-compatible alias: `from .vector_store import VectorStore`.
def VectorStore(config: MemoryConfig) -> VectorStoreBase:  # noqa: N802
    return create_vector_store(config)
