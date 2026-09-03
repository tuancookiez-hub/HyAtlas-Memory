param([string]$Action = "status")
$ErrorActionPreference = "Stop"

$port   = 19528
$goDir  = "F:\HyAtlas-Memory-Go"
$goExe  = Join-Path $goDir "hyatlas-go.exe"
$data   = Join-Path $goDir "data"
$log    = Join-Path $data "hyatlas-v4.log"

function Start-V4 {
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:$port/healthz" -TimeoutSec 2 -UseBasicParsing
        if ($r.StatusCode -eq 200) { Write-Host "HyAtlas v4 already running on $port."; return }
    } catch { }

    if (-not (Test-Path $goExe)) {
        Write-Error "Binary not found: $goExe`nRun: cd $goDir && go build"
        exit 1
    }

    $authPath = Join-Path $env:USERPROFILE "AppData\Local\hermes\auth.json"
    if (-not (Test-Path $authPath)) { Write-Error "auth.json not found: $authPath"; exit 1 }
    $auth   = Get-Content $authPath -Raw | ConvertFrom-Json
    $nous   = $auth.providers.nous
    $llmKey = $nous.agent_key
    if (-not $llmKey) { $llmKey = $nous.access_token }
    if (-not $llmKey) { Write-Error "No Nous Portal agent_key in Hermes auth.json. Run: hermes auth login"; exit 1 }

    $env:HYATLAS_GO_PORT    = $port
    $env:HYATLAS_GO_DATA    = $data
    $env:HYATLAS_GRAPH_PATH = (Join-Path $data "graph.json")
    $env:HYATLAS_EMBED_BASE = "bge"
    $env:HYATLAS_MODEL_DIR  = (Join-Path $goDir "models")
    $env:HYATLAS_LLM_BASE   = "https://inference-api.nousresearch.com/v1"
    $env:HYATLAS_LLM_MODEL  = "poolside/laguna-s-2.1:free"
    $env:HYATLAS_LLM_KEY    = $llmKey

    Set-Location $goDir
    $proc = Start-Process -FilePath ".\hyatlas-go.exe" -WindowStyle Hidden -RedirectStandardOutput $log -RedirectStandardError "$log.err" -PassThru
    Write-Host "Started hyatlas-go.exe (pid $($proc.Id))"

    for ($i = 0; $i -lt 20; $i++) {
        Start-Sleep -Milliseconds 500
        try {
            $r = Invoke-WebRequest -Uri "http://127.0.0.1:$port/healthz" -TimeoutSec 1 -UseBasicParsing
            if ($r.StatusCode -eq 200) { Write-Host "HyAtlas v4 started on $port."; return }
        } catch { }
    }
    Write-Error "HyAtlas v4 did not respond on $port after 10s. Check $log"
    exit 1
}

function Stop-V4 {
    Get-Process -Name "hyatlas-go" -ErrorAction SilentlyContinue | ForEach-Object {
        Write-Host "Stopped hyatlas-go.exe (pid $($_.Id))"
        Stop-Process -Id $_.Id -Force
    }
    Write-Host "HyAtlas v4 stopped."
}

function Status-V4 {
    Write-Host "v4: 127.0.0.1:$port" -ForegroundColor Cyan
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:$port/healthz" -TimeoutSec 4 -UseBasicParsing
        Write-Host $r.Content -ForegroundColor Green
    } catch { Write-Host "[v4 offline]" -ForegroundColor Red }
    Write-Host ""
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:$port/api/v1/status" -TimeoutSec 4 -UseBasicParsing
        $d  = $r.Content | ConvertFrom-Json
        Write-Host "  LLM       : $($d.llm_model)" -ForegroundColor Yellow
        Write-Host "  writes    : $($d.writes)"
        Write-Host "  searches  : $($d.searches)"
        Write-Host "  vdb points: $($d.vdb_points)"
        Write-Host "  layers    : l3_fact=$($d.layers.l3_fact) l4_summary=$($d.layers.l4_summary) l7_intention=$($d.layers.l7_intention)"
    } catch { Write-Host "[status offline]" -ForegroundColor Red }
}

switch ($Action.ToLower()) {
    "start"   { Start-V4 }
    "stop"    { Stop-V4 }
    "restart" { Stop-V4; Start-V4 }
    "status"  { Status-V4 }
    "health"  { Status-V4 }
    default   { Get-Help $PSCommandPath -ErrorAction SilentlyContinue
                if ($LASTEXITCODE -ne 0) { Write-Host "Usage: hyatlas.ps1 start|stop|restart|status|health" } }
}
