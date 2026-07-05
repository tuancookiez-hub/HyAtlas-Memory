"""
Quick test for patch #9 — triggers digest() and checks the L5 trigger
fires (or is debounced). Run while the server is up.
"""
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

# Use the hermes-agent venv Python (where hy_memory is installed)
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", ".."))

# Set env vars (must match server config)
os.environ["HY_MEMORY_MODE"] = "ultra"
os.environ["MEMORY_SYSTEM2_TRIGGER_MODE"] = "manual"
os.environ["HY_MEMORY_USER_ID"] = "hermes-user"
os.environ["HY_MEMORY_AGENT_ID"] = "default"
os.environ["HY_MEMORY_LLM_API_KEY"] = "x"  # not used for digest trigger

print("=== Triggering digest() via SDK ===")
print(f"  server URL: http://127.0.0.1:19527 (assumed)")

# Easier: hit the server's digest endpoint if it has one
# But the digest endpoint requires SDK client (not HTTP).
# So we use the SDK client directly.

# Verify server is up
try:
    r = urllib.request.urlopen("http://127.0.0.1:19527/healthz", timeout=5)
    print(f"  Server healthy: {r.read().decode()}")
except Exception as e:
    print(f"  Server not reachable: {e}")
    sys.exit(1)

# Now use the SDK client
from hyatlas_memory.core.client import HyMemoryClient

# Find config
config_path = str(Path(os.environ.get("HERMES_HOME", str(Path.home() / "AppData" / "Local" / "hermes"))) / "hy_memory.json")
cfg = json.loads(Path(config_path).read_text())

# Load the actual API key from config
os.environ["MEMORY_LLM_API_KEY"] = cfg["llm"].get("api_key", "x")
os.environ["MEMORY_LLM_PROVIDER"] = cfg["llm"].get("provider", "openai")
os.environ["MEMORY_LLM_MODEL"] = cfg["llm"].get("model", "dola-seed-2.0-lite")
if cfg["llm"].get("base_url"):
    os.environ["MEMORY_LLM_BASE_URL"] = cfg["llm"]["base_url"]

# Reload env (after settings)
os.environ["MEMORY_SYSTEM2_TRIGGER_MODE"] = "manual"

print()
print("Creating SDK client...")
client = HyMemoryClient(mode="ultra")
print("  Client created.")

print()
print("Triggering digest() ...")
result = client.digest(user_id="hermes-user", agent_id="default")
print()
print("=== digest() result ===")
print(json.dumps(result, indent=2, default=str)[:2000])

# Wait a moment then check the L5 trigger
print()
print("=== Waiting 5s, then checking L5 pipeline state ===")
time.sleep(5)

state_path = Path(os.environ.get("HERMES_HOME", str(Path.home() / "AppData" / "Local" / "hermes"))) / "logs" / "l5_pipeline_state.json"
if state_path.exists():
    print(f"  L5 state file: {state_path.read_text(encoding='utf-8')}")
else:
    print("  No L5 state file yet (pipeline might still be running or was debounced)")

# Check if the L5 pipeline is running
import subprocess
result = subprocess.run(["tasklist"], capture_output=True, text=True)
l5_running = "l5_full_pipeline" in result.stdout
print(f"  L5 pipeline running: {l5_running}")

client.close()
