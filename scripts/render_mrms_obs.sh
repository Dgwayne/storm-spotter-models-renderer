#!/usr/bin/env bash
# MRMS observation catalog — latest-frame products (hail / echo tops /
# VIL / rotation tracks / FLASH flooding) for the app's Observations
# (MRMS) layer.
#
# These differ from the QPE products (render_mrms_qpe.sh) in every way
# that script hardcodes:
#   * sub-hourly publishes: 2-min radar mosaics, 10-min FLASH — filenames
#     carry arbitrary -HHMMSS stamps, never -HH0000;
#   * no Pass1/Pass2 gauge-correction cycle (that's MultiSensor QPE only);
#   * per-product unit scaling (mm→in, km→kft, or pass-through).
#
# Strategy: each tick, for every OBS product that declares `mrms_dir`
# (the full CONUS/ prefix on noaa-mrms-pds), list the S3 day directory,
# take the NEWEST object, and render it into the hourly "run" slot
# v1/OBS/<code>/<YYYYMMDDHH>/F000.png — overwriting within the hour as
# fresher files land. Hourly 10-digit stamps keep build_manifest.py,
# prune_old_runs.py, and the app's frame plumbing unchanged; the newest
# frame is never older than the tick cadence + product cadence.
#
# Idempotency: a zero-byte marker F000.src-<HHMMSS> records which source
# stamp the slot currently holds; if the newest S3 object matches the
# marker, the tick skips the product entirely.
set -euo pipefail

MODEL="OBS"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CONFIG="${REPO_ROOT}/config/products.yml"
COLOR_TABLES="${REPO_ROOT}/config/color_tables"

ALL_PRODUCTS=$(yq -r ".models.${MODEL}.products[]" "$CONFIG")
RETAIN=$(yq -r ".models.${MODEL}.retain_runs" "$CONFIG")
BBOX=$(yq -r ".models.${MODEL}.bbox_lonlat | join(\" \")" "$CONFIG")
IMG_W=$(yq -r ".models.${MODEL}.image_size[0]" "$CONFIG")
IMG_H=$(yq -r ".models.${MODEL}.image_size[1]" "$CONFIG")

# ── Pre-fetch R2 listing (same trick as render_mrms_qpe.sh) ────────────
EXISTING_KEYS_FILE=$(mktemp)
trap 'rm -f "$EXISTING_KEYS_FILE"' EXIT
echo "==> Pre-listing R2 contents under v1/${MODEL}/"
if rclone lsf --recursive "r2:${R2_BUCKET}/v1/${MODEL}/" \
    --files-only 2>/dev/null > "$EXISTING_KEYS_FILE"; then
  echo "  $(wc -l < "$EXISTING_KEYS_FILE") existing keys cached"
else
  echo "  (no existing keys, fresh bucket)"
  : > "$EXISTING_KEYS_FILE"
fi

has_key() { grep -qxF "$1" "$EXISTING_KEYS_FILE"; }

# Newest .grib2.gz key under CONUS/<dir>/<date>/ (S3 lists ascending; a
# 2-min product day is ~720 objects — one 1000-key page). Empty if none.
newest_key() {
  local dir="$1" date="$2"
  curl -sf "https://noaa-mrms-pds.s3.amazonaws.com/?list-type=2&prefix=CONUS/${dir}/${date}/&max-keys=1000" \
    | grep -oE "<Key>[^<]+</Key>" | sed -e 's|</\?Key>||g' \
    | grep '\.grib2\.gz$' | tail -1
}

RENDERED_ANY=false

for product in $ALL_PRODUCTS; do
  mrms_dir=$(yq -r ".products.${product}.mrms_dir // \"\"" "$CONFIG")
  # QPE products (mrms_product / Pass cycle) belong to render_mrms_qpe.sh.
  [ -z "${mrms_dir}" ] && continue

  scale=$(yq -r ".products.${product}.scale // 1" "$CONFIG")
  sentinel_lt=$(yq -r ".products.${product}.sentinel_lt // 0" "$CONFIG")
  units_out=$(yq -r ".products.${product}.units_out // \"\"" "$CONFIG")
  clr_file=$(yq -r ".products.${product}.clr" "$CONFIG")
  dmin=$(yq -r ".products.${product}.data_png.min // \"\"" "$CONFIG")
  dmax=$(yq -r ".products.${product}.data_png.max // \"\"" "$CONFIG")
  point_values=$(yq -r ".products.${product}.point_values // false" "$CONFIG")
  decimals=$(yq -r ".products.${product}.point_decimals // 2" "$CONFIG")
  grid_w=$(yq -r ".products.${product}.point_grid[0] // 512" "$CONFIG")
  grid_h=$(yq -r ".products.${product}.point_grid[1] // 512" "$CONFIG")

  # Newest published object — today first, yesterday around 00Z.
  TODAY=$(date -u +%Y%m%d)
  YESTERDAY=$(date -u -d "yesterday" +%Y%m%d)
  key=$(newest_key "${mrms_dir}" "${TODAY}")
  [ -z "${key}" ] && key=$(newest_key "${mrms_dir}" "${YESTERDAY}")
  if [ -z "${key}" ]; then
    echo "[${product}] no published files today/yesterday; skip"
    continue
  fi

  # MRMS_<dir>_<YYYYMMDD>-<HHMMSS>.grib2.gz → run stamp + source stamp.
  fname="${key##*/}"
  src_stamp=$(echo "${fname}" | grep -oE '[0-9]{8}-[0-9]{6}' | head -1)
  if [ -z "${src_stamp}" ]; then
    echo "[${product}] unparseable filename ${fname}; skip"
    continue
  fi
  vdate="${src_stamp%-*}"
  vtime="${src_stamp#*-}"
  vhour="${vtime:0:2}"
  stamp="${vdate}${vhour}"

  out_rel="v1/${MODEL}/${product}/${stamp}/F000.png"
  json_rel="${out_rel%.png}.json"
  marker_rel="${out_rel%.png}.src-${vtime}"

  if has_key "${marker_rel#v1/${MODEL}/}" && [ -z "${FORCE_RERENDER:-}" ]; then
    continue  # slot already holds exactly this source file
  fi

  echo "[${product}] newest=${src_stamp} → run ${stamp}Z"
  work="$(mktemp -d)"
  gz="${work}/in.grib2.gz"
  url="https://noaa-mrms-pds.s3.amazonaws.com/${key}"
  if ! curl -sf "${url}" -o "${gz}"; then
    echo "  download failed (${url}); skip"
    rm -rf "${work}"
    continue
  fi
  gunzip -f "${gz}"
  grib="${work}/in.grib2"

  # GRIB → Float32 GTiff (applies any packing scale/offset).
  gdal_translate -q -of GTiff -ot Float32 -b 1 "${grib}" "${work}/native.tif"

  # Longitude guard: 0-360 grids shift west by 360 (same as QPE script).
  west=$(python3 - "${work}/native.tif" <<'PY'
import json, subprocess, sys
info = json.loads(subprocess.check_output(["gdalinfo", "-json", sys.argv[1]]))
print(info["cornerCoordinates"]["upperLeft"][0])
PY
  )
  if python3 -c "import sys; sys.exit(0 if float('${west}') > 180.0 else 1)"; then
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

  # MRMS sentinel negatives (-1 missing / -3 no coverage) → NoData, then
  # per-product unit scale. Every catalog product is non-negative in
  # display units, so the A<0 mask is safe across the board.
  # sentinel_lt: products whose REAL values go negative (dBZ, raw
  # azimuthal shear) set a floor below their physical range so the
  # -99/-999 sentinels are still caught without eating valid data.
  gdal_calc.py --quiet -A "${work}/native.tif" --outfile="${work}/raw.tif" \
    --calc="where(A<(${sentinel_lt}),-9999,A*${scale})" --NoDataValue=-9999 --type=Float32 --overwrite

  # DATA products warp with NEAREST — the app's crisp renderer
  # interpolates in data space client-side, and cubic here would
  # pre-blur real values (and invent overshoot ones).
  resample="cubic"
  [ -n "${dmin}" ] && resample="near"
  # shellcheck disable=SC2086
  gdalwarp -q -overwrite -t_srs EPSG:3857 -te_srs EPSG:4326 -te ${BBOX} \
    -ts "${IMG_W}" "${IMG_H}" -r "${resample}" -dstnodata -9999 \
    "${work}/raw.tif" "${work}/merc.tif"

  if [ "${point_values}" = "true" ]; then
    if python3 "${REPO_ROOT}/scripts/sample_point_values.py" \
         "${work}/merc.tif" "${work}/F000.json" "${MODEL}" "${product}" \
         "${stamp}" "0" "${units_out}" "${decimals}" "${BBOX}" "${grid_w}" "${grid_h}"; then
      rclone copyto "${work}/F000.json" "r2:${R2_BUCKET}/${json_rel}" \
        --s3-no-check-bucket --no-traverse \
        --header-upload "Cache-Control: public, max-age=300"
    else
      echo "  point-value sampling FAILED (non-fatal, PNG continues)"
    fi
  fi

  if [ -n "${dmin}" ]; then
    # ── DATA PNG (gray + alpha) ────────────────────────────────────
    # gray 1..255 = dmin..dmax linear (0 reserved for nodata), alpha
    # 255 = valid. The app decodes values back and runs the same
    # crisp data-space renderer the live reflectivity uses —
    # colorized client-side with the product's legend bins.
    # ONE multi-band gdal_calc with --hideNoData: masked-array
    # handling silently zeroed a separate constant-valued alpha calc
    # (verified live 2026-07-12) — hideNoData feeds the raw -9999s to
    # the expressions so the where() guards do exactly what they say.
    gdal_calc.py --quiet -A "${work}/merc.tif" --outfile="${work}/ga.tif" \
      --calc="where(A==-9999,0,minimum(255,maximum(1,1+round((A-(${dmin}))*254.0/((${dmax})-(${dmin}))))))" \
      --calc="where(A==-9999,0,255)" \
      --type=Byte --hideNoData --overwrite
    gdal_translate -q -of PNG -co ZLEVEL=9 "${work}/ga.tif" "${work}/F000.png"
  else
    gdaldem color-relief -q -alpha -nearest_color_entry \
      "${work}/merc.tif" "${COLOR_TABLES}/${clr_file}" "${work}/rgba.tif"
    gdal_translate -q -of PNG -co ZLEVEL=9 "${work}/rgba.tif" "${work}/F000.png"
  fi

  rclone copyto "${work}/F000.png" "r2:${R2_BUCKET}/${out_rel}" \
    --s3-no-check-bucket --no-traverse \
    --header-upload "Cache-Control: public, max-age=300"
  echo "  uploaded ${out_rel} ($(stat -c%s "${work}/F000.png" 2>/dev/null || echo '?') bytes)"

  # Source marker: makes the next tick a no-op until a fresher file
  # publishes. Old markers for the same run are removed so the listing
  # stays one-marker-per-slot.
  while IFS= read -r old_marker; do
    rclone deletefile "r2:${R2_BUCKET}/v1/${MODEL}/${old_marker}" 2>/dev/null || true
  done < <(grep -E "^${product}/${stamp}/F000\.src-" "$EXISTING_KEYS_FILE" || true)
  : > "${work}/marker"
  rclone copyto "${work}/marker" "r2:${R2_BUCKET}/${marker_rel}" \
    --s3-no-check-bucket --no-traverse
  RENDERED_ANY=true
  rm -rf "${work}"
done

# ── Prune + manifest (only when something changed — the QPE script
# already rebuilds the manifest every tick regardless) ──────────────────
if [ "${RENDERED_ANY}" = true ] || [ -n "${FORCE_RERENDER:-}" ]; then
  echo ""
  echo "==> Pruning OBS to last ${RETAIN} valid hours"
  python3 "${SCRIPT_DIR}/prune_old_runs.py" "${MODEL}" "${RETAIN}"
  echo "==> Rebuilding OBS manifest"
  python3 "${SCRIPT_DIR}/build_manifest.py" "${MODEL}"
else
  echo "==> No new MRMS observation frames this tick"
fi

echo ""
echo "==> MRMS observation catalog render complete"
