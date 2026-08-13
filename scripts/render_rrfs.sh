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
# ⚠ NOMADS BRIDGE (2026-08-13 → cutover 2026-10-06): the rrfs_a feed froze
# at 2026-08-12 11z; frames now come from the slim mirror that
# mirror_rrfs.sh publishes under v1/RRFS/_src/ (see products.yml). Two
# bridge-only changes in this script, both to revert at cutover:
#   1. Non-synoptic cycles are SKIPPED — NOMADS publishes them as
#      sub-hourly files only, which the mirror doesn't ingest.
#   2. HOURS_BACK is widened for the 6-hourly cadence (see below).

set -euo pipefail

MODEL="RRFS"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CONFIG="${REPO_ROOT}/config/products.yml"

# Number of recent run-hours to sweep on every tick.
#
# BRIDGE VALUE (revert to 4 at cutover): with only synoptic cycles
# rendering, offsets 0..9 contain at most two 00/06/12/18z runs — the
# newest (still trailing f84 in over ~4 h of publish + one mirror tick)
# and the previous one. The July-25 render/prune-thrash trap (window
# wider than retention) can't bite here: retain_runs=5 synoptic runs
# span 30 h, three times this window.
HOURS_BACK=9

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
  # BRIDGE: only synoptic cycles exist on the mirror (see header).
  # Delete this skip at cutover to restore hourly runs.
  case "${RUN_HOUR}" in
    00|06|12|18) ;;
    *) continue ;;
  esac

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
  # (Gated on the first ATTEMPTED run, not offset 0 — during the bridge
  # offset 0 is usually a skipped non-synoptic hour.)
  if [ "${#ATTEMPTED_RUNS[@]}" -eq 1 ]; then
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
