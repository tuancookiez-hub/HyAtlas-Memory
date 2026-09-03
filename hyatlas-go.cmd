@echo off
REM HyAtlas v4 (Go) control wrapper. Delegates to hyatlas-go.ps1.
powershell -NoProfile -ExecutionPolicy Bypass -File "F:\HyAtlas-Memory-Go\hyatlas-go.ps1" %*
