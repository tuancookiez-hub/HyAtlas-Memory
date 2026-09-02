#!/usr/bin/env bash
# HyAtlas-Memory v4 — one-line installer for Linux / macOS / Windows (Git Bash / MSYS).
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/tuancookiez-hub/HyAtlas-Memory/main/scripts/install.sh | bash
#
# What it does:
#   1. Detects your OS (linux / macos / windows) and architecture (amd64 / arm64)
#   2. Downloads a prebuilt release binary for your platform, if one exists
#   3. If no prebuilt binary exists, falls back to building from source
#      (requires Go 1.26+ and a C compiler — it checks and tells you)
#   4. Fetches the BGE-small embedding model (~133 MB) — required for the
#      server to run. Cached in ~/.hyatlas/models or $HYATLAS_MODEL_DIR.
#   5. Installs the binary to a directory on your PATH
#   6. Verifies the install by starting the server and hitting /healthz
#
# Environment variables (all optional):
#   HYATLAS_VERSION   — release tag to install (default: v4.0.1)
#   HYATLAS_INSTALL_DIR — where to put the binary (default: ~/.local/bin, or
#                         %LOCALAPPDATA%\hyatlas on Windows)
#   HYATLAS_MODEL_DIR — where to cache the model (default: ~/.hyatlas/models)
#   HYATLAS_NO_MODEL  — set to 1 to skip the model download (server will
#                       fail to start until you supply models/ manually)

set -euo pipefail

REPO="tuancookiez-hub/HyAtlas-Memory"
VERSION="${HYATLAS_VERSION:-v4.0.1}"
INSTALL_DIR="${HYATLAS_INSTALL_DIR:-}"
MODEL_DIR="${HYATLAS_MODEL_DIR:-}"
NO_MODEL="${HYATLAS_NO_MODEL:-0}"

# Model files needed by the server (BGE-small-en-v1.5, Xenova ONNX export)
MODEL_BASE="https://huggingface.co/Xenova/bge-small-en-v1.5/resolve/main"
MODEL_FILES=(
    "onnx/model.onnx:bge-small-en-v1.5.onnx"
    "vocab.txt:vocab.txt"
)

# onnxruntime version must match onnxruntime_go v1.32.0's declared API (28)
ORT_VERSION="1.28.1"

info()  { printf '\033[0;34m==>\033[0m %s\n' "$*"; }
ok()    { printf '\033[0;32m  ✓\033[0m %s\n' "$*"; }
warn()  { printf '\033[0;33m  !\033[0m %s\n' "$*" >&2; }
err()   { printf '\033[0;31m  ✗\033[0m %s\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------

detect_platform() {
    local os arch
    case "$(uname -s)" in
        Linux*)   os="linux" ;;
        Darwin*)  os="macos" ;;
        MINGW*|MSYS*|CYGWIN*) os="windows" ;;
        *)        err "Unsupported OS: $(uname -s). Install manually — see README." ;;
    esac

    case "$(uname -m)" in
        x86_64|amd64) arch="amd64" ;;
        arm64|aarch64) arch="arm64" ;;
        *) err "Unsupported architecture: $(uname -m). Only amd64 and arm64 are prebuilt." ;;
    esac

    PLATFORM_OS="$os"
    PLATFORM_ARCH="$arch"
    # Asset naming: hyatlas-go-<version>-<os>-<arch>[.exe]
    local suffix=""
    [ "$os" = "windows" ] && suffix=".exe"
    ASSET_NAME="hyatlas-go-${VERSION}-${os}-${arch}${suffix}"
    BINARY_NAME="hyatlas-go${suffix}"
}

set_default_install_dir() {
    [ -n "$INSTALL_DIR" ] && return
    case "$PLATFORM_OS" in
        windows) INSTALL_DIR="${LOCALAPPDATA:-$HOME/AppData/Local}/hyatlas" ;;
        macos)   INSTALL_DIR="$HOME/.local/bin" ;;
        linux)   INSTALL_DIR="$HOME/.local/bin" ;;
    esac
}

# ---------------------------------------------------------------------------
# PATH handling
# ---------------------------------------------------------------------------

ensure_on_path() {
    case ":$PATH:" in
        *":$INSTALL_DIR:"*) return 0 ;;
    esac
    warn "$INSTALL_DIR is not on your PATH."
    local shellrc=""
    case "${SHELL:-}" in
        */bash) shellrc="$HOME/.bashrc" ;;
        */zsh)  shellrc="$HOME/.zshrc" ;;
        */fish) shellrc="$HOME/.config/fish/config.fish" ;;
    esac
    if [ -n "$shellrc" ]; then
        info "Adding $INSTALL_DIR to PATH in $shellrc"
        if [ "${SHELL##*/}" = "fish" ]; then
            echo "set -gx PATH \$PATH $INSTALL_DIR" >> "$shellrc"
        else
            echo "export PATH=\"\$PATH:$INSTALL_DIR\"" >> "$shellrc"
        fi
        warn "Restart your shell (or run: source $shellrc) to pick it up."
    else
        warn "Add $INSTALL_DIR to your PATH manually."
    fi
}

# ---------------------------------------------------------------------------
# Download prebuilt binary
# ---------------------------------------------------------------------------

try_download_binary() {
    local url="https://github.com/$REPO/releases/download/$VERSION/$ASSET_NAME"
    info "Looking for prebuilt binary: $ASSET_NAME"
    if curl -fsSL --retry 3 -o "$TMP_DIR/$BINARY_NAME" "$url" 2>/dev/null; then
        ok "Downloaded prebuilt binary ($(du -h "$TMP_DIR/$BINARY_NAME" | cut -f1))"
        return 0
    fi
    info "No prebuilt binary for this platform yet — will build from source."
    return 1
}

# ---------------------------------------------------------------------------
# Build from source
# ---------------------------------------------------------------------------

check_build_prereqs() {
    command -v go >/dev/null 2>&1 || err "Go is required to build from source.
    Install Go 1.26+: https://go.dev/dl/
    Or set HYATLAS_VERSION to a release that has a prebuilt binary for $PLATFORM_OS-$PLATFORM_ARCH."

    local go_version
    go_version="$(go version | awk '{print $3}' | sed 's/go//')"
    local go_major go_minor
    go_major="$(echo "$go_version" | cut -d. -f1)"
    go_minor="$(echo "$go_version" | cut -d. -f2)"
    if [ "$go_major" -lt 1 ] || { [ "$go_major" -eq 1 ] && [ "$go_minor" -lt 26 ]; }; then
        err "Go 1.26+ required (found $go_version). Update: https://go.dev/dl/"
    fi

    # C compiler is required because onnxruntime-go uses cgo
    if ! command -v gcc >/dev/null 2>&1 && ! command -v cc >/dev/null 2>&1 && ! command -v clang >/dev/null 2>&1; then
        err "A C compiler is required (onnxruntime-go uses cgo).
    Linux:  apt install gcc        (or your distro's equivalent)
    macOS:  xcode-select --install
    Windows: winget install BrechtSanders.WinLibs.POSIX.UCRT  (then restart your terminal)"
    fi
    ok "Build prerequisites OK (Go $go_version, C compiler present)"
}

build_from_source() {
    info "Building HyAtlas-Go from source (this takes 1-3 minutes)..."
    local repo_dir="$TMP_DIR/source"
    git clone --depth 1 "https://github.com/$REPO.git" "$repo_dir" >/dev/null 2>&1 \
        || err "git clone failed into $repo_dir. Is git installed and is GitHub reachable?
    (Note: on Windows this script must run from Git Bash / MSYS, not cmd.exe.)"
    cd "$repo_dir"
    CGO_ENABLED=1 go build -o "$TMP_DIR/$BINARY_NAME" . \
        || err "Build failed. See the error above; check that you have Go 1.26+ and a C compiler."
    ok "Built from source ($(du -h "$TMP_DIR/$BINARY_NAME" | cut -f1))"
    cd - >/dev/null
}

# ---------------------------------------------------------------------------
# Model download (required for the server to run)
# ---------------------------------------------------------------------------

set_model_dir() {
    [ -n "$MODEL_DIR" ] && return
    case "$PLATFORM_OS" in
        windows) MODEL_DIR="${LOCALAPPDATA:-$HOME/AppData/Local}/hyatlas/models" ;;
        *)       MODEL_DIR="$HOME/.hyatlas/models" ;;
    esac
}

download_model() {
    [ "$NO_MODEL" = "1" ] && { warn "Skipping model download (HYATLAS_NO_MODEL=1)."; return; }

    # Skip if the model AND the onnxruntime library are both already there
    local need_download=0
    for f in bge-small-en-v1.5.onnx vocab.txt; do
        [ -f "$MODEL_DIR/$f" ] || need_download=1
    done
    if ! ls "$MODEL_DIR"/onnxruntime* "$MODEL_DIR"/libonnxruntime* >/dev/null 2>&1; then
        need_download=1
    fi
    if [ "$need_download" = "0" ]; then
        ok "Model + onnxruntime library already present in $MODEL_DIR"
        return
    fi

    info "Downloading BGE-small embedding model (~133 MB) to $MODEL_DIR"
    info "This is required — the server cannot start without it."
    info "Press Ctrl+C to abort; you can re-run this installer later."
    mkdir -p "$MODEL_DIR"

    for pair in "${MODEL_FILES[@]}"; do
        local remote="${pair%%:*}" local_name="${pair##*:}"
        local url="$MODEL_BASE/$remote"
        info "  fetching $local_name"
        if ! curl -fsSL --retry 3 -o "$MODEL_DIR/$local_name.part" "$url"; then
            rm -f "$MODEL_DIR/$local_name.part"
            err "Model download failed: $url
    The server will not start without the BGE model.
    You can:
      (a) re-run this installer when you have a better connection, or
      (b) download $local_name manually from $url
          and place it in $MODEL_DIR/"
        fi
        mv "$MODEL_DIR/$local_name.part" "$MODEL_DIR/$local_name"
    done

    # The onnxruntime shared library is also required — the embedder needs it.
    # Name and download URL differ per OS.
    download_onnxruntime
    ok "Model + onnxruntime library ready in $MODEL_DIR"
}

download_onnxruntime() {
    # Already present under any accepted name? (bge.go has a findLibFallback
    # that accepts onnxruntime* or libonnxruntime* prefixes.)
    if ls "$MODEL_DIR"/onnxruntime* "$MODEL_DIR"/libonnxruntime* >/dev/null 2>&1; then
        ok "onnxruntime library already present"
        return
    fi

    local pkg base url
    case "$PLATFORM_OS" in
        windows) pkg="win-x64" ;;
        macos)   pkg="osx-arm64" ;;
        linux)   pkg="linux-x64" ;;
    esac
    base="https://github.com/microsoft/onnxruntime/releases/download/v${ORT_VERSION}/onnxruntime-${pkg}-${ORT_VERSION}"

    info "Fetching onnxruntime ${ORT_VERSION} for ${PLATFORM_OS} (${pkg})..."
    local payload="$TMP_DIR/ort_payload"
    rm -rf "$payload"; mkdir -p "$payload"

    case "$PLATFORM_OS" in
        windows)
            url="${base}.zip"
            curl -fsSL --retry 3 -o "$payload/ort.zip" "$url" \
                || err "onnxruntime download failed: $url"
            # Extraction fallback chain: bsdtar (ships with Windows 10+ and
            # reads zip natively) -> unzip -> PowerShell Expand-Archive.
            if tar -xzf "$payload/ort.zip" -C "$payload" 2>/dev/null; then
                :
            elif command -v unzip >/dev/null 2>&1; then
                unzip -o -q "$payload/ort.zip" -d "$payload"
            elif command -v powershell >/dev/null 2>&1; then
                powershell -NoProfile -Command \
                    "Expand-Archive -Force '$(cygpath -w "$payload/ort.zip" 2>/dev/null || echo "$payload/ort.zip")' '$(cygpath -w "$payload" 2>/dev/null || echo "$payload")'"
            else
                err "No unzip tool found (tried tar, unzip, powershell).
    Download onnxruntime.dll manually from $url
    and place it in $MODEL_DIR/"
            fi
            cp "$payload/onnxruntime-${pkg}-${ORT_VERSION}/lib/onnxruntime.dll" "$MODEL_DIR/"
            ;;
        *)
            url="${base}.tgz"
            curl -fsSL --retry 3 -o "$payload/ort.tgz" "$url" \
                || err "onnxruntime download failed: $url"
            tar -xzf "$payload/ort.tgz" -C "$payload"
            local src="$payload/onnxruntime-${pkg}-${ORT_VERSION}/lib"
            if [ "$PLATFORM_OS" = "macos" ]; then
                cp "$src/libonnxruntime.dylib" "$MODEL_DIR/"
            else
                cp "$src/libonnxruntime.so" "$MODEL_DIR/"
            fi
            ;;
    esac
    ok "onnxruntime library installed"
}

# ---------------------------------------------------------------------------
# Install + verify
# ---------------------------------------------------------------------------

install_binary() {
    info "Installing to $INSTALL_DIR/$BINARY_NAME"
    mkdir -p "$INSTALL_DIR"
    cp "$TMP_DIR/$BINARY_NAME" "$INSTALL_DIR/$BINARY_NAME"
    chmod +x "$INSTALL_DIR/$BINARY_NAME"
    ok "Installed: $INSTALL_DIR/$BINARY_NAME"
}

verify_install() {
    info "Verifying install (starting server, probing /healthz)..."

    # Start the server on a scratch port so we don't clash with a running instance
    local probe_port=19599
    local data_dir
    data_dir="$(mktemp -d)"
    trap 'kill $pid 2>/dev/null || true; rm -rf "$data_dir"' RETURN

    # The binary is env-var configured (no CLI flags). Set the essentials:
    #   HYATLAS_EMBED_BASE=bge   -> use the local BGE model, not the HTTP embedder
    #   HYATLAS_MODEL_DIR        -> where the BGE model lives
    #   HYATLAS_GO_DATA          -> scratch data dir so we don't touch real memory
    # LLM points at a dead port on purpose: the server still starts and
    # reports /healthz ok (vdb + embed healthy, llm degraded). That's enough
    # to prove the install works; a real LLM key is a separate config step.
    HYATLAS_GO_PORT="$probe_port" \
    HYATLAS_GO_DATA="$data_dir" \
    HYATLAS_EMBED_BASE="bge" \
    HYATLAS_MODEL_DIR="$MODEL_DIR" \
    HYATLAS_LLM_BASE="http://127.0.0.1:1/v1" \
    HYATLAS_LLM_MODEL="probe" \
    HYATLAS_LLM_KEY="probe" \
        "$INSTALL_DIR/$BINARY_NAME" >/dev/null 2>&1 &
    local pid=$!

    # Wait up to 15s for the server to come up (model load can take a few seconds)
    local i
    for i in $(seq 1 30); do
        if curl -fsS "http://127.0.0.1:$probe_port/healthz" >/dev/null 2>&1; then
            ok "Server started and /healthz responded."
            kill $pid 2>/dev/null || true
            rm -rf "$data_dir"
            trap - RETURN
            return 0
        fi
        sleep 0.5
    done

    kill $pid 2>/dev/null || true
    rm -rf "$data_dir"
    trap - RETURN
    warn "Server did not respond within 15s. It installed, but may need attention."
    warn "Check logs by running: $INSTALL_DIR/$BINARY_NAME"
    return 1
}

print_next_steps() {
    cat <<EOF

$(printf '\033[0;32m' )HyAtlas-Memory v4 installed.$(printf '\033[0m')

  Binary:  $INSTALL_DIR/$BINARY_NAME
  Model:   $MODEL_DIR

  Start the server (local BGE embeddings, loopback only):
      export HYATLAS_EMBED_BASE=bge
      export HYATLAS_MODEL_DIR="$MODEL_DIR"
      $BINARY_NAME

  Then set your LLM endpoint (any OpenAI-compatible API):
      export HYATLAS_LLM_BASE="http://127.0.0.1:49200/v1"
      export HYATLAS_LLM_MODEL="deepseek:deepseek-v4-flash"
      export HYATLAS_LLM_KEY="your-key"

  Wire it into Hermes (in ~/.hermes/config.yaml):
      memory:
        provider: hy_memory
        providers:
          hy_memory:
            server_port: 19528

  Docs: https://github.com/$REPO#readme
EOF
}

main() {
    info "HyAtlas-Memory v4 installer"
    detect_platform
    set_default_install_dir
    set_model_dir
    info "Platform: $PLATFORM_OS-$PLATFORM_ARCH"
    info "Install dir: $INSTALL_DIR"
    info "Model dir: $MODEL_DIR"

    TMP_DIR="$(mktemp -d)"
    # MSYS trap: mktemp gives a POSIX path (/tmp/...) that native Windows
    # tools (git, go) cannot resolve. Convert to a native path before any
    # native tool touches it.
    if [ "$PLATFORM_OS" = "windows" ]; then
        TMP_DIR="$(cd "$TMP_DIR" && pwd -W 2>/dev/null || echo "$TMP_DIR")"
    fi
    trap 'rm -rf "$TMP_DIR"' EXIT

    if ! try_download_binary; then
        check_build_prereqs
        build_from_source
    fi

    download_model
    install_binary
    ensure_on_path
    verify_install || true
    print_next_steps
}

main "$@"
