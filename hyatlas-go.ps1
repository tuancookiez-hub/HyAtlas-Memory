param([string]$Action = "status")
$ErrorActionPreference = "Stop"

$port   = 19528
# The folder this script sits in, which holds hyatlas-go.exe, models\ and data\.
$goDir  = $PSScriptRoot
$goExe  = Join-Path $goDir "hyatlas-go.exe"
$data   = Join-Path $goDir "data"
$log    = Join-Path $data "hyatlas-v4.log"

function Start-V4 {
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:$port/healthz" -TimeoutSec 2 -UseBasicParsing
        if ($r.StatusCode -eq 200) { Write-Host "HyAtlas already running on $port."; return }
    } catch { }

    if (-not (Test-Path $goExe)) {
        Write-Error "Binary not found: $goExe`nRun: cd $goDir && go build"
        exit 1
    }

    $env:HYATLAS_GO_PORT    = $port
    $env:HYATLAS_GO_DATA    = $data
    $env:HYATLAS_GRAPH_PATH = (Join-Path $data "graph.json")
    $env:HYATLAS_EMBED_BASE = "bge"
    $env:HYATLAS_MODEL_DIR  = (Join-Path $goDir "models")
    # Extraction mode: lite (no LLM call, text never leaves the machine),
    # pro (per-write extraction, reasons within one turn), ultra (adds the slow
    # path: periodic consolidation across memories). Unset means ultra.
    # HYATLAS_SYNC_EXTRACT=on|off separately controls whether a write blocks.

    # Personal settings (model, batch size, owner aliases, any HYATLAS_* variable)
    # go in hyatlas-go.local.ps1 beside this script; see hyatlas-go.local.ps1.example.
    # It is git-ignored, so personal values never reach the repository, and it runs
    # before the defaults below, so whatever it sets wins.
    $localSettings = Join-Path $goDir "hyatlas-go.local.ps1"
    if (Test-Path $localSettings) { . $localSettings }

    if ($env:HYATLAS_MODE) { Write-Host "  mode: $env:HYATLAS_MODE" }

    # The Nous Portal endpoint and a free model, set only when unset so the local
    # settings always win. The server itself assumes no endpoint.
    if (-not $env:HYATLAS_LLM_BASE)  { $env:HYATLAS_LLM_BASE  = "https://inference-api.nousresearch.com/v1" }
    if (-not $env:HYATLAS_LLM_MODEL) { $env:HYATLAS_LLM_MODEL = "poolside/laguna-s-2.1:free" }
    # No key is read here. This launcher never opens Hermes's credential file: a
    # program reading another application's credentials is what credential
    # stealers do, and antivirus rightly blocks it. Until you set a key of your
    # own (HYATLAS_LLM_KEY in hyatlas-go.local.ps1, or in the environment), status
    # shows llm: "awaiting_key".

    Set-Location $goDir
    $proc = Start-Process -FilePath ".\hyatlas-go.exe" -WindowStyle Hidden -RedirectStandardOutput $log -RedirectStandardError "$log.err" -PassThru
    Write-Host "Started hyatlas-go.exe (pid $($proc.Id))"

    for ($i = 0; $i -lt 20; $i++) {
        Start-Sleep -Milliseconds 500
        try {
            $r = Invoke-WebRequest -Uri "http://127.0.0.1:$port/healthz" -TimeoutSec 1 -UseBasicParsing
            if ($r.StatusCode -eq 200) { Write-Host "HyAtlas started on $port."; return }
        } catch { }
    }
    Write-Error "HyAtlas did not respond on $port after 10s. Check $log"
    exit 1
}

function Stop-V4 {
    # Stop-Process is a hard kill, so ask the server to write its buffered state first.
    try { Invoke-WebRequest -Uri "http://127.0.0.1:$port/api/v1/flush" -Method Post -TimeoutSec 3 -UseBasicParsing | Out-Null } catch { }
    Get-Process -Name "hyatlas-go" -ErrorAction SilentlyContinue | ForEach-Object {
        Write-Host "Stopped hyatlas-go.exe (pid $($_.Id))"
        Stop-Process -Id $_.Id -Force
    }
    Write-Host "HyAtlas stopped."
}

function Status-V4 {
    Write-Host "HyAtlas: 127.0.0.1:$port" -ForegroundColor Cyan
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:$port/healthz" -TimeoutSec 4 -UseBasicParsing
        Write-Host $r.Content -ForegroundColor Green
    } catch { Write-Host "[HyAtlas offline]" -ForegroundColor Red }
    Write-Host ""
    try {
        $r = Invoke-WebRequest -Uri "http://127.0.0.1:$port/api/v1/status" -TimeoutSec 4 -UseBasicParsing
        $d  = $r.Content | ConvertFrom-Json
        Write-Host "  version   : $($d.version)"
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
