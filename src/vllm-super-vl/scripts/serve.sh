#!/bin/bash
# Fetch the API token, download the weights once (resumable), then serve Nemotron 3.5 Super VL on :8080.
set -euo pipefail
log() { echo "[serve] $(date -u +%H:%M:%S) $*"; }

TOKEN=$(python3 - "$API_TOKEN_GCS" <<'PY'
import json, sys, urllib.parse, urllib.request
bucket, obj = sys.argv[1][5:].split("/", 1)
md = urllib.request.Request("http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
                            headers={"Metadata-Flavor": "Google"})
tok = json.load(urllib.request.urlopen(md))["access_token"]
url = f"https://storage.googleapis.com/storage/v1/b/{bucket}/o/{urllib.parse.quote(obj, safe='')}?alt=media"
print(urllib.request.urlopen(urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}"})).read().decode().strip())
PY
)
[ -n "$TOKEN" ] || { log "no API token at $API_TOKEN_GCS; refusing to serve without one"; exit 1; }

if [ ! -f "$MODEL_DIR/.complete" ]; then
  log "downloading $MODEL_REPO to $MODEL_DIR"
  mkdir -p "$MODEL_DIR"
  python3 -c "from huggingface_hub import snapshot_download; snapshot_download('$MODEL_REPO', local_dir='$MODEL_DIR', max_workers=16)"
  touch "$MODEL_DIR/.complete"
fi
nvidia-smi --query-gpu=name,memory.total --format=csv,noheader || true
NGPU=$(nvidia-smi -L | wc -l)
log "serving on $NGPU GPUs, max_model_len=$MAX_MODEL_LEN max_num_seqs=$MAX_NUM_SEQS"
# shellcheck disable=SC2086
exec vllm serve "$MODEL_DIR" \
  --served-model-name nemotron-3.5-super-vl \
  --host 0.0.0.0 --port 8080 --api-key "$TOKEN" \
  --tensor-parallel-size "$NGPU" \
  --trust-remote-code \
  --max-model-len "$MAX_MODEL_LEN" \
  --max-num-seqs "$MAX_NUM_SEQS" \
  --gpu-memory-utilization "$GPU_MEMORY_UTILIZATION" \
  --enable-mamba-shared-prefix-checkpoint \
  --enable-prefix-caching \
  --limit-mm-per-prompt '{"video": 0, "image": 1}' \
  --enable-auto-tool-choice \
  --tool-call-parser qwen3_coder \
  --reasoning-parser nemotron_v3 \
  $EXTRA_ARGS
