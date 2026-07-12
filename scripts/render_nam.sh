#!/usr/bin/env bash
# render_nam.sh — fan out across the recent NAM 3km CONUS-nest cycles so
# new runs land on R2 as soon as NOAA publishes them.
#
# Structural copy of render_rrfs.sh, adapted for a 4-cycles/day model:
# NAM runs at 00/06/12/18Z only, so the sweep steps back in 6-hour cycles
# instead of hours. Every cycle publishes the full hourly f00-f60 range;
# f00 appears roughly 1.5 h after init and later hours trail in over the
# next ~1.5 h. decode_pipeline.sh skips frames already on R2 and forecast
# hours whose idx hasn't published yet, so sweeping "too early" is free.
#
# ⚠ NAM is slated for retirement after RRFS goes operational (post
# 2026-08-31 cutover) — see the model notes in config/products.yml.

set -euo pipefail

MODEL="NAM"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CONFIG="${REPO_ROOT}/config/products.yml"

# Number of recent cycles to sweep on every tick. The newest cycle is the
# one currently publishing; by the time a cycle is 3 back (18 h old) it has
# been complete for many hours, so anything older is guaranteed done.
CYCLES_BACK=2

FH_END=$(yq -r ".models.${MODEL}.forecast_hours_default.end" "$CONFIG")
PRODUCTS=$(yq -r ".models.${MODEL}.products[]" "$CONFIG")
RETAIN=$(yq -r ".models.${MODEL}.retain_runs" "$CONFIG")

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

# ── Sweep recent cycles newest → oldest ────────────────────────────
# Snap "now" to the most recent synoptic hour, then step back 6 h per
# offset. No publish-latency fudge needed: a cycle whose idx isn't up yet
# just skips every frame this tick and fills in on the next one.
NOW_EPOCH=$(date -u +%s)
BASE_EPOCH=$(( (NOW_EPOCH / 21600) * 21600 ))
ATTEMPTED_RUNS=()

for offset in $(seq 0 "${CYCLES_BACK}"); do
  TARGET_EPOCH=$(( BASE_EPOCH - offset * 21600 ))
  RUN_DATE=$(date -u -d "@${TARGET_EPOCH}" +%Y%m%d)
  RUN_HOUR=$(date -u -d "@${TARGET_EPOCH}" +%H)
  echo ""
  echo "==> NAM sweep: run=${RUN_DATE}${RUN_HOUR}Z (offset -$((offset * 6))h)"

  for product in $PRODUCTS; do
    for fh in $(seq 0 "${FH_END}"); do
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
echo "==> Pruning NAM to last ${RETAIN} runs"
python3 "${SCRIPT_DIR}/prune_old_runs.py" "${MODEL}" "${RETAIN}"

# ── Rebuild manifest (final, post-backfill + post-prune) ──────────
echo "==> Rebuilding NAM manifest"
python3 "${SCRIPT_DIR}/build_manifest.py" "${MODEL}"

echo ""
echo "==> NAM render complete (attempted runs: ${ATTEMPTED_RUNS[*]})"
