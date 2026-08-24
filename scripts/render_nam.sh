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
# ⚠ RETRY, do not simplify: a bare `yq` here has been observed returning
# EMPTY on a loaded box (box 1 sits at load 21/16 with several render
# groups reading this same file concurrently). An empty PRODUCTS made the
# PRODUCT_FILTER loop below reject every VALID code with
# "not in models.<M>.products" and exit 1, silently costing that group its
# whole tick. Data survived only because the next tick redid it. Retry the
# read, then hard-fail loudly if it is still empty — "I could not read the
# config" must never masquerade as "your config is wrong".
PRODUCTS=""
for _try in 1 2 3; do
  PRODUCTS=$(yq -r ".models.${MODEL}.products[]" "$CONFIG" 2>/dev/null || true)
  [ -n "$PRODUCTS" ] && break
  echo "WARN: empty product list for ${MODEL} (attempt ${_try}/3); retrying" >&2
  sleep 2
done
if [ -z "$PRODUCTS" ]; then
  echo "FATAL: could not read models.${MODEL}.products from ${CONFIG} after 3 attempts" >&2
  exit 1
fi
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
      # Plan-gate published filter (see PUBLISHED_FH_SPEC parse above).
      if [ -n "${PUBLISHED_FH_SPEC:-}" ] && [ -z "${PUB_FH["${RUN_DATE}${RUN_HOUR} ${fh}"]:-}" ]; then
        continue
      fi
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

  # Publish the manifest as soon as the newest cycle is rendered so the app
  # sees it 10-20 min earlier than waiting for the full sweep + prune.
  if [ "${offset}" -eq 0 ]; then
    echo "==> Publishing manifest early (newest cycle rendered)"
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
  echo "==> Pruning NAM to last ${RETAIN} runs"
  python3 "${SCRIPT_DIR}/prune_old_runs.py" "${MODEL}" "${RETAIN}"

  echo "==> Rebuilding NAM manifest"
  python3 "${SCRIPT_DIR}/build_manifest.py" "${MODEL}"
fi

echo ""
echo "==> NAM render complete (attempted runs: ${ATTEMPTED_RUNS[*]})"
