#!/usr/bin/env bash
# decode_pipeline.sh — render one (model, product, run, fh) tile to PNG and upload to R2.
#
# Usage:
#   decode_pipeline.sh <model> <product> <run_date> <run_hour> <fh>
#     model:      HRRR | GFS
#     product:    code from config/products.yml (refc, t2m, td2m, wind10m, gust10m,
#                 mslp, sfccape, precip1h, wind500, precipTotal)
#     run_date:   YYYYMMDD
#     run_hour:   00..23
#     fh:         forecast hour, integer
#
# Required env: R2_BUCKET, R2_ENDPOINT, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY
# Required tools: curl, wgrib2, gdal_translate, gdaldem, gdalwarp, gdal_calc.py, yq, rclone

set -euo pipefail

MODEL="$1"
PRODUCT="$2"
RUN_DATE="$3"
RUN_HOUR="$4"
FH="$5"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CONFIG="${REPO_ROOT}/config/products.yml"
COLOR_TABLES="${REPO_ROOT}/config/color_tables"

# --- Resolve product + model config from YAML ----------------------------------
WGRIB2_MATCH=$(yq -r ".products.${PRODUCT}.wgrib2_match" "$CONFIG")
# Substitute fh placeholders so per-hour accumulation products work.
WGRIB2_MATCH="${WGRIB2_MATCH//\{fh\}/${FH}}"
WGRIB2_MATCH="${WGRIB2_MATCH//\{fh_minus_1\}/$((FH - 1))}"
COMPOSITE_UV=$(yq -r ".products.${PRODUCT}.composite_uv // false" "$CONFIG")
CONVERT_EXPR=$(yq -r ".products.${PRODUCT}.convert // \"\"" "$CONFIG")
CLR_FILE=$(yq -r ".products.${PRODUCT}.clr" "$CONFIG")
CLR_PATH="${COLOR_TABLES}/${CLR_FILE}"

S3_BUCKET=$(yq -r ".models.${MODEL}.s3_bucket" "$CONFIG")
S3_KEY_TEMPLATE=$(yq -r ".models.${MODEL}.s3_key_template" "$CONFIG")
BBOX=$(yq -r ".models.${MODEL}.bbox_lonlat | join(\" \")" "$CONFIG")
IMG_W=$(yq -r ".models.${MODEL}.image_size[0]" "$CONFIG")
IMG_H=$(yq -r ".models.${MODEL}.image_size[1]" "$CONFIG")

# Expand the key template (Python so we get printf-style {fh:02d}/{fh:03d}).
# Pass run/fh as strings then int() to avoid Python rejecting "02" as a literal.
S3_KEY=$(python3 - <<PY
date = "${RUN_DATE}"
run = int("${RUN_HOUR}")
fh = int("${FH}")
tmpl = r"""${S3_KEY_TEMPLATE}"""
print(tmpl.format(date=date, run=run, fh=fh))
PY
)
GRIB_URL="https://${S3_BUCKET}.s3.amazonaws.com/${S3_KEY}"
IDX_URL="${GRIB_URL}.idx"

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

OUT_REL="v1/${MODEL}/${PRODUCT}/${RUN_DATE}${RUN_HOUR}/F$(printf '%03d' "$FH").png"
echo "[${MODEL}/${PRODUCT}] run=${RUN_DATE}${RUN_HOUR} fh=${FH}"
echo "  source: ${GRIB_URL}"
echo "  target: ${OUT_REL}"

# --- 1. Confirm the GRIB file + idx are published ------------------------------
if ! curl -sfI "${IDX_URL}" > /dev/null; then
  echo "  idx not yet published; exit 0"
  exit 0
fi

# Skip if R2 already has it (idempotent re-runs). The FORCE_RERENDER env var
# bypasses this — set via the workflow_dispatch input when recovering from a
# bug that produced a corrupt batch of frames (e.g. all-transparent PNGs).
if [ -z "${FORCE_RERENDER:-}" ]; then
  if rclone lsf "r2:${R2_BUCKET}/${OUT_REL}" 2>/dev/null | grep -q .; then
    echo "  already on R2; skip"
    exit 0
  fi
fi

# --- 2. Fetch idx and compute byte range for matching message(s) ---------------
curl -sf "${IDX_URL}" -o "${WORK}/file.idx"

# Build a list of (start, end) byte ranges that match the regex.
# .idx lines: msgnum:offset:d=yyyymmddhh:VAR:level:fcsthour:
mapfile -t MATCH_RANGES < <(python3 - <<PY
import re, sys
match_re = re.compile(r"""${WGRIB2_MATCH}""")
lines = open("${WORK}/file.idx").read().splitlines()
parsed = []
for ln in lines:
    parts = ln.split(":")
    if len(parts) >= 3:
        try:
            parsed.append((int(parts[0]), int(parts[1]), ln))
        except ValueError:
            pass
parsed.sort()
ranges = []
for i, (msgnum, offset, ln) in enumerate(parsed):
    if match_re.search(ln):
        if i + 1 < len(parsed):
            end = parsed[i + 1][1] - 1
        else:
            end = ""  # to EOF
        ranges.append(f"{offset}-{end}")
for r in ranges:
    print(r)
PY
)

if [ "${#MATCH_RANGES[@]}" -eq 0 ]; then
  echo "  no matching messages in idx; skip"
  exit 0
fi

# --- 3. Range-GET each matching message ----------------------------------------
GRIB_LOCAL="${WORK}/in.grib2"
: > "${GRIB_LOCAL}"
for r in "${MATCH_RANGES[@]}"; do
  curl -sf -r "${r}" "${GRIB_URL}" >> "${GRIB_LOCAL}"
done

GRIB_SIZE=$(stat -c%s "${GRIB_LOCAL}" 2>/dev/null || stat -f%z "${GRIB_LOCAL}")
echo "  fetched ${GRIB_SIZE} bytes"

# --- 4. Read GRIB directly with gdal (preserves Lambert Conformal SRS) -------
# Skipping wgrib2 -netcdf because that pipeline silently strips the LCC
# geolocation metadata on conda-forge wgrib2 builds, leaving gdalwarp
# unable to reproject — every output frame ends up fully transparent
# inside the CONUS bbox. gdal's native GRIB driver reads the LCC grid
# directly and gdalwarp picks up the correct transformation.
#
# Pipeline order (single-variable case):
#   1. gdal_translate GRIB → native GTiff (preserves LCC SRS)
#   2. gdal_calc on the GTiff to convert units (Kelvin → °F, Pa → hPa, etc.)
#   3. gdalwarp the converted GTiff to EPSG:3857 (downstream)
#   4. gdaldem color-relief (downstream)
#
# Doing the convert on a GTiff (rather than directly on the GRIB)
# guarantees gdal_calc sees a uniformly-typed Float32 raster with
# clean band 1 selection. gdal_calc on GRIB sometimes mis-reads
# scaled GRIB packing and drops the unit conversion entirely, which
# was the cause of the flat magenta/yellow bands on the temperature
# and dewpoint products.

RAW_TIF="${WORK}/raw.tif"
if [ "${COMPOSITE_UV}" = "true" ]; then
  # Composite UV: two GRIB messages (UGRD + VGRD), each as a band.
  # The byte-range GET preserves idx ordering (UGRD first, then VGRD)
  # so band 1 = UGRD, band 2 = VGRD.
  U_TIF="${WORK}/u.tif"
  V_TIF="${WORK}/v.tif"
  gdal_translate -q -of GTiff -b 1 "${GRIB_LOCAL}" "${U_TIF}"
  gdal_translate -q -of GTiff -b 2 "${GRIB_LOCAL}" "${V_TIF}"
  gdal_calc.py --quiet -A "${U_TIF}" -B "${V_TIF}" \
    --outfile="${WORK}/mag.tif" --calc="sqrt(A*A + B*B)" \
    --NoDataValue=-9999 --type=Float32 --overwrite
  if [ -n "${CONVERT_EXPR}" ]; then
    gdal_calc.py --quiet -A "${WORK}/mag.tif" --outfile="${RAW_TIF}" \
      --calc="${CONVERT_EXPR}" --NoDataValue=-9999 --type=Float32 --overwrite
  else
    cp "${WORK}/mag.tif" "${RAW_TIF}"
  fi
else
  # Single-variable product:
  #   1) GRIB → GTiff with band 1 selected (preserves LCC SRS)
  #   2) Unit-conversion the GTiff with gdal_calc
  gdal_translate -q -of GTiff -b 1 "${GRIB_LOCAL}" "${WORK}/native.tif"
  if [ -n "${CONVERT_EXPR}" ]; then
    gdal_calc.py --quiet -A "${WORK}/native.tif" --outfile="${RAW_TIF}" \
      --calc="${CONVERT_EXPR}" --NoDataValue=-9999 --type=Float32 --overwrite
  else
    cp "${WORK}/native.tif" "${RAW_TIF}"
  fi
fi

# --- 6. Reproject to Web Mercator with fixed CONUS bbox -----------------------
MERC_TIF="${WORK}/merc.tif"
# shellcheck disable=SC2086
gdalwarp -q -overwrite \
  -t_srs EPSG:3857 \
  -te_srs EPSG:4326 \
  -te ${BBOX} \
  -ts "${IMG_W}" "${IMG_H}" \
  -r bilinear \
  -dstnodata -9999 \
  "${RAW_TIF}" "${MERC_TIF}"

# --- 7. Color-relief to RGBA + PNG translate ----------------------------------
RGBA_TIF="${WORK}/rgba.tif"
gdaldem color-relief -q -alpha -nearest_color_entry \
  "${MERC_TIF}" "${CLR_PATH}" "${RGBA_TIF}"

PNG_OUT="${WORK}/F$(printf '%03d' "$FH").png"
gdal_translate -q -of PNG -co ZLEVEL=9 "${RGBA_TIF}" "${PNG_OUT}"

PNG_SIZE=$(stat -c%s "${PNG_OUT}" 2>/dev/null || stat -f%z "${PNG_OUT}")
echo "  png ${PNG_SIZE} bytes"

# --- 8. Upload to R2 ----------------------------------------------------------
rclone copyto "${PNG_OUT}" "r2:${R2_BUCKET}/${OUT_REL}" \
  --s3-no-check-bucket --no-traverse

echo "  uploaded ${OUT_REL}"
