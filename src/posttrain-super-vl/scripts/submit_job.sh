#!/bin/bash
# Run on the Jupyter VM (gcloud available). Talks to the training app only through the workspace bucket.
#   submit_job.sh data <export_dir>   upload export_sft.py output to $PREFIX/sft/<UTC time>/ and $PREFIX/sft/latest/
#   submit_job.sh job <job.yaml>      queue a job (the app picks it up within a minute)
#   submit_job.sh status <name>       state + last progress lines
#   submit_job.sh cancel <name>
set -euo pipefail
PREFIX=${GCS_PREFIX:-gs://eval-benchmark-wb-meteoric-gourd-5139/posttrain}
case "${1:-}" in
  data)
    d=${2:?export dir}; ts=$(date -u +%Y%m%dT%H%M%SZ)
    for dst in "$PREFIX/sft/$ts/" "$PREFIX/sft/latest/"; do
      gcloud storage cp "$d"/train.jsonl "$d"/val.jsonl "$d"/train_weighted.jsonl "$d"/manifest.json "$dst"
    done
    echo "uploaded to $PREFIX/sft/$ts/ (and sft/latest/)";;
  job)
    f=${2:?job yaml}; gcloud storage cp "$f" "$PREFIX/jobs/$(python3 -c "import yaml,sys;print(yaml.safe_load(open(sys.argv[1]))['name'])" "$f").yaml";;
  status)
    gcloud storage cat "$PREFIX/runs/${2:?name}/status.json" | python3 -c "import json,sys;d=json.load(sys.stdin);print(d['name'],d['state'],d.get('returncode',''))"
    gcloud storage cat "$PREFIX/runs/$2/progress.log" 2>/dev/null | tail -n 20 || true;;
  cancel)
    echo cancel | gcloud storage cp - "$PREFIX/jobs/${2:?name}.cancel";;
  *) sed -n 2,7p "$0"; exit 1;;
esac
