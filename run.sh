#!/usr/bin/env sh
# Start the Vidura World API on http://0.0.0.0:8790 (Linux/macOS)
cd "$(dirname "$0")"
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8790
