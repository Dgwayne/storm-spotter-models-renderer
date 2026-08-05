#!/usr/bin/env bash
# render_rrfs.sh — fan out across multiple recent RRFS run hours so
# new runs land on R2 as soon as NOAA publishes them.
#
# Structural copy of render_hrrr.sh (see the strategy notes there): RRFS is
# hourly like HRRR, publishes f00 within roughly an hour of init, and trails
# later forecast hours in over the following hour. Every cron tick sweeps the
# past N candidate runs newest → oldest; decode_pipeline.sh skips frames
# already on R2 and forecast hours whose idx hasn't published yet.
#
# The only model-specific bits are MODEL and the forecast-hour window:
# hourly runs go to f18, synoptic runs (00/06/12/18z) to f84.
#
# ⚠ Source is the pre-operational rrfs_a feed until RRFS goes operational
# (2026-08-31) — see the s3_key_template note in config/products.yml.

set -euo pipefail

MODEL="RRFS"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CONFIG="${REPO_ROOT}/config/products.yml"

# Number of recent run-hours to sweep on every tick. retain_runs=5, so
# offsets 0..4 cover exactly the runs that survive the prune — anything
# older just gets deleted at the end, so rendering it is wasted work.
HOURS_BACK=4

PRODUCTS=$(yq -r ".models.${MODEL}.products[]" "$CONFIG")
RETAIN=$(yq -r ".models.${MODEL}.retain_runs" "$CONFIG")

# PRODUCT_FILTER: optional space-separated subset of the product list —
# set by the workflow's matrix jobs so render groups run in parallel
# (see render_groups in products.yml). Unset = render everything.
if [ -n "${PRODUCT_FILTER:-}" ]; then
  FILTERED=""
  for p in ${PRODUCT_FILTER}; do
    if ! printf '%s\n' ${PRODUCTS} | grep -qxF "${p}"; then
      echo "ERROR: PRODUCT_FILTER contains '${p}', not in models.${MODEL}.products" >&2
      exit 1
    fi
    FILTERED="${FILTERED}${p}"$'\n'
  done
  PRODUCTS="${FILTERED}"
  echo "==> Product filter active: $(echo ${PRODUCTS} | tr '\n' ' ')"
fi

# Per-product forecast-hour floors/caps (fh_min products like ptype/ltng
# have no f00 message). Skipping here — not just inside decode_pipeline.sh
# — avoids ~1 s of spawn overhead per skipped tuple (see render_hrrr.sh).
declare -A FH_CAPS
declare -A FH_MINS
for product in $PRODUCTS; do
  FH_CAPS[$product]=$(yq -r ".products.${product}.fh_cap // \"\"" "$CONFIG")
  FH_MINS[$product]=$(yq -r ".products.${product}.fh_min // \"\"" "$CONFIG")
done

# ── Pre-fetch R2 listing for fast idempotent skips ────────────────
# One recursive listing per tick instead of an rclone round-trip per
# (run × product × fh) tuple — see render_hrrr.sh for the numbers.
EXISTING_KEYS_FILE=$(mktemp)
export EXISTING_KEYS_FILE
trap 'rm -f "$EXISTING_KEYS_FILE"' EXIT

echo "==> Pre-listing R2 contents under v1/${MODEL}/"
if rclone lsf --recursive "r2:${R2_BUCKET}/v1/${MODEL}/" \
    --files-only 2>/dev/null > "$EXISTING_KEYS_FILE"; then
  EXIST_COUNT=$(wc -l < "$EXISTING_KEYS_FILE")
  echo "  ${EXIST_COUNT} existing keys cached"
else
  echo "  (no existing keys, fresh bucket)"
  : > "$EXISTING_KEYS_FILE"
fi

# ── Sweep recent runs newest → oldest ──────────────────────────────
NOW_EPOCH=$(date -u +%s)
ATTEMPTED_RUNS=()

for offset in $(seq 0 "${HOURS_BACK}"); do
  TARGET_EPOCH=$(( NOW_EPOCH - offset * 3600 ))
  RUN_DATE=$(date -u -d "@${TARGET_EPOCH}" +%Y%m%d)
  RUN_HOUR=$(date -u -d "@${TARGET_EPOCH}" +%H)
  echo ""
  echo "==> RRFS sweep: run=${RUN_DATE}${RUN_HOUR}Z (offset -${offset}h)"

  case "${RUN_HOUR}" in
    00|06|12|18) FH_END=84 ;;
    *)           FH_END=18 ;;
  esac

  for product in $PRODUCTS; do
    for fh in $(seq 0 "${FH_END}"); do
      # ── Per-product fh cap/floor ──────────────────────────────────
      if [ -n "${FH_CAPS[$product]}" ] && [ "${fh}" -gt "${FH_CAPS[$product]}" ]; then
        continue
      fi
      if [ -n "${FH_MINS[$product]}" ] && [ "${fh}" -lt "${FH_MINS[$product]}" ]; then
        continue
      fi
      # Parent-level idempotent skip against the pre-listed bucket cache —
      # an already-rendered frame costs one in-memory grep and never spawns
      # decode_pipeline.sh at all (see render_hrrr.sh for rationale).
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
  done

  ATTEMPTED_RUNS+=("${RUN_DATE}${RUN_HOUR}")

  # Publish the manifest as soon as the newest run is rendered so the app
  # sees it 10-20 min earlier than waiting for the full sweep + prune.
  if [ "${offset}" -eq 0 ]; then
    echo "==> Publishing manifest early (newest run rendered)"
    python3 "${SCRIPT_DIR}/build_manifest.py" "${MODEL}" \
      || echo "  (early manifest build failed; will retry at end of tick)"
  fi
done

# ── Prune + final manifest (single finalize job when fanned out) ───
if [ -n "${SKIP_FINALIZE:-}" ]; then
  echo ""
  echo "==> SKIP_FINALIZE set — prune + final manifest left to the finalize job"
else
  echo ""
  echo "==> Pruning RRFS to last ${RETAIN} runs"
  python3 "${SCRIPT_DIR}/prune_old_runs.py" "${MODEL}" "${RETAIN}"

  echo "==> Rebuilding RRFS manifest"
  python3 "${SCRIPT_DIR}/build_manifest.py" "${MODEL}"
fi

echo ""
echo "==> RRFS render complete (attempted runs: ${ATTEMPTED_RUNS[*]})"
