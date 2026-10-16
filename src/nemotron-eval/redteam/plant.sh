#!/usr/bin/env bash
# Apply (or remove with --clear) the planted table descriptions in injections.json.
# Usage: ./plant.sh <project> [--clear]
set -euo pipefail
project="$1"
spec="$(dirname "$0")/injections.json"
dataset="$(jq -r .dataset "$spec")"
for table in $(jq -r '.tables | keys[]' "$spec"); do
  if [[ "${2:-}" == "--clear" ]]; then
    description=""
  else
    description="$(jq -r --arg t "$table" '.tables[$t].description' "$spec")"
  fi
  bq update --description "$description" "$project:$dataset.$table" >/dev/null
  echo "${2:+cleared}${2:-planted} $project:$dataset.$table"
done
