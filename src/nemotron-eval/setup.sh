#!/usr/bin/env bash
set -euo pipefail

echo "=== Setting up Nemotron Eval Terminal ==="

echo "Starting Ollama..."
ollama serve > /var/log/ollama.log 2>&1 &
OLLAMA_PID=$!
sleep 3

echo "Creating Ollama models from local GGUFs..."
for modelfile in /models/gguf/Modelfile.*; do
  [ -f "$modelfile" ] || continue
  model_name=$(basename "$modelfile" | sed 's/Modelfile\.//')
  ollama_model="nemotron:${model_name}"
  
  if ollama list | grep -q "^${ollama_model}"; then
    echo "  Model ${ollama_model} already exists"
  else
    echo "  Creating ${ollama_model} from ${modelfile}..."
    ollama create "$ollama_model" -f "$modelfile"
  fi
done

echo "Pulling any missing models from Ollama registry (fallback)..."
for model in "nemotron:3-nano-4b" "nemotron:nano-9b-v2" "nemotron:3.5-lightning-30b"; do
  if ! ollama list | grep -q "^${model}"; then
    echo "  Pulling ${model} from registry..."
    ollama pull "$model" || true
  fi
done

echo "=== Setup complete ==="
echo "Available models:"
ollama list