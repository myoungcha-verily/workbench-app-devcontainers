#!/bin/bash
# Container entrypoint. Never trains on its own: it prepares the environment once (AutoModel overlay, base weights),
# serves a status page on :8080 and then waits for job specs in $GCS_PREFIX/jobs/ (run_job.py). Without JOB_POLL it
# just idles; jobs can also be started by hand: docker exec -it application-server python3 /opt/posttrain/scripts/run_job.py <spec.yaml>
set -uo pipefail
S=/opt/posttrain/scripts
mkdir -p /data/status /data/logs /data/runs /data/sft
log() { echo "[start] $(date -u +%FT%TZ) $*" | tee -a /data/logs/start.log; }

log "GPUs:"; nvidia-smi --query-gpu=index,name,memory.total,driver_version --format=csv,noheader 2>&1 | tee -a /data/logs/start.log
python3 -c "import torch; print('torch', torch.__version__, 'cuda', torch.version.cuda, 'available', torch.cuda.is_available(), 'devices', torch.cuda.device_count())" 2>&1 | tee -a /data/logs/start.log
df -h /data | tee -a /data/logs/start.log

python3 $S/status.py >> /data/logs/status.log 2>&1 &          # status page on :8080 (GPU use + step/loss lines only)

( $S/setup_automodel.sh >> /data/logs/setup_automodel.log 2>&1 && log "AutoModel overlay ready" || log "AutoModel overlay FAILED (see setup_automodel.log)" ) &

if [ "${PREFETCH_WEIGHTS:-1}" = "1" ] && [ ! -f "$MODEL_DIR/.complete" ]; then
  ( log "downloading $MODEL_REPO@$MODEL_REVISION to $MODEL_DIR"
    python3 -c "from huggingface_hub import snapshot_download; snapshot_download('$MODEL_REPO', revision='$MODEL_REVISION', local_dir='$MODEL_DIR', max_workers=16)" \
      >> /data/logs/download.log 2>&1 && touch "$MODEL_DIR/.complete" && log "weights ready" || log "weight download FAILED" ) &
fi

if [ "${JOB_POLL:-1}" = "1" ]; then
  exec $S/job_poller.sh
fi
log "idle (JOB_POLL=0)"; exec sleep infinity
