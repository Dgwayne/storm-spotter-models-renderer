#!/usr/bin/env bash
# mirror_rrfs.sh — NOMADS→B2 slim mirror for RRFS (the Aug 2026 bridge).
#
# WHY THIS EXISTS: NOAA froze the rrfs_a/rrfs_public prototype feeds on
# noaa-rrfs-pds at 2026-08-12 11z when the pre-implementation parallel
# began (SCN 26-48). Until operational implementation (2026-10-06 12z)
# the only live RRFS source is NOMADS.
#
# WHY NOT READ NOMADS DIRECTLY: NOMADS blocks IPs over ~120 requests/min.
# decode_pipeline.sh makes 3-4 requests per (product, fh), ~12,000 per
# synoptic run across 8 parallel render jobs plus the plan job. This job
# is the ONE paced NOMADS client; everything else reads our own CDN.
#
# THE BRIDGE: for each forecast hour this job reads NOMADS's .idx, takes
# the union of messages our products consume, byte-range fetches only
# those (~46 of 318 msgs, ~43 MB of a ~366 MB file, ~25 requests), then
# regenerates a NOAA-style .idx for the slim file with `wgrib2 -s` and
# uploads both to v1/RRFS/_src/. decode_pipeline.sh runs unchanged
# against that mirror (products.yml points base_url at
# models.dgwaynes.com). Renders trail the mirror by at most one cron tick.
# (Until late Sept 2026 NOMADS served no .idx, so this downloaded every
# whole file: ~124 GB/day. The idx path is ~18 GB/day with 2x the runs.)
#
# 3-HOURLY CYCLES: NOMADS publishes plain 2dfld/prslev files + .idx for
# 00/03/../21z (synoptic to f084, the rest to f018). The hours between
# are .subh. only (4 sub-hourly steps per fh). Do NOT point this at
# .subh. files without per-valid-time band selection.
#
# _src IS INVISIBLE to prune_old_runs.py and build_manifest.py (both
# iterate the config product list), so this script prunes its own runs.
#
# REMOVE AT CUTOVER (2026-10-06): once AWS resumes on noaa-rrfs-pds,
# restore the S3 source in products.yml (commented block there), delete
# this script + the mirror job in render_rrfs.yml, re-enable hourly
# cycles in render_rrfs.sh, and drop the RRFS run-hour filter in
# plan_model_work.py.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
# shellcheck source=lib/scratch.sh
source "${REPO_ROOT}/scripts/lib/scratch.sh"
stp_scratch_init mirror_rrfs
CONFIG="${REPO_ROOT}/config/products.yml"

NOMADS_BASE="https://nomads.ncep.noaa.gov/pub/data/nccf/com/rrfs/v1.0"
# NOMADS etiquette: identify ourselves so throttling decisions are
# informed, not blind. Sequential requests only (never parallelize),
# and at least NOMADS_GAP_S between them (~85/min worst case).
UA="SpotterToolsPro-models-renderer/1.0 (contact: dgwaynesllc@gmail.com)"
NOMADS_GAP_S=0.7
nomads_curl() { sleep "$NOMADS_GAP_S"; curl -A "$UA" "$@"; }

# 3-hourly cycles to sweep, newest first: 0,3,6,9 h back. Matches the
# render window (render_rrfs.sh HOURS_BACK=9); a synoptic run finishes
# publishing f084 ~4 h after init, well inside it.
CYCLES_BACK=4
# Keep this many mirrored runs under _src, one more than the render
# window can reach. Storage ~ (2 x 85 + 3 x 19) fh x ~43 MB ~ 10 GB.
SRC_RETAIN=5
# A complete 2dfld CONUS idx inventories ~318 messages (~290 at f000).
# Fewer means we caught NOMADS mid-upload: skip, retry next tick.
MIN_IDX_LINES=150
# Per-tick ceiling on fresh files so one job stays inside its timeout
# during backfill (~25 paced requests, ~25-30 s per file, so 60 files
# ~ 30 min). The sweep is newest-run-first, so the app gets the
# freshest data first and older runs backfill on later ticks.
MAX_NEW_FILES="${MIRROR_MAX_NEW:-60}"

# ── Collect every match regex our RRFS products consume ────────────────
# Non-derived products contribute wgrib2_match; derived products
# contribute their inputs map values (decode_pipeline.sh never
# substitutes fh placeholders into inputs, so those are literal).
MATCHES_RAW=$(mktemp)
trap 'rm -f "$MATCHES_RAW"; rm -rf "$STP_SCRATCH"' EXIT
for product in $(yq -r '.models.RRFS.products[]' "$CONFIG"); do
  if [ "$(yq -r ".products.${product}.derived // false" "$CONFIG")" = "true" ]; then
    yq -r ".products.${product}.inputs | to_entries[].value" "$CONFIG" >> "$MATCHES_RAW"
  else
    yq -r ".products.${product}.wgrib2_match" "$CONFIG" >> "$MATCHES_RAW"
  fi
done
echo "==> $(wc -l < "$MATCHES_RAW") match expressions collected from products.yml"

# ── Pre-list the mirror so already-done frames cost one grep ───────────
EXISTING=$(mktemp)
trap 'rm -f "$MATCHES_RAW" "$EXISTING"; rm -rf "$STP_SCRATCH"' EXIT
rclone lsf --recursive "r2:${R2_BUCKET}/v1/RRFS/_src/" --files-only 2>/dev/null > "$EXISTING" || : > "$EXISTING"
echo "==> $(wc -l < "$EXISTING") existing mirror keys"

NEW_COUNT=0
NOW_EPOCH=$(date -u +%s)
# Anchor to the most recent 3-hourly cycle: truncate the epoch to the
# hour, then step back to hour % 3 == 0. (The 10# prefix guards
# zero-padded hours like "08" from octal parsing.)
CUR_HOUR=$(date -u +%H)
HOUR_EPOCH=$(( NOW_EPOCH - NOW_EPOCH % 3600 ))
LAST_CYC_EPOCH=$(( HOUR_EPOCH - (10#$CUR_HOUR % 3) * 3600 ))

for cyc in $(seq 0 $(( CYCLES_BACK - 1 ))); do
  RUN_EPOCH=$(( LAST_CYC_EPOCH - cyc * 10800 ))
  RUN_DATE=$(date -u -d "@${RUN_EPOCH}" +%Y%m%d)
  RUN_HOUR=$(date -u -d "@${RUN_EPOCH}" +%H)
  case "${RUN_HOUR}" in
    00|06|12|18) FH_END=84 ;;
    *)           FH_END=18 ;;
  esac
  echo ""
  echo "==> Mirror sweep: run=${RUN_DATE}${RUN_HOUR}Z (f000-f$(printf '%03d' "$FH_END"))"

  for fh in $(seq 0 "$FH_END"); do
    FH3=$(printf '%03d' "$fh")
    REL="_src/${RUN_DATE}${RUN_HOUR}/F${FH3}"
    # idx is uploaded LAST, so its presence means the pair is complete.
    # (The pre-listing is relative to _src/, hence the stripped key.)
    if grep -qxF "${RUN_DATE}${RUN_HOUR}/F${FH3}.grib2.idx" "$EXISTING"; then
      continue
    fi
    if [ "$NEW_COUNT" -ge "$MAX_NEW_FILES" ]; then
      echo "  per-tick file ceiling (${MAX_NEW_FILES}) reached; resuming next tick"
      break 2
    fi

    SRC_URL="${NOMADS_BASE}/rrfs.${RUN_DATE}/${RUN_HOUR}/rrfs.t${RUN_HOUR}z.2dfld.3km.f${FH3}.conus.grib2"
    WORK=$(mktemp -d)
    # A missing idx = not published yet (fhs trail in over ~4 h). One GET,
    # no HEAD first: every request counts against the NOMADS budget.
    if ! nomads_curl -sf --max-time 60 -o "${WORK}/src.idx" "${SRC_URL}.idx"; then
      rm -rf "$WORK"; continue
    fi
    if [ "$(wc -l < "${WORK}/src.idx")" -lt "$MIN_IDX_LINES" ]; then
      echo "  f${FH3}: short idx ($(wc -l < "${WORK}/src.idx") lines); retry next tick"
      rm -rf "$WORK"; continue
    fi

    # Substitute decode_pipeline.sh's fh placeholders, then take the
    # union of every product's matching idx lines. sort -u dedups
    # products sharing a field; the numeric re-sort restores file order
    # (float keys keep subfield pairs like 624.1/624.2 intact, and
    # preserve TCDC's instantaneous-before-average message order that
    # band-1 selection depends on).
    sed -e "s/{fh}/${fh}/g" \
        -e "s/{fh_minus_1}/$(( fh - 1 ))/g" \
        -e "s/{fh_minus_3}/$(( fh - 3 ))/g" \
        -e "s/{fh_minus_6}/$(( fh - 6 ))/g" \
        "$MATCHES_RAW" > "${WORK}/matches.txt"
    grep -E -f "${WORK}/matches.txt" "${WORK}/src.idx" | sort -u | sort -t: -k1,1g > "${WORK}/union.inv" || true
    if [ ! -s "${WORK}/union.inv" ]; then
      echo "  f${FH3}: union matched nothing (unexpected); skipping"
      rm -rf "$WORK"; continue
    fi

    # Byte ranges for the selected messages (helper below): line 1 is how
    # many idx lines the slim file must inventory, then one range per line.
    python3 "${SCRIPT_DIR}/lib/idx_ranges.py" "${WORK}/src.idx" "${WORK}/union.inv" > "${WORK}/ranges.txt"
    EXPECT_MSGS=$(head -n1 "${WORK}/ranges.txt")

    FETCH_OK=1
    : > "${WORK}/slim.grib2"
    while read -r r; do
      if ! nomads_curl -sfS --retry 3 --retry-delay 5 --max-time 300 -r "$r" "$SRC_URL" >> "${WORK}/slim.grib2"; then
        FETCH_OK=0; break
      fi
    done < <(tail -n +2 "${WORK}/ranges.txt")
    NEW_COUNT=$(( NEW_COUNT + 1 ))
    if [ "$FETCH_OK" -ne 1 ]; then
      echo "  f${FH3}: range fetch FAILED (continuing)"
      rm -rf "$WORK"; continue
    fi

    # Re-inventory the slim file. A count mismatch means a truncated
    # range (a NOMADS idx can land before its grib finishes uploading):
    # never publish that; retry next tick.
    if ! wgrib2 -s "${WORK}/slim.grib2" > "${WORK}/slim.idx" 2>/dev/null \
        || [ "$(wc -l < "${WORK}/slim.idx")" -ne "$EXPECT_MSGS" ]; then
      echo "  f${FH3}: slim inventory $(wc -l < "${WORK}/slim.idx" 2>/dev/null || echo 0)/${EXPECT_MSGS} msgs; retry next tick"
      rm -rf "$WORK"; continue
    fi
    SLIM_MB=$(( $(stat -c%s "${WORK}/slim.grib2") / 1048576 ))
    echo "  f${FH3}: slim ${SLIM_MB} MB, ${EXPECT_MSGS} msgs, $(( $(wc -l < "${WORK}/ranges.txt") - 1 )) requests"

    # grib2 first, idx last: decode gates on the idx, so a killed job
    # can never leave a readable idx pointing at a missing grib.
    rclone copyto "${WORK}/slim.grib2" "r2:${R2_BUCKET}/v1/RRFS/${REL}.grib2" \
      --s3-no-check-bucket --no-traverse \
      --header-upload "Cache-Control: public, max-age=3600"
    rclone copyto "${WORK}/slim.idx" "r2:${R2_BUCKET}/v1/RRFS/${REL}.grib2.idx" \
      --s3-no-check-bucket --no-traverse \
      --header-upload "Cache-Control: public, max-age=3600"
    rm -rf "$WORK"
  done
done

# ── Prune the mirror to the newest SRC_RETAIN runs ─────────────────────
echo ""
echo "==> Pruning _src to newest ${SRC_RETAIN} runs"
rclone lsf --dirs-only "r2:${R2_BUCKET}/v1/RRFS/_src/" 2>/dev/null | sort | head -n -"$SRC_RETAIN" | while read -r d; do
  [ -n "$d" ] || continue
  echo "  prune _src/${d}"
  rclone purge "r2:${R2_BUCKET}/v1/RRFS/_src/${d}" --s3-no-check-bucket
done

echo ""
echo "==> Mirror tick complete (${NEW_COUNT} new files)"
