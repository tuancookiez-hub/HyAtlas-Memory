# HyAtlas v4.2.4 — async-persistence drain fix

> No breaking changes. Drop-in upgrade from 4.2.3.

## Fixed

**`Close()` never drained async persistence, despite its comment claiming to.** `persistUsageAsync()` spawned fire-and-forget goroutines with nothing tracking them, so `Close()` returned while they were still running. They then wrote `usage.json.tmp` into the data dir *after* the caller had moved on.

On CI this surfaced as `TempDir RemoveAll cleanup: unlinkat ...: directory not empty`, failing the whole `go test` run in the `Build (Go 1.26, plain — Linux)` job. `Close()` now tracks in-flight goroutines with a `sync.WaitGroup` and a `sync.Once`, so it is both synchronous and idempotent.

**Concurrent usage writes could clobber each other.** `persistUsage()` writes a single fixed `.tmp` path, and every `Add`/`Search` fires an async persist, so two goroutines could interleave and rename a half-written file. The write is now serialized by a mutex. This is a real (if narrow) corruption window on a busy server — not only a test artifact.

## Added

`store_close_test.go` (3 tests) covering the drain guarantee, `Close` idempotency under concurrent calls, and non-corruption of the usage file under concurrent persists.

The drain test was validated by reverting the fix and confirming it fails with the *exact* CI error. An earlier draft passed vacuously — it hardcoded the wrong filename and let the driver goroutines finish before `Close` — so it now derives the path from the store and keeps persists continuously in flight across the `Close` call.

## Assets

Single self-contained binary per platform — no Python at runtime, embeddings in-process.

- `hyatlas-go-v4.2.4-linux-amd64`
- `hyatlas-go-v4.2.4-macos-arm64`
- `hyatlas-go-v4.2.4-windows-amd64.exe`

**Full Changelog**: https://github.com/tuancookiez-hub/HyAtlas-Memory/compare/v4.2.3...v4.2.4
