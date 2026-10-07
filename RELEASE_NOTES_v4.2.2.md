# HyAtlas v4.2.2 — plugin prompt fix

> No breaking changes. Drop-in upgrade from 4.2.1.

## Fixed

**The agent-facing prompt advertised a tool that does not exist.** `system_prompt_block()` told the agent that a `hyatlas_save` tool was mirrored to the L1 Profile layer. No such tool was ever registered — the v4.2.0 `hy_memory` → `hyatlas` rename sweep wrongly renamed a reference to Hermes **core's** `memory` tool, which is not ours to rename.

The provider registers exactly four tools: `hyatlas_status`, `hyatlas_search`, `hyatlas_recent`, `hyatlas_add`. Mirroring into L1 happens through the `on_memory_write` hook, which core calls for its own `memory` tool. The prompt now names the real tools and refers to the standard `memory` tool correctly.

Found while auditing tool names for the Plugin Catalog submission disclosure.

## Added

- **Regression test** asserting every `hyatlas_*` tool name mentioned in `system_prompt_block()` is one `get_tool_schemas()` actually registers. The guard was verified by reverting the fix and confirming the test fails with the offending name.

## Assets

Single self-contained binary per platform — no Python at runtime, embeddings in-process.

- `hyatlas-go-v4.2.2-linux-amd64`
- `hyatlas-go-v4.2.2-macos-arm64`
- `hyatlas-go-v4.2.2-windows-amd64.exe`

**Full Changelog**: https://github.com/tuancookiez-hub/HyAtlas-Memory/compare/v4.2.1...v4.2.2
