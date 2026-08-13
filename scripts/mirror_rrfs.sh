#!/usr/bin/env bash
# mirror_rrfs.sh — NOMADS→B2 slim mirror for RRFS (the Aug 2026 bridge).
#
# WHY THIS EXISTS: NOAA froze the rrfs_a/rrfs_public prototype feeds on
# noaa-rrfs-pds at 2026-08-12 11z when the pre-implementation parallel
# began (SCN 26-48). Until operational implementation (2026-10-06 12z)
# the only live RRFS source is NOMADS — which publishes NO .idx files,
# so the byte-range decode pipeline can't read it directly.
#
# THE BRIDGE: this job downloads each whole 2dfld CONUS GRIB (~366 MB)
# ONCE, extracts only the messages our products actually consume into a
# slim GRIB (~10-20% of the file), generates a NOAA-style .idx for it
# with `wgrib2 -s` (the same tool NOAA uses), and uploads both to
# v1/RRFS/_src/ on the bucket. decode_pipeline.sh then runs BYTE-FOR-BYTE
# unchanged against our own CDN mirror — products.yml just points
# base_url at models.dgwaynes.com. Renders trail the mirror by at most
# one cron tick.
#
# SYNOPTIC-ONLY (deliberate): on NOMADS, non-synoptic cycles publish
# ONLY .subh. files (4 sub-hourly steps per forecast hour — verified
# 2026-08-13: the completed 14z cycle has zero plain 2dfld files). Our
# matches would grab the wrong sub-hourly band, so during the bridge
# RRFS updates 4×/day (00/06/12/18z, f000-f084). Do NOT "fix" this by
# pointing at .subh. files without per-valid-time band selection.
#
# _src IS INVISIBLE to prune_old_runs.py and build_manifest.py (both
# iterate the config product list), so this script prunes its own runs.
#
# ⚠ REMOVE AT CUTOVER (2026-10-06): once AWS resumes on noaa-rrfs-pds,
# restore the S3 source in products.yml (commented block there), delete
# this script + the mirror job in render_rrfs.yml, and re-enable hourly
# cycles in render_rrfs.sh.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CONFIG="${REPO_ROOT}/config/products.yml"

NOMADS_BASE="https://nomads.ncep.noaa.gov/pub/data/nccf/com/rrfs/v1.0"
# NOMADS etiquette: identify ourselves so throttling decisions are
# informed, not blind. Sequential downloads only — never parallelize.
UA="SpotterToolsPro-models-renderer/1.0 (contact: dgwaynesllc@gmail.com)"

# How many recent synoptic cycles to sweep. 3 cycles = 18 h, far past the
# ~4 h it takes a run to finish publishing f084.
SYNOPTIC_SWEEP=3
FH_END=84
# Keep this many mirrored runs under _src. Renders lag the mirror by one
# tick, so 3 is generous; storage ≈ 3 runs × 85 fh × ~40 MB ≈ 10 GB.
SRC_RETAIN=3
# A complete 2dfld CONUS file inventories ~315 messages (~290 at f000).
# Fewer means we caught NOMADS mid-upload — skip, retry next tick.
MIN_IDX_LINES=150
# Per-tick ceiling on fresh downloads so one job stays well inside its
# timeout during backfill (~60 files ≈ 22 GB ≈ 30 min). The sweep is
# newest-run-first, so the app always gets the freshest data first and
# older runs backfill on later ticks.
MAX_NEW_FILES="${MIRROR_MAX_NEW:-60}"

# ── Collect every match regex our RRFS products consume ────────────────
# Non-derived products contribute wgrib2_match; derived products
# contribute their inputs map values (decode_pipeline.sh never
# substitutes fh placeholders into inputs, so those are literal).
MATCHES_RAW=$(mktemp)
trap 'rm -f "$MATCHES_RAW"' EXIT
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
trap 'rm -f "$MATCHES_RAW" "$EXISTING"' EXIT
rclone lsf --recursive "r2:${R2_BUCKET}/v1/RRFS/_src/" --files-only 2>/dev/null > "$EXISTING" || : > "$EXISTING"
echo "==> $(wc -l < "$EXISTING") existing mirror keys"

NEW_COUNT=0
NOW_EPOCH=$(date -u +%s)
# Anchor to the most recent synoptic hour (00/06/12/18): truncate the
# epoch to the hour, then step back to hour % 6 == 0. (The 10# prefix
# guards zero-padded hours like "08" from octal parsing.)
CUR_HOUR=$(date -u +%H)
HOUR_EPOCH=$(( NOW_EPOCH - NOW_EPOCH % 3600 ))
LAST_SYN_EPOCH=$(( HOUR_EPOCH - (10#$CUR_HOUR % 6) * 3600 ))

for cyc in $(seq 0 $(( SYNOPTIC_SWEEP - 1 ))); do
  RUN_EPOCH=$(( LAST_SYN_EPOCH - cyc * 21600 ))
  RUN_DATE=$(date -u -d "@${RUN_EPOCH}" +%Y%m%d)
  RUN_HOUR=$(date -u -d "@${RUN_EPOCH}" +%H)
  echo ""
  echo "==> Mirror sweep: run=${RUN_DATE}${RUN_HOUR}Z"

  for fh in $(seq 0 "$FH_END"); do
    FH3=$(printf '%03d' "$fh")
    REL="_src/${RUN_DATE}${RUN_HOUR}/F${FH3}"
    # idx is uploaded LAST, so its presence means the pair is complete.
    # (The pre-listing is relative to _src/, hence the stripped key.)
    if grep -qxF "${RUN_DATE}${RUN_HOUR}/F${FH3}.grib2.idx" "$EXISTING"; then
      continue
    fi
    if [ "$NEW_COUNT" -ge "$MAX_NEW_FILES" ]; then
      echo "  per-tick download ceiling (${MAX_NEW_FILES}) reached — resuming next tick"
      break 2
    fi

    SRC_URL="${NOMADS_BASE}/rrfs.${RUN_DATE}/${RUN_HOUR}/rrfs.t${RUN_HOUR}z.2dfld.3km.f${FH3}.conus.grib2"
    if ! curl -sfI -A "$UA" --max-time 30 "$SRC_URL" > /dev/null; then
      continue   # not published yet (fhs trail in over ~4 h)
    fi

    WORK=$(mktemp -d)
    SRC="${WORK}/src.grib2"
    echo "  f${FH3}: downloading"
    if ! curl -sfS -A "$UA" --retry 3 --retry-delay 5 --max-time 900 -o "$SRC" "$SRC_URL"; then
      echo "  f${FH3}: download FAILED (continuing)"
      rm -rf "$WORK"; continue
    fi
    NEW_COUNT=$(( NEW_COUNT + 1 ))

    # Inventory the source ourselves (NOMADS has no idx). A short or
    # unparseable inventory = we raced NOAA's upload; retry next tick.
    if ! wgrib2 -s "$SRC" > "${WORK}/src.inv" 2>/dev/null \
        || [ "$(wc -l < "${WORK}/src.inv")" -lt "$MIN_IDX_LINES" ]; then
      echo "  f${FH3}: partial/unparseable GRIB ($(wc -l < "${WORK}/src.inv" 2>/dev/null || echo 0) msgs) — retry next tick"
      rm -rf "$WORK"; continue
    fi

    # Substitute decode_pipeline.sh's fh placeholders, then take the
    # union of every product's matching inventory lines. sort -u dedups
    # products sharing a field; the numeric re-sort restores file order
    # (float keys keep NAM-style subfield pairs like 624.1/624.2 intact,
    # and preserves TCDC's instantaneous-before-average message order
    # that band-1 selection depends on).
    sed -e "s/{fh}/${fh}/g" \
        -e "s/{fh_minus_1}/$(( fh - 1 ))/g" \
        -e "s/{fh_minus_3}/$(( fh - 3 ))/g" \
        -e "s/{fh_minus_6}/$(( fh - 6 ))/g" \
        "$MATCHES_RAW" > "${WORK}/matches.txt"
    grep -E -f "${WORK}/matches.txt" "${WORK}/src.inv" | sort -u | sort -t: -k1,1g > "${WORK}/union.inv" || true
    if [ ! -s "${WORK}/union.inv" ]; then
      echo "  f${FH3}: union matched nothing (unexpected) — skipping"
      rm -rf "$WORK"; continue
    fi

    wgrib2 "$SRC" -i -grib "${WORK}/slim.grib2" < "${WORK}/union.inv" > /dev/null
    wgrib2 -s "${WORK}/slim.grib2" > "${WORK}/slim.idx"
    SLIM_MB=$(( $(stat -c%s "${WORK}/slim.grib2") / 1048576 ))
    echo "  f${FH3}: slim ${SLIM_MB} MB, $(wc -l < "${WORK}/union.inv") msgs"

    # grib2 first, idx last — decode gates on the idx, so a killed job
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
