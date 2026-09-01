# Contributing to HyAtlas Memory

Thanks for your interest. HyAtlas v4 is a pure-Go rewrite — contributions should respect the new architecture.

## What to work on

Open an issue before opening a PR for any non-trivial change. The `feat/l1-raw-transparency-and-system2-tuning` branch (in the v3.5 history, now archived) shows the kinds of changes the maintainer reviews.

## Build

```bash
# Embedded build (model weights bundled in the binary)
go build -tags embedded -o hyatlas-go.exe .

# Non-embedded build (reads models/ from disk)
go build -o hyatlas-go.exe .
```

Requires:
- Go 1.26+
- MinGW-W64 (cgo for onnxruntime-go) on Windows: `winget install BrechtSanders.WinLibs.POSIX.UCRT`
- onnxruntime 1.28.1 DLL on Windows (matches `onnxruntime_go` v1.32.0's declared API)

## Test

```bash
go vet ./...
go build ./...
```

(End-to-end tests live in the user's own runtime, not in this repo.)

## Commit style

- Imperative subject: `feat: add L6 schema endpoint`, `fix: panic on empty search results`
- One concern per commit
- Reference any related issue in the body: `Closes #N`

## Code style

- Standard `gofmt` formatting
- Prefer stdlib over dependencies
- All HTTP handlers return JSON, never HTML
- Errors logged with context, never swallowed
- Long-running operations: context.Context first parameter

## License

Apache 2.0. By contributing, you agree to license your work under the same terms.
