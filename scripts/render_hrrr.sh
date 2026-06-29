#!/usr/bin/env bash
# render_hrrr.sh — fan out across multiple recent HRRR run hours so
# new runs land on R2 as soon as NOAA publishes them.
#
# Strategy
# --------
# HRRR runs every hour. f00 of run N typically publishes ~45-50 minutes
# after init time, with trailing forecast hours arriving over the next
# 30-60 min. The old single-target render (NOW - LATENCY_HOURS) missed
# this entirely: by the time we waited 2 hours, run N+1 had already
# arrived and we never went back for it.
#
# Instead, every cron tick now sweeps the past N hours of candidate
# runs (newest → oldest), letting `decode_pipeline.sh`:
#   * skip frames already on R2 (idempotent, single in-memory set
#     lookup against a pre-fetched listing — see the EXISTING_KEYS_FILE
#     dance below)
#   * skip forecast hours whose idx hasn't published yet (HTTP HEAD
#     against the bucket idx file returns 404 → exit 0)
#
# Net effect: each tick fills in whatever just became available since
# the prior tick, across every run that's still in the active window.
# A partial 17z run shows up within ~15 min of NOAA publishing its
# first frame, with later frames trickling in as the cron iterates.

set -euo pipefail

MODEL="HRRR"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CONFIG="${REPO_ROOT}/config/products.yml"

# Number of recent run-hours to sweep on every tick. retain_runs=5, so
# offsets 0..4 cover exactly the runs that survive the prune — anything
# older just gets deleted at the end, so rendering it is wasted work.
HOURS_BACK=4

PRODUCTS=$(yq -r ".models.${MODEL}.products[]" "$CONFIG")
RETAIN=$(yq -r ".models.${MODEL}.retain_runs" "$CONFIG")

# ── Pre-fetch R2 listing for fast idempotent skips ────────────────
# Without this, every (run × product × fh) tuple would `rclone lsf`
# a single key — ~150 ms each, ~150 KB worth of Class B ops per
# tick. One recursive listing at the start does the same work in
# one round-trip, and decode_pipeline.sh consults the local cache.
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
  echo "==> HRRR sweep: run=${RUN_DATE}${RUN_HOUR}Z (offset -${offset}h)"

  case "${RUN_HOUR}" in
    00|06|12|18) FH_END=48 ;;
    *)           FH_END=18 ;;
  esac

  for product in $PRODUCTS; do
    for fh in $(seq 0 "${FH_END}"); do
      # ── Parent-level idempotent skip ──────────────────────────────
      # Previously decode_pipeline.sh was invoked for EVERY (run × product
      # × fh) tuple — ~1,300 per tick — and each invocation paid ~1 s of
      # overhead (≈10 yq spawns + a python spawn + a curl HEAD to AWS S3)
      # *before* reaching its own R2-existence check. On a steady-state
      # bucket where almost every frame already exists, that overhead was
      # the entire tick. Grep the pre-listed bucket cache here so an
      # already-rendered frame costs one in-memory lookup and never spawns
      # a subprocess at all. FORCE_RERENDER bypasses the skip (same as the
      # decode script) so the recovery path still re-renders everything.
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

  # ── Publish the manifest as soon as the newest run is rendered ────
  # build_manifest.py lists R2 directly, so once offset 0's frames are
  # uploaded the freshest run is already visible to it. Publishing here —
  # rather than only after the whole 4-run sweep + prune finishes — moves
  # what the app sees forward by the remainder of the tick (often 10-20
  # min). Older offsets keep back-filling trailing forecast hours after.
  if [ "${offset}" -eq 0 ]; then
    echo "==> Publishing manifest early (newest run rendered)"
    python3 "${SCRIPT_DIR}/build_manifest.py" "${MODEL}" \
      || echo "  (early manifest build failed; will retry at end of tick)"
  fi
done

# ── Prune R2 to the most recent N runs ─────────────────────────────
echo ""
echo "==> Pruning HRRR to last ${RETAIN} runs"
python3 "${SCRIPT_DIR}/prune_old_runs.py" "${MODEL}" "${RETAIN}"

# ── Rebuild manifest (final, post-backfill + post-prune) ──────────
echo "==> Rebuilding HRRR manifest"
python3 "${SCRIPT_DIR}/build_manifest.py" "${MODEL}"

echo ""
echo "==> HRRR render complete (attempted runs: ${ATTEMPTED_RUNS[*]})"
