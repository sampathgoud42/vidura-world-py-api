@echo off
REM Start the Vidura World API on http://0.0.0.0:8790
REM Stops any previous instance first: two servers on one port double every
REM background loop and saturate the CPU (observed 2026-07-27).
cd /d "%~dp0"

powershell -NoProfile -Command ^
  "Get-CimInstance Win32_Process -Filter \"Name like 'python%%'\" | Where-Object { $_.CommandLine -like '*uvicorn*app.main:app*' } | ForEach-Object { Write-Host ('stopping old API pid ' + $_.ProcessId); Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"

.venv\Scripts\uvicorn app.main:app --host 0.0.0.0 --port 8790 --timeout-keep-alive 15 --limit-concurrency 128
