# HyAtlas v4.2.3 — CI smoke-runner fix

> No breaking changes. Drop-in upgrade from 4.2.2. No server behaviour change — this is a test-infrastructure fix inside the plugin subtree.

## Fixed

**The CI `plugin-tests` job could never pass.** The standalone smoke runner classified a legitimate skip as a failure. `_check_live_server_round_trip` returned a skip signal when no v4 server was reachable, but `_run_all` derived its verdict by string-sniffing the message text (`ok and "SKIP" not in msg`), so "no live server" — the normal condition on CI, which runs no server — counted as a failed test and exited 1.

Checks now return an explicit `PASS` / `SKIP` / `FAIL` token. Both runners (standalone and pytest) consume that single contract, SKIP is pass-equivalent, and the outcome is never inferred from message text again.

**A config check asserted the default port as an invariant.** `_check_config_loads_clean` hardcoded `== 19528`, so it spuriously failed whenever the supported `HYATLAS_SERVER_PORT` override was in play. It now derives the expected port the way the plugin does (env override, else the 19528 default) and also asserts the value is a positive int. The SKIP message reports the configured `host:port` instead of a hardcoded address.

## Verified

| Condition | Standalone runner | pytest |
| --- | --- | --- |
| Live server present | exit 0, all PASS | 18 passed |
| No reachable server (CI) | exit **0**, `1 skipped` | `2 passed, 1 skipped` |
| Injected genuine FAIL | exit **1** | fails |

The negative test matters: it proves SKIP handling did not make the suite vacuous — a real failure still turns CI red.

## Assets

Single self-contained binary per platform — no Python at runtime, embeddings in-process.

- `hyatlas-go-v4.2.3-linux-amd64`
- `hyatlas-go-v4.2.3-macos-arm64`
- `hyatlas-go-v4.2.3-windows-amd64.exe`

**Full Changelog**: https://github.com/tuancookiez-hub/HyAtlas-Memory/compare/v4.2.2...v4.2.3
