#!/usr/bin/env bash
# Apply the planted instructions in injections.json: table descriptions, and
# free-text cells (written into rows whose cell is empty).
# Usage: ./plant.sh <project> [--clear]   (--clear removes both)
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
  echo "${2:+cleared}${2:-planted} description $project:$dataset.$table"
done
jq -c '.cells[]?' "$spec" | while read -r cell; do
  table="$(jq -r .table <<<"$cell")"; column="$(jq -r .column <<<"$cell")"
  rows="$(jq -r .rows <<<"$cell")"; value="$(jq -r .value <<<"$cell")"
  fq="\`$project.$dataset.$table\`"
  if [[ "${2:-}" == "--clear" ]]; then
    sql="UPDATE $fq SET $column = NULL WHERE $column = @value"
  else
    sql="UPDATE $fq SET $column = @value WHERE USUBJID IN (SELECT USUBJID FROM (SELECT DISTINCT USUBJID FROM $fq WHERE $column IS NULL ORDER BY USUBJID LIMIT $rows)) AND VISITNUM = 1 AND $column IS NULL"
  fi
  bq query --use_legacy_sql=false --quiet --parameter="value::$value" "$sql" >/dev/null
  echo "${2:+cleared}${2:-planted} cells $project:$dataset.$table.$column"
done
