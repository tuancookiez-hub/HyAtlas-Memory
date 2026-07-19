#!/usr/bin/env bash
# HyAtlas Docker entrypoint — zvec-native (v3.4+)
set -euo pipefail

export HYATLAS_HOME="${HYATLAS_HOME:-/data/hyatlas}"
export HY_MEMORY_HOST="${HY_MEMORY_HOST:-0.0.0.0}"
export HY_MEMORY_PORT="${HY_MEMORY_PORT:-19527}"
export HY_DASH_BIND="${HY_DASH_BIND:-0.0.0.0}"
export HY_DASH_PORT="${HY_DASH_PORT:-8765}"
export HY_MEMORY_BASE="${HY_MEMORY_BASE:-http://127.0.0.1:${HY_MEMORY_PORT}}"
export FOR_DISABLE_CONSOLE_CLOSE_HANDLER="${FOR_DISABLE_CONSOLE_CLOSE_HANDLER:-1}"

mkdir -p \
  "${HYATLAS_HOME}/config" \
  "${HYATLAS_HOME}/data" \
  "${HYATLAS_HOME}/logs" \
  "${HYATLAS_HOME}/zvec" \
  "${HYATLAS_HOME}/cache" \
  "${HYATLAS_HOME}/snapshots"

CFG="${HYATLAS_HOME}/config/hy_memory.json"
if [[ ! -f "${CFG}" ]]; then
  echo "[entrypoint] seeding ${CFG}"
  # Prefer env-driven defaults for first boot; user can edit the volume later.
  python - <<'PY'
import json, os
from pathlib import Path
home = Path(os.environ["HYATLAS_HOME"])
cfg = home / "config" / "hy_memory.json"
model = os.environ.get("HY_MEMORY_LLM_MODEL", "gpt-4o-mini")
base = os.environ.get("HY_MEMORY_LLM_BASE_URL", "https://api.openai.com/v1")
key = os.environ.get("HY_MEMORY_LLM_API_KEY", "")
emb_model = os.environ.get("HY_MEMORY_EMBEDDER_MODEL", "BAAI/bge-large-en-v1.5")
emb_dims = int(os.environ.get("HY_MEMORY_EMBEDDER_DIMS", "1024"))
# remote = OpenAI-compatible HTTP embeddings; local = sentence-transformers in-process
emb_provider = os.environ.get("HY_MEMORY_EMBEDDER_PROVIDER", "remote")
mode = os.environ.get("MEMORY_MODE", os.environ.get("HY_MEMORY_MODE", "ultra"))
data = {
  "llm": {
    "api_key": key,
    "model": model,
    "base_url": base,
  },
  "embedder": {
    "model": emb_model,
    "dims": emb_dims,
    "provider": emb_provider,
  },
  "mode": mode,
  "vector_store": {"provider": "zvec"},
  "auto_start": False,
  "port": int(os.environ.get("HY_MEMORY_PORT", "19527")),
}
cfg.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
print(f"[entrypoint] wrote {cfg}")
PY
fi

# Keep env LLM overrides useful without rewriting json on every start.
# start_server prefers json when present; empty json api_key falls back to env.

cmd="${1:-stack}"
shift || true

case "${cmd}" in
  stack)
    echo "[entrypoint] starting dashboard on ${HY_DASH_BIND}:${HY_DASH_PORT}"
    python -m hyatlas_memory.server.dashboard.dashboard &
    dash_pid=$!
    cleanup() {
      echo "[entrypoint] shutting down..."
      kill "${dash_pid}" 2>/dev/null || true
      wait "${dash_pid}" 2>/dev/null || true
    }
    trap cleanup EXIT INT TERM
    echo "[entrypoint] starting server on ${HY_MEMORY_HOST}:${HY_MEMORY_PORT}"
    # Foreground so container lifecycle tracks the API server
    exec python -m hyatlas_memory.server.start_server
    ;;
  server)
    exec python -m hyatlas_memory.server.start_server
    ;;
  dashboard)
    exec python -m hyatlas_memory.server.dashboard.dashboard
    ;;
  doctor|status)
    exec python -m hyatlas_memory.start doctor
    ;;
  shell)
    exec /bin/bash "$@"
    ;;
  *)
    exec "${cmd}" "$@"
    ;;
esac
