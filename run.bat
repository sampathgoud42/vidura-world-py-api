@echo off
REM Start the Vidura World API on http://0.0.0.0:8790
cd /d "%~dp0"
.venv\Scripts\uvicorn app.main:app --host 0.0.0.0 --port 8790
