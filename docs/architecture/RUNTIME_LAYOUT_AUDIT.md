# Runtime Layout Audit — Patch 24
Generated: 2026-07-04T21:07:29
## Scope
Read-only inventory before introducing `HYATLAS_HOME`. No data was moved.
## Current runtime roots
### `F:\HyAtlas-Memory`
Size/status:
```text
841M	F:\HyAtlas-Memory
```
Sample contents:
```text
F:\HyAtlas-Memory
F:\HyAtlas-Memory/.dockerignore
F:\HyAtlas-Memory/.env.example
F:\HyAtlas-Memory/.git
F:\HyAtlas-Memory/.git/COMMIT_EDITMSG
F:\HyAtlas-Memory/.git/config
F:\HyAtlas-Memory/.git/description
F:\HyAtlas-Memory/.git/HEAD
F:\HyAtlas-Memory/.git/hooks
F:\HyAtlas-Memory/.git/index
F:\HyAtlas-Memory/.git/info
F:\HyAtlas-Memory/.git/kilo
F:\HyAtlas-Memory/.git/logs
F:\HyAtlas-Memory/.git/objects
F:\HyAtlas-Memory/.git/packed-refs
F:\HyAtlas-Memory/.git/refs
F:\HyAtlas-Memory/.git/shallow
F:\HyAtlas-Memory/.git-commit-msg-draft.txt
F:\HyAtlas-Memory/.github
F:\HyAtlas-Memory/.github/workflows
F:\HyAtlas-Memory/.gitignore
F:\HyAtlas-Memory/.hermes
F:\HyAtlas-Memory/.hermes/handoffs
F:\HyAtlas-Memory/.hermes/plans
F:\HyAtlas-Memory/.pytest_cache
F:\HyAtlas-Memory/.pytest_cache/.gitignore
F:\HyAtlas-Memory/.pytest_cache/CACHEDIR.TAG
F:\HyAtlas-Memory/.pytest_cache/README.md
F:\HyAtlas-Memory/.pytest_cache/v
F:\HyAtlas-Memory/.qdrant-initialized
F:\HyAtlas-Memory/.ruff_cache
F:\HyAtlas-Memory/.ruff_cache/.gitignore
F:\HyAtlas-Memory/.ruff_cache/0.15.10
F:\HyAtlas-Memory/.ruff_cache/0.15.20
F:\HyAtlas-Memory/.ruff_cache/CACHEDIR.TAG
F:\HyAtlas-Memory/assets
F:\HyAtlas-Memory/assets/01-hyatlas-system-overview.png
F:\HyAtlas-Memory/assets/02-dual-path-memory.png
F:\HyAtlas-Memory/assets/03-knowledge-graph.png
F:\HyAtlas-Memory/assets/04-memory-evolution.png
F:\HyAtlas-Memory/assets/05-three-gear-modes.png
F:\HyAtlas-Memory/assets/dashboard-demo.gif
F:\HyAtlas-Memory/assets/dashboard-overview.png
F:\HyAtlas-Memory/assets/header-image-prompt-short.txt
F:\HyAtlas-Memory/assets/header-image-prompt.txt
F:\HyAtlas-Memory/assets/hyatlas-architecture.png
F:\HyAtlas-Memory/assets/social-preview-mid.png
F:\HyAtlas-Memory/assets/social-preview-top.png
F:\HyAtlas-Memory/assets/social-preview.png
F:\HyAtlas-Memory/build
F:\HyAtlas-Memory/build/bdist.win-amd64
F:\HyAtlas-Memory/build/lib
F:\HyAtlas-Memory/CHANGELOG.md
F:\HyAtlas-Memory/CONTRIBUTING.md
F:\HyAtlas-Memory/docker-compose.yml
F:\HyAtlas-Memory/Dockerfile
F:\HyAtlas-Memory/docs
F:\HyAtlas-Memory/docs/API.md
F:\HyAtlas-Memory/docs/architecture.md
F:\HyAtlas-Memory/docs/DASHBOARD.md
F:\HyAtlas-Memory/docs/LAYERS.md
F:\HyAtlas-Memory/docs/MIGRATION_v2_SCLASS.md
F:\HyAtlas-Memory/docs/SERVER.md
F:\HyAtlas-Memory/docs/TROUBLESHOOTING.md
F:\HyAtlas-Memory/hy_memory.json.example
F:\HyAtlas-Memory/LICENSE
F:\HyAtlas-Memory/logs
F:\HyAtlas-Memory/logs/qdrant.log
F:\HyAtlas-Memory/MANIFEST.in
F:\HyAtlas-Memory/NOTICE
F:\HyAtlas-Memory/pyproject.toml
F:\HyAtlas-Memory/README.md
F:\HyAtlas-Memory/ROADMAP_v2_public.md
F:\HyAtlas-Memory/scripts
F:\HyAtlas-Memory/scripts/backfill_importance.py
F:\HyAtlas-Memory/scripts/cleanup_qdrant_snapshots.ps1
F:\HyAtlas-Memory/scripts/smoke_test.py
F:\HyAtlas-Memory/snapshots
F:\HyAtlas-Memory/snapshots/tmp
F:\HyAtlas-Memory/src
```
### `C:\Users\<user>\AppData\Local\hermes`
Size/status:
```text
[Command timed out after 60s]
```
Sample contents:
```text
C:\Users\<user>\AppData\Local\hermes
C:\Users\<user>\AppData\Local\hermes/.archive
C:\Users\<user>\AppData\Local\hermes/.archive/references
C:\Users\<user>\AppData\Local\hermes/.archive/SKILL.md
C:\Users\<user>\AppData\Local\hermes/.env
C:\Users\<user>\AppData\Local\hermes/.env.bak-pre-v2-bootstrap
C:\Users\<user>\AppData\Local\hermes/.env.bak.1781452442
C:\Users\<user>\AppData\Local\hermes/.env.env.bak_hymem_v2_phase_0_5
C:\Users\<user>\AppData\Local\hermes/.hermes_history
C:\Users\<user>\AppData\Local\hermes/.hy_memory_zhvz2kcj.tmp
C:\Users\<user>\AppData\Local\hermes/.models_dev_cache_f1tf0gog.tmp
C:\Users\<user>\AppData\Local\hermes/.models_dev_cache_rcf2q0sf.tmp
C:\Users\<user>\AppData\Local\hermes/.processes_3fozkr_2.tmp
C:\Users\<user>\AppData\Local\hermes/.processes_8wl3_ich.tmp
C:\Users\<user>\AppData\Local\hermes/.provider_models_cache_8z9oa3y0.tmp
C:\Users\<user>\AppData\Local\hermes/.qdrant-initialized
C:\Users\<user>\AppData\Local\hermes/.restart_last_processed.json
C:\Users\<user>\AppData\Local\hermes/.skills_prompt_snapshot.json
C:\Users\<user>\AppData\Local\hermes/.tirith-install-failed
C:\Users\<user>\AppData\Local\hermes/.update_check
C:\Users\<user>\AppData\Local\hermes/archive_memories.py
C:\Users\<user>\AppData\Local\hermes/assets
C:\Users\<user>\AppData\Local\hermes/assets/apple-touch-icon-152.png
C:\Users\<user>\AppData\Local\hermes/assets/apple-touch-icon.png
C:\Users\<user>\AppData\Local\hermes/assets/favicon-16.png
C:\Users\<user>\AppData\Local\hermes/assets/favicon-32.png
C:\Users\<user>\AppData\Local\hermes/assets/favicon-48.png
C:\Users\<user>\AppData\Local\hermes/assets/favicon-comparison.png
C:\Users\<user>\AppData\Local\hermes/assets/favicon-monogram-16.png
C:\Users\<user>\AppData\Local\hermes/assets/favicon-monogram-32.png
C:\Users\<user>\AppData\Local\hermes/assets/favicon-monogram-48.png
C:\Users\<user>\AppData\Local\hermes/assets/favicon-monogram-64.png
C:\Users\<user>\AppData\Local\hermes/assets/favicon.ico
C:\Users\<user>\AppData\Local\hermes/assets/hy-memory-ch-master.png
C:\Users\<user>\AppData\Local\hermes/assets/hy-memory-icon.png
C:\Users\<user>\AppData\Local\hermes/assets/hy-memory-winged-tight.png
C:\Users\<user>\AppData\Local\hermes/assets/hy-memory-winged.png
C:\Users\<user>\AppData\Local\hermes/assets/icon-192.png
C:\Users\<user>\AppData\Local\hermes/assets/icon-512.png
C:\Users\<user>\AppData\Local\hermes/assets/og-image.png
C:\Users\<user>\AppData\Local\hermes/attachments
C:\Users\<user>\AppData\Local\hermes/attachments/20260530_050509_42c31f
C:\Users\<user>\AppData\Local\hermes/audio_cache
C:\Users\<user>\AppData\Local\hermes/audio_cache/tts_20260626_061558.mp3
C:\Users\<user>\AppData\Local\hermes/audio_cache/tts_20260626_064030.mp3
C:\Users\<user>\AppData\Local\hermes/audio_cache/tts_20260626_174011.mp3
C:\Users\<user>\AppData\Local\hermes/audio_cache/tts_20260626_181551.mp3
C:\Users\<user>\AppData\Local\hermes/audio_cache/tts_20260626_182529.mp3
C:\Users\<user>\AppData\Local\hermes/auth.json
C:\Users\<user>\AppData\Local\hermes/auth.json.corrupt
C:\Users\<user>\AppData\Local\hermes/auth.lock
C:\Users\<user>\AppData\Local\hermes/backup_reset_1779225530
C:\Users\<user>\AppData\Local\hermes/backup_reset_1779225530/.env
C:\Users\<user>\AppData\Local\hermes/backup_reset_1779225530/auth.json
C:\Users\<user>\AppData\Local\hermes/backup_reset_1779225530/config.yaml.old
C:\Users\<user>\AppData\Local\hermes/backup_reset_1779225530/discord_command_sync_state.json
C:\Users\<user>\AppData\Local\hermes/bench-prompts
C:\Users\<user>\AppData\Local\hermes/bench-prompts/model_bench_smoke.txt
C:\Users\<user>\AppData\Local\hermes/benchmarks
C:\Users\<user>\AppData\Local\hermes/benchmarks/model-fit-2026-07-01
C:\Users\<user>\AppData\Local\hermes/bin
C:\Users\<user>\AppData\Local\hermes/bin/hymemory.py
C:\Users\<user>\AppData\Local\hermes/bin/l5_batch_extract.py
C:\Users\<user>\AppData\Local\hermes/bin/l5_digest_writer.py
C:\Users\<user>\AppData\Local\hermes/bin/l5_entity_resolver.py
C:\Users\<user>\AppData\Local\hermes/bin/l5_export_json.py
C:\Users\<user>\AppD
```
### `C:\Users\<user>\.hy_memory`
Size/status:
```text
1.5G	C:\Users\<user>\.hy_memory
```
Sample contents:
```text
C:\Users\<user>\.hy_memory
C:\Users\<user>\.hy_memory/.dashboard_token
C:\Users\<user>\.hy_memory/data
C:\Users\<user>\.hy_memory/data/cache.db
C:\Users\<user>\.hy_memory/data/cache.db-shm
C:\Users\<user>\.hy_memory/data/cache.db-wal
C:\Users\<user>\.hy_memory/data/chroma.sqlite3
C:\Users\<user>\.hy_memory/data/coding_memory.db
C:\Users\<user>\.hy_memory/data/history.db
C:\Users\<user>\.hy_memory/data/history.db-shm
C:\Users\<user>\.hy_memory/data/history.db-wal
C:\Users\<user>\.hy_memory/data/kuzu_db
C:\Users\<user>\.hy_memory/data/kuzu_db.bak-pre-v2-reset
C:\Users\<user>\.hy_memory/data/kuzu_db.wal
C:\Users\<user>\.hy_memory/data/kuzu_db_384_backup
C:\Users\<user>\.hy_memory/data/kuzu_db_384_pre_migrate_20260628_153814
C:\Users\<user>\.hy_memory/data/kuzu_db_384_pre_migrate_20260628_153814.wal
C:\Users\<user>\.hy_memory/data/kuzu_db_test
C:\Users\<user>\.hy_memory/data/qdrant
C:\Users\<user>\.hy_memory/data/vector_db
C:\Users\<user>\.hy_memory/logs
C:\Users\<user>\.hy_memory/logs/hy_memory.log
C:\Users\<user>\.hy_memory/logs/hy_memory.log.2026-06-19
C:\Users\<user>\.hy_memory/logs/hy_memory.log.2026-06-20
C:\Users\<user>\.hy_memory/logs/hy_memory.log.2026-06-21
C:\Users\<user>\.hy_memory/logs/hy_memory.log.2026-06-22
C:\Users\<user>\.hy_memory/logs/hy_memory.log.2026-06-23
C:\Users\<user>\.hy_memory/logs/hy_memory.log.2026-06-24
C:\Users\<user>\.hy_memory/logs/hy_memory.log.2026-06-25
C:\Users\<user>\.hy_memory/logs/hy_memory.log.2026-06-26
C:\Users\<user>\.hy_memory/logs/hy_memory.log.2026-06-27
C:\Users\<user>\.hy_memory/logs/hy_memory.log.2026-06-28
C:\Users\<user>\.hy_memory/logs/hy_memory.log.2026-06-29
C:\Users\<user>\.hy_memory/logs/hy_memory.log.2026-06-30
C:\Users\<user>\.hy_memory/logs/hy_memory.log.2026-07-01
C:\Users\<user>\.hy_memory/logs/hy_memory.log.2026-07-02
C:\Users\<user>\.hy_memory/logs/hy_memory.log.2026-07-03
C:\Users\<user>\.hy_memory/logs/pipeline
C:\Users\<user>\.hy_memory/pkg
C:\Users\<user>\.hy_memory/pkg/.env
C:\Users\<user>\.hy_memory/_kuzu_test_checkpoint
C:\Users\<user>\.hy_memory/_kuzu_test_checkpoint/test.db
C:\Users\<user>\.hy_memory/_kuzu_test_checkpoint/test2.db
```
### `C:\qdrant`
Size/status:
```text
1.2G	C:\qdrant
```
Sample contents:
```text
C:\qdrant
C:\qdrant/.qdrant-initialized
C:\qdrant/config.yaml
C:\qdrant/qdrant.exe
C:\qdrant/snapshots
C:\qdrant/snapshots/tmp
C:\qdrant/storage
C:\qdrant/storage/.deleted
C:\qdrant/storage/aliases
C:\qdrant/storage/collections
C:\qdrant/storage/raft_state.json
C:\qdrant/storage/tmp
```
### `C:\qdrant-data`
Size/status:
```text
3.7G	C:\qdrant-data
```
Sample contents:
```text
C:\qdrant-data
C:\qdrant-data/.deleted
C:\qdrant-data/aliases
C:\qdrant-data/aliases/data.json
C:\qdrant-data/collections
C:\qdrant-data/collections/agent_memories_1024
C:\qdrant-data/collections/agent_memories_1024_tag_index
C:\qdrant-data/collections/agent_memories_1536
C:\qdrant-data/collections/agent_memories_384_tag_index
C:\qdrant-data/collections/agent_memories_coding_keys_1024
C:\qdrant-data/collections/agent_memories_coding_keys_384
C:\qdrant-data/raft_state.json
```
### `C:\Users\<user>\AppData\Local\Temp\hy-memory-pkg`
Size/status:
```text
2.4M	C:\Users\<user>\AppData\Local\Temp\hy-memory-pkg
```
Sample contents:
```text
C:\Users\<user>\AppData\Local\Temp\hy-memory-pkg
C:\Users\<user>\AppData\Local\Temp\hy-memory-pkg/aiohappyeyeballs
C:\Users\<user>\AppData\Local\Temp\hy-memory-pkg/aiohappyeyeballs-2.6.2.dist-info
C:\Users\<user>\AppData\Local\Temp\hy-memory-pkg/aiohappyeyeballs-2.6.2.dist-info/licenses
C:\Users\<user>\AppData\Local\Temp\hy-memory-pkg/aiohttp
C:\Users\<user>\AppData\Local\Temp\hy-memory-pkg/aiohttp/.hash
C:\Users\<user>\AppData\Local\Temp\hy-memory-pkg/aiohttp/_websocket
C:\Users\<user>\AppData\Local\Temp\hy-memory-pkg/aiohttp-3.13.5.dist-info
C:\Users\<user>\AppData\Local\Temp\hy-memory-pkg/aiohttp-3.13.5.dist-info/licenses
C:\Users\<user>\AppData\Local\Temp\hy-memory-pkg/aiosignal
C:\Users\<user>\AppData\Local\Temp\hy-memory-pkg/aiosignal-1.4.0.dist-info
C:\Users\<user>\AppData\Local\Temp\hy-memory-pkg/aiosignal-1.4.0.dist-info/licenses
C:\Users\<user>\AppData\Local\Temp\hy-memory-pkg/annotated_doc
C:\Users\<user>\AppData\Local\Temp\hy-memory-pkg/annotated_doc-0.0.4.dist-info
C:\Users\<user>\AppData\Local\Temp\hy-memory-pkg/annotated_doc-0.0.4.dist-info/licenses
C:\Users\<user>\AppData\Local\Temp\hy-memory-pkg/annotated_types
C:\Users\<user>\AppData\Local\Temp\hy-memory-pkg/annotated_types-0.7.0.dist-info
C:\Users\<user>\AppData\Local\Temp\hy-memory-pkg/annotated_types-0.7.0.dist-info/licenses
C:\Users\<user>\AppData\Local\Temp\hy-memory-pkg/anyio
C:\Users\<user>\AppData\Local\Temp\hy-memory-pkg/anyio/abc
C:\Users\<user>\AppData\Local\Temp\hy-memory-pkg/anyio/streams
C:\Users\<user>\AppData\Local\Temp\hy-memory-pkg/anyio/_backends
C:\Users\<user>\AppData\Local\Temp\hy-memory-pkg/anyio/_core
C:\Users\<user>\AppData\Local\Temp\hy-memory-pkg/anyio-4.13.0.dist-info
C:\Users\<user>\AppData\Local\Temp\hy-memory-pkg/anyio-4.13.0.dist-info/licenses
C:\Users\<user>\AppData\Local\Temp\hy-memory-pkg/attr
C:\Users\<user>\AppData\Local\Temp\hy-memory-pkg/attrs
C:\Users\<user>\AppData\Local\Temp\hy-memory-pkg/attrs-26.1.0.dist-info
C:\Users\<user>\AppData\Local\Temp\hy-memory-pkg/attrs-26.1.0.dist-info/licenses
C:\Users\<user>\AppData\Local\Temp\hy-memory-pkg/bcrypt
C:\Users\<user>\AppData\Local\Temp\hy-memory-pkg/bcrypt-5.0.0.dist-info
C:\Users\<user>\AppData\Local\Temp\hy-memory-pkg/bin
C:\Users\<user>\AppData\Local\Temp\hy-memory-pkg/build
C:\Users\<user>\AppData\Local\Temp\hy-memory-pkg/build/_compat
C:\Users\<user>\AppData\Local\Temp\hy-memory-pkg/build-1.5.0.dist-info
C:\Users\<user>\AppData\Local\Temp\hy-memory-pkg/build-1.5.0.dist-info/licenses
C:\Users\<user>\AppData\Local\Temp\hy-memory-pkg/certifi
C:\Users\<user>\AppData\Local\Temp\hy-memory-pkg/certifi-2026.5.20.dist-info
C:\Users\<user>\AppData\Local\Temp\hy-memory-pkg/certifi-2026.5.20.dist-info/licenses
C:\Users\<user>\AppData\Local\Temp\hy-memory-pkg/charset_normalizer
C:\Users\<user>\AppData\Local\Temp\hy-memory-pkg/charset_normalizer/cli
C:\Users\<user>\AppData\Local\Temp\hy-memory-pkg/charset_normalizer-3.4.7.dist-info
C:\Users\<user>\AppData\Local\Temp\hy-memory-pkg/charset_normalizer-3.4.7.dist-info/licenses
C:\Users\<user>\AppData\Local\Temp\hy-memory-pkg/chromadb
C:\Users\<user>\AppData\Local\Temp\hy-memory-pkg/chromadb/api
C:\Users\<user>\AppData\Local\Temp\hy-memory-pkg/chromadb/auth
C:\Users\<user>\AppData\Local\Temp\hy-memory-pkg/chromadb/cli
C:\Users\<user>\AppData\Local\Temp\hy-memory-pkg/chromadb/db
C:\Users\<user>\AppData\Local\Temp\hy-memory-pkg/chromadb/execution
C:\Users\<user>\AppData\Local\Temp\hy-memory-pkg/chromadb/experimental
C:\Users\<user>\AppData\Local\Temp\hy-memory-pkg/chromadb/ingest
C:\Users\<user>\AppData\Local\Temp\hy-memory-pkg/chromadb/logservice
C:\Users\<user>\AppData\Local\Temp\hy-memory-pkg/chromadb/migrations
C:\Users\<user>\AppData\Local\Temp\hy-memory-pkg/chromadb/proto
C:\Users\<user>\AppData\Local\Temp\hy-memory-pkg/chromadb/quota
C:\Users\<user>\AppData\Local\Temp\hy-memory-pkg/chromadb/rate_limit
C:\Users\<user>\AppData\Local\Temp\hy-memory-pkg/chromadb/segment
C:\Users\<user>\AppData\Local\Temp\hy-memory-pkg/chromadb/server
C:\Users\<user>\AppData\Local\Temp\hy-memory-pkg/chromadb/tel
```
### `C:\Users\<user>\AppData\Local\Temp\hymem_src`
Size/status:
```text
44K	C:\Users\<user>\AppData\Local\Temp\hymem_src
```
Sample contents:
```text
C:\Users\<user>\AppData\Local\Temp\hymem_src
C:\Users\<user>\AppData\Local\Temp\hymem_src/hy_memory
C:\Users\<user>\AppData\Local\Temp\hymem_src/hy_memory/agent
C:\Users\<user>\AppData\Local\Temp\hymem_src/hy_memory/coding
C:\Users\<user>\AppData\Local\Temp\hymem_src/hy_memory/core
C:\Users\<user>\AppData\Local\Temp\hymem_src/hy_memory/data
C:\Users\<user>\AppData\Local\Temp\hymem_src/hy_memory/models
C:\Users\<user>\AppData\Local\Temp\hymem_src/hy_memory/pipelines
C:\Users\<user>\AppData\Local\Temp\hymem_src/hy_memory/utils
C:\Users\<user>\AppData\Local\Temp\hymem_src/hy_memory-1.2.18.dist-info
C:\Users\<user>\AppData\Local\Temp\hymem_src/hy_memory-1.2.18.dist-info/licenses
C:\Users\<user>\AppData\Local\Temp\hymem_src/plugins
C:\Users\<user>\AppData\Local\Temp\hymem_src/plugins/hook
C:\Users\<user>\AppData\Local\Temp\hymem_src/plugins/mcp
C:\Users\<user>\AppData\Local\Temp\hymem_src/plugins/native
```
## Live process snapshot
```text
4207712       0       0      13408  ?              0 13:56:24 C:\Program Files\WindowsApps\MicrosoftWindows.Client.WebExperience_526.11701.50.0_x64__cw5n1h2txyewy\Dashboard\Widgets.exe
      428       1     428      35300  ?         197609 20:55:11 /c/qdrant/qdrant
```
## Qdrant collection health
```json
{

  "status": "green",

  "optimizer_status": "ok",

  "indexed_vectors_count": 4112,

  "points_count": 5556,

  "segments_count": 2,

  "config": {

    "params": {

      "vectors": {

        "size": 1024,

        "distance": "Cosine"

      },

      "shard_number": 1,

      "replication_factor": 1,

      "write_consistency_factor": 1,

      "on_disk_payload": true

    },

    "hnsw_config": {

      "m": 16,

      "ef_construct": 100,

      "full_scan_threshold": 10000,

      "max_indexing_threads": 0,

      "on_disk": false

    },

    "optimizer_config": {

      "deleted_threshold": 0.2,

      "vacuum_min_vector_number": 1000,

      "default_segment_number": 2,

      "max_segment_size": null,

      "memmap_threshold": null,

      "indexing_threshold": 10000,

      "flush_interval_sec": 5,

      "max_optimization_threads": null,

      "prevent_unoptimized": null

    },

    "wal_config": {

      "wal_capacity_mb": 32,

      "wal_segments_ahead": 0,

      "wal_retain_closed": 1

    },

    "quantization_config": null

  },

  "payload_schema": {

    "search_text": {

      "data_type": "text",

      "params": {

        "type": "text",

        "tokenizer": "whitespace",

        "min_token_len": 2,

        "max_token_len": 20

      },

      "points": 4690

    }

  },

  "update_queue": {

    "length": 0

  }

}
```
## Dashboard graph counts
```json
{"l5_knowledge": 1208, "l6_schema": 460, "l7_intention": 146, "total": 1814}
```
## Git state
```text
78e7747 Patch 23 follow-up: adversarial review fixes + integration tests
c886a6e Docs: Patch 23 — updated all references from export-file snapshot to live endpoint
d8e9718 Feat: Patch 23 live graph endpoint /api/v1/graph (replaces stale export file)
a0b8eed Relicense MIT -> Apache 2.0
7e8e84e Fix: Path.home() -> _P.home() in llm_fast_smart and l5_auto_trigger patches
0a234af Feat: Patch 21 auto-forgetting (recency scoring + expiry sweep) + Qdrant path fix
4a456a4 Fix CI: ruff import sort order in Patch 20
2a74ede Fix: S1 extractor entity_type crash + dashboard graph-counts limit
```
## Source path/config references
### `HYATLAS_HOME`
```text
{'total_count': 0}
```
### `HYATLAS_KUZU_PATH`
```text
F:\HyAtlas-Memory\scripts\smoke_test.py
  57: )
  58: HYATLAS_KUZU_PATH = os.environ.get(
  59:     "HYATLAS_KUZU_PATH", str(Path.home() / ".hy_memory" / "data" / "kuzu_db")
  60: )
  394:     t0 = time.perf_counter()
  395:     p = Path(HYATLAS_KUZU_PATH)
  396:     if not p.exists():
```
### `HY_MEMORY`
```text
ERROR JSONDecodeError('Extra data: line 3 column 1 (char 1996)')
```
### `hy_memory.json`
```text
ERROR JSONDecodeError('Extra data: line 3 column 1 (char 1874)')
```
### `Path.home`
```text
ERROR JSONDecodeError('Extra data: line 3 column 1 (char 1825)')
```
### `\.hermes`
```text
ERROR JSONDecodeError('Extra data: line 3 column 1 (char 1982)')
```
### `\.hy_memory`
```text
ERROR JSONDecodeError('Extra data: line 3 column 1 (char 2071)')
```
### `qdrant`
```text
ERROR JSONDecodeError('Extra data: line 3 column 1 (char 1513)')
```
### `kuzu`
```text
ERROR JSONDecodeError('Extra data: line 3 column 1 (char 1496)')
```
### `storage_path`
```text
{'total_count': 0}
```
### `base_url`
```text
ERROR JSONDecodeError('Extra data: line 3 column 1 (char 1389)')
```
### `gpt-4o-mini`
```text
F:\HyAtlas-Memory\src\hyatlas_memory\__init__.py
  1096:             {"key": "llm_model", "description": "LLM model",
  1097:              "default": "gpt-4o-mini", "when": {"mode": ["pro", "ultra"]}},
  1098:             {"key": "llm_base_url", "description": "LLM API base URL",
  1203: 
  1204:             val = input(f"  LLM model [{llm_cfg.get('model', 'gpt-4o-mini')}]: ").strip()
  1205:             if val:
```
### `bge-small`
```text
F:\HyAtlas-Memory\src\hyatlas_memory\embed_server.py
  12: 
  13:     python embed_server.py --model BAAI/bge-small-en-v1.5 --port 19528
  14: """
  56: _KNOWN_DIMS = {
  57:     "BAAI/bge-small-en-v1.5": 384,
  58:     "BAAI/bge-base-en-v1.5": 768,
  257:     p = argparse.ArgumentParser(description="Hy-Memory local embedder")
  258:     p.add_argument("--model", default="BAAI/bge-small-en-v1.5",
  259:                    help="sentence-transformers model ID")
F:\HyAtlas-Memory\src\hyatlas_memory\patches.py
  632:     import os as _os
  633:     model_name = _os.environ.get("MEMORY_EMBEDDER_MODEL", "BAAI/bge-small-en-v1.5")
  634:     device = _os.environ.get("MEMORY_EMBEDDER_DEVICE", "cpu")
F:\HyAtlas-Memory\src\hyatlas_memory\server\start_server.py
  64: os.environ["MEMORY_EMBEDDER_PROVIDER"] = "openai"
  65: os.environ["MEMORY_EMBEDDER_MODEL"] = emb.get("model", "BAAI/bge-small-en-v1.5")
  66: os.environ["MEMORY_EMBEDDING_DIMS"] = str(emb.get("dims", 384))
F:\HyAtlas-Memory\src\hyatlas_memory\server\dashboard\dashboard.py
  2842:     ['VDB points', s.vdb_points ?? '—'],
  2843:     ['Embed model', 'BAAI/bge-small-en-v1.5'],
  2844:     ['Embed dims', s.embed_dims ?? '—'],
```
### `bge-large`
```text
F:\HyAtlas-Memory\src\hyatlas_memory\client.py
  32: _DEFAULT_TIMEOUT = 10
  33: _SEARCH_TIMEOUT = 180  # bge-large on CPU can be slow on first query
  34: _ADD_TIMEOUT = 60  # LLM extraction can take a while
F:\HyAtlas-Memory\src\hyatlas_memory\embed_server.py
  58:     "BAAI/bge-base-en-v1.5": 768,
  59:     "BAAI/bge-large-en-v1.5": 1024,
  60:     "sentence-transformers/all-MiniLM-L6-v2": 384,
```
## Initial findings
- Runtime state is split across project root, Hermes home, `.hy_memory`, `C:/qdrant`, `C:/qdrant-data`, and temp package clones.
- `HYATLAS_HOME` is not currently implemented in source search.
- Existing CLI exists; provider/model UX should extend `hyatlas init` and add `hyatlas config`, not create a parallel CLI.
- Qdrant health must validate point count/storage path. We recently observed green/0-points when launched from Hermes-bundled qdrant.
- README and examples still present GPT-4o-mini/BGE-small defaults; current live data uses `agent_memories_1024` and BGE-large/1024.
## Proposed target layout
```text
~/.hyatlas/
  config/
    hy_memory.json
    qdrant.yaml
    .env
  data/
    qdrant/
    kuzu/
    exports/
  logs/
  cache/
  snapshots/
```
## Init UX direction
Hindsight-style: choose local/custom provider path, collect base URL + API key + model, then choose mode: lite / pro / ultra. OAuth bridge is out of scope for now.
