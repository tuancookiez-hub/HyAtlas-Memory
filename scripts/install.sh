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
#   4. Source builds only: fetches the BGE-small embedding model (~133 MB) and
#      onnxruntime. Release binaries are embedded builds and already carry the
#      model, so nothing is downloaded for them. Cached in ~/.hyatlas/models
#      (Windows: %LOCALAPPDATA%\hyatlas\models) or $HYATLAS_MODEL_DIR. If the
#      download fails, the binary is still installed, the manual steps are printed,
#      and the script exits non-zero at the end.
#   5. Installs the binary to a directory on your PATH
#   6. Verifies the install by starting the server and hitting /healthz
#
# Environment variables (all optional):
#   HYATLAS_VERSION   — release tag to install (default: v4.3.3)
#   HYATLAS_INSTALL_DIR — where to put the binary (default: ~/.local/bin, or
#                         %LOCALAPPDATA%\hyatlas on Windows)
#   HYATLAS_MODEL_DIR — where to cache the model (default: ~/.hyatlas/models)
#   HYATLAS_NO_MODEL  — set to 1 to skip the model download (server will
#                       fail to start until you supply models/ manually
#   HYATLAS_MODE      — lite | pro | ultra (default ultra). lite does no LLM
#                       extraction at all, so conversation text never leaves
#                       the machine; pro extracts per write and reasons within
#                       one turn; ultra adds the slow path — periodic
#                       consolidation that reasons across memories.
#   HYATLAS_SYNC_EXTRACT
#                     — on | off. Whether a write blocks on extraction. Unset
#                       follows the mode (pro blocks, ultra does not).
#   HYATLAS_CONSOLIDATE_EVERY
#                     — ultra only; how often the slow path runs (default 6h).
#   HYATLAS_CONSOLIDATE_BATCH
#                     — ultra only; max facts per consolidation call (200).
#   HYATLAS_RAW_RETENTION
#                     — ultra only; age after which uncited L2 raw is decayed.
#                       Unset means raw history is never deleted.)

set -euo pipefail

REPO="tuancookiez-hub/HyAtlas-Memory"
VERSION="${HYATLAS_VERSION:-v4.3.3}"
INSTALL_DIR="${HYATLAS_INSTALL_DIR:-}"
MODEL_DIR="${HYATLAS_MODEL_DIR:-}"
NO_MODEL="${HYATLAS_NO_MODEL:-0}"
# 1 when the binary came from a release asset. Release binaries are built with
# -tags embedded (.github/workflows/release.yml), so they carry the BGE model
# and onnxruntime needs no separate download.
BINARY_EMBEDDED=0
MODEL_STATUS=0

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
# soft_err: a failure the installer can survive. Prints like err but returns 1.
soft_err() { printf '\033[0;31m  ✗\033[0m %s\n' "$*" >&2; }

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
        BINARY_EMBEDDED=1
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

# print_manual_model_steps — where the files go, for a model fetched by hand.
print_manual_model_steps() {
    warn "The BGE model could not be installed. The server will not start without it."
    warn "Download these into $MODEL_DIR/ (create it first), then re-run this installer:"
    warn "  1. $MODEL_BASE/onnx/model.onnx  ->  $MODEL_DIR/bge-small-en-v1.5.onnx"
    warn "  2. $MODEL_BASE/vocab.txt        ->  $MODEL_DIR/vocab.txt"
    warn "  3. onnxruntime ${ORT_VERSION} shared library (onnxruntime.dll / libonnxruntime.so /"
    warn "     libonnxruntime.dylib) from https://github.com/microsoft/onnxruntime/releases"
    warn "     -> $MODEL_DIR/"
    warn "Or set HYATLAS_MODEL_DIR to the folder that already holds them. The server also"
    warn "looks in a models/ folder next to the binary."
}

# bge_present / ort_present — the two kinds of file the server needs in MODEL_DIR.
# ort_present accepts any onnxruntime* / libonnxruntime* name, matching bge.go's
# findLibFallback, so a library the user placed by hand counts.
bge_present() {
    [ -f "$MODEL_DIR/bge-small-en-v1.5.onnx" ] && [ -f "$MODEL_DIR/vocab.txt" ]
}
ort_present() {
    # Glob test, not `ls a* b*`: ls exits non-zero when either pattern has no
    # match, so a libonnxruntime.so alone would be reported as missing.
    local f
    for f in "$MODEL_DIR"/onnxruntime* "$MODEL_DIR"/libonnxruntime*; do
        [ -e "$f" ] && return 0
    done
    return 1
}

# ort_lib_name — the file name this installer writes for the onnxruntime library.
ort_lib_name() {
    case "$PLATFORM_OS" in
        windows) echo "onnxruntime.dll" ;;
        macos)   echo "libonnxruntime.dylib" ;;
        *)       echo "libonnxruntime.so" ;;
    esac
}

# onnxruntime_package — set ORT_PKG to the release asset suffix for this OS and
# arch (onnxruntime-<ORT_PKG>-<ORT_VERSION>.tgz|.zip). Asset names are the ones
# microsoft/onnxruntime publishes for ORT_VERSION; an unlisted combination fails
# here, before any download, instead of fetching the wrong library.
onnxruntime_package() {
    case "$PLATFORM_OS-$PLATFORM_ARCH" in
        linux-amd64)   ORT_PKG="linux-x64" ;;
        linux-arm64)   ORT_PKG="linux-aarch64" ;;
        macos-arm64)   ORT_PKG="osx-arm64" ;;
        windows-amd64) ORT_PKG="win-x64" ;;
        windows-arm64) ORT_PKG="win-arm64" ;;
        *)
            soft_err "No onnxruntime ${ORT_VERSION} build is published for $PLATFORM_OS-$PLATFORM_ARCH."
            soft_err "Install onnxruntime ${ORT_VERSION} yourself and put its shared library in the model folder."
            return 1
            ;;
    esac
}

# extract_zip ARCHIVE DEST — bsdtar (Windows 10+) -> unzip -> PowerShell.
# Every branch checks its own status: this runs inside `cmd || ...`, where set -e
# is ignored, so a failed extraction must be caught here or it goes unnoticed.
extract_zip() {
    if tar -xf "$1" -C "$2" 2>/dev/null; then
        return 0
    fi
    if command -v unzip >/dev/null 2>&1; then
        unzip -o -q "$1" -d "$2" || return 1
        return 0
    fi
    if command -v powershell >/dev/null 2>&1; then
        powershell -NoProfile -Command \
            "Expand-Archive -Force '$(cygpath -w "$1" 2>/dev/null || echo "$1")' '$(cygpath -w "$2" 2>/dev/null || echo "$2")'" \
            || return 1
        return 0
    fi
    soft_err "No unzip tool found (tried tar, unzip, powershell)."
    return 1
}

# download_model — returns 1 (never exits) on failure, so main() can still install
# the binary. Release binaries skip this entirely: they already carry the model.
#
# This runs as `download_model || MODEL_STATUS=$?`, and bash ignores set -e in
# every function reached that way, so each fallible command below carries its
# own failure check. "ready" is printed only after the files are confirmed.
download_model() {
    if [ "$BINARY_EMBEDDED" = "1" ]; then
        ok "Release binary is embedded: the BGE model and onnxruntime are included, no download needed."
        return 0
    fi
    [ "$NO_MODEL" = "1" ] && { warn "Skipping model download (HYATLAS_NO_MODEL=1)."; return 0; }

    if bge_present && ort_present; then
        ok "Model + onnxruntime library already present in $MODEL_DIR"
        return 0
    fi

    # Refuse an unsupported platform before fetching 133 MB of model for nothing.
    # Only needed when this run has to fetch onnxruntime itself.
    if ! ort_present && ! onnxruntime_package; then
        print_manual_model_steps
        return 1
    fi

    info "Downloading BGE-small embedding model (~133 MB) to $MODEL_DIR"
    info "This is required — the server cannot start without it."
    info "Press Ctrl+C to abort; you can re-run this installer later."
    if ! mkdir -p "$MODEL_DIR"; then
        soft_err "Cannot create model folder $MODEL_DIR"
        print_manual_model_steps
        return 1
    fi

    local pair remote local_name url
    for pair in "${MODEL_FILES[@]}"; do
        remote="${pair%%:*}"
        local_name="${pair##*:}"
        url="$MODEL_BASE/$remote"
        info "  fetching $local_name"
        if ! curl -fsSL --retry 3 -o "$MODEL_DIR/$local_name.part" "$url"; then
            rm -f "$MODEL_DIR/$local_name.part" || true
            soft_err "Model download failed: $url"
            print_manual_model_steps
            return 1
        fi
        if ! mv "$MODEL_DIR/$local_name.part" "$MODEL_DIR/$local_name"; then
            soft_err "Could not move the download into place: $MODEL_DIR/$local_name"
            print_manual_model_steps
            return 1
        fi
    done

    # The onnxruntime shared library is also required — the embedder needs it.
    # Name and download URL differ per OS.
    if ! download_onnxruntime; then
        print_manual_model_steps
        return 1
    fi

    # Final gate: say "ready" only when the files the server loads are on disk.
    if ! bge_present; then
        soft_err "Model files are missing from $MODEL_DIR after the download."
        print_manual_model_steps
        return 1
    fi
    if ! ort_present; then
        soft_err "The onnxruntime library is missing from $MODEL_DIR after the download."
        print_manual_model_steps
        return 1
    fi
    ok "Model + onnxruntime library ready in $MODEL_DIR"
    return 0
}

# download_onnxruntime — fetch and install the onnxruntime shared library for
# ORT_VERSION. Returns 1 with a message on any failure; see download_model for why
# every command is checked explicitly.
download_onnxruntime() {
    if ort_present; then
        ok "onnxruntime library already present"
        return 0
    fi

    onnxruntime_package || return 1
    local base="https://github.com/microsoft/onnxruntime/releases/download/v${ORT_VERSION}/onnxruntime-${ORT_PKG}-${ORT_VERSION}"
    local lib_name src url payload

    info "Fetching onnxruntime ${ORT_VERSION} for ${PLATFORM_OS} (${ORT_PKG})..."
    payload="$TMP_DIR/ort_payload"
    if ! { rm -rf "$payload" && mkdir -p "$payload"; }; then
        soft_err "Cannot create $payload"
        return 1
    fi
    lib_name="$(ort_lib_name)"

    if [ "$PLATFORM_OS" = "windows" ]; then
        url="${base}.zip"
        if ! curl -fsSL --retry 3 -o "$payload/ort.zip" "$url"; then
            soft_err "onnxruntime download failed: $url"
            return 1
        fi
        if ! extract_zip "$payload/ort.zip" "$payload"; then
            soft_err "Could not extract $url"
            return 1
        fi
    else
        url="${base}.tgz"
        if ! curl -fsSL --retry 3 -o "$payload/ort.tgz" "$url"; then
            soft_err "onnxruntime download failed: $url"
            return 1
        fi
        if ! tar -xzf "$payload/ort.tgz" -C "$payload"; then
            soft_err "Could not extract $url (truncated or corrupt archive?)"
            return 1
        fi
    fi

    src="$payload/onnxruntime-${ORT_PKG}-${ORT_VERSION}/lib/$lib_name"
    if [ ! -f "$src" ]; then
        soft_err "The onnxruntime archive has no $lib_name (expected it at lib/ inside onnxruntime-${ORT_PKG}-${ORT_VERSION})."
        return 1
    fi
    if ! cp "$src" "$MODEL_DIR/"; then
        soft_err "Could not copy $lib_name into $MODEL_DIR"
        return 1
    fi
    if [ ! -f "$MODEL_DIR/$lib_name" ]; then
        soft_err "$MODEL_DIR/$lib_name is missing after the copy."
        return 1
    fi
    ok "onnxruntime library installed"
    return 0
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
    for _ in $(seq 1 30); do
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

  Choose an extraction mode with HYATLAS_MODE (default ultra):
      lite   no LLM call; raw + local embeddings only, nothing leaves the machine
      pro    one extraction per write; reasons within that single turn
      ultra  pro, plus the slow path: periodic consolidation across memories
             (merges contradictions, generalises schemas, synthesises an arc)

    Whether a write BLOCKS is a separate knob, not the mode:
      HYATLAS_SYNC_EXTRACT=on   the write waits and reports done/failed
      HYATLAS_SYNC_EXTRACT=off  the write returns pending; extraction runs behind
      (unset follows the mode: pro blocks, ultra does not)

  Then, unless you chose lite, set your LLM endpoint for fact extraction. The
  server assumes none, so this is required for pro/ultra. Pick per your tier —
  any OpenAI-compatible API works, including a local one:
      export HYATLAS_LLM_BASE="https://inference-api.nousresearch.com/v1"
      export HYATLAS_LLM_MODEL="poolside/laguna-s-2.1:free"
      export HYATLAS_LLM_KEY="your-key"

  Wire it into Hermes. Enable the provider:
      memory:
        provider: hyatlas

  If your server is not on the default 127.0.0.1:19528, set the plugin's own
  settings (NOT a memory.providers block — the plugin does not read one):
      plugins:
        entries:
          hyatlas:
            settings:
              server_host: 127.0.0.1
              server_port: 19528

  Or just run \`hermes memory setup\` and pick hyatlas; \`hermes plugins install\`
  plus that command is the whole path.

  Docs: https://github.com/$REPO#readme
EOF
}

# ---------------------------------------------------------------------------
# Onboarding: mode + LLM endpoint + key
# ---------------------------------------------------------------------------
#
# A fresh install used to print instructions and exit, so picking pro or ultra
# without exporting HYATLAS_LLM_* left every write returning "unconfigured" with
# the reason buried in a log. Three questions is enough to make it work.
#
# Only asked when stdin is a terminal AND the answer is not already supplied by
# the environment, so `curl | bash` in CI and pre-seeded installs never block.

# strip_cr removes a trailing carriage return. `IFS= read -r` strips only \n,
# so CRLF input leaves the \r attached and turns "3" into an unrecognised mode.
strip_cr() { printf '%s' "${1%$'\r'}"; }

# ask VARNAME PROMPT [default] — read a value into VARNAME unless already set.
ask() {
    local name="$1" prompt="$2" def="${3:-}" current val
    current="${!name:-}"
    if [ -n "$current" ]; then
        return 0
    fi
    if [ "$def" != "" ]; then
        printf '  %s [%s]: ' "$prompt" "$def"
    else
        printf '  %s: ' "$prompt"
    fi
    if ! IFS= read -r val; then
        return 0
    fi
    val="$(strip_cr "$val")"
    if [ -z "$val" ] && [ "$def" != "" ]; then
        val="$def"
    fi
    printf -v "$name" '%s' "$val"
}

# ask_secret VARNAME PROMPT — same, but the terminal does not echo the key.
ask_secret() {
    local name="$1" prompt="$2" current val
    current="${!name:-}"
    if [ -n "$current" ]; then
        return 0
    fi
    printf '  %s (input hidden): ' "$prompt"
    if command -v stty >/dev/null 2>&1; then stty -echo 2>/dev/null || true; fi
    IFS= read -r val || val=""
    val="$(strip_cr "$val")"
    if command -v stty >/dev/null 2>&1; then stty echo 2>/dev/null || true; fi
    printf '\n'
    [ -n "$val" ] && printf -v "$name" '%s' "$val"
    return 0
}

# choose_mode — present the three tiers by what they actually do.
choose_mode() {
    [ -n "${HYATLAS_MODE:-}" ] && return 0
    printf '\n'
    info "Extraction mode"
    printf '    1) lite   no LLM call; raw + local embeddings only.\n'
    printf '              Conversation text never leaves the machine.\n'
    printf '    2) pro    one extraction per write; reasons within that single turn.\n'
    printf '              Fills L1-L4 and L7.\n'
    printf '    3) ultra  pro, plus the slow path: periodic consolidation that reasons\n'
    printf '              ACROSS memories — merges contradictions, generalises schemas,\n'
    printf '              synthesises a cross-session arc. System1 (L1-L4,L7) +\n'
    printf '              System2 (L5,L6) together fill all 7 layers. [default]\n'
    printf '  Choice [1-3, default 3]: '
    local pick
    IFS= read -r pick || pick=""
    pick="$(strip_cr "$pick")"
    # Both the number and the name are accepted for every tier. "3" was
    # missing from this table, so picking the advertised default produced
    # HYATLAS_MODE=3 — a value the server rejects at startup.
    case "$pick" in
        1|lite)  HYATLAS_MODE="lite" ;;
        2|pro)   HYATLAS_MODE="pro" ;;
        3|ultra|"") HYATLAS_MODE="ultra" ;;
        *)       HYATLAS_MODE="$pick" ;;
    esac
    case "$HYATLAS_MODE" in
        lite|pro|ultra) ok "mode: $HYATLAS_MODE" ;;
        # Never persist an invalid mode: it would make the server refuse to
        # start, and the .env would carry the bad value into every later run.
        *) warn "unrecognised mode '$HYATLAS_MODE'; falling back to ultra."
           warn "valid values are lite, pro, ultra"
           HYATLAS_MODE="ultra" ;;
    esac
}

# validate_url — cheap structural check so a typo is caught here, not at 401.
validate_url() {
    case "$1" in
        http://*|https://*) return 0 ;;
        *) return 1 ;;
    esac
}

configure_llm() {
    if [ "${HYATLAS_MODE:-ultra}" = "lite" ]; then
        ok "lite mode: no LLM endpoint needed"
        return 0
    fi
    printf '\n'
    info "LLM endpoint (any OpenAI-compatible API)"
    ask HYATLAS_LLM_BASE "Base URL" "https://inference-api.nousresearch.com/v1"
    if [ -n "${HYATLAS_LLM_BASE:-}" ] && ! validate_url "$HYATLAS_LLM_BASE"; then
        warn "'$HYATLAS_LLM_BASE' does not look like a URL; extraction will fail until fixed"
    fi
    ask HYATLAS_LLM_MODEL "Model" "poolside/laguna-s-2.1:free"

    if [ -z "${HYATLAS_LLM_KEY:-}" ] && [ -z "${HYATLAS_LLM_KEY_FILE:-}" ]; then
        ask_secret HYATLAS_LLM_KEY "API key"
    fi
    if [ -z "${HYATLAS_LLM_KEY:-}" ] && [ -z "${HYATLAS_LLM_KEY_FILE:-}" ]; then
        warn "no API key set; $HYATLAS_MODE mode will report llm=unconfigured"
        warn "set HYATLAS_LLM_KEY later, or run: export HYATLAS_MODE=lite"
        return 0
    fi
    ok "endpoint: ${HYATLAS_LLM_BASE} (${HYATLAS_LLM_MODEL})"
    # Never echo the key back.
    ok "api key: configured"
}

# write_hermes_env — persist what we collected so a spawned server inherits it.
#
# Hermes reads $HERMES_HOME/.env, where HERMES_HOME defaults to ~/.hermes (the
# same rule as hermes_constants.get_hermes_home). The file is never $HOME/.env:
# Hermes does not read that, so a key written there would silently do nothing.
# 0600 from creation, and the key is masked in the success message.
# Never fatal: a failure here is a warning, because the binary is already installed.
write_hermes_env() {
    local home="${HERMES_HOME:-}"
    if [ -z "$home" ]; then
        if [ -z "${HOME:-}" ]; then
            warn "neither HERMES_HOME nor HOME is set; settings were not saved to a Hermes .env"
            return 0
        fi
        home="$HOME/.hermes"
    fi
    local envfile="$home/.env"
    [ -n "${HYATLAS_LLM_KEY:-}${HYATLAS_MODE:-}${HYATLAS_LLM_BASE:-}" ] || return 0
    if ! mkdir -p "$home" 2>/dev/null; then
        warn "cannot create $home; settings were not saved to $envfile"
        return 0
    fi
    if ! touch "$envfile" 2>/dev/null; then
        warn "cannot write $envfile"
        return 0
    fi
    chmod 600 "$envfile" 2>/dev/null || true

    local wrote=0 failed=0
    _env_set() {
        local k="$1" v="$2" tmp
        [ -z "$v" ] && return 0
        # Replace an existing line so a re-run updates instead of duplicating.
        if grep -q "^${k}=" "$envfile" 2>/dev/null; then
            tmp="$(mktemp 2>/dev/null)" || { failed=1; return 0; }
            grep -v "^${k}=" "$envfile" > "$tmp" 2>/dev/null || true
            if ! printf '%s=%s\n' "$k" "$v" >> "$tmp" || ! mv "$tmp" "$envfile"; then
                rm -f "$tmp"
                failed=1
                return 0
            fi
            chmod 600 "$envfile" 2>/dev/null || true
        else
            printf '%s=%s\n' "$k" "$v" >> "$envfile" 2>/dev/null || { failed=1; return 0; }
        fi
        wrote=1
    }
    _env_set HYATLAS_MODE      "${HYATLAS_MODE:-}"
    _env_set HYATLAS_LLM_BASE  "${HYATLAS_LLM_BASE:-}"
    _env_set HYATLAS_LLM_MODEL "${HYATLAS_LLM_MODEL:-}"
    _env_set HYATLAS_LLM_KEY   "${HYATLAS_LLM_KEY:-}"
    if [ "$failed" = "1" ]; then
        warn "could not update $envfile; set the values above in Hermes' .env by hand"
    elif [ "$wrote" = "1" ]; then
        ok "wrote settings to $envfile (0600)"
    fi
    return 0
}

# onboarding runs only on an interactive terminal.
onboarding() {
    if [ ! -t 0 ]; then
        info "non-interactive install: skipping setup questions"
        info "configure later with: hermes memory setup"
        return 0
    fi
    printf '\n'
    info "Setup — three questions, then it works"
    choose_mode
    configure_llm
    write_hermes_env
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

    # A failed model fetch must not stop the binary from being installed.
    # Record it, finish the install, and exit non-zero at the very end.
    download_model || MODEL_STATUS=$?
    install_binary
    ensure_on_path
    verify_install || true
    onboarding
    print_next_steps

    if [ "$MODEL_STATUS" -ne 0 ]; then
        warn "Installed the binary, but the model is missing (exit $MODEL_STATUS). Follow the steps above, then re-run this installer."
        exit "$MODEL_STATUS"
    fi
}

# HYATLAS_INSTALL_LIB=1 sources the functions without running the installer
# (used by the test harness). Unset for every real install, including curl | bash.
if [ "${HYATLAS_INSTALL_LIB:-0}" != "1" ]; then
    main "$@"
fi
