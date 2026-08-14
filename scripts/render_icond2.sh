#!/usr/bin/env bash
# render_icond2.sh — sweep the recent ICON-D2 cycles from the Open-Meteo
# data_spatial bucket (see the ICOND2 model notes in config/products.yml).
#
# Structural copy of render_ecmwf.sh with two om-source twists:
#   * cycles are 3-hourly (8 runs/day) with ~46-80 min publish latency,
#     so a 3-cycle window always covers the freshest published run;
#   * the sweep walks FORECAST-HOUR-MAJOR (the render_rrfs.sh lesson —
#     partial coverage is always "all products out to f_n") AND one .om
#     file carries every variable for a valid time, so OM_CACHE_DIR lets
#     the first product's download feed all 27 (decode_pipeline.sh reuses
#     the cached file); the file is deleted after each fh completes.
#
# Data: Open-Meteo AWS Open Data (CC-BY-4.0 — "Weather data by
# Open-Meteo.com"); underlying model (c) DWD, also CC-BY-4.0.

set -euo pipefail

MODEL="ICOND2"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CONFIG="${REPO_ROOT}/config/products.yml"

# 3 cycles (9 h window): ICON-D2 publishes ~46-80 min after init, so the
# newest cycle is usually mid-upload on some tick and complete on the
# next; older cycles are immutable backfill.
CYCLES_BACK=2
CYCLE_SECONDS=10800

FH_START=$(yq -r ".models.${MODEL}.forecast_hours_default.start" "$CONFIG")
FH_END=$(yq -r ".models.${MODEL}.forecast_hours_default.end" "$CONFIG")
FH_STEP=$(yq -r ".models.${MODEL}.forecast_hours_default.step" "$CONFIG")
PRODUCTS=$(yq -r ".models.${MODEL}.products[]" "$CONFIG")
RETAIN=$(yq -r ".models.${MODEL}.retain_runs" "$CONFIG")
OM_MODEL_PATH=$(yq -r ".models.${MODEL}.om_model_path" "$CONFIG")
OM_BASE_URL=$(yq -r ".models.${MODEL}.om_base_url // \"https://openmeteo.s3.amazonaws.com/data_spatial\"" "$CONFIG")

# ── Pre-fetch R2 listing for fast idempotent skips ────────────────
EXISTING_KEYS_FILE=$(mktemp)
export EXISTING_KEYS_FILE
OM_CACHE_DIR=$(mktemp -d)
export OM_CACHE_DIR
trap 'rm -f "$EXISTING_KEYS_FILE"; rm -rf "$OM_CACHE_DIR"' EXIT

echo "==> Pre-listing R2 contents under v1/${MODEL}/"
if rclone lsf --recursive "r2:${R2_BUCKET}/v1/${MODEL}/" \
    --files-only 2>/dev/null > "$EXISTING_KEYS_FILE"; then
  EXIST_COUNT=$(wc -l < "$EXISTING_KEYS_FILE")
  echo "  ${EXIST_COUNT} existing keys cached"
else
  echo "  (no existing keys, fresh bucket)"
  : > "$EXISTING_KEYS_FILE"
fi

# Publish a manifest immediately (zero runs on a cold start is fine) so
# the freshness monitor's ICOND2 check and the app never see a 404 while
# the first sweep is still rendering its ~1300-frame backfill.
python3 "${SCRIPT_DIR}/build_manifest.py" "${MODEL}" \
  || echo "  (bootstrap manifest build failed; sweep continues)"

# ── Sweep recent cycles newest → oldest ────────────────────────────
NOW_EPOCH=$(date -u +%s)
BASE_EPOCH=$(( (NOW_EPOCH / CYCLE_SECONDS) * CYCLE_SECONDS ))
ATTEMPTED_RUNS=()

for offset in $(seq 0 "${CYCLES_BACK}"); do
  TARGET_EPOCH=$(( BASE_EPOCH - offset * CYCLE_SECONDS ))
  RUN_DATE=$(date -u -d "@${TARGET_EPOCH}" +%Y%m%d)
  RUN_HOUR=$(date -u -d "@${TARGET_EPOCH}" +%H)
  RUN_DIR_URL="${OM_BASE_URL}/${OM_MODEL_PATH}/${RUN_DATE:0:4}/${RUN_DATE:4:2}/${RUN_DATE:6:2}/${RUN_HOUR}00Z"
  echo ""
  echo "==> ICOND2 sweep: run=${RUN_DATE}${RUN_HOUR}Z (offset -$((offset * 3))h)"

  # One cheap gate per cycle: the run's f00 file (valid time == run time)
  # is the first thing Open-Meteo uploads. Absent → run not started →
  # skip 27x49 per-frame HEADs for a cycle that doesn't exist yet.
  F00_STAMP=$(date -u -d "@${TARGET_EPOCH}" +%Y-%m-%dT%H%M)
  if ! curl -sfI "${RUN_DIR_URL}/${F00_STAMP}.om" > /dev/null; then
    echo "  run not yet publishing; skip cycle"
    continue
  fi

  for fh in $(seq "${FH_START}" "${FH_STEP}" "${FH_END}"); do
    for product in $PRODUCTS; do
      if [ -z "${FORCE_RERENDER:-}" ]; then
        rel_key="${product}/${RUN_DATE}${RUN_HOUR}/F$(printf '%03d' "$fh").png"
        if grep -qxF "${rel_key}" "${EXISTING_KEYS_FILE}"; then
          continue
        fi
      fi
      bash "${SCRIPT_DIR}/decode_pipeline.sh" "${MODEL}" "${product}" \
        "${RUN_DATE}" "${RUN_HOUR}" "${fh}" || {
          echo "  fh=${fh} ${product} FAILED (continuing)"
        }
    done
    # Every product for this valid time is done — drop the shared om file.
    rm -f "${OM_CACHE_DIR}/${MODEL}_${RUN_DATE}${RUN_HOUR}_F$(printf '%03d' "$fh").om"
  done

  ATTEMPTED_RUNS+=("${RUN_DATE}${RUN_HOUR}")

  # Publish the manifest as soon as the newest cycle is rendered so the app
  # sees it 10-20 min earlier than waiting for the full sweep + prune.
  if [ "${offset}" -eq 0 ]; then
    echo "==> Publishing manifest early (newest cycle rendered)"
    python3 "${SCRIPT_DIR}/build_manifest.py" "${MODEL}" \
      || echo "  (early manifest build failed; will retry at end of tick)"
  fi
done

# ── Prune R2 to the most recent N runs ─────────────────────────────
echo ""
echo "==> Pruning ICOND2 to last ${RETAIN} runs"
python3 "${SCRIPT_DIR}/prune_old_runs.py" "${MODEL}" "${RETAIN}"

# ── Rebuild manifest (final, post-backfill + post-prune) ──────────
echo "==> Rebuilding ICOND2 manifest"
python3 "${SCRIPT_DIR}/build_manifest.py" "${MODEL}"

echo ""
echo "==> ICOND2 render complete (attempted runs: ${ATTEMPTED_RUNS[*]})"
