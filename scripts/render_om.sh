#!/usr/bin/env bash
# render_om.sh <MODEL> — sweep the recent cycles of ANY openmeteo_spatial
# model (see the ICOND2/AIGFS/GFS013/UKMO/GDPS/HGEFS blocks in
# config/products.yml). Generalization of the original render_icond2.sh.
#
# Design notes carried over (learned the hard way on ICON-D2's first day):
#   * FORECAST-HOUR-MAJOR sweep + OM_CACHE_DIR: one .om file per valid
#     time carries every variable, so the first product's download feeds
#     all the others; partial coverage is always "all products out to f_n".
#   * Bootstrap manifest at tick start: a cold start must never 404 the
#     manifest (freshness monitor + app both read it).
#   * INTERIM manifest publishes during the newest cycle: a cold-start
#     backfill outlives the 30-min dispatch interval and GitHub supersedes
#     the older queued run, so publish-only-at-the-end never publishes.
#   * Per-cycle existence gate = HEAD on the run's FIRST valid-time file
#     (not f000 — AIGFS/HGEFS start at f006).
#
# Data: Open-Meteo AWS Open Data (CC-BY-4.0 — "Weather data by
# Open-Meteo.com"); underlying models (c) their agencies (DWD, NOAA,
# UKMO, ECCC), each CC-BY-4.0 or public domain.

set -euo pipefail

MODEL="${1:?usage: render_om.sh <MODEL>}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CONFIG="${REPO_ROOT}/config/products.yml"

CYCLE_HOURS=$(yq -r ".models.${MODEL}.om_cycle_hours" "$CONFIG")
CYCLE_SECONDS=$(( CYCLE_HOURS * 3600 ))
CYCLES_BACK=$(yq -r ".models.${MODEL}.om_cycles_back // 2" "$CONFIG")

# Sub-hourly models (om_step_minutes): every fh in this sweep is MINUTES
# since run init and frames are M#####.png — see decode_pipeline.sh.
STEP_MINUTES=$(yq -r ".models.${MODEL}.om_step_minutes // \"\"" "$CONFIG")
FH_UNIT_SECONDS=3600
[ -n "${STEP_MINUTES}" ] && FH_UNIT_SECONDS=60

# Forecast-hour list: segments-aware (GFS013/UKMO/GDPS mix hourly + 3-hourly);
# sub-hourly models list minutes from forecast_minutes_default instead.
FH_LIST=$(python3 - "$MODEL" "$CONFIG" <<'PY'
import sys, yaml
mc = yaml.safe_load(open(sys.argv[2]))["models"][sys.argv[1]]
if mc.get("om_step_minutes"):
    d = mc["forecast_minutes_default"]
    hours = list(range(d["start"], d["end"] + 1, d.get("step", mc["om_step_minutes"])))
else:
    segs = mc.get("forecast_hours_segments")
    if segs:
        hours = sorted({h for s in segs for h in range(s["start"], s["end"] + 1, s.get("step", 1))})
    else:
        d = mc["forecast_hours_default"]
        hours = list(range(d["start"], d["end"] + 1, d.get("step", 1)))
print(" ".join(map(str, hours)))
PY
)
FIRST_FH=$(echo "${FH_LIST}" | cut -d' ' -f1)
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
# the freshness monitor and the app never see a 404 mid-backfill.
python3 "${SCRIPT_DIR}/build_manifest.py" "${MODEL}" \
  || echo "  (bootstrap manifest build failed; sweep continues)"

# ── Sweep recent cycles newest → oldest ────────────────────────────
NOW_EPOCH=$(date -u +%s)
BASE_EPOCH=$(( (NOW_EPOCH / CYCLE_SECONDS) * CYCLE_SECONDS ))
ATTEMPTED_RUNS=()
# Interim/early manifest publishes belong to the NEWEST cycle that is
# actually rendering, not literally offset 0 — when the newest cycle
# hasn't published upstream yet (gate-skips), pinning to offset 0 means
# a cold start renders a full older cycle with zero manifest updates.
PUBLISHING_OFFSET=-1

for offset in $(seq 0 "${CYCLES_BACK}"); do
  TARGET_EPOCH=$(( BASE_EPOCH - offset * CYCLE_SECONDS ))
  RUN_DATE=$(date -u -d "@${TARGET_EPOCH}" +%Y%m%d)
  RUN_HOUR=$(date -u -d "@${TARGET_EPOCH}" +%H)
  RUN_DIR_URL="${OM_BASE_URL}/${OM_MODEL_PATH}/${RUN_DATE:0:4}/${RUN_DATE:4:2}/${RUN_DATE:6:2}/${RUN_HOUR}00Z"
  echo ""
  echo "==> ${MODEL} sweep: run=${RUN_DATE}${RUN_HOUR}Z (offset -$((offset * CYCLE_HOURS))h)"

  # One cheap gate per cycle: the run's FIRST valid-time file is the
  # first thing Open-Meteo uploads. Absent → run not started → skip the
  # per-frame HEAD fan-out for a cycle that doesn't exist yet.
  FIRST_STAMP=$(date -u -d "@$(( TARGET_EPOCH + FIRST_FH * FH_UNIT_SECONDS ))" +%Y-%m-%dT%H%M)
  if ! curl -sfI "${RUN_DIR_URL}/${FIRST_STAMP}.om" > /dev/null; then
    echo "  run not yet publishing; skip cycle"
    continue
  fi
  if [ "${PUBLISHING_OFFSET}" -lt 0 ]; then
    PUBLISHING_OFFSET=${offset}
  fi

  FH_COUNT=0
  for fh in ${FH_LIST}; do
    # Frame stem must match decode_pipeline.sh's FRAME_BASE exactly —
    # the skip grep and the om-cache rm below both key on it.
    if [ -n "${STEP_MINUTES}" ]; then
      frame_base="M$(printf '%05d' "$fh")"
    else
      frame_base="F$(printf '%03d' "$fh")"
    fi
    for product in $PRODUCTS; do
      if [ -z "${FORCE_RERENDER:-}" ]; then
        rel_key="${product}/${RUN_DATE}${RUN_HOUR}/${frame_base}.png"
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
    rm -f "${OM_CACHE_DIR}/${MODEL}_${RUN_DATE}${RUN_HOUR}_${frame_base}.om"

    # Interim manifest publish every 12 swept hours on the newest cycle
    # (counter-based, NOT fh%N — a 6-hourly model would otherwise publish
    # on every single step). See the cold-start rationale in the header.
    FH_COUNT=$(( FH_COUNT + 1 ))
    if [ "${offset}" -eq "${PUBLISHING_OFFSET}" ] && [ $(( FH_COUNT % 12 )) -eq 0 ]; then
      echo "==> Interim manifest publish (through f${fh})"
      python3 "${SCRIPT_DIR}/build_manifest.py" "${MODEL}" \
        || echo "  (interim manifest build failed; continuing)"
    fi
  done

  ATTEMPTED_RUNS+=("${RUN_DATE}${RUN_HOUR}")

  # Publish the manifest as soon as the newest cycle is rendered so the app
  # sees it 10-20 min earlier than waiting for the full sweep + prune.
  if [ "${offset}" -eq "${PUBLISHING_OFFSET}" ]; then
    echo "==> Publishing manifest early (newest cycle rendered)"
    python3 "${SCRIPT_DIR}/build_manifest.py" "${MODEL}" \
      || echo "  (early manifest build failed; will retry at end of tick)"
  fi
done

# ── Prune R2 to the most recent N runs ─────────────────────────────
echo ""
echo "==> Pruning ${MODEL} to last ${RETAIN} runs"
python3 "${SCRIPT_DIR}/prune_old_runs.py" "${MODEL}" "${RETAIN}"

# ── Rebuild manifest (final, post-backfill + post-prune) ──────────
echo "==> Rebuilding ${MODEL} manifest"
python3 "${SCRIPT_DIR}/build_manifest.py" "${MODEL}"

echo ""
echo "==> ${MODEL} render complete (attempted runs: ${ATTEMPTED_RUNS[*]})"
