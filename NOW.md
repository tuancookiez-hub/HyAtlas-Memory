# NOW — HyAtlas-Memory

- Runtime layout consolidation (Patches 24–31) is live on `main` as v2.1.0.
- All runtime state now resolves under `~/.hyatlas`. Migration applied 2026-07-05:
  - Qdrant data copied from `C:\qdrant-data` to `~/.hyatlas/data/qdrant` (3.61 GB)
  - Kuzu DB copied from `~/.hy_memory/data/kuzu_db` to `~/.hyatlas/data/kuzu_db` (83 MB)
  - Legacy paths preserved as rollback safety net.
- Migration repair: the original `~/.hyatlas/data/qdrant` had 2,041+ corrupted empty files (metadata/config/WAL). Replaced with a clean copy from legacy `C:\qdrant-data`; stack now healthy.
- Legacy cleanup done 2026-07-05: renamed `C:\qdrant-data` → `C:\qdrant-data.legacy` and `~/.hy_memory` → `~/.hy_memory.legacy`. These backups are kept as a final safety net and can be deleted after a stable period.
- Active LLM is MiniMax-M3 via the official MiniMax API (`https://api.minimax.io/v1`).
- Multi-key LLM resilience configured: `llm.api_keys` populated with the current key as fallback list.
- Qdrant binary already points to `~/.hyatlas/vector/qdrant/qdrant.exe`.
- Stack running detached on ports 6333/19527/8765; dashboard on port 8765.
- Smart memory pruning configured: cronjob `smart-memory-prune` runs every 4 hours, triggers at 80%, prunes down to 70%, uses recent session context, archives to HyAtlas, posts to Discord thread `1523091423556276365`.
- Full test suite: 33 passed, 19 skipped.

Next:
1. Verify stack remains stable over the next few hours.
2. Watch the first autonomous `smart-memory-prune` run at 12:05.
3. After stable period, optionally delete legacy `C:\qdrant-data` and `~/.hy_memory` to complete cleanup.

Blocker: none.
