#!/usr/bin/env bash
set -euo pipefail

echo "=== Starting Nemotron Eval Terminal ==="
mkdir -p "${DATA_DIR:-/data}" "${OLLAMA_MODELS:-/data/ollama}"

nvidia-smi -L 2>/dev/null || echo "nvidia-smi not available: models will run on CPU"

# Keep Ollama running for the life of the container; the backend waits for it.
(
  while true; do
    ollama serve || true
    echo "ollama serve exited; restarting in 3s" >&2
    sleep 3
  done
) &

cd "${APP_ROOT:-/app}/backend"
exec /usr/local/bin/python -m uvicorn main:app --host 0.0.0.0 --port 8080 --workers 1 --proxy-headers
