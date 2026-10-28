#!/bin/bash
# Launch the Kumo Relational NIM next to this container and serve a token-checking proxy on :8080.
set -uo pipefail
log() { echo "[kumo-nim] $(date -u +%H:%M:%S) $*"; }

gcs_read() {  # read a small object from the workspace bucket with the VM service account
  python3 - "$1" <<'PY'
import json, sys, urllib.parse, urllib.request
bucket, obj = sys.argv[1][5:].split("/", 1)
md = urllib.request.Request("http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token",
                            headers={"Metadata-Flavor": "Google"})
tok = json.load(urllib.request.urlopen(md))["access_token"]
url = f"https://storage.googleapis.com/storage/v1/b/{bucket}/o/{urllib.parse.quote(obj, safe='')}?alt=media"
print(urllib.request.urlopen(urllib.request.Request(url, headers={"Authorization": f"Bearer {tok}"})).read().decode().strip())
PY
}

TOKEN=$(gcs_read "$API_TOKEN_GCS" 2>/dev/null)
[ -n "$TOKEN" ] || { log "no API token at $API_TOKEN_GCS; refusing to serve without one"; sleep 60; exit 1; }
NGC_KEY=$(gcs_read "$NGC_KEY_GCS" 2>/dev/null)
[ -n "$NGC_KEY" ] || { log "no NGC key at $NGC_KEY_GCS (see README)"; sleep 60; exit 1; }

docker info >/dev/null 2>&1 || { log "host Docker socket not available"; sleep 60; exit 1; }
if ! docker info 2>/dev/null | grep -qi nvidia; then
  log "WARNING: host Docker has no nvidia runtime listed (Workbench GPU detection race?); restart the app if the NIM fails"
fi
printf '%s' "$NGC_KEY" | docker login nvcr.io -u '$oauthtoken' --password-stdin >/dev/null 2>&1 \
  || { log "docker login to nvcr.io failed (check the key and that the NIM terms are accepted on NGC)"; sleep 60; exit 1; }
log "logged in to nvcr.io; pulling $NIM_IMAGE"
docker pull -q "$NIM_IMAGE" || { log "pull failed"; sleep 60; exit 1; }

if ! docker ps --format '{{.Names}}' | grep -qx kumo-nim; then
  docker rm -f kumo-nim >/dev/null 2>&1 || true
  # No published port: only this launcher (same network) can reach the NIM, and only through the token proxy.
  docker run -d --name kumo-nim --restart unless-stopped --gpus all --shm-size 16g --network app-network \
    "$NIM_IMAGE" >/dev/null || { log "docker run failed (GPUs visible to Docker?)"; sleep 60; exit 1; }
fi
docker logout nvcr.io >/dev/null 2>&1 || true
log "NIM container started; GPU: $(docker exec kumo-nim nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null | head -n 1 || echo 'not visible')"
export TOKEN UPSTREAM="http://kumo-nim:8000"
exec python3 /opt/kumo-nim/proxy.py
