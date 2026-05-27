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

# Skip if R2 already has it (idempotent re-runs).
if rclone lsf "r2:${R2_BUCKET}/${OUT_REL}" 2>/dev/null | grep -q .; then
  echo "  already on R2; skip"
  exit 0
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

# --- 4. Decode to NetCDF / GTiff ----------------------------------------------
NC="${WORK}/out.nc"
wgrib2 "${GRIB_LOCAL}" -netcdf "${NC}" >/dev/null

# --- 5. Build a single-band raster (handles composite UV magnitude) -----------
RAW_TIF="${WORK}/raw.tif"
if [ "${COMPOSITE_UV}" = "true" ]; then
  # Pull UGRD/VGRD bands, compute magnitude, then convert to kt.
  U_TIF="${WORK}/u.tif"
  V_TIF="${WORK}/v.tif"
  gdal_translate -q -of GTiff -b 1 NETCDF:"${NC}":UGRD_10maboveground "${U_TIF}" \
    2>/dev/null || gdal_translate -q -of GTiff NETCDF:"${NC}":UGRD_500mb "${U_TIF}"
  gdal_translate -q -of GTiff -b 1 NETCDF:"${NC}":VGRD_10maboveground "${V_TIF}" \
    2>/dev/null || gdal_translate -q -of GTiff NETCDF:"${NC}":VGRD_500mb "${V_TIF}"
  gdal_calc.py --quiet -A "${U_TIF}" -B "${V_TIF}" \
    --outfile="${WORK}/mag.tif" --calc="sqrt(A*A + B*B)" --NoDataValue=-9999 --type=Float32
  # Convert m/s -> kt
  gdal_calc.py --quiet -A "${WORK}/mag.tif" --outfile="${RAW_TIF}" \
    --calc="${CONVERT_EXPR//A/A}" --NoDataValue=-9999 --type=Float32
else
  # Single variable: list NetCDF subdatasets, take the first that isn't 'time_bnds'.
  SUBDS=$(gdalinfo "${NC}" | awk -F= '/SUBDATASET_[0-9]+_NAME/ && !/time_bnds/ {print $2; exit}')
  if [ -z "${SUBDS}" ]; then
    SUBDS="NETCDF:${NC}"
  fi
  gdal_translate -q -of GTiff -b 1 "${SUBDS}" "${WORK}/var.tif"
  if [ -n "${CONVERT_EXPR}" ]; then
    gdal_calc.py --quiet -A "${WORK}/var.tif" --outfile="${RAW_TIF}" \
      --calc="${CONVERT_EXPR}" --NoDataValue=-9999 --type=Float32
  else
    cp "${WORK}/var.tif" "${RAW_TIF}"
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
