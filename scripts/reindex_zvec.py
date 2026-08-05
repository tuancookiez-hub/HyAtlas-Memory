"""Rebuild zvec collection from Kuzu graph data using HyAtlas's own classes.

This ensures the schema matches exactly what the server expects.

Usage:
    python scripts/reindex_zvec.py

The script reads the active ``HYATLAS_HOME`` configuration and uses its
embedder model, dimensions, Kuzu database, and dimension-specific zvec
collection. Stop the HyAtlas server before running it.
"""
import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

import kuzu  # noqa: E402
import numpy as np  # noqa: E402

from hyatlas_memory import layout  # noqa: E402
from hyatlas_memory.core.config import MemoryConfig  # noqa: E402
from hyatlas_memory.core.data.vector_store_zvec import ZvecVectorStore  # noqa: E402
from hyatlas_memory.core.models.memory import (  # noqa: E402
    MemoryLayer,
    MemoryNode,
    MemoryStatus,
    SourceType,
)


def runtime():
    path = layout.active_config_path() or layout.cfgfile()
    with path.open(encoding="utf-8") as file:
        raw = json.load(file)
    cfg = MemoryConfig.from_dict(raw)
    emb = raw.get("embedder") or {}
    vec = raw.get("vector_store") or {}
    cfg.vector_store.embedding_dims = int(
        emb.get("dims") or vec.get("embedding_dims") or cfg.vector_store.embedding_dims
    )
    return cfg, str(emb.get("model") or cfg.embedder.model), layout.kdata()


def get_all_memory_nodes(path):
    """Read all Memory nodes from Kuzu."""
    print(f"[1/4] Reading all Memory nodes from Kuzu ({path})...")
    db = kuzu.Database(str(path))
    conn = kuzu.Connection(db)

    result = conn.execute("MATCH (m:Memory) RETURN m;")
    nodes = []
    while result.has_next():
        row = result.get_next()
        node = row[0]
        nodes.append(node)

    print(f"  Found {len(nodes)} Memory nodes")
    return nodes


def load_embedder(name):
    """Load the local BGE embedder."""
    print("[2/4] Loading local BGE embedder...")
    from sentence_transformers import SentenceTransformer
    model = SentenceTransformer(name, device="cpu")
    print(f"  Embedder loaded (dim={model.get_sentence_embedding_dimension()})")
    return model


def embed_batch(model, texts, batch_size=16):
    """Embed texts in batches."""
    all_embeddings = []
    total = len(texts)
    for i in range(0, total, batch_size):
        batch = texts[i:i + batch_size]
        embeddings = model.encode(batch, convert_to_numpy=True, show_progress_bar=False)
        all_embeddings.append(embeddings)
        pct = min(100, (i + len(batch)) / total * 100)
        print(f"  Embedded {i + len(batch)}/{total} ({pct:.0f}%)", end="\r")
    print()
    return np.vstack(all_embeddings)


async def rebuild_collection(nodes, model, config):
    """Use HyAtlas ZvecVectorStore to create collection and insert."""
    print("[3/4] Initializing HyAtlas ZvecVectorStore...")

    vs = ZvecVectorStore(config)
    await vs.initialize()
    print(f"  Collection initialized: {vs._path}")

    # Embed all texts
    texts = []
    for node in nodes:
        content = node.get("content", "") or ""
        if not content:
            content = node.get("layer", "unknown")
        texts.append(content)

    print(f"[4/4] Embedding and inserting {len(nodes)} nodes...")
    t0 = time.time()
    embeddings = embed_batch(model, texts, batch_size=16)
    t1 = time.time()
    print(f"  Embedding done in {t1 - t0:.1f}s ({len(texts)} texts)")

    # Insert using upsert
    inserted = 0
    errors = 0
    t2 = time.time()

    for i, node in enumerate(nodes):
        try:
            mem_id = str(node.get("node_id", f"reidx-{i}"))
            content = str(node.get("content", "") or "")
            layer = str(node.get("layer", "l5_knowledge") or "l5_knowledge")
            status = str(node.get("status", "active") or "active")
            user_id = str(node.get("user_id", "default") or "default")
            agent_id = str(node.get("agent_id", "default") or "default")
            session_id = str(node.get("source_session_id", "") or "")
            owner = str(node.get("owner", "") or "")
            confidence = str(node.get("confidence", "") or "")
            source_type = str(node.get("source_type", "") or "")
            emotional_valence = str(node.get("emotional_valence", "") or "")
            emotional_arousal = str(node.get("emotional_arousal", "") or "")
            specificity_score = str(node.get("specificity_score", "") or "")
            rarity_score = str(node.get("rarity_score", "") or "")
            longtail_flag = str(node.get("longtail_flag", "") or "")
            temporal_anchor = str(node.get("temporal_anchor", "") or "")
            access_count = str(node.get("access_count", "") or "")
            supersedes = str(node.get("previous_version_id", "") or "")
            superseded_by = str(node.get("superseded_by_id", "") or "")
            custom_json = node.get("custom_json", "") or ""
            custom_dict = {}
            if custom_json:
                try:
                    custom_dict = json.loads(custom_json) if isinstance(custom_json, str) else custom_json
                except (json.JSONDecodeError, TypeError):
                    custom_dict = {}
            tags_raw = node.get("tags", "") or ""
            if isinstance(tags_raw, str):
                try:
                    tags = json.loads(tags_raw) if tags_raw else []
                except (json.JSONDecodeError, TypeError):
                    tags = [tags_raw] if tags_raw else []
            else:
                tags = list(tags_raw) if tags_raw else []
            speculate = str(node.get("extra_json", "") or "")
            is_latest = "true" if not superseded_by else "false"

            mn = MemoryNode(
                node_id=mem_id,
                user_id=user_id,
                agent_id=agent_id,
                session_id=session_id,
                owner=owner or None,
                layer=MemoryLayer(layer),
                content=content,
                status=MemoryStatus(status),
                confidence=float(confidence) if confidence else 1.0,
                source_type=SourceType(source_type) if source_type else SourceType.EXPLICIT,
                emotional_valence=float(emotional_valence) if emotional_valence else 0.0,
                emotional_arousal=float(emotional_arousal) if emotional_arousal else 0.0,
                specificity_score=float(specificity_score) if specificity_score else 0.0,
                rarity_score=float(rarity_score) if rarity_score else 0.0,
                longtail_flag=longtail_flag == "True" if longtail_flag else False,
                meta_tags=[],
                source_session_id=session_id,
                memory_at=None,
                temporal_anchor=temporal_anchor or None,
                gmt_created=None,
                gmt_modified=None,
                valid_from=None,
                valid_until=None,
                access_count=int(access_count) if access_count else 0,
                last_accessed_at=None,
                supersedes=[supersedes] if supersedes else None,
                superseded_by=[superseded_by] if superseded_by else None,
                custom=custom_dict,
                tags=tags or [],
                speculate=speculate or None,
                source_raw_memory_id=None,
                is_latest=is_latest == "true",
                embedding=embeddings[i].tolist(),
            )

            await vs.upsert(mn)
            inserted += 1

            if (i + 1) % 100 == 0:
                print(f"  Inserted {i + 1}/{len(nodes)} ({inserted} ok, {errors} errors)")
        except Exception as e:
            errors += 1
            if errors <= 5:
                print(f"  ERROR at node {i} (id={node.get('node_id', '?')}): {e}")

    t3 = time.time()
    print(f"\n  Insert done in {t3 - t2:.1f}s")
    print(f"  Total: {inserted} inserted, {errors} errors")

    stats = vs._coll.stats
    print(f"  Final stats: {stats}")

    await vs.close()
    return inserted, errors


def main():
    print("=" * 60)
    print("  HyAtlas zvec Reindex (using HyAtlas classes)")
    print("=" * 60)
    print()

    t_start = time.time()

    config, model_name, kuzu_path = runtime()
    print(f"  HyAtlas home: {layout.home()}")
    print(f"  Embedder: {model_name} ({config.vector_store.embedding_dims}d)")
    print()

    nodes = get_all_memory_nodes(kuzu_path)
    if not nodes:
        print("ERROR: No nodes found in Kuzu. Aborting.")
        return

    model = load_embedder(model_name)

    inserted, errors = asyncio.run(rebuild_collection(nodes, model, config))

    t_end = time.time()
    print()
    print("=" * 60)
    print(f"  Reindex complete in {t_end - t_start:.1f}s")
    print(f"  Inserted: {inserted}")
    print(f"  Errors:   {errors}")
    print("=" * 60)


if __name__ == "__main__":
    main()
