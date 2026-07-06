#!/usr/bin/env python
"""Start hy_memory.server with the correct config from hy_memory.json."""
import json
import os

from hyatlas_memory import layout

# Read config
root = layout.home()

# Load HyAtlas config env before constructing MemoryConfig-derived env.
# Hy-Memory v2 operational flags (MEMORY_CACHE_BACKEND,
# MEMORY_HISTORY_ENABLE, MEMORY_SYSTEM2_TRIGGER_MODE, MEMORY_VECTOR_*) can live
# in HYATLAS_HOME/config/.env. Legacy Hermes/.hy_memory env files are loaded as
# fallback by layout.load_envs().

layout.load_envs()
config_path = layout.active_config_path()
if config_path is None:
    raise FileNotFoundError(
        f"No hy_memory.json found. Expected {layout.cfgfile()} or one of "
        f"{', '.join(str(p) for p in layout.legacy_cfgs())}"
    )
config = json.loads(config_path.read_text(encoding="utf-8"))

# Resolve LLM settings. The active runtime config wins; HY_MEMORY_LLM_*
# env vars are legacy fallback only. A stale shell env key can otherwise
# silently override the user's `hyatlas config model` choice and break auth.
def _resolve(key, default=""):
    json_val = config.get("llm", {}).get(key, "")
    env_val = os.environ.get(f"HY_MEMORY_LLM_{key.upper()}", "")
    if json_val:
        return json_val, "json"
    if env_val:
        return env_val, f"env:HY_MEMORY_LLM_{key.upper()}"
    return default, "default"

model, model_src = _resolve("model", "dola-seed-2.0-lite")
api_key, key_src = _resolve("api_key")
base_url, url_src = _resolve("base_url")

os.environ["MEMORY_MODE"] = os.environ.get("MEMORY_MODE_OVERRIDE") or config.get("mode", "ultra")
os.environ["MEMORY_DATA_DIR"] = os.environ.get("MEMORY_DATA_DIR") or str(layout.home())
os.environ["MEMORY_LOG_DIR"] = os.environ.get("MEMORY_LOG_DIR") or str(layout.logs())
os.environ["MEMORY_PIPELINE_DEFAULT_VERSION"] = os.environ.get("MEMORY_PIPELINE_DEFAULT_VERSION") or "ultra"
os.environ["MEMORY_SUMMARY_ENABLED_IN_SYS2"] = os.environ.get("MEMORY_SUMMARY_ENABLED_IN_SYS2") or "true"
os.environ["MEMORY_READER_ENABLE_SUMMARY"] = os.environ.get("MEMORY_READER_ENABLE_SUMMARY") or "true"
os.environ["MEMORY_LLM_PROVIDER"] = "openai"
os.environ["MEMORY_LLM_MODEL"] = model
os.environ["MEMORY_LLM_API_KEY"] = api_key
os.environ["MEMORY_LLM_BASE_URL"] = base_url
os.environ["MEMORY_LLM_TEMPERATURE"] = "0.1"

# Pass LLM extra_body (e.g., reasoning_effort) through to the OpenAI client.
extra_body = config.get("llm", {}).get("extra_body")
if extra_body:
    os.environ["MEMORY_LLM_EXTRA_BODY"] = json.dumps(extra_body)

emb = config.get("embedder", {})
os.environ["MEMORY_EMBEDDER_PROVIDER"] = "openai"
os.environ["MEMORY_EMBEDDER_MODEL"] = emb.get("model", "BAAI/bge-small-en-v1.5")
os.environ["MEMORY_EMBEDDING_DIMS"] = str(emb.get("dims", 384))
# In-process embedding — no embedder sidecar needed.
# The inprocess_embed patch (patches.py #3) replaces the HTTP call
# with a direct sentence-transformers call in the same process.
# This eliminates the sidecar failure mode entirely.

vs = config.get("vector_store", {})
os.environ["MEMORY_VECTOR_STORE"] = vs.get("provider", "chroma") or os.environ.get("MEMORY_VECTOR_STORE", "")
os.environ["MEMORY_COLLECTION_NAME"] = vs.get("collection") or vs.get("collection_name") or os.environ.get("MEMORY_COLLECTION_NAME", "agent_memories")
if vs.get("host"):
    os.environ["MEMORY_VECTOR_HOST"] = str(vs["host"])
if vs.get("port"):
    os.environ["MEMORY_VECTOR_PORT"] = str(vs["port"])

print(f"Starting hy_memory.server with config from {config_path}")
print(f"  HyAtlas home: {root}")
print(f"  Mode: {os.environ['MEMORY_MODE']}")
print(f"  LLM: {os.environ['MEMORY_LLM_MODEL']} (key from {key_src}, url from {url_src})")
print(f"  Embedder: {os.environ['MEMORY_EMBEDDER_MODEL']} ({os.environ['MEMORY_EMBEDDING_DIMS']}d)")
print(f"  Vector store: {os.environ['MEMORY_VECTOR_STORE']}")
print(f"  Embedder: in-process (no sidecar)")

# Apply our user-space patches to the SDK. The patches.py file lives
# in the standalone hyatlas_memory package (this is the post-extraction
# layout as of 2026-06-16). The legacy `plugins/memory/hy_memory/`
# in-fork path is intentionally NOT checked any more — it was the
# old layout before HyAtlas-Memory was extracted into its own repo.
# patches.py is a no-op if the SDK is already patched.
os.environ["MEMORY_SYSTEM2_TRIGGER_MODE"] = "scheduled"
_l5_auto = os.environ.get("MEMORY_L5_AUTO", "true").strip().lower()
os.environ["MEMORY_L5_AUTO"] = "true" if _l5_auto in ("1", "true", "yes") else "false"
os.environ["MEMORY_EMOTION_ENABLED"] = "true"

try:
    from hyatlas_memory.integrations import wire_all
    wire_all()
    print("  Integrations wired (13 first-class modules)")
except Exception as _e:
    print(f"  WARN: integrations failed: {_e}")
    import traceback
    traceback.print_exc()

from hyatlas_memory.core.server import run_server
run_server(port=19527, host="127.0.0.1")
