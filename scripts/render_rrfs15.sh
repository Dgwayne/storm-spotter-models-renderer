#!/usr/bin/env bash
# render_rrfs15.sh — RRFS sub-hourly (RRFS15) sweep: NOAA's 2dfld.3km.subh
# files, 15-minute steps to f018 for EVERY hourly cycle.
#
# Structural copy of render_rrfs.sh on the minute-frame scheme (see the
# RRFS15 block in products.yml): every "fh" here is MINUTES since init
# (forecast_minutes_default), frames publish as M#####.png, and
# decode_pipeline.sh resolves {fhour} + the subh_time_filter step match.
# Minute-major ordering like render_rrfs.sh's hour-major: partial runs are
# always "all products out to minute N".

set -euo pipefail

MODEL="RRFS15"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CONFIG="${REPO_ROOT}/config/products.yml"

# Hourly cycles, f018 lands ~1h45m after init (synoptic ~2h20m): offsets
# 0..4 cover it. retain_runs (5) must stay wider than this window or
# render/prune thrash (rrfs notes, July 25).
HOURS_BACK=4

# The product-list read is shared and concurrency-safe: see
# lib/model_config.sh for why a bare `yq` here used to come back SHORT
# and take the whole render group down with it.
. "${SCRIPT_DIR}/lib/model_config.sh"
load_model_products "${MODEL}" "${CONFIG}"
RETAIN=$(yq -r ".models.${MODEL}.retain_runs" "$CONFIG")

# PRODUCT_FILTER: optional space-separated subset of the product list —
# set by the workflow's matrix jobs so render groups run in parallel
# (see render_groups in products.yml). Unset = render everything.
if [ -n "${PRODUCT_FILTER:-}" ]; then
  filter_products "${MODEL}" "${PRODUCT_FILTER}" "${CONFIG}"
  echo "==> Product filter active: $(echo ${PRODUCTS} | tr '
' ' ')"
fi

MIN_START=$(yq -r ".models.${MODEL}.forecast_minutes_default.start" "$CONFIG")
MIN_END=$(yq -r ".models.${MODEL}.forecast_minutes_default.end" "$CONFIG")
MIN_STEP=$(yq -r ".models.${MODEL}.forecast_minutes_default.step" "$CONFIG")

# Per-product forecast-hour floors/caps (fh_min products like ptype/ltng
# have no f00 message). Skipping here — not just inside decode_pipeline.sh
# — avoids ~1 s of spawn overhead per skipped tuple (see render_hrrr.sh).
declare -A FH_CAPS
declare -A FH_MINS
for product in $PRODUCTS; do
  FH_CAPS[$product]=$(yq -r ".products.${product}.fh_cap // \"\"" "$CONFIG")
  FH_MINS[$product]=$(yq -r ".products.${product}.fh_min // \"\"" "$CONFIG")
done

# ── Plan-gate published-fh filter ───────────────────────────────────
# PUBLISHED_FH_SPEC ("RUNSTAMP:fh,fh;RUNSTAMP:fh,...") is exported by the
# workflow's plan job (scripts/plan_model_work.py): the (run, fh) tuples
# whose source idx was published upstream with at least one product frame
# still missing. When set, everything else is either already rendered
# (the bucket-listing grep catches it anyway) or not yet published — and
# skipping unpublished tuples here saves ~1 s of decode-spawn overhead
# each (hundreds per tick while a run's tail publishes). Unset (manual
# runs, FORCE_RERENDER) = no filter; the sweep behaves exactly as before.
declare -A PUB_FH
if [ -n "${PUBLISHED_FH_SPEC:-}" ]; then
  IFS=';' read -ra _spec_entries <<< "${PUBLISHED_FH_SPEC}"
  for _entry in "${_spec_entries[@]}"; do
    [ -n "${_entry}" ] || continue
    _run="${_entry%%:*}"
    IFS=',' read -ra _fhs <<< "${_entry#*:}"
    for _h in "${_fhs[@]}"; do PUB_FH["${_run} ${_h}"]=1; done
  done
  echo "==> Published-fh filter active: ${#PUB_FH[@]} (run, fh) tuples"
fi

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

# ── Parallel frame renders + rolling manifest (2026-11-04) ─────────
# Serial rendering measured ~10 s/frame on the 4-vCPU runner (the
# gdal_calc.py data-PNG bake alone is 4-7 s, mostly Python start-up),
# so the 5-product surface group needed ~60 min per HOURLY run: zero
# headroom. Any backlog then ran the job into its 90-min timeout, the
# 15-min cron ticks queued behind it were evicted, and the manifest
# (written only at job start + finalize) sat ~1 h stale while finished
# frames were invisible. RENDER_JOBS frames now render concurrently
# (decode_pipeline.sh is concurrency-safe: own mktemp WORK dir, no
# shared files for GRIB models), and the manifest is rebuilt every
# MANIFEST_EVERY_S while frames are landing plus at each run boundary.
# Dispatch order stays minute-major, so partial runs still read
# "all products out to minute N" (at most RENDER_JOBS frames ragged).
RENDER_JOBS="${RENDER_JOBS:-$(nproc 2>/dev/null || echo 4)}"
MANIFEST_EVERY_S="${MANIFEST_EVERY_S:-240}"
echo "==> Parallel renders: ${RENDER_JOBS}; manifest refresh every ${MANIFEST_EVERY_S}s"
RUNNING=0
RENDERED_SINCE_MANIFEST=0
LAST_MANIFEST_EPOCH=$(date -u +%s)

refresh_manifest() {
  # $1 = reason. Only when something new landed since the last build.
  [ "${RENDERED_SINCE_MANIFEST}" -gt 0 ] || return 0
  echo "==> Manifest refresh ($1, ${RENDERED_SINCE_MANIFEST} frames dispatched since last)"
  python3 "${SCRIPT_DIR}/build_manifest.py" "${MODEL}" \
    || echo "  (manifest refresh failed; next refresh/finalize retries)"
  RENDERED_SINCE_MANIFEST=0
  LAST_MANIFEST_EPOCH=$(date -u +%s)
}

render_frame() {
  # Buffer each frame's log and print it whole, so parallel output
  # stays readable (one frame block at a time, not interleaved lines).
  local log
  log=$(mktemp)
  bash "${SCRIPT_DIR}/decode_pipeline.sh" "${MODEL}" "$1" \
    "${RUN_DATE}" "${RUN_HOUR}" "$2" > "${log}" 2>&1 \
    || echo "  fh=$2 $1 FAILED (continuing)" >> "${log}"
  cat "${log}"
  rm -f "${log}"
}

drain_renders() {
  # `|| true`: a failed frame already logged itself; errexit must not
  # take wait's status as fatal (render_mrms_obs.sh lesson).
  wait || true
  RUNNING=0
}

# ── Sweep recent runs newest → oldest ──────────────────────────────
NOW_EPOCH=$(date -u +%s)
ATTEMPTED_RUNS=()

for offset in $(seq 0 "${HOURS_BACK}"); do
  TARGET_EPOCH=$(( NOW_EPOCH - offset * 3600 ))
  RUN_DATE=$(date -u -d "@${TARGET_EPOCH}" +%Y%m%d)
  RUN_HOUR=$(date -u -d "@${TARGET_EPOCH}" +%H)
  echo ""
  echo "==> RRFS15 sweep: run=${RUN_DATE}${RUN_HOUR}Z (offset -${offset}h)"

  # FORECAST-HOUR-MAJOR (inverted 2026-08-13; render_hrrr.sh is still
  # product-major). Product-major finished one product's whole f0-f84
  # sweep before starting the next, so a mid-sweep run showed wildly
  # uneven depth across products (measured on the 18z backfill: t2m=85
  # while wind10m=22 and mcc=1) and the user just saw "some products
  # lag". Walking fh on the OUTSIDE advances every product together, so
  # partial coverage is always "all products out to f_n" — which also
  # matches how the app scrubs (near hours first). Same total work and
  # same number of idx fetches; purely an ordering change.
  for fh in $(seq "${MIN_START}" "${MIN_STEP}" "${MIN_END}"); do
    # Plan-gate published filter (see PUBLISHED_FH_SPEC parse above).
    if [ -n "${PUBLISHED_FH_SPEC:-}" ] && [ -z "${PUB_FH["${RUN_DATE}${RUN_HOUR} ${fh}"]:-}" ]; then
      continue
    fi
    for product in $PRODUCTS; do
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
        rel_key="${product}/${RUN_DATE}${RUN_HOUR}/M$(printf '%05d' "$fh").png"
        if grep -qxF "${rel_key}" "${EXISTING_KEYS_FILE}"; then
          continue
        fi
      fi
      render_frame "${product}" "${fh}" &
      RUNNING=$((RUNNING + 1))
      RENDERED_SINCE_MANIFEST=$((RENDERED_SINCE_MANIFEST + 1))
      if [ "${RUNNING}" -ge "${RENDER_JOBS}" ]; then
        wait -n || true
        RUNNING=$((RUNNING - 1))
      fi
    done
    # Rolling manifest while frames land. In-flight frames simply show
    # up in the next refresh (their PNG isn't listed until uploaded).
    if [ $(( $(date -u +%s) - LAST_MANIFEST_EPOCH )) -ge "${MANIFEST_EVERY_S}" ]; then
      refresh_manifest "periodic"
    fi
  done

  # Run boundary: let this run's frames finish, then publish it, so a
  # finished run never waits on older runs or the other render groups
  # (finalize only runs after EVERY group is done).
  drain_renders
  ATTEMPTED_RUNS+=("${RUN_DATE}${RUN_HOUR}")
  refresh_manifest "run ${RUN_DATE}${RUN_HOUR} done"
done

# ── Prune + final manifest (single finalize job when fanned out) ───
if [ -n "${SKIP_FINALIZE:-}" ]; then
  echo ""
  echo "==> SKIP_FINALIZE set — prune + final manifest left to the finalize job"
else
  echo ""
  echo "==> Pruning RRFS15 to last ${RETAIN} runs"
  python3 "${SCRIPT_DIR}/prune_old_runs.py" "${MODEL}" "${RETAIN}"

  echo "==> Rebuilding RRFS15 manifest"
  python3 "${SCRIPT_DIR}/build_manifest.py" "${MODEL}"
fi

echo ""
echo "==> RRFS15 render complete (attempted runs: ${ATTEMPTED_RUNS[*]})"
