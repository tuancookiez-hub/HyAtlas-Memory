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
#   HYATLAS_VERSION   — release tag to install (default: v4.2.0)
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
VERSION="${HYATLAS_VERSION:-v4.3.1}"
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

  Or just run `hermes memory setup` and pick hyatlas; `hermes plugins install`
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
# Appends to Hermes' own .env (the file `hermes memory setup` uses for secrets)
# rather than inventing a second config location. 0600 from creation, and the key
# is masked in the success message.
write_hermes_env() {
    # Split across two statements: on one line the second assignment expands
    # $home before the first has taken effect, which is an unbound-variable
    # error under `set -u`.
    local home="${HERMES_HOME:-${HOME:-$HOME/.hermes}}"
    local envfile="$home/.env"
    [ -d "$home" ] || return 0
    [ -n "${HYATLAS_LLM_KEY:-}${HYATLAS_MODE:-}${HYATLAS_LLM_BASE:-}" ] || return 0
    mkdir -p "$home" 2>/dev/null || return 0
    touch "$envfile" 2>/dev/null || { warn "cannot write $envfile"; return 0; }
    chmod 600 "$envfile" 2>/dev/null || true

    local wrote=0
    _env_set() {
        local k="$1" v="$2"
        [ -z "$v" ] && return 0
        # Replace an existing line so a re-run updates instead of duplicating.
        if grep -q "^${k}=" "$envfile" 2>/dev/null; then
            local tmp
            tmp="$(mktemp)"
            grep -v "^${k}=" "$envfile" > "$tmp" 2>/dev/null || true
            printf '%s=%s\n' "$k" "$v" >> "$tmp"
            mv "$tmp" "$envfile"
            chmod 600 "$envfile" 2>/dev/null || true
        else
            printf '%s=%s\n' "$k" "$v" >> "$envfile"
        fi
        wrote=1
    }
    _env_set HYATLAS_MODE      "${HYATLAS_MODE:-}"
    _env_set HYATLAS_LLM_BASE  "${HYATLAS_LLM_BASE:-}"
    _env_set HYATLAS_LLM_MODEL "${HYATLAS_LLM_MODEL:-}"
    _env_set HYATLAS_LLM_KEY   "${HYATLAS_LLM_KEY:-}"
    if [ "$wrote" = "1" ]; then
        ok "wrote settings to $envfile (0600)"
    fi
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

    download_model
    install_binary
    ensure_on_path
    verify_install || true
    onboarding
    print_next_steps
}

main "$@"
