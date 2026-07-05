# HyAtlas-Memory — Current State

## v3.0.0 — Battle-Tested and Maturing

**Branch:** `feat/v3-fork` (14 commits)
**Tests:** 51 passed, 1 skipped
**Latest commit:** `8181f42` — restored hybrid_tag reader, emotion analyzer, arousal-weighted strength

### What's Working
- Full pipeline: L1_RAW → L2_FACT extraction → L4_IDENTITY → L5_KNOWLEDGE (in-process) → S2 digest
- 3 reader strategies: legacy, hybrid_v2, hybrid_tag (3-channel RRF)
- Emotion analyzer restored (valence/arousal scoring, think-block parsing fix)
- Arousal-weighted memory strength: emotionally significant memories decay slower
- Circuit breaker, L1_RAW rolling delete, L1_RAW dedup skip, multi-key rotation
- Dashboard with live graph, CLI (hyatlas init/config/status/start/stop)
- MiniMax-M3 / reasoning model compatibility (think-block stripping in all JSON parsers)

### Architecture
- 7-layer model: L1 RAW, L2 FACT, L3 SUMMARY, L4 IDENTITY, L5 KNOWLEDGE, L6 SCHEMA, L7 INTENTION
- Storage: Qdrant (VDB) + Kuzu (graph) + SQLite (cache/history)
- 51,559 lines across 121 files — zero external hy-memory dependency
- Coding judge disabled (incompatible with agent OS tool-heavy workflows)

### Upstream Comparison (vs hy-memory 1.2.20)
- **Our advantages:** L5 implemented (upstream stub), agent OS compat, reasoning model support, resilience patterns, operational tooling, no upstream drift risk
- **Upstream advantages:** benchmark validation (85.20% LongMemEval), coding memory path (3,823 lines), backend diversity (Chroma/FAISS/Neo4j/MySQL), Chinese prompts, emotion→strength connection (now restored in our fork)
- **Gap closed:** emotion analyzer + arousal-weighted strength + hybrid_tag reader restored from upstream; only coding path and Chinese prompts remain as upstream-only features

### Next Steps
1. Wire emotion analyzer into extractor write path (currently restored but not called during writes)
2. Run LoCoMo/LongMemEval benchmarks to get real accuracy numbers
3. Consider upstreaming resilience features (circuit breaker, think-block parsing) to Tencent
4. Docs rewrite for v3.0.0 release
