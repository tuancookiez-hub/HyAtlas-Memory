@echo off
REM HyAtlas v4 (Go) memory server - start detached
REM Reads LLM key from Hermes .env; embeddings are in-Go (no Python).
setlocal
set HYATLAS_GO_PORT=19528
set HYATLAS_GO_DATA=F:\HyAtlas-Memory-Go\data
set HYATLAS_GRAPH_PATH=F:\HyAtlas-Memory-Go\data\graph.json
set HYATLAS_EMBED_BASE=bge
set HYATLAS_MODEL_DIR=F:\HyAtlas-Memory-Go\models
set HYATLAS_LLM_BASE=http://127.0.0.1:49200/v1
set HYATLAS_LLM_MODEL=deepseek:deepseek-v4-flash

REM Pull the ai2api key from the Hermes env file
for /f "tokens=1,* delims==" %%A in ('findstr /b "AI2API_KEY=" "C:\Users\tuanc\AppData\Local\hermes\.env"') do set HYATLAS_LLM_KEY=%%B

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
