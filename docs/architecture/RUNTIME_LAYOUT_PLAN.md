# Runtime Layout Plan — HyAtlas Organization

## Goal

Make HyAtlas stop living in scattered roots and give it one predictable runtime home.

- Repo/code: `F:\HyAtlas-Memory`
- Runtime/config/data: `HYATLAS_HOME`, default `~/.hyatlas`

Target runtime layout:

```text
~/.hyatlas/
  config/
    hy_memory.json
    qdrant.yaml
    .env
  data/
    qdrant/
    kuzu_db/
    exports/
  logs/
  cache/
  snapshots/
```

## Patch sequence

### Patch 24 — Runtime layout audit

Read-only inventory of all current roots and hardcoded path references. No data movement.

Output: `docs/architecture/RUNTIME_LAYOUT_AUDIT.md`.

### Patch 25 — `HYATLAS_HOME` resolver + config precedence

Add a single resolver used by start/doctor/init/config paths.

Precedence:

1. CLI flags
2. environment variables
3. `~/.hyatlas/config/.env`
4. `~/.hyatlas/config/hy_memory.json`
5. legacy fallback paths
6. built-in defaults

Compatibility window:

- prefer `HYATLAS_HOME`
- auto-detect legacy paths
- warn when legacy paths are used
- never silently create a second empty Qdrant store when existing data is found elsewhere

### Patch 26 — Existing CLI upgrade: init/config/model/validate

Do not create a second CLI. Extend the existing `hyatlas` CLI.

Add:

```bash
hyatlas config show
hyatlas config model
hyatlas config validate
```

Upgrade `hyatlas init` with a simple Hindsight-style flow:

1. Choose runtime home (`~/.hyatlas` by default)
2. Enter OpenAI-compatible base URL
   - examples: `https://api.openai.com/v1`, `https://openrouter.ai/api/v1`, `https://api.minimax.io/v1`, `http://127.0.0.1:11434/v1`
3. Enter model name
4. Enter API key, masked
   - local servers may accept a dummy value like `local`, but the field is still collected for one consistent config shape
5. Choose mode:
   - `lite`: embeddings/search only, no LLM extraction
   - `pro`: LLM extraction per add
   - `ultra`: pro + System 2/Kuzu graph
6. Choose local embedder:
   - recommended: `BAAI/bge-large-en-v1.5`, 1024 dims
   - lightweight: `BAAI/bge-small-en-v1.5`, 384 dims
   - custom local sentence-transformers model/path + explicit dims
   - show a loud warning: changing embedder model or dims later requires a new Qdrant collection or full re-vectorization
7. Write config under `HYATLAS_HOME/config/`
8. Run `hyatlas config validate`

Provider scope now:

- one generic OpenAI-compatible `base_url + model + api_key` config path
- optional convenience examples in docs for OpenAI, MiniMax, TokenRouter/OpenRouter, Ollama/LM Studio
- no provider-specific auth logic in this pass

Out of scope for now:

- Hermes OAuth provider bridge
- MiniMax OAuth token reuse from Hermes auth state

Rules:

- Changing LLM model/provider does not require reindexing.
- Changing embedder model or vector dimensions requires re-vectorization or a new Qdrant collection.
- `hyatlas config show` always redacts secrets.

### Patch 27 — Start/status correctness

Fix service start and health checks so the system validates the actual data store, not just open ports.

Qdrant start resolution:

1. explicit config
2. `HYATLAS_QDRANT_BIN`
3. project/bundled managed binary
4. system PATH
5. clear failure

Qdrant health requires:

- port responds
- collection exists
- vector size matches configured dims
- storage path matches active config when known
- for existing installs, `points_count` must match/approx previous manifest and must not silently drop to 0

Fresh installs may have 0 points. Migrated/existing installs may not.

### Patch 28 — Snapshot + data migration

Only after patches 25-27 are verified.

Required commands/design:

```bash
hyatlas snapshot create --label pre-layout-migration
hyatlas migrate layout --dry-run
hyatlas migrate layout --apply
hyatlas migrate layout --rollback
```

Minimum behavior:

- stop services first
- snapshot before copy
- copy, do not delete old dirs
- record manifest: source, target, byte count, file count, redacted config hash, Qdrant point count, Kuzu graph count
- verify after migration: Qdrant points, memory search, graph counts, dashboard

### Patch 29 — Docs rewrite

Update README, INSTALL, CONFIGURATION, MIGRATION, TROUBLESHOOTING.

Fresh user flow:

```bash
git clone https://github.com/tuancookiez-hub/HyAtlas-Memory.git
cd HyAtlas-Memory
pip install -e .
hyatlas init
hyatlas start
```

Change model later:

```bash
hyatlas config model --base-url URL --model MODEL
hyatlas config validate
```

### Patch 30 — Legacy path warnings

Warn when using old roots:

```text
~/.hermes/hy_memory.json
~/.hyatlas
C:\qdrant-data
project-root/storage
```

### Patch 31 — Old-path removal

Future breaking release only.

## Non-goals

- No OAuth bridge in this pass.
- No destructive data move without snapshot + rollback.
- No embedder dimension migration bundled with LLM provider UX.
- No second CLI.

## Key regression to prevent

Qdrant can be `green` while serving the wrong empty storage. Health checks must catch this exact state:

```text
Qdrant up, collection exists, points_count = 0, existing manifest expected >0
=> WRONG STORAGE / FAIL
```
