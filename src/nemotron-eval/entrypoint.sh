#!/usr/bin/env bash
set -euo pipefail

echo "=== Starting Nemotron Eval Terminal ==="

if ! pgrep -x "ollama" > /dev/null; then
  echo "Starting Ollama..."
  ollama serve > /var/log/ollama.log 2>&1 &
  sleep 2
fi

echo "Starting FastAPI backend..."
cd /app/backend
exec python -m uvicorn main:app --host 0.0.0.0 --port 8080 --workers 1