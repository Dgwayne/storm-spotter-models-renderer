#!/usr/bin/env bash
# render_rap.sh — fan out across multiple recent RAP run hours so
# new runs land on R2 as soon as NOAA publishes them.
#
# Strategy
# --------
# RAP runs every hour (parent model of the HRRR). f00 typically lands ~50 minutes
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

MODEL="RAP"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CONFIG="${REPO_ROOT}/config/products.yml"

# Number of recent run-hours to sweep on every tick. retain_runs=5, so
# offsets 0..4 cover exactly the runs that survive the prune — anything
# older just gets deleted at the end, so rendering it is wasted work.
HOURS_BACK=4

PRODUCTS=$(yq -r ".models.${MODEL}.products[]" "$CONFIG")
RETAIN=$(yq -r ".models.${MODEL}.retain_runs" "$CONFIG")

# PRODUCT_FILTER: optional space-separated subset of the product list.
# The workflow's matrix jobs set this so render groups run in parallel
# (see render_groups in products.yml). Unset = render everything, so a
# manual `bash scripts/render_rap.sh` behaves exactly as before.
# Unknown codes are a hard error: a typo in a render group must fail the
# job, not silently render nothing.
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

# Per-product forecast-hour caps (mesoanalysis products render f00-f01
# only). Skipping capped hours HERE — not just inside decode_pipeline.sh —
# matters: even an exit-0 decode spawn costs ~1 s of yq/python overhead,
# and the capped (product × fh × run) tuples would add ~1,900 of them
# per tick.
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
  echo "==> RAP sweep: run=${RUN_DATE}${RUN_HOUR}Z (offset -${offset}h)"

  case "${RUN_HOUR}" in
    03|09|15|21) FH_END=51 ;;
    *)           FH_END=21 ;;
  esac

  for product in $PRODUCTS; do
    for fh in $(seq 0 "${FH_END}"); do
      # Plan-gate published filter (see PUBLISHED_FH_SPEC parse above).
      if [ -n "${PUBLISHED_FH_SPEC:-}" ] && [ -z "${PUB_FH["${RUN_DATE}${RUN_HOUR} ${fh}"]:-}" ]; then
        continue
      fi
      # ── Per-product fh cap/floor (meso products: analysis frames) ─
      if [ -n "${FH_CAPS[$product]}" ] && [ "${fh}" -gt "${FH_CAPS[$product]}" ]; then
        continue
      fi
      if [ -n "${FH_MINS[$product]}" ] && [ "${fh}" -lt "${FH_MINS[$product]}" ]; then
        continue
      fi
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

# ── Prune + final manifest ─────────────────────────────────────────
# SKIP_FINALIZE: set by the workflow's matrix render jobs — prune and the
# authoritative manifest rebuild belong to the single finalize job that
# runs after every group finishes, so parallel groups never race a prune
# against another group's uploads. The early per-group manifest publish
# above stays: build_manifest.py derives everything from a fresh bucket
# listing, so concurrent snapshots are all valid and last-writer-wins.
if [ -n "${SKIP_FINALIZE:-}" ]; then
  echo ""
  echo "==> SKIP_FINALIZE set — prune + final manifest left to the finalize job"
else
  echo ""
  echo "==> Pruning RAP to last ${RETAIN} runs"
  python3 "${SCRIPT_DIR}/prune_old_runs.py" "${MODEL}" "${RETAIN}"

  # Final manifest (post-backfill + post-prune)
  echo "==> Rebuilding RAP manifest"
  python3 "${SCRIPT_DIR}/build_manifest.py" "${MODEL}"
fi

echo ""
echo "==> RAP render complete (attempted runs: ${ATTEMPTED_RUNS[*]})"
