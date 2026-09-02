@echo off
REM HyAtlas v4 (Go) memory server - start detached
REM Extraction uses a Nous Portal :free model (OAuth agent key from Hermes auth.json).
REM Embeddings are in-Go BGE-small (no Python, no LLM).
setlocal
set HYATLAS_GO_PORT=19528
set HYATLAS_GO_DATA=F:\HyAtlas-Memory-Go\data
set HYATLAS_GRAPH_PATH=F:\HyAtlas-Memory-Go\data\graph.json
set HYATLAS_EMBED_BASE=bge
set HYATLAS_MODEL_DIR=F:\HyAtlas-Memory-Go\models
set HYATLAS_LLM_BASE=https://inference-api.nousresearch.com/v1
set HYATLAS_LLM_MODEL=poolside/laguna-s-2.1:free

REM Pull the Nous Portal agent key from Hermes auth.json (never echo it).
for /f "usebackq delims=" %%A in (`python -c "import json; d=json.load(open(r'C:\Users\tuanc\AppData\Local\hermes\auth.json',encoding='utf-8')); n=d['providers']['nous']; print(n.get('agent_key') or n.get('access_token') or '')"`) do set HYATLAS_LLM_KEY=%%A
if not defined HYATLAS_LLM_KEY (
    echo ERROR: no Nous Portal agent_key in Hermes auth.json. Run: hermes auth login
    exit /b 1
)

REM Skip if already healthy
curl -sS --max-time 3 http://127.0.0.1:19528/healthz >nul 2>&1
if %ERRORLEVEL%==0 (
    echo HyAtlas v4 already running.
    exit /b 0
)

cd /d F:\HyAtlas-Memory-Go
start "" /b hyatlas-go.exe
timeout /t 3 >nul
curl -sS --max-time 5 http://127.0.0.1:19528/healthz
echo.
