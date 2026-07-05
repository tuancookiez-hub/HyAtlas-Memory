# HyAtlas-Memory — Current State

## v3.0.0 — Release-Ready (Final Verification Done)

**Branch:** `feat/v3-fork` (21 commits, 51 total)
**Tests:** 47 passed, 5 skipped
**Latest commit:** `cf1f998` — unclosed think-block parsing + agent_max_tokens 8192

### End-to-End Verification (2026-07-06)
- [x] Server health: VDB ok, embed ok, LLM ok (Qdrant + MiniMax-M3 + BGE)
- [x] Write: S1 extraction success (270 tokens, 10s, error_code=null)
- [x] L2 facts: YC test content extracted, tagged [startup, yc, milestone], score 0.672
- [x] Search: semantic recall working, returns relevant L2 facts
- [x] Graph: 1,358 nodes, 5,922 relations (live endpoint)
- [x] Circuit breaker: CLOSED (no VDB failures)
- [x] Tests: 47 passed, 5 skipped

### What's Working
- Full pipeline: L1_RAW → L2_FACT → L4_IDENTITY → L5_KNOWLEDGE (in-process) → S2 digest
- Emotion analyzer: valence/arousal wired into write path
- Kuzu WAL checkpoint: CHECKPOINT + db.close() on shutdown (verified: 0KB WAL)
- 3 reader strategies: legacy, hybrid_v2, hybrid_tag (3-channel RRF)
- Circuit breaker, L1_RAW rolling delete, dedup skip, multi-key rotation
- MiniMax-M3 compatibility: think-block stripping in all 3 _parse_json implementations
  - Handles: closed ⋖⋗, closed , unclosed ⋖ (truncated), markdown fences, regex fallback
- agent_max_tokens raised 2000 → 8192 (reasoning models need budget for thinking + output)

### Upstream Comparison (hy-memory 1.2.20)
- Upstream has NO L5 knowledge graph (their "L5" = profile summary text, not entity/relation extraction)
- Upstream has same Kuzu WAL bug (just nulls refs, no checkpoint)
- Upstream _parse_json: no think-block handling (markdown fence + regex fallback only)
- Upstream agent_max_tokens default: 2000 (too low for reasoning models)
- Upstream HY_MEMORY_THINKING_MODE=disabled works for DeepSeek/Qwen/Kimi/Hunyuan but NOT MiniMax
- Our fork is genuinely ahead on reasoning model compatibility and L5 reliability

### LLM Configuration
- Current: deepseek-v4-flash via https://hyper.charm.land/v1 (switched from MiniMax-M3)
- No reasoning/thinking — clean JSON output, no think-block stripping needed
- Our think-block parsing remains as a safety net for future reasoning model use

### Next Steps
1. Merge feat/v3-fork to main, tag v3.0.0 (awaiting Tuna's approval)
2. Run formal benchmarks (LongMemEval/LoCoMo)
3. Consider upstreaming resilience features (circuit breaker, WAL checkpoint, think-block parsing)
