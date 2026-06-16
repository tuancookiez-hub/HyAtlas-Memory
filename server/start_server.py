#!/usr/bin/env python
"""Start hy_memory.server with the correct config from hy_memory.json."""
import json
import os
import sys
from pathlib import Path

# Read config
home = Path(os.environ.get("HERMES_HOME", Path.home() / "AppData/Local/hermes"))

# Load ~/.hermes/.env before constructing MemoryConfig-derived env.
# Hy-Memory v2 operational flags (MEMORY_CACHE_BACKEND,
# MEMORY_HISTORY_ENABLE, MEMORY_SYSTEM2_TRIGGER_MODE, MEMORY_VECTOR_*) live
# there; without this, the standalone launcher silently ignores them.
def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value

_load_dotenv(home / ".env")
config = json.loads((home / "hy_memory.json").read_text(encoding="utf-8"))

# Resolve LLM settings: JSON wins, fall back to HY_MEMORY_* env vars
# (the herm TUI "memory setup" screen writes to HY_MEMORY_LLM_* env vars,
# not to hy_memory.json — this lets the TUI's settings actually take effect
# on the next server restart without the user having to re-enter them.)
def _resolve(key, default=""):
    json_val = config.get("llm", {}).get(key, "")
    env_val = os.environ.get(f"HY_MEMORY_LLM_{key.upper()}", "")
    if env_val and env_val != json_val:
        return env_val, f"env:HY_MEMORY_LLM_{key.upper()}"
    if json_val:
        return json_val, "json"
    return default, "default"

model, model_src = _resolve("model", "dola-seed-2.0-lite")
api_key, key_src = _resolve("api_key")
base_url, url_src = _resolve("base_url")

os.environ["MEMORY_MODE"] = os.environ.get("MEMORY_MODE_OVERRIDE") or config.get("mode", "ultra")
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
os.environ["MEMORY_VECTOR_STORE"] = os.environ.get("MEMORY_VECTOR_STORE") or vs.get("provider", "chroma")

print(f"Starting hy_memory.server with config from {home / 'hy_memory.json'}")
print(f"  Mode: {os.environ['MEMORY_MODE']}")
print(f"  LLM: {os.environ['MEMORY_LLM_MODEL']} (key from {key_src}, url from {url_src})")
print(f"  Embedder: {os.environ['MEMORY_EMBEDDER_MODEL']} ({os.environ['MEMORY_EMBEDDING_DIMS']}d)")
print(f"  Vector store: {os.environ['MEMORY_VECTOR_STORE']}")
print(f"  Embedder: in-process (no sidecar)")

# Import and run.
# First apply our user-space patches to the SDK so we don't depend on
# site-packages edits (which get wiped on `pip install --upgrade hy-memory`).
# patches.py is a no-op if the SDK is already patched.
import importlib.util
# Disable System2 background tasks at the launcher level. The S2 task is
# a no-op with MEMORY_CACHE_BACKEND=disabled, and it has been the source
# of crashes in the running server. With this set, no S2 background
# task will fire on add(), and the metrics/cleanup background tasks are
# unaffected.
os.environ["MEMORY_SYSTEM2_TRIGGER_MODE"] = "scheduled"
# Honor MEMORY_L5_AUTO from .env (don't override). The L5 pipeline spawns
# a detached subprocess whose first step is to STOP the hy-memory server
# (to get an exclusive Kuzu lock). It's debounced by L5_MIN_INTERVAL_HOURS
# so it doesn't run constantly. Set MEMORY_L5_AUTO=false in .env to disable.
_l5_auto = os.environ.get("MEMORY_L5_AUTO", "true").strip().lower()
os.environ["MEMORY_L5_AUTO"] = "true" if _l5_auto in ("1", "true", "yes") else "false"
_patches_path = Path(__file__).parent / "hermes-agent" / "plugins" / "memory" / "hy_memory" / "patches.py"
if not _patches_path.exists():
    # Try without the hermes-agent subdir (if launcher is alongside the plugin)
    _patches_path = Path(__file__).parent / "plugins" / "memory" / "hy_memory" / "patches.py"
if _patches_path.exists():
    spec = importlib.util.spec_from_file_location("_hy_memory_patches", _patches_path)
    if spec and spec.loader:
        _pm = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(_pm)
        _result = _pm.apply_all_patches()
        print(f"  Patches applied: {_result}")
        # Make patches available to the server subprocess via PYTHONPATH
        # (so the server can also apply them on import)
        plugin_dir = str(_patches_path.parent)
        existing_pp = os.environ.get("PYTHONPATH", "")
        if plugin_dir not in existing_pp:
            os.environ["PYTHONPATH"] = plugin_dir + os.pathsep + existing_pp

from hy_memory.server import run_server
run_server(port=19527, host="127.0.0.1")
