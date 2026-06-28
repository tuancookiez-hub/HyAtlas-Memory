# Migrating to HyAtlas-Memory 2.0 (S-Class)

**Audience:** Upgrading from **1.5.x** with an existing Qdrant + Kuzu data directory.

## What changed in 2.0

- **Retrieval:** `HY_MEMORY_READER=hybrid_v2`, optional cross-encoder rerank (`MEMORY_RERANK_ENABLED=true`).
- **Embeddings:** default path is **bge-large-en-v1.5** (1024-d) collection `agent_memories_1024` (not 384-d `agent_memories_384`).
- **L5 knowledge graph:** in-process writer (`MEMORY_L5_VERSION=2`) — no subprocess batch lock; incremental watermark in `l5_state.json`.
- **L4 identity:** pre-write dedup (`MEMORY_L4_DEDUP_SKIP=0.90`), `identity_type`, evolution enrich on search.
- **Kuzu graph:** vector dims must match embedder (**1024** after large-model migration). Old 384-d graph DB must be recreated or migrated.

## Recommended env (`.env` or hy_memory env file)

```env
HY_MEMORY_READER=hybrid_v2
MEMORY_RERANK_ENABLED=true
MEMORY_RERANK_MODEL=cross-encoder/ms-marco-MiniLM-L-6-v2
MEMORY_RERANK_TOPK=20
MEMORY_RERANK_WEIGHT=0.6
HY_MEMORY_WRITE_TURN_WINDOW=3
MEMORY_L5_VERSION=2
MEMORY_L4_DEDUP_ENABLED=true
MEMORY_L4_DEDUP_SKIP=0.90
```

## Upgrade steps

1. **Stop stack:** `hyatlas stop`
2. **Install 2.0:** `pip install -U hyatlas-memory` (or install from this repo).
3. **Re-embed / collection:** If still on 384-d collection, run your migration script (see `Maintainer/reembed_migration.py` in HermesVision notes) or restore from Qdrant backup before dropping old collection.
4. **Kuzu:** If S2 logs show `Expected: 384, Actual: 1024`, reset graph store per your backup policy (fresh Kuzu DB at 1024d). L6/L7/L5 graph counts will **rebuild** via System2 digests (hours, not instant).
5. **Clear patch cache after pip upgrade:** remove `hyatlas_memory/__pycache__/patches*.pyc` and `l5_inprocess*.pyc` if you hot-patched an older install.
6. **Start:** `hyatlas start` once — verify log contains `l5_inprocess: True`, `l4_identity: True`.
7. **Smoke:** `curl http://127.0.0.1:19527/healthz` → 200; dashboard :8765; search returns `profile` + `normal` channels.

## Not included in the package

- Your Qdrant point data, Kuzu files, or API keys — never commit these.
- LongMemEval benchmark scores — run separately when graph layers have repopulated.

## Rollback

- Set `MEMORY_L5_VERSION=` empty or `1` for legacy L5 path (if still present).
- Revert pip to `1.5.0` and restore Qdrant/Kuzu backups taken before migration.