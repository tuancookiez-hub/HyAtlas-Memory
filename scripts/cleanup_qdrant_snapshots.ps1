# Keep last 2 snapshots per Qdrant collection, delete the rest.
# Schedule weekly via Task Scheduler to prevent unbounded growth.

$ErrorActionPreference = "Stop"

$SnapshotsRoot = "C:\qdrant\snapshots"
$KeepCount = 2

if (-not (Test-Path $SnapshotsRoot)) {
    Write-Host "No snapshots directory at $SnapshotsRoot - nothing to do."
    exit 0
}

$totalDeleted = 0
$totalBytesFreed = 0

$collections = Get-ChildItem -Path $SnapshotsRoot -Directory
foreach ($col in $collections) {
    $snapshots = Get-ChildItem -Path $col.FullName -File |
        Sort-Object LastWriteTime -Descending

    if ($snapshots.Count -le $KeepCount) {
        Write-Host "[$($col.Name)] has $($snapshots.Count) snapshots, keeping all (limit is $KeepCount)."
        continue
    }

    $toDelete = $snapshots | Select-Object -Skip $KeepCount
    foreach ($snap in $toDelete) {
        $size = $snap.Length
        Remove-Item -Path $snap.FullName -Force
        $totalDeleted += 1
        $totalBytesFreed += $size
        Write-Host "[$($col.Name)] deleted $($snap.Name) ($([math]::Round($size / 1MB, 2)) MB, $($snap.LastWriteTime))"
    }
}

if ($totalDeleted -gt 0) {
    Write-Host ""
    Write-Host "Total: deleted $totalDeleted snapshots, freed $([math]::Round($totalBytesFreed / 1GB, 2)) GB."
} else {
    Write-Host "No snapshots needed cleanup."
}
