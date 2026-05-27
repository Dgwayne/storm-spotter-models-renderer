#!/usr/bin/env bash
# render_gfs.sh — render the most recent GFS run.
#
# GFS runs every 6 hours (00, 06, 12, 18Z) and reaches f120 about 5 hours after
# run time. We pick the most-recent run whose f000 is published and iterate.

set -euo pipefail

MODEL="GFS"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CONFIG="${REPO_ROOT}/config/products.yml"

LATENCY_HOURS=5

NOW_EPOCH=$(date -u +%s)
TARGET_EPOCH=$(( NOW_EPOCH - LATENCY_HOURS * 3600 ))
# Snap back to the most recent synoptic hour.
RUN_HOUR_RAW=$(date -u -d "@${TARGET_EPOCH}" +%H)
RUN_HOUR=$(( (10#${RUN_HOUR_RAW} / 6) * 6 ))
RUN_HOUR=$(printf "%02d" "${RUN_HOUR}")
RUN_DATE=$(date -u -d "@${TARGET_EPOCH}" +%Y%m%d)

echo "==> GFS render: run=${RUN_DATE}${RUN_HOUR}Z"

FH_START=$(yq -r ".models.${MODEL}.forecast_hours_default.start" "$CONFIG")
FH_END=$(yq -r ".models.${MODEL}.forecast_hours_default.end" "$CONFIG")
FH_STEP=$(yq -r ".models.${MODEL}.forecast_hours_default.step" "$CONFIG")

PRODUCTS=$(yq -r ".models.${MODEL}.products[]" "$CONFIG")

for product in $PRODUCTS; do
  for fh in $(seq "${FH_START}" "${FH_STEP}" "${FH_END}"); do
    bash "${SCRIPT_DIR}/decode_pipeline.sh" "${MODEL}" "${product}" \
      "${RUN_DATE}" "${RUN_HOUR}" "${fh}" || {
        echo "  fh=${fh} ${product} FAILED (continuing)"
      }
  done
done

RETAIN=$(yq -r ".models.${MODEL}.retain_runs" "$CONFIG")
echo "==> Pruning GFS to last ${RETAIN} runs"
python3 "${SCRIPT_DIR}/prune_old_runs.py" "${MODEL}" "${RETAIN}"

echo "==> Rebuilding GFS manifest"
python3 "${SCRIPT_DIR}/build_manifest.py" "${MODEL}"

echo "==> GFS render complete"
