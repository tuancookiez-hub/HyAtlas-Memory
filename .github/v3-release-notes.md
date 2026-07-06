## What's new in v3.0.0

![v3.0.0 Upgrade Overview](v3-upgrade-overview.jpeg)

**A full architecture evolution. From patched dependency to owned system. From text summaries to living knowledge graph.**

### Architecture
- **Full SDK fork.** Entire hy-memory 1.2.20 SDK (70 files, 42K+ lines) forked into `src/hyatlas_memory/core/`. Zero external pip dependency. Every line owned and maintained by HyAtlas.
- **23 monkey-patches → 13 first-class integrations.** No more runtime patching of upstream code. Native modules. Clean, maintainable, owned.
- **Unified runtime layout.** Scattered paths (7+ roots) consolidated into `HYATLAS_HOME` (`~/.hyatlas`). Config CLI: `hyatlas init`, `config show`, `config model`, `config validate`.

### New Features
- **L5 Knowledge Graph.** In-process entity/relation extraction → Kuzu graph database. Upstream hy-memory doesn't have this (their "L5" is just text summaries). Live endpoint at `/api/v1/graph`. Verified: 1,444 nodes, 6,374 relations.
- **Emotion-Aware Memory.** LLM-based valence/arousal scoring on every write. Emotionally significant memories resist time decay. Verified: `valence=0.95, arousal=0.9`.
- **Auto-forgetting.** Recency scoring + archival of stale memories.

### Reliability Fixes
- **Reasoning model compatibility.** Think-block parsing for MiniMax-M3, DeepSeek-R1, o1-style models. Handles closed AND unclosed/truncated think blocks. `agent_max_tokens` raised 1024 → 8192.
- **Kuzu WAL checkpoint.** `close()` now calls `CHECKPOINT + db.close()` instead of just nulling references. Upstream has the same bug. Verified: 0KB WAL after shutdown (was 7MB).
- **VDB circuit breaker.** Protects against Qdrant failures. State: CLOSED (healthy).
- **L1_RAW rolling delete + dedup.** Prevents unprocessed raw points from accumulating.
- **Multi-key LLM rotation.** `llm.api_keys` list probed at startup, first valid key wins.

### Model
- **Switched to deepseek-v4-flash.** Non-reasoning model, clean JSON output, zero parse errors. Previous: MiniMax-M3 (reasoning model requiring think-block workarounds).

### CI
- **Ruff lint clean.** Per-file-ignores for forked upstream code, our code fixed properly. All three Python versions (3.10/3.11/3.12) pass.

### Key Numbers
- 85 files changed, +29,144 lines
- 1,444 graph nodes, 6,374 relations
- 47 tests passing (33 offline + 14 server-dependent)
- 0 WAL bytes after shutdown
- 0 JSON parse errors with new model

**Full changelog:** https://github.com/tuancookiez-hub/HyAtlas-Memory/blob/main/CHANGELOG.md
