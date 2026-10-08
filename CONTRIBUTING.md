# Contributing to HyAtlas Memory

Thanks for your interest. HyAtlas v4 is a pure-Go rewrite — contributions should respect the new architecture.

## What to work on

Open an issue before opening a PR for any non-trivial change. The `feat/l1-raw-transparency-and-system2-tuning` branch is a remote branch on origin (`origin/feat/l1-raw-transparency-and-system2-tuning`) and shows the kinds of changes the maintainer has reviewed before.

## Build

```bash
# Embedded build (BGE model and onnxruntime bundled in the binary).
# Needs models/ populated first; see "Embedded build inputs" below.
go build -tags embedded -o hyatlas-go .

# Plain build (small; reads the model from a models/ folder at runtime)
go build -o hyatlas-go .
```

On Windows, name the output `hyatlas-go.exe`.

Requires:
- Go 1.26+ (`go.mod` declares 1.26.5)
- A C toolchain, since the build uses cgo for onnxruntime-go: gcc or clang on Linux, Xcode command-line tools on macOS, MinGW-W64 on Windows (`winget install BrechtSanders.WinLibs.POSIX.UCRT`)
- At run time, a plain build needs the onnxruntime 1.28.1 shared library (`onnxruntime.dll`, `libonnxruntime.so` or `libonnxruntime.dylib`) next to the model. It matches `onnxruntime_go` v1.32.0's declared API. The embedded build carries it.

### Embedded build inputs

`assets_embedded_<os>.go` uses `go:embed` on these files in `models/`, so the build fails without them:

| File | Source used by CI (`.github/workflows/release.yml`) |
|---|---|
| `bge-small-en-v1.5.onnx` | `https://huggingface.co/Xenova/bge-small-en-v1.5/resolve/main/onnx/model.onnx` |
| `bge-small-en-v1.5.onnx.data` | an empty file is fine (`: > models/bge-small-en-v1.5.onnx.data`) |
| `vocab.txt` | `https://huggingface.co/Xenova/bge-small-en-v1.5/resolve/main/vocab.txt` |
| `libonnxruntime.so` / `.dylib` / `onnxruntime.dll` | the onnxruntime 1.28.1 release archive for your platform |

## Test

```bash
go vet ./...
go test ./...
go build ./...
```

CI (`.github/workflows/tests.yml`) runs these on Linux, plus a smoke test of a plain build with `HYATLAS_EMBED_BASE=local`, and a compile check of the embedded build on each OS. The release workflow smoke-tests the embedded binary against the real BGE model. Plugin tests (`plugins/hyatlas/tests`) run separately.

### Testing the installer

`scripts/install.sh` runs the installer when it is executed or piped to bash. Sourcing
it also runs the installer, unless `HYATLAS_INSTALL_LIB=1` is set. With that variable,
sourcing defines the functions without running `main`, so a test can call them one by
one. The variable is a testing aid only. If it is set on a plain run, the script warns
and runs the installer anyway.

## Commit style

- Imperative subject: `feat: add L6 schema endpoint`, `fix: panic on empty search results`
- One concern per commit
- Reference any related issue in the body: `Closes #N`

## Code style

- Standard `gofmt` formatting
- Prefer stdlib over dependencies
- API handlers return JSON. The one exception is the static dashboard under `/dashboard/`, which serves HTML, CSS and JS from the embedded `dashboard/dist`.
- Errors logged with context, not silently dropped
- Long-running operations take a `context.Context` as the first parameter

## License

Apache 2.0. By contributing, you agree to license your work under the same terms.
