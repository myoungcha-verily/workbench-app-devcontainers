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

# Optional: serve a fine-tuned (merged) checkpoint from the workspace bucket instead of the Hugging Face base. The pointer
# file holds a gs:// directory; it is synced once per distinct path into /data/models/served-<name>.
# Several servers can run at once: a pointer named after this VM (current_<instance-name>.txt) wins over the shared one.
INSTANCE=$(curl -s -H 'Metadata-Flavor: Google' http://metadata.google.internal/computeMetadata/v1/instance/name 2>/dev/null)
SERVE_POINTER_DIR=gs://eval-benchmark-wb-meteoric-gourd-5139/posttrain/serve
read_gcs() { python3 - "$1" <<'PY' 2>/dev/null
import json, sys, urllib.parse, urllib.request
bucket, obj = sys.argv[1][5:].split("/", 1)
md = urllib.request.Request("http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
                            headers={"Metadata-Flavor": "Google"})
tok = json.load(urllib.request.urlopen(md))["access_token"]
url = f"https://storage.googleapis.com/storage/v1/b/{bucket}/o/{urllib.parse.quote(obj, safe='')}?alt=media"
print(urllib.request.urlopen(urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}"})).read().decode().strip())
PY
}
if [ -n "$INSTANCE" ] && read_gcs "$SERVE_POINTER_DIR/current_$INSTANCE.txt" >/dev/null; then
  SERVE_POINTER=${SERVE_POINTER:-$SERVE_POINTER_DIR/current_$INSTANCE.txt}
else
  SERVE_POINTER=${SERVE_POINTER:-$SERVE_POINTER_DIR/current.txt}
fi
log "pointer file: $SERVE_POINTER"
SERVED=$(python3 - "$SERVE_POINTER" <<'PY' 2>/dev/null
import json, sys, urllib.parse, urllib.request
bucket, obj = sys.argv[1][5:].split("/", 1)
md = urllib.request.Request("http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
                            headers={"Metadata-Flavor": "Google"})
tok = json.load(urllib.request.urlopen(md))["access_token"]
url = f"https://storage.googleapis.com/storage/v1/b/{bucket}/o/{urllib.parse.quote(obj, safe='')}?alt=media"
print(urllib.request.urlopen(urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}"})).read().decode().strip())
PY
)
if [ -n "$SERVED" ]; then
  name=$(basename "$SERVED")
  log "pointer -> $SERVED"
  MODEL_DIR=/data/models/served-$name
  if [ ! -f "$MODEL_DIR/.complete" ]; then
    mkdir -p "$MODEL_DIR"
    python3 - "$SERVED" "$MODEL_DIR" <<'PY'
import json, os, sys, urllib.parse, urllib.request
src, dst = sys.argv[1], sys.argv[2]
bucket, prefix = src[5:].split("/", 1); prefix = prefix.rstrip("/") + "/"
md = urllib.request.Request("http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
                            headers={"Metadata-Flavor": "Google"})
def H():  # fresh token per request: syncing ~250 GB outlasts one access token
    return {"Authorization": "Bearer " + json.load(urllib.request.urlopen(md))["access_token"]}
page = None
while True:
    q = {"prefix": prefix, **({"pageToken": page} if page else {})}
    r = json.load(urllib.request.urlopen(urllib.request.Request(
        f"https://storage.googleapis.com/storage/v1/b/{bucket}/o?" + urllib.parse.urlencode(q), headers=H())))
    for o in r.get("items", []):
        rel = o["name"][len(prefix):]
        if not rel or rel.endswith("/"): continue
        out = os.path.join(dst, rel); os.makedirs(os.path.dirname(out), exist_ok=True)
        url = f"https://storage.googleapis.com/storage/v1/b/{bucket}/o/{urllib.parse.quote(o['name'], safe='')}?alt=media"
        with urllib.request.urlopen(urllib.request.Request(url, headers=H())) as resp, open(out, "wb") as f:
            while True:
                b = resp.read(1 << 24)
                if not b: break
                f.write(b)
        print("synced", rel, flush=True)
    page = r.get("nextPageToken")
    if not page: break
PY
    touch "$MODEL_DIR/.complete"
  fi
fi

# Optional extra vLLM flags from the workspace bucket (e.g. MTP speculative decoding on H100), read at every start.
# per GPU type, so enabling a flag for H100 servers never changes the A100 server: extra_args_h100.txt / extra_args_a100.txt
GPU_TAG=$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -n 1 | grep -oE 'H100|A100|H200|B200|L4' | tr 'A-Z' 'a-z')
EXTRA_GCS=${EXTRA_GCS:-gs://eval-benchmark-wb-meteoric-gourd-5139/posttrain/serve/extra_args_${GPU_TAG:-unknown}.txt}
EXTRA_FROM_GCS=$(python3 - "$EXTRA_GCS" <<'PY' 2>/dev/null
import json, sys, urllib.parse, urllib.request
bucket, obj = sys.argv[1][5:].split("/", 1)
md = urllib.request.Request("http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
                            headers={"Metadata-Flavor": "Google"})
tok = json.load(urllib.request.urlopen(md))["access_token"]
url = f"https://storage.googleapis.com/storage/v1/b/{bucket}/o/{urllib.parse.quote(obj, safe='')}?alt=media"
print(urllib.request.urlopen(urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}"})).read().decode().strip())
PY
)
[ -n "$EXTRA_FROM_GCS" ] && { EXTRA_ARGS="$EXTRA_ARGS $EXTRA_FROM_GCS"; log "extra args from bucket: $EXTRA_FROM_GCS"; }

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
