# Troubleshooting

> Common issues and how to fix them. Organized by symptom → diagnosis → fix.

## Quick diagnosis

Start here:

```bash
# 1. Is the plugin importable?
python -c "import hyatlas_memory; print('OK')"

# 2. Is the upstream server running?
curl http://127.0.0.1:19527/info

# 3. Is the dashboard running?
curl http://127.0.0.1:8765/api/health

# 4. Is the config valid?
cat ~/.hyatlas/config/hy_memory.json
cat ~/.hermes/.env | grep -i memory
```

If all four pass, the system is healthy. If any fail, see the matching section below.

---

## Plugin won't load

**Symptom:** Hermes Agent logs `No module named 'hyatlas_memory'` or `ImportError`.

**Diagnosis:**
```bash
python -c "import hyatlas_memory"
```

**Fix:**
```bash
# Re-install in editable mode
pip install -e .

# Or if using uv:
uv pip install -e .

# Verify the package is importable from the same Python Hermes uses
which hermes
head -1 $(which hermes)  # check shebang → which python
$(head -1 $(which hermes) | sed 's/^#!//') -c "import hyatlas_memory; print(hyatlas_memory.__file__)"
```

If that prints the package path, you're good. If it errors, the Python that runs Hermes can't find the package.

---

## Upstream server won't start

**Symptom:** `python -m server.start_server` exits immediately, or the plugin logs "subprocess failed to start".

**Diagnosis:**
```bash
# Run the server in foreground to see the error
python -m server.start_server
```

Common errors:

### `ModuleNotFoundError: No module named 'hy_memory_sdk'`

The upstream Hy-Memory SDK isn't installed.
```bash
pip install hy-memory-sdk
# or
uv pip install hy-memory-sdk
```

### `OSError: [Errno 48] Address already in use` (port 19527)

Another process is on 19527. Find and kill it:
```bash
# macOS/Linux
lsof -ti:19527 | xargs kill

# Windows
netstat -ano | findstr 19527
taskkill /F /PID <pid>
```

### `KeyError: 'OPENAI_API_KEY'`

The upstream SDK needs an LLM API key. Set it in `~/.hyatlas/config/hy_memory.json` or `~/.hermes/.env`:
```bash
echo 'OPENAI_API_KEY=sk-...' >> ~/.hermes/.env
```

Restart the upstream server after setting the key.

---

## Dashboard won't load

**Symptom:** Browser shows "Can't connect" or times out at `http://127.0.0.1:8765`.

**Diagnosis:**
```bash
# Check the process
ps aux | grep dashboard.py    # macOS/Linux
tasklist | findstr dashboard  # Windows

# Check the port
netstat -an | grep 8765
```

**Fix:**
```bash
# Start manually to see errors
python server/dashboard/dashboard.py

# If port is busy, find the process and kill it
lsof -ti:8765 | xargs kill -9    # macOS/Linux
netstat -ano | findstr 8765      # Windows → taskkill
```

### Dashboard starts but shows blank page

The HTML is being served but the JS isn't running. Open browser DevTools console (F12) and look for errors. Common causes:
- `THREE` undefined → CDN blocked, check network
- `__REFRESH_S__` not replaced → server is using the inline `HTML` constant, not the file. Check `dashboard.html` exists next to `dashboard.py`.

### Boot screen won't fade

`hideBootScreen()` is called after `loadAllData()` resolves. If it's stuck, open DevTools and check:
```js
console.log(allMemories.length)  // should be > 0
console.log(typeof statusData)   // should be 'object'
```

If either is wrong, an API call failed. Check the network tab in DevTools for 502s or CORS errors.

---

## Dashboard shows stale or no data

**Symptom:** Memory counts are wrong, or the Observatory is empty.

**Diagnosis:**
```bash
# Check if the upstream is responding
curl http://127.0.0.1:19527/api/v1/status

# Check what HERMES_USER_IDS is set to
echo $HERMES_USER_IDS

# Check the raw memory data
curl 'http://127.0.0.1:8765/api/memories?limit=5' | jq '.memories[0]'
```

**Fix:**
- If upstream returns an error → restart it
- If `HERMES_USER_IDS` is empty → set it in `~/.hermes/.env`:
  ```bash
  echo 'HERMES_USER_IDS=your-username' >> ~/.hermes/.env
  ```
- If the raw data looks right but the dashboard is empty → hard-refresh the browser (Ctrl+Shift+R)

---

## Memory Observatory is empty

**Symptom:** Observatory page loads but no nodes appear.

**Diagnosis:**
1. Check `OBS_LAYER_ORDER` matches your data:
   ```js
   // In browser console on the Observatory page:
   console.log(OBS_LAYER_ORDER)
   console.log(window.__obsDebug.layerSummary)
   ```
2. Verify the API returns layer counts:
   ```bash
   curl http://127.0.0.1:8765/api/layer-counts
   ```
3. Check the console for errors during edge computation

**Fix:**
- If `layerSummary` is empty → `/api/memories` returned no items. Check upstream.
- If `layerSummary` has layers but `nodes.length === 0` → `sampleNodesForScope` filtering issue. Check `OBS_LAYER_ORDER` matches the `layer` field in your data.
- If the page is just slow to render → wait for the entrance animation (~1 second) before interacting.

---

## Qdrant issues

**Symptom:** Errors mentioning Qdrant, vector store, or "collection not found".

**Diagnosis:**
```bash
# Check if Qdrant is reachable
curl http://127.0.0.1:6333/collections

# List collections
curl http://127.0.0.1:6333/collections | jq
```

**Fix:**

### Qdrant not running

```bash
# Start Qdrant (Docker example)
docker run -d -p 6333:6333 -p 6334:6334 \
  -v $(pwd)/qdrant_data:/qdrant/storage \
  qdrant/qdrant

# Or if installed via the project's scripts
python scripts/start_qdrant.py
```

### Collection missing

The plugin should auto-create collections on first write. If it didn't:
```bash
# Via Qdrant API
curl -X PUT http://127.0.0.1:6333/collections/l1_raw \
  -H 'Content-Type: application/json' \
  -d '{"vectors": {"size": 768, "distance": "Cosine"}}'
```

Repeat for each layer: `l0_basic_info`, `l1_raw`, `l2_fact`, `l3_summary`, `l4_identity`.

### Qdrant data corruption

Last resort — backup and reset:
```bash
# Stop Qdrant
# Move the data directory aside
mv qdrant_data qdrant_data.bak
# Restart Qdrant (will re-create empty collections)
```

You'll lose all vector memories but keep L0 (raw JSONL) and L5+ (Kuzu graph).

---

## Kuzu issues

**Symptom:** L5 page is blank, or `/api/l5/graph` returns 404.

**Diagnosis (v2.0.0+, Patch 23):**
```bash
# Check live endpoint directly — what the dashboard now uses by default
curl http://localhost:19527/api/v1/graph?n=5

# Check fallback path — used only if the live endpoint fails
ls -la ~/.hyatlas/data/kuzu_db
```

**Fix:**

### Kuzu directory missing

The directory is created by the L5 pipeline (`server/bin/l5_full_pipeline.py`). Run it:
```bash
python server/bin/l5_full_pipeline.py
```

### Live endpoint returns 503 (graph_store unavailable)

The upstream server's Kuzu connection isn't initialized. Restart the server:
```bash
hyatlas stop && hyatlas start
```

If still 503, check the server log for Kuzu init errors and that `MEMORY_L5_ENABLED=true` in `hy_memory.json`.

### Fallback export file missing (older installs)

If the live endpoint is unavailable AND `l5_kuzu_export.json` doesn't exist, regenerate it:
```bash
python server/bin/l5_export_json.py
```

This is no longer required for day-to-day viewing — the dashboard reads live from the server by default — but remains useful as a snapshot/backup of the graph at a point in time.

This takes 5-30 minutes for thousands of facts.

### Kuzu locked

If a previous run crashed, the lock file may be stale:
```bash
rm ~/.hyatlas/data/kuzu_db/*.lock
# Or if a wal file is stuck:
rm ~/.hyatlas/data/kuzu_db/*.wal
```

---

## Coding memory issues

**Symptom:** `/api/coding-count` returns `0` even though you've used coding sessions.

**Diagnosis:**
```bash
ls -la ~/.hyatlas/data/coding_memory.db
sqlite3 ~/.hyatlas/data/coding_memory.db "SELECT COUNT(*) FROM coding_memory_meta"
```

**Fix:**

### Database doesn't exist

The coding memory database is created on first coding session. If you've never used one, it won't exist. Trigger a coding session to create it.

### Database exists but is empty

Check the writer:
```bash
# Look for the cron job or scheduled task
crontab -l | grep coding
# Windows: Task Scheduler
```

If the writer isn't running, the database won't populate. See `server/bin/l5_digest_writer.py` for the script that should be running.

---

## Patches not applying

**Symptom:** Upstream SDK issues that the patches should fix are still present.

**Diagnosis:**
```bash
python -c "import hyatlas_memory.patches; print('patches module loaded')"
```

**Fix:**

### Patches module not found

Ensure `patches.py` is in the `src/hyatlas_memory/` directory:
```bash
ls src/hyatlas_memory/patches.py
```

If missing, reinstall the package:
```bash
pip install -e . --force-reinstall
```

### A specific patch is failing

Run with verbose logging:
```python
import logging
logging.basicConfig(level=logging.DEBUG)
import hyatlas_memory
```

The patches log warnings to `logging.getLogger("hyatlas_memory.patches")`. Look for exceptions there.

---

## Performance issues

**Symptom:** Dashboard is slow, or memory recall takes >5 seconds.

**Diagnosis:**
```bash
# Check Qdrant collection sizes
curl http://127.0.0.1:6333/collections | jq '.result.collections[] | {name, points: .points_count}'
```

**Common causes:**

### L1 raw collection is huge (>10K points)

Recall queries slow down with collection size. Solutions:
- Increase Qdrant's HNSW `ef` parameter (trade accuracy for speed)
- Prune old L1 facts (move to L2 rollups and delete from L1)
- Switch from CPU to GPU Qdrant

### Observatory is slow to render

- Use a smaller scope (Last 25 instead of Last 500)
- The edge computation is O(n²) — at scope 500 with 960 nodes, it's ~460K comparisons
- Wait for the entrance animation to finish before interacting

### LLM extraction is slow

If `OPENAI_API_KEY` points to a slow model, extraction will be slow. Consider:
- Using a faster model (e.g., `gpt-4o-mini` instead of `gpt-4`)
- Using a local LLM (Ollama, vLLM) and pointing the SDK at it

---

## Reset / nuclear options

If everything is broken and you want to start over:

### Soft reset (keeps config, clears data)

```bash
# Stop upstream server
pkill -f "server.start_server"

# Clear VDB data (Qdrant)
curl -X DELETE http://127.0.0.1:6333/collections/l1_raw
# Repeat for l0_basic_info, l2_fact, l3_summary, l4_identity

# Clear Kuzu graph
rm -rf ~/.hyatlas/data/kuzu_db
rm ~/.hyatlas/data/l5_kuzu_export.json

# Clear raw JSONL
rm ~/.hyatlas/data/l1_raw.jsonl

# Restart everything
python -m server.start_server
python server/dashboard/dashboard.py
```

### Hard reset (wipes everything)

```bash
# Backs up config, wipes all data
mv ~/.hyatlas ~/.hyatlas.backup.$(date +%Y%m%d)
# Re-initialize
hermes hy-memory init
```

### Uninstall completely

```bash
pip uninstall hyatlas-memory
rm -rf ~/.hyatlas
rm -rf ~/.hermes/hy_memory*  # old plugin artifacts
```

---

## Getting help

If the troubleshooting steps don't resolve your issue:

1. **Check the logs:**
   - `~/.hyatlas/data/logs/` — upstream server logs
   - `server/dashboard/logs/` — dashboard request logs
   - Your terminal where you launched the plugin

2. **Run the doctor command:**
   ```bash
   hermes hy-memory doctor
   ```
   This runs a read-only health check across all components.

3. **Open an issue** at https://github.com/tuancookiez-hub/HyAtlas-Memory/issues with:
   - Output of `hermes hy-memory doctor`
   - Relevant log lines
   - Steps to reproduce

4. **Join the discussion** (see README for links)
