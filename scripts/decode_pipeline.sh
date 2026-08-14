#!/usr/bin/env bash
# decode_pipeline.sh — render one (model, product, run, fh) tile to PNG and upload to R2.
#
# Usage:
#   decode_pipeline.sh <model> <product> <run_date> <run_hour> <fh>
#     model:      HRRR | GFS
#     product:    code from config/products.yml (refc, t2m, td2m, wind10m, gust10m,
#                 mslp, sfccape, precip1h, wind500, precipTotal, + the meso set:
#                 mlcape, mucape, mlcin, srh1, srh3, shear6, stp, scp)
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
WGRIB2_MATCH="${WGRIB2_MATCH//\{fh_minus_3\}/$((FH - 3))}"
WGRIB2_MATCH="${WGRIB2_MATCH//\{fh_minus_6\}/$((FH - 6))}"
COMPOSITE_UV=$(yq -r ".products.${PRODUCT}.composite_uv // false" "$CONFIG")
CONVERT_EXPR=$(yq -r ".products.${PRODUCT}.convert // \"\"" "$CONFIG")
# When true, gdaldem interpolates RGBA linearly between color stops
# (smooth gradient). When false/absent we pass -nearest_color_entry to
# snap each pixel to the nearest stop (hard binned colors).
INTERPOLATE=$(yq -r ".products.${PRODUCT}.interpolate_color // false" "$CONFIG")
CLR_FILE=$(yq -r ".products.${PRODUCT}.clr" "$CONFIG")
CLR_PATH="${COLOR_TABLES}/${CLR_FILE}"
# Derived products combine several GRIB fields via a gdal_calc formula
# (products.yml `inputs:` letter->match map + `calc:`). See step 4b.
DERIVED=$(yq -r ".products.${PRODUCT}.derived // false" "$CONFIG")

# Mesoanalysis products cap their forecast hours (fh_cap: the app's meso
# layer only consumes analysis frames). Skip anything beyond the cap.
# fh_min skips hours BELOW a floor — for fields HRRR leaves empty at f00
# (e.g. the ptype categorical flags).
FH_CAP=$(yq -r ".products.${PRODUCT}.fh_cap // \"\"" "$CONFIG")
if [ -n "${FH_CAP}" ] && [ "${FH}" -gt "${FH_CAP}" ]; then
  exit 0
fi
FH_MIN=$(yq -r ".products.${PRODUCT}.fh_min // \"\"" "$CONFIG")
if [ -n "${FH_MIN}" ] && [ "${FH}" -lt "${FH_MIN}" ]; then
  exit 0
fi
# fh_step renders only every Nth forecast hour — for products whose source
# field only completes on a coarser cadence than the model's output (e.g.
# NAM's 3-hour APCP buckets on an hourly-output model).
FH_STEP=$(yq -r ".products.${PRODUCT}.fh_step // \"\"" "$CONFIG")
if [ -n "${FH_STEP}" ] && [ $(( FH % FH_STEP )) -ne 0 ]; then
  exit 0
fi

S3_BUCKET=$(yq -r ".models.${MODEL}.s3_bucket // \"\"" "$CONFIG")
S3_KEY_TEMPLATE=$(yq -r ".models.${MODEL}.s3_key_template" "$CONFIG")
BBOX=$(yq -r ".models.${MODEL}.bbox_lonlat | join(\" \")" "$CONFIG")
IMG_W=$(yq -r ".models.${MODEL}.image_size[0]" "$CONFIG")
IMG_H=$(yq -r ".models.${MODEL}.image_size[1]" "$CONFIG")
# base_url: non-S3 sources (ECMWF open data) serve the same key template
# from a plain HTTPS root instead of an AWS bucket hostname.
BASE_URL=$(yq -r ".models.${MODEL}.base_url // \"\"" "$CONFIG")
# index_format: "wgrib2" (NOAA .idx text lines, default) or "ecmwf"
# (open-data JSON-lines .index with _offset/_length).
INDEX_FORMAT=$(yq -r ".models.${MODEL}.index_format // \"wgrib2\"" "$CONFIG")
# source_type: "grib" (default: idx byte-range subsetting) or
# "openmeteo_spatial" (whole .om files from the Open-Meteo AWS Open Data
# bucket's data_spatial layout — one file per valid time carrying every
# variable; extraction happens locally via scripts/om_extract.py).
SOURCE_TYPE=$(yq -r ".models.${MODEL}.source_type // \"grib\"" "$CONFIG")
OM_MODEL_PATH=$(yq -r ".models.${MODEL}.om_model_path // \"\"" "$CONFIG")
OM_BASE_URL=$(yq -r ".models.${MODEL}.om_base_url // \"https://openmeteo.s3.amazonaws.com/data_spatial\"" "$CONFIG")

# om-source products can gate hours independently of the GRIB models that
# share the product code (e.g. precip1h: Open-Meteo's `precipitation` is a
# preceding-hour sum that has no meaning at f00, while the GRIB models
# self-gate via the {fh_minus_1} idx match failing).
if [ "${SOURCE_TYPE}" = "openmeteo_spatial" ]; then
  OM_FH_MIN=$(yq -r ".products.${PRODUCT}.om_fh_min // \"\"" "$CONFIG")
  if [ -n "${OM_FH_MIN}" ] && [ "${FH}" -lt "${OM_FH_MIN}" ]; then
    exit 0
  fi
fi

# The match expression compute_ranges consumes depends on the index format.
if [ "${INDEX_FORMAT}" = "ecmwf" ]; then
  MATCH_EXPR=$(yq -r ".products.${PRODUCT}.ecmwf_match // \"\"" "$CONFIG")
  if [ -z "${MATCH_EXPR}" ]; then
    echo "[${MODEL}/${PRODUCT}] no ecmwf_match defined; skip"
    exit 0
  fi
else
  MATCH_EXPR="${WGRIB2_MATCH}"
fi

if [ "${SOURCE_TYPE}" = "openmeteo_spatial" ]; then
  # Valid-time keyed .om file: data_spatial/<model>/<Y>/<M>/<D>/<HH00>Z/<valid>.om
  RUN_EPOCH=$(date -u -d "${RUN_DATE:0:4}-${RUN_DATE:4:2}-${RUN_DATE:6:2} ${RUN_HOUR}:00:00" +%s)
  VALID_STAMP=$(date -u -d "@$(( RUN_EPOCH + FH * 3600 ))" +%Y-%m-%dT%H%M)
  OM_URL="${OM_BASE_URL}/${OM_MODEL_PATH}/${RUN_DATE:0:4}/${RUN_DATE:4:2}/${RUN_DATE:6:2}/${RUN_HOUR}00Z/${VALID_STAMP}.om"
  GRIB_URL="${OM_URL}"   # for the log line below
  IDX_URL=""
else
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
  if [ -n "${BASE_URL}" ]; then
    GRIB_URL="${BASE_URL}/${S3_KEY}"
  else
    GRIB_URL="https://${S3_BUCKET}.s3.amazonaws.com/${S3_KEY}"
  fi
  if [ "${INDEX_FORMAT}" = "ecmwf" ]; then
    # ECMWF: ...-fc.grib2 pairs with ...-fc.index (suffix swap, not append).
    IDX_URL="${GRIB_URL%.grib2}.index"
  else
    IDX_URL="${GRIB_URL}.idx"
  fi
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

OUT_REL="v1/${MODEL}/${PRODUCT}/${RUN_DATE}${RUN_HOUR}/F$(printf '%03d' "$FH").png"
echo "[${MODEL}/${PRODUCT}] run=${RUN_DATE}${RUN_HOUR} fh=${FH}"
echo "  source: ${GRIB_URL}"
echo "  target: ${OUT_REL}"

# --- 1. Confirm the source file is published -----------------------------------
if [ "${SOURCE_TYPE}" = "openmeteo_spatial" ]; then
  # Reuse a sweep-cached download when present (render_icond2.sh walks
  # fh-major so every product shares one om fetch per valid time) — a
  # cached file IS the publish check.
  OM_CACHED="${OM_CACHE_DIR:+${OM_CACHE_DIR}/${MODEL}_${RUN_DATE}${RUN_HOUR}_F$(printf '%03d' "$FH").om}"
  if [ -z "${OM_CACHED}" ] || [ ! -s "${OM_CACHED}" ]; then
    if ! curl -sfI "${OM_URL}" > /dev/null; then
      echo "  om file not yet published; exit 0"
      exit 0
    fi
  fi
elif ! curl -sfI "${IDX_URL}" > /dev/null; then
  echo "  idx not yet published; exit 0"
  exit 0
fi

# Skip if R2 already has it (idempotent re-runs). The FORCE_RERENDER env var
# bypasses this — set via the workflow_dispatch input when recovering from a
# bug that produced a corrupt batch of frames (e.g. all-transparent PNGs).
#
# Performance: render_hrrr.sh pre-fetches the entire bucket listing into
# $EXISTING_KEYS_FILE and we grep that in-memory set instead of calling
# `rclone lsf` per-key — one R2 listing per workflow tick instead of
# 152 × 7 listings.
if [ -z "${FORCE_RERENDER:-}" ]; then
  if [ -n "${EXISTING_KEYS_FILE:-}" ] && [ -f "${EXISTING_KEYS_FILE}" ]; then
    # The pre-list rclone listed files relative to `v1/<MODEL>/`, so
    # strip that prefix from OUT_REL before grepping the cache.
    relative_key="${OUT_REL#v1/${MODEL}/}"
    if grep -qxF "${relative_key}" "${EXISTING_KEYS_FILE}"; then
      # Silent skip — too noisy to log 1000+ existing-key skips per tick.
      exit 0
    fi
  else
    # Fallback for single-frame manual runs that don't pre-list.
    if rclone lsf "r2:${R2_BUCKET}/${OUT_REL}" 2>/dev/null | grep -q .; then
      echo "  already on R2; skip"
      exit 0
    fi
  fi
fi

# --- 2. Fetch idx once; helper computes byte ranges for any match regex --------
# (om sources have no idx — IDX_URL is empty and the whole-file fetch
# happens in step 4om instead.)
if [ "${SOURCE_TYPE}" != "openmeteo_spatial" ]; then
  curl -sf "${IDX_URL}" -o "${WORK}/file.idx"
fi

# Echo "start-end" byte ranges (one per line) for idx lines matching $1.
# .idx lines: msgnum:offset:d=yyyymmddhh:VAR:level:fcsthour:
# The regex travels via the environment (not string interpolation) so
# characters like ( | ) in product matches never hit the Python parser.
compute_ranges() {
  if [ "${INDEX_FORMAT}" = "ecmwf" ]; then
    # ECMWF open-data index: one JSON object per line with _offset/_length.
    # Match against a canonical "param:levtype:levelist" key (levelist
    # empty for sfc). Sorting by param keeps U before V (10u < 10v,
    # u < v) so composite_uv band order matches the wgrib2 path.
    MATCH_RE="$1" IDX_FILE="${WORK}/file.idx" python3 - <<'PY'
import json, os, re
match_re = re.compile(os.environ["MATCH_RE"])
entries = []
for ln in open(os.environ["IDX_FILE"]):
    ln = ln.strip()
    if not ln:
        continue
    try:
        e = json.loads(ln)
    except ValueError:
        continue
    key = f"{e.get('param', '')}:{e.get('levtype', '')}:{e.get('levelist', '')}"
    if match_re.search(key) and "_offset" in e and "_length" in e:
        entries.append((str(e.get("param", "")), int(e["_offset"]), int(e["_length"])))
entries.sort()
for _, off, length in entries:
    print(f"{off}-{off + length - 1}")
PY
    return
  fi
  MATCH_RE="$1" IDX_FILE="${WORK}/file.idx" python3 - <<'PY'
import os, re
match_re = re.compile(os.environ["MATCH_RE"])
lines = open(os.environ["IDX_FILE"]).read().splitlines()
parsed = []
for ln in lines:
    parts = ln.split(":")
    if len(parts) >= 3:
        try:
            # float(): NAM packs U/V wind pairs as SUBFIELD idx lines
            # ("624.1" / "624.2") sharing one byte offset — int() dropped
            # those lines entirely, so composite_uv products silently
            # skipped every NAM frame ("no matching messages in idx").
            parsed.append((float(parts[0]), int(parts[1]), ln))
        except ValueError:
            pass
parsed.sort()
# A message's byte range ends at the next DISTINCT offset (subfield lines
# share their message's offset, so "next parsed line" is not enough), and
# a subfield pair that matches twice must be fetched once.
offsets = sorted({off for _, off, _ in parsed})
range_end = {
    off: (offsets[i + 1] - 1 if i + 1 < len(offsets) else "")
    for i, off in enumerate(offsets)
}
emitted = set()
for msgnum, offset, ln in parsed:
    if match_re.search(ln) and offset not in emitted:
        emitted.add(offset)
        print(f"{offset}-{range_end[offset]}")
PY
}

if [ "${SOURCE_TYPE}" != "openmeteo_spatial" ] && [ "${DERIVED}" != "true" ]; then
  mapfile -t MATCH_RANGES < <(compute_ranges "${MATCH_EXPR}")

  if [ "${#MATCH_RANGES[@]}" -eq 0 ]; then
    echo "  no matching messages in idx; skip"
    exit 0
  fi

  # --- 3. Range-GET each matching message --------------------------------------
  GRIB_LOCAL="${WORK}/in.grib2"
  : > "${GRIB_LOCAL}"
  for r in "${MATCH_RANGES[@]}"; do
    curl -sf -r "${r}" "${GRIB_URL}" >> "${GRIB_LOCAL}"
  done

  GRIB_SIZE=$(stat -c%s "${GRIB_LOCAL}" 2>/dev/null || stat -f%z "${GRIB_LOCAL}")
  echo "  fetched ${GRIB_SIZE} bytes"
fi

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
if [ "${SOURCE_TYPE}" = "openmeteo_spatial" ]; then
  # --- 4om. Open-Meteo spatial source: whole-file fetch + local extraction ----
  # One .om file carries EVERY variable for a valid time, so the sweep
  # (render_icond2.sh) walks fh-major with OM_CACHE_DIR set — the first
  # product downloads the file (~25 MB) and the other 20+ reuse it.
  BBOX_ARGS=(${BBOX})   # west south east north (bbox_lonlat order)
  if [ -n "${OM_CACHE_DIR:-}" ]; then
    mkdir -p "${OM_CACHE_DIR}"
    OM_LOCAL="${OM_CACHE_DIR}/${MODEL}_${RUN_DATE}${RUN_HOUR}_F$(printf '%03d' "$FH").om"
  else
    OM_LOCAL="${WORK}/src.om"
  fi
  if [ ! -s "${OM_LOCAL}" ]; then
    curl -sf "${OM_URL}" -o "${OM_LOCAL}.part.$$" && mv "${OM_LOCAL}.part.$$" "${OM_LOCAL}" || {
      echo "  om download failed; exit 0"
      exit 0
    }
    echo "  fetched $(stat -c%s "${OM_LOCAL}" 2>/dev/null || stat -f%z "${OM_LOCAL}") bytes (om)"
  fi

  OM_VAR=$(yq -r ".products.${PRODUCT}.om_var // \"\"" "$CONFIG")
  OM_UV=$(yq -r "(.products.${PRODUCT}.om_uv // []) | join(\" \")" "$CONFIG")
  OM_CALC=$(yq -r ".products.${PRODUCT}.om_calc // \"\"" "$CONFIG")
  # om_convert overrides convert for om sources (om values arrive in
  # Open-Meteo's units, which usually — but not always — match the GRIB
  # path's post-normalization unit space; e.g. pressure_msl is already
  # hPa where PRMSL is Pa). An explicit empty string means "no conversion".
  OM_CONVERT=$(yq -r "(.products.${PRODUCT}.om_convert // .products.${PRODUCT}.convert) // \"\"" "$CONFIG")

  if [ -n "${OM_CALC}" ]; then
    # Derived om product (products.yml om_inputs letter->variable map +
    # om_calc formula in FINAL display units — convert is not applied).
    GDAL_INPUT_ARGS=()
    while IFS=$'\t' read -r letter om_name; do
      python3 "${REPO_ROOT}/scripts/om_extract.py" "${OM_LOCAL}" \
        "${WORK}/om_${letter}.tif" "${BBOX_ARGS[@]}" "${om_name}"
      GDAL_INPUT_ARGS+=("-${letter}" "${WORK}/om_${letter}.tif")
    done < <(yq -r ".products.${PRODUCT}.om_inputs | to_entries[] | [.key, .value] | @tsv" "$CONFIG")
    gdal_calc.py --quiet "${GDAL_INPUT_ARGS[@]}" --outfile="${RAW_TIF}" \
      --calc="${OM_CALC}" --NoDataValue=-9999 --type=Float32 --overwrite
  elif [ "${COMPOSITE_UV}" = "true" ]; then
    # om_uv: [u_variable, v_variable] -> band 1 = U, band 2 = V.
    python3 "${REPO_ROOT}/scripts/om_extract.py" "${OM_LOCAL}" \
      "${WORK}/om_uv.tif" "${BBOX_ARGS[@]}" ${OM_UV}
    gdal_calc.py --quiet -A "${WORK}/om_uv.tif" --A_band=1 -B "${WORK}/om_uv.tif" --B_band=2 \
      --outfile="${WORK}/mag.tif" --calc="sqrt(A*A + B*B)" \
      --NoDataValue=-9999 --type=Float32 --overwrite
    if [ -n "${OM_CONVERT}" ]; then
      gdal_calc.py --quiet -A "${WORK}/mag.tif" --outfile="${RAW_TIF}" \
        --calc="${OM_CONVERT}" --NoDataValue=-9999 --type=Float32 --overwrite
    else
      cp "${WORK}/mag.tif" "${RAW_TIF}"
    fi
  else
    if [ -z "${OM_VAR}" ]; then
      echo "  no om_var defined for ${PRODUCT}; skip"
      exit 0
    fi
    python3 "${REPO_ROOT}/scripts/om_extract.py" "${OM_LOCAL}" \
      "${WORK}/native.tif" "${BBOX_ARGS[@]}" "${OM_VAR}"
    if [ -n "${OM_CONVERT}" ]; then
      gdal_calc.py --quiet -A "${WORK}/native.tif" --outfile="${RAW_TIF}" \
        --calc="${OM_CONVERT}" --NoDataValue=-9999 --type=Float32 --overwrite
    else
      cp "${WORK}/native.tif" "${RAW_TIF}"
    fi
  fi
  echo "  raw.tif stats:"
  gdalinfo -stats "${RAW_TIF}" 2>/dev/null | grep -E "Min|Max|Mean|StdDev" | head -4 | sed 's/^/    /'
elif [ "${DERIVED}" = "true" ]; then
  # --- 4b. Derived product: fetch each named input, evaluate calc formula ------
  # Each input letter maps to one GRIB message (products.yml `inputs:`).
  # Every field lives on the same native LCC grid, so after gdal_translate
  # the rasters are pixel-aligned and one gdal_calc evaluates the formula.
  CALC_EXPR=$(yq -r ".products.${PRODUCT}.calc" "$CONFIG")
  GDAL_INPUT_ARGS=()
  while IFS=$'\t' read -r letter match; do
    mapfile -t IN_RANGES < <(compute_ranges "${match}")
    if [ "${#IN_RANGES[@]}" -eq 0 ]; then
      echo "  derived input ${letter} (${match}) missing from idx; skip"
      exit 0
    fi
    IN_GRIB="${WORK}/in_${letter}.grib2"
    : > "${IN_GRIB}"
    for r in "${IN_RANGES[@]}"; do
      curl -sf -r "${r}" "${GRIB_URL}" >> "${IN_GRIB}"
    done
    gdal_translate -q -of GTiff -ot Float32 -b 1 "${IN_GRIB}" "${WORK}/in_${letter}.tif"
    GDAL_INPUT_ARGS+=("-${letter}" "${WORK}/in_${letter}.tif")
  done < <(yq -r ".products.${PRODUCT}.inputs | to_entries[] | [.key, .value] | @tsv" "$CONFIG")

  gdal_calc.py --quiet "${GDAL_INPUT_ARGS[@]}" --outfile="${RAW_TIF}" \
    --calc="${CALC_EXPR}" --NoDataValue=-9999 --type=Float32 --overwrite
  echo "  raw.tif stats:"
  gdalinfo -stats "${RAW_TIF}" 2>/dev/null | grep -E "Min|Max|Mean|StdDev" | head -4 | sed 's/^/    /'
elif [ "${COMPOSITE_UV}" = "true" ]; then
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
  #   1) GRIB → GTiff with band 1 selected, forced to Float32 so any
  #      GRIB scale_factor/add_offset is applied (raw scaled integers
  #      would land outside any sane ramp range).
  #   2) Unit-conversion the GTiff with gdal_calc (when configured).
  gdal_translate -q -of GTiff -ot Float32 -b 1 "${GRIB_LOCAL}" "${WORK}/native.tif"
  if [ -n "${CONVERT_EXPR}" ]; then
    gdal_calc.py --quiet -A "${WORK}/native.tif" --outfile="${RAW_TIF}" \
      --calc="${CONVERT_EXPR}" --NoDataValue=-9999 --type=Float32 --overwrite
  else
    cp "${WORK}/native.tif" "${RAW_TIF}"
  fi
  # Diagnostic: print the value range of the raster that gdaldem will
  # see. Lets us tell if the GRIB was decoded into the expected unit
  # space (e.g. Kelvin 230-320 for temperature) or something else.
  echo "  raw.tif stats:"
  gdalinfo -stats "${RAW_TIF}" 2>/dev/null | grep -E "Min|Max|Mean|StdDev" | head -4 | sed 's/^/    /'
fi

# --- 6. Reproject to Web Mercator with fixed CONUS bbox -----------------------
# Cubic convolution (vs bilinear) gives noticeably crisper edges when the
# coarse model grid is upsampled to the output raster — reflectivity cores
# and fronts keep their gradient instead of smearing. Slight overshoot near
# sharp boundaries is harmless here (it stays within the color ramp; nodata
# is masked via -dstnodata so edges don't bleed into the -9999 fill).
MERC_TIF="${WORK}/merc.tif"
# shellcheck disable=SC2086
gdalwarp -q -overwrite \
  -t_srs EPSG:3857 \
  -te_srs EPSG:4326 \
  -te ${BBOX} \
  -ts "${IMG_W}" "${IMG_H}" \
  -r cubic \
  -dstnodata -9999 \
  "${RAW_TIF}" "${MERC_TIF}"

# --- 6b. Point-value grid JSON (Pivotal-style on-map numbers) ------------------
# Sampled from the same Float32 mercator raster the PNG is color-mapped from,
# so the numbers always agree with the fill. Uploaded BEFORE the PNG: the app
# treats PNG availability (via the manifest) as implying the JSON exists, so
# the JSON must never lag the frame it describes. Failure here is non-fatal —
# a frame without numbers beats no frame.
#
# point_value_fh_cap caps the JSON (not the PNG) at fh <= N — for a product
# whose grid is only ever read at one hour. Currently unused (srh/ehi briefly
# shipped capped before the crosshair inspector needed every scrubbed hour);
# build_manifest.py mirrors any future cap by reporting pointValues false in
# productCatalog, so the models tab never asks for a grid that isn't there.
POINT_VALUES=$(yq -r ".products.${PRODUCT}.point_values // false" "$CONFIG")
POINT_VALUE_FH_CAP=$(yq -r ".products.${PRODUCT}.point_value_fh_cap // \"\"" "$CONFIG")
if [ -n "${POINT_VALUE_FH_CAP}" ] && [ "${FH}" -gt "${POINT_VALUE_FH_CAP}" ]; then
  POINT_VALUES="false"
fi
if [ "${POINT_VALUES}" = "true" ]; then
  POINT_DECIMALS=$(yq -r ".products.${PRODUCT}.point_decimals // 0" "$CONFIG")
  UNITS_OUT=$(yq -r ".products.${PRODUCT}.units_out // \"\"" "$CONFIG")
  # Per-product point_grid override (meso products publish a denser
  # grid for the cell-picker readout), falling back to the model's.
  GRID_W=$(yq -r ".products.${PRODUCT}.point_grid[0] // .models.${MODEL}.point_grid[0] // 128" "$CONFIG")
  GRID_H=$(yq -r ".products.${PRODUCT}.point_grid[1] // .models.${MODEL}.point_grid[1] // 128" "$CONFIG")
  JSON_LOCAL="${WORK}/F$(printf '%03d' "$FH").json"
  JSON_REL="${OUT_REL%.png}.json"
  if python3 "${REPO_ROOT}/scripts/sample_point_values.py" \
       "${MERC_TIF}" "${JSON_LOCAL}" "${MODEL}" "${PRODUCT}" \
       "${RUN_DATE}${RUN_HOUR}" "${FH}" "${UNITS_OUT}" "${POINT_DECIMALS}" \
       "${BBOX}" "${GRID_W}" "${GRID_H}"; then
    rclone copyto "${JSON_LOCAL}" "r2:${R2_BUCKET}/${JSON_REL}" \
      --s3-no-check-bucket --no-traverse \
      --header-upload "Cache-Control: public, max-age=300"
    echo "  uploaded ${JSON_REL}"
  else
    echo "  point-value sampling FAILED (non-fatal, PNG continues)"
  fi
fi

# --- 7. Color-relief to RGBA + PNG translate ----------------------------------
# Products with interpolate_color: true (e.g. refc) get smooth gradients;
# all others snap to the nearest color stop for crisp binned output.
RGBA_TIF="${WORK}/rgba.tif"
NEAREST_FLAG="-nearest_color_entry"
[ "${INTERPOLATE}" = "true" ] && NEAREST_FLAG=""
# shellcheck disable=SC2086
gdaldem color-relief -q -alpha ${NEAREST_FLAG} \
  "${MERC_TIF}" "${CLR_PATH}" "${RGBA_TIF}"

PNG_OUT="${WORK}/F$(printf '%03d' "$FH").png"
gdal_translate -q -of PNG -co ZLEVEL=9 "${RGBA_TIF}" "${PNG_OUT}"

PNG_SIZE=$(stat -c%s "${PNG_OUT}" 2>/dev/null || stat -f%z "${PNG_OUT}")
echo "  png ${PNG_SIZE} bytes"

# --- 8. Upload to R2 ----------------------------------------------------------
# Cache-Control header travels with the object — Cloudflare's CDN
# respects it and refreshes more aggressively after a force_rerender,
# instead of serving up-to-4-hour-old cached frames per the default
# Cloudflare browser cache TTL. 5-min cache strikes the balance:
# users still see the previous frame instantly on a repeated GET,
# but any pipeline-bug rerender lands on every edge node within
# minutes rather than hours.
rclone copyto "${PNG_OUT}" "r2:${R2_BUCKET}/${OUT_REL}" \
  --s3-no-check-bucket --no-traverse \
  --header-upload "Cache-Control: public, max-age=300"

echo "  uploaded ${OUT_REL}"

# --- 9. Optional value-encoded data PNG for the GPU model layer ---------------
# Products with gpu_data:{min,max} ALSO publish a gray+alpha PNG alongside the
# colored one: gray 1..255 = min..max linear in DISPLAY units, 0/alpha-0 =
# nodata. The app's GPU model layer samples this on the GPU and colorizes in a
# fragment shader (crisp at any zoom). Baked from the SAME merc.tif the colored
# PNG uses, so map and legend can't disagree. Non-fatal: the colored frame is
# already uploaded, so a data-PNG failure just means that frame falls back to
# the raster layer on device.
#
# ⚠ Backfill: already-rendered colored frames are skipped wholesale at the top
# of this script, so they never gain a data PNG — coverage arrives as runs
# cycle through retain_runs (~hours), or immediately via FORCE_RERENDER.
GPU_MIN=$(yq -r ".products.${PRODUCT}.gpu_data.min // \"\"" "$CONFIG")
GPU_MAX=$(yq -r ".products.${PRODUCT}.gpu_data.max // \"\"" "$CONFIG")
if [ -n "${GPU_MIN}" ] && [ -n "${GPU_MAX}" ]; then
  DATA_PNG="${WORK}/F$(printf '%03d' "$FH").data.png"
  DATA_REL="${OUT_REL%.png}.data.png"
  # ONE multi-band gdal_calc with --hideNoData (the render_mrms_obs.sh lesson:
  # masked-array handling silently zeroed a separate constant alpha calc).
  if gdal_calc.py --quiet -A "${MERC_TIF}" --outfile="${WORK}/gpu_ga.tif" \
       --calc="where(A==-9999,0,minimum(255,maximum(1,1+round((A-(${GPU_MIN}))*254.0/((${GPU_MAX})-(${GPU_MIN}))))))" \
       --calc="where(A==-9999,0,255)" \
       --type=Byte --hideNoData --overwrite \
     && gdal_translate -q -of PNG -co ZLEVEL=9 "${WORK}/gpu_ga.tif" "${DATA_PNG}"; then
    rclone copyto "${DATA_PNG}" "r2:${R2_BUCKET}/${DATA_REL}" \
      --s3-no-check-bucket --no-traverse \
      --header-upload "Cache-Control: public, max-age=300"
    echo "  uploaded ${DATA_REL}"
  else
    echo "  data-PNG bake FAILED (non-fatal, colored PNG already uploaded)"
  fi
fi
