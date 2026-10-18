#!/bin/bash
# Waits for job specs at $GCS_PREFIX/jobs/<name>.yaml and runs them one at a time (run_job.py). A job runs once:
# its state is kept in $GCS_PREFIX/runs/<name>/status.json. Cancel: upload $GCS_PREFIX/jobs/<name>.cancel.
set -uo pipefail
S=/opt/posttrain/scripts
while true; do
  for spec in $(python3 $S/gcs.py ls "$GCS_PREFIX/jobs/" 2>/dev/null | awk '{print $1}' | grep -E '\.yaml$'); do
    name=$(basename "$spec" .yaml)
    [ -f "/data/runs/$name/.started" ] && continue
    if python3 $S/gcs.py ls "$GCS_PREFIX/runs/$name/status.json" 2>/dev/null | grep -q status.json; then
      mkdir -p "/data/runs/$name"; touch "/data/runs/$name/.started"; continue   # ran before (other VM or earlier container)
    fi
    mkdir -p "/data/runs/$name"; touch "/data/runs/$name/.started"
    python3 $S/gcs.py get "$GCS_PREFIX/jobs/$name.yaml" "/data/runs/$name/job.yaml" || continue
    python3 $S/run_job.py "/data/runs/$name/job.yaml" >> "/data/runs/$name/runner.log" 2>&1
  done
  sleep 60
done
