#!/usr/bin/env bash
# render_hrrr.sh — render the most recent HRRR run for all configured products.
#
# Strategy:
#   1. Determine the most recent run that *should* be available (now - latency_min).
#   2. For each product in config/products.yml, iterate forecast hours.
#   3. Call decode_pipeline.sh per (product, fh). decode_pipeline.sh skips if not
#      yet published (idx HEAD check) or already on R2 (idempotent).
#   4. Prune R2 to the most recent N runs.
#   5. Rebuild manifest.json.
#
# Required env: R2_BUCKET, R2_ENDPOINT, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY,
#               MODELS_DOMAIN (for manifest base URL embedding, optional)

set -euo pipefail

MODEL="HRRR"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CONFIG="${REPO_ROOT}/config/products.yml"

# HRRR usually publishes ~50-60 minutes after the run hour for f00, then trailing
# hours follow. Run 'now-2h' to be safe; decode_pipeline skips missing hours.
LATENCY_HOURS=2

NOW_EPOCH=$(date -u +%s)
TARGET_EPOCH=$(( NOW_EPOCH - LATENCY_HOURS * 3600 ))
RUN_DATE=$(date -u -d "@${TARGET_EPOCH}" +%Y%m%d)
RUN_HOUR=$(date -u -d "@${TARGET_EPOCH}" +%H)

echo "==> HRRR render: run=${RUN_DATE}${RUN_HOUR}Z"

# Decide forecast hour range: synoptic runs (00/06/12/18) go to f48, else f18.
case "${RUN_HOUR}" in
  00|06|12|18) FH_END=48 ;;
  *)           FH_END=18 ;;
esac

PRODUCTS=$(yq -r ".models.${MODEL}.products[]" "$CONFIG")

for product in $PRODUCTS; do
  for fh in $(seq 0 "${FH_END}"); do
    bash "${SCRIPT_DIR}/decode_pipeline.sh" "${MODEL}" "${product}" \
      "${RUN_DATE}" "${RUN_HOUR}" "${fh}" || {
        echo "  fh=${fh} ${product} FAILED (continuing)"
      }
  done
done

# --- Prune R2 to the most recent N runs --------------------------------------
RETAIN=$(yq -r ".models.${MODEL}.retain_runs" "$CONFIG")
echo "==> Pruning HRRR to last ${RETAIN} runs"
python3 "${SCRIPT_DIR}/prune_old_runs.py" "${MODEL}" "${RETAIN}"

# --- Rebuild manifest --------------------------------------------------------
echo "==> Rebuilding HRRR manifest"
python3 "${SCRIPT_DIR}/build_manifest.py" "${MODEL}"

echo "==> HRRR render complete"
