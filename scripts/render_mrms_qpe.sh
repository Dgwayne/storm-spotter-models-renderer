#!/usr/bin/env bash
# render_mrms_qpe.sh — observed MRMS precip accumulation (OBS pseudo-model).
#
# Renders the MultiSensor QPE 1/3/6/12/24-hour accumulations from the
# noaa-mrms-pds AWS bucket into the same R2 layout the model renderers use:
#
#   v1/OBS/<product>/<VALIDSTAMP>/F000.png   (+ F000.json point grid)
#
# where VALIDSTAMP = YYYYMMDDHH of the accumulation END hour, so
# build_manifest.py / prune_old_runs.py / the app's frame fetcher treat each
# valid hour as a one-frame "run".
#
# Freshness: Pass2 (full gauge correction) publishes ~58 min after the valid
# hour; Pass1 (~90% of the gauges) publishes ~16 min after. Each tick renders
# Pass1 as a bridge when Pass2 isn't out yet, and drops an F000.pass1 marker
# next to the frame so a later tick knows to overwrite it with Pass2. Net
# effect: the layer runs ~15-20 min behind the clock instead of an hour plus.
#
# MRMS grib gotchas handled here (vs decode_pipeline.sh, which this script
# deliberately does NOT reuse — no .idx files, no forecast hours, gzip
# transport, sentinel negatives):
#   * values are mm with -1 (missing) / -3 (outside radar coverage) sentinels
#     → A<0 mapped to NoData BEFORE the mm→inch conversion
#   * the 0.01° grid is published on 0-360 longitudes; GDAL's GRIB driver
#     normally normalizes to ±180 but we guard anyway (gdal_edit shift)
#
# Required env: R2_BUCKET, R2_ENDPOINT, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY
# Required tools: curl, gdal (translate/calc/warp/dem/edit), yq, rclone, python3

set -euo pipefail

MODEL="OBS"
# Bucket path segment, separate from the model used for config lookup. Same
# split as render_mrms_obs.sh: OBS_PREFIX moves where this writes without
# touching what it reads. Unset behaves exactly as before.
#
# This script owns the manifest as much as the OBS one does — it rebuilds
# it twice per tick and prunes. Two processes rebuilding v1/OBS/manifest.json
# on different schedules is survivable (each rebuild derives from a fresh
# listing) but it lets a fast-tick srcTimes patch land on top of a rebuild
# and briefly revert QPE availability. Giving this the same prefix knob is
# what lets the whole OBS model, QPE included, move to one box and one
# writer.
PREFIX="${OBS_PREFIX:-$MODEL}"
LIVE_PREFIX="OBS"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CONFIG="${REPO_ROOT}/config/products.yml"
COLOR_TABLES="${REPO_ROOT}/config/color_tables"

# Valid hours to sweep each tick (newest → oldest). 3 back covers a missed
# tick or a late NOAA publish without re-listing the whole day.
HOURS_BACK=3

PRODUCTS=$(yq -r ".models.${MODEL}.products[]" "$CONFIG")
RETAIN=$(yq -r ".models.${MODEL}.retain_runs" "$CONFIG")
BBOX=$(yq -r ".models.${MODEL}.bbox_lonlat | join(\" \")" "$CONFIG")
IMG_W=$(yq -r ".models.${MODEL}.image_size[0]" "$CONFIG")
IMG_H=$(yq -r ".models.${MODEL}.image_size[1]" "$CONFIG")

# Retention override, shadow-only by construction — a live prefix always
# uses the config value, so forgetting to unset a shadow's shallow
# retention can never prune production history.
if [ -n "${OBS_RETAIN:-}" ] && [ "${PREFIX}" != "${LIVE_PREFIX}" ]; then
  RETAIN="${OBS_RETAIN}"
fi

# ── Pre-fetch R2 listing (same trick as render_hrrr.sh) ────────────────
EXISTING_KEYS_FILE=$(mktemp)
trap 'rm -f "$EXISTING_KEYS_FILE"' EXIT
echo "==> Pre-listing R2 contents under v1/${PREFIX}/"
if rclone lsf --recursive "r2:${R2_BUCKET}/v1/${PREFIX}/" \
    --files-only 2>/dev/null > "$EXISTING_KEYS_FILE"; then
  echo "  $(wc -l < "$EXISTING_KEYS_FILE") existing keys cached"
else
  echo "  (no existing keys, fresh bucket)"
  : > "$EXISTING_KEYS_FILE"
fi

has_key() { grep -qxF "$1" "$EXISTING_KEYS_FILE"; }

# ── Render one (product, valid hour) from a specific pass ──────────────
# Args: product date(YYYYMMDD) hour(HH) pass(1|2)
render_one() {
  local product="$1" vdate="$2" vhour="$3" pass="$4"
  local mrms_prefix clr_file decimals grid_w grid_h
  mrms_prefix=$(yq -r ".products.${product}.mrms_product" "$CONFIG")
  clr_file=$(yq -r ".products.${product}.clr" "$CONFIG")
  decimals=$(yq -r ".products.${product}.point_decimals // 2" "$CONFIG")
  grid_w=$(yq -r ".products.${product}.point_grid[0] // 512" "$CONFIG")
  grid_h=$(yq -r ".products.${product}.point_grid[1] // 512" "$CONFIG")
  # Optional additive value PNG (F000.data.png) for the app's crosshair
  # inspector — empty unless the product declares `gpu_data`.
  local gd_min gd_max
  gd_min=$(yq -r ".products.${product}.gpu_data.min // \"\"" "$CONFIG")
  gd_max=$(yq -r ".products.${product}.gpu_data.max // \"\"" "$CONFIG")

  local stamp="${vdate}${vhour}"
  local src_dir="${mrms_prefix}_Pass${pass}_00.00"
  local fname="MRMS_${src_dir}_${vdate}-${vhour}0000.grib2.gz"
  local url="https://noaa-mrms-pds.s3.amazonaws.com/CONUS/${src_dir}/${vdate}/${fname}"
  local out_rel="v1/${PREFIX}/${product}/${stamp}/F000.png"
  local json_rel="${out_rel%.png}.json"
  local sentinel_rel="${out_rel%.png}.pass1"

  local work
  work="$(mktemp -d)"
  # Subshell-safe cleanup: render_one runs in the main shell, so scope the
  # temp dir removal to this invocation instead of clobbering the EXIT trap.
  local gz="${work}/in.grib2.gz"

  echo "[${product}] valid=${stamp}Z pass${pass}"
  if ! curl -sf "${url}" -o "${gz}"; then
    echo "  download failed (${url}); skip"
    rm -rf "${work}"
    return 1
  fi
  gunzip -f "${gz}"
  local grib="${work}/in.grib2"

  # GRIB → Float32 GTiff (applies any packing scale/offset).
  gdal_translate -q -of GTiff -ot Float32 -b 1 "${grib}" "${work}/native.tif"

  # Longitude guard: if the driver left the grid on 0-360 lons, shift the
  # georeference west by 360 so the EPSG:4326 → 3857 warp lands on CONUS.
  local west
  west=$(python3 - "${work}/native.tif" <<'PY'
import json, subprocess, sys
info = json.loads(subprocess.check_output(["gdalinfo", "-json", sys.argv[1]]))
print(info["cornerCoordinates"]["upperLeft"][0])
PY
  )
  if python3 -c "import sys; sys.exit(0 if float('${west}') > 180.0 else 1)"; then
    echo "  0-360 longitude grid detected (west=${west}); shifting -360"
    python3 - "${work}/native.tif" <<'PY'
import json, subprocess, sys
p = sys.argv[1]
info = json.loads(subprocess.check_output(["gdalinfo", "-json", p]))
(ulx, uly) = info["cornerCoordinates"]["upperLeft"]
(lrx, lry) = info["cornerCoordinates"]["lowerRight"]
subprocess.check_call(["gdal_edit.py", "-a_ullr",
                       str(ulx - 360), str(uly), str(lrx - 360), str(lry), p])
PY
  fi

  # Sentinel negatives (-1 missing / -3 no coverage) → NoData, then mm → in.
  gdal_calc.py --quiet -A "${work}/native.tif" --outfile="${work}/raw.tif" \
    --calc="where(A<0,-9999,A/25.4)" --NoDataValue=-9999 --type=Float32 --overwrite
  echo "  raw.tif stats:"
  gdalinfo -stats "${work}/raw.tif" 2>/dev/null | grep -E "Min|Max|Mean" | head -3 | sed 's/^/    /'

  # Reproject to Web Mercator at the OBS render size. Cubic keeps gradient
  # edges crisp when the 1 km grid maps to ~1.9 km output pixels; small
  # negative undershoot near sharp edges snaps to the transparent 0.00 bin.
  # shellcheck disable=SC2086
  gdalwarp -q -overwrite -t_srs EPSG:3857 -te_srs EPSG:4326 -te ${BBOX} \
    -ts "${IMG_W}" "${IMG_H}" -r cubic -dstnodata -9999 \
    "${work}/raw.tif" "${work}/merc.tif"

  # Point-value grid for the app's inspector (inches, 2 decimals). Uploaded
  # BEFORE the PNG — same contract as decode_pipeline.sh (PNG availability
  # implies the JSON exists). Non-fatal on failure.
  if python3 "${REPO_ROOT}/scripts/sample_point_values.py" \
       "${work}/merc.tif" "${work}/F000.json" "${MODEL}" "${product}" \
       "${stamp}" "0" "in" "${decimals}" "${BBOX}" "${grid_w}" "${grid_h}"; then
    rclone copyto "${work}/F000.json" "r2:${R2_BUCKET}/${json_rel}" \
      --s3-no-check-bucket --no-traverse \
      --header-upload "Cache-Control: public, max-age=300"
  else
    echo "  point-value sampling FAILED (non-fatal, PNG continues)"
  fi

  # Binned colors (nearest stop) — crisp Pivotal-style accumulation bands.
  gdaldem color-relief -q -alpha -nearest_color_entry \
    "${work}/merc.tif" "${COLOR_TABLES}/${clr_file}" "${work}/rgba.tif"
  gdal_translate -q -of PNG -co ZLEVEL=9 "${work}/rgba.tif" "${work}/F000.png"
  echo "  png $(stat -c%s "${work}/F000.png" 2>/dev/null || stat -f%z "${work}/F000.png") bytes"

  rclone copyto "${work}/F000.png" "r2:${R2_BUCKET}/${out_rel}" \
    --s3-no-check-bucket --no-traverse \
    --header-upload "Cache-Control: public, max-age=300"
  echo "  uploaded ${out_rel}"

  # Additive value PNG (gray + alpha) for the app's crosshair inspector —
  # encoded from the SAME merc.tif the colorized fill above is built from,
  # so a readout always agrees with the color under it. gray 1..255 =
  # gd_min..gd_max linear, 0 = nodata (matches render_mrms_obs.sh's data_png
  # encode). The colorized F000.png is untouched — the app still renders it
  # as the fill; only the inspector reads this file. Non-fatal on failure.
  # (--hideNoData feeds the raw -9999s so the where() guards see them; see
  # the same note in render_mrms_obs.sh.)
  if [ -n "${gd_min}" ] && [ -n "${gd_max}" ]; then
    local data_rel="${out_rel%.png}.data.png"
    if gdal_calc.py --quiet -A "${work}/merc.tif" --outfile="${work}/ga.tif" \
         --calc="where(A==-9999,0,minimum(255,maximum(1,1+round((A-(${gd_min}))*254.0/((${gd_max})-(${gd_min}))))))" \
         --calc="where(A==-9999,0,255)" \
         --type=Byte --hideNoData --overwrite \
       && gdal_translate -q -of PNG -co ZLEVEL=9 "${work}/ga.tif" "${work}/F000.data.png"; then
      rclone copyto "${work}/F000.data.png" "r2:${R2_BUCKET}/${data_rel}" \
        --s3-no-check-bucket --no-traverse \
        --header-upload "Cache-Control: public, max-age=300"
      echo "  uploaded ${data_rel} (inspector values)"
    else
      echo "  value-PNG encode FAILED (non-fatal, fill + grid unaffected)"
    fi
  fi

  # Pass bookkeeping: mark Pass1 bridges for later Pass2 overwrite; clear
  # the marker once a Pass2 render lands.
  if [ "${pass}" = "1" ]; then
    : > "${work}/marker"
    rclone copyto "${work}/marker" "r2:${R2_BUCKET}/${sentinel_rel}" \
      --s3-no-check-bucket --no-traverse
  elif has_key "${sentinel_rel#v1/${PREFIX}/}"; then
    rclone deletefile "r2:${R2_BUCKET}/${sentinel_rel}" 2>/dev/null || true
  fi

  rm -rf "${work}"
  return 0
}

# ── Sweep recent valid hours newest → oldest ───────────────────────────
NOW_EPOCH=$(date -u +%s)
FLOOR_EPOCH=$(( NOW_EPOCH - NOW_EPOCH % 3600 ))

for offset in $(seq 0 "${HOURS_BACK}"); do
  TARGET_EPOCH=$(( FLOOR_EPOCH - offset * 3600 ))
  VDATE=$(date -u -d "@${TARGET_EPOCH}" +%Y%m%d)
  VHOUR=$(date -u -d "@${TARGET_EPOCH}" +%H)
  STAMP="${VDATE}${VHOUR}"
  echo ""
  echo "==> OBS sweep: valid=${STAMP}Z (offset -${offset}h)"

  for product in $PRODUCTS; do
    png_key="${product}/${STAMP}/F000.png"
    sentinel_key="${product}/${STAMP}/F000.pass1"
    mrms_prefix=$(yq -r ".products.${product}.mrms_product" "$CONFIG")
    # Catalog products (mrms_dir, no Pass cycle) belong to render_mrms_obs.sh.
    [ "${mrms_prefix}" = "null" ] && continue

    have_png=false;  has_key "${png_key}" && have_png=true
    is_pass1=false;  has_key "${sentinel_key}" && is_pass1=true

    # Settled frame (Pass2 already rendered): nothing to do.
    if [ "${have_png}" = true ] && [ "${is_pass1}" = false ] && [ -z "${FORCE_RERENDER:-}" ]; then
      continue
    fi

    pass2_url="https://noaa-mrms-pds.s3.amazonaws.com/CONUS/${mrms_prefix}_Pass2_00.00/${VDATE}/MRMS_${mrms_prefix}_Pass2_00.00_${VDATE}-${VHOUR}0000.grib2.gz"
    if curl -sfI "${pass2_url}" > /dev/null; then
      render_one "${product}" "${VDATE}" "${VHOUR}" 2 || echo "  ${product} ${STAMP} pass2 FAILED (continuing)"
      continue
    fi

    # No Pass2 yet. If a Pass1 bridge is already up, keep it; otherwise try Pass1.
    if [ "${have_png}" = true ] && [ -z "${FORCE_RERENDER:-}" ]; then
      continue
    fi
    pass1_url="https://noaa-mrms-pds.s3.amazonaws.com/CONUS/${mrms_prefix}_Pass1_00.00/${VDATE}/MRMS_${mrms_prefix}_Pass1_00.00_${VDATE}-${VHOUR}0000.grib2.gz"
    if curl -sfI "${pass1_url}" > /dev/null; then
      render_one "${product}" "${VDATE}" "${VHOUR}" 1 || echo "  ${product} ${STAMP} pass1 FAILED (continuing)"
    else
      echo "[${product}] valid=${STAMP}Z not yet published (pass1/pass2); skip"
    fi
  done

  # Publish the manifest as soon as the newest hour is rendered (same
  # early-manifest trick as render_hrrr.sh).
  if [ "${offset}" -eq 0 ]; then
    echo "==> Publishing ${PREFIX} manifest early (newest hour rendered)"
    OBS_PREFIX="${PREFIX}" python3 "${SCRIPT_DIR}/build_manifest.py" "${MODEL}" \
      || echo "  (early manifest build failed; will retry at end of tick)"
  fi
done

# ── Prune + final manifest ──────────────────────────────────────────────
echo ""
echo "==> Pruning ${PREFIX} to last ${RETAIN} valid hours"
OBS_PREFIX="${PREFIX}" python3 "${SCRIPT_DIR}/prune_old_runs.py" "${MODEL}" "${RETAIN}"

echo "==> Rebuilding ${PREFIX} manifest"
OBS_PREFIX="${PREFIX}" python3 "${SCRIPT_DIR}/build_manifest.py" "${MODEL}"

echo ""
echo "==> OBS render complete"
