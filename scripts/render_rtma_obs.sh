#!/usr/bin/env bash
# render_rtma_obs.sh — RTMA surface observations (RTMA pseudo-model).
#
# RTMA (Real-Time Mesoscale Analysis) is NCEP's 2.5 km CONUS *analysis of
# observations* — METARs + mesonets + satellite assimilated into a gridded
# "what's happening right now" field. Same feed the wind-particle layer
# already ingests (wind_field.py), extended to shaded map products for the
# app's Observations layer "Surface" section:
#
#   v1/RTMA/<code>/<YYYYMMDDHHMM>/F000.png       colorized fill
#   v1/RTMA/<code>/<YYYYMMDDHHMM>/F000.data.png  value PNG (inspector)
#
# Why each analysis is its OWN immutable run (12-digit minute stamp)
# instead of the OBS-style hourly slot overwritten in place: the app's
# frame disk-cache is keyed model/product/runStamp/F000.png and never
# refetches an unchanged URL, so an overwritten slot would pin phones to
# the FIRST analysis of the hour (~55 min stale temps). A new run stamp
# per analysis makes edge cache, app disk cache, and the 60 s manifest
# poll all correct with zero cache busting. 12-digit stamps are handled
# by r2_listing.RUN_RE and build_manifest.py's length-aware parse.
#
# Discovery mirrors wind_field.py: prefer the Rapid-Update cycle
# (:00/:15/:30/:45, ~20-30 min behind real time), fall back to the hourly
# analysis. Idempotency needs no markers — the newest analysis either has
# its run dir on R2 already (tick is a no-op) or it doesn't.
#
# Wind gotcha (same note as wind_field.py): RTMA U/V are GRID-relative on
# a Lambert grid. Only SPEED is published here — magnitude is
# rotation-invariant, so no wgrib2 -new_grid_winds pass is needed.
# Direction products would need the wind_field.py treatment.
#
# Theta-e is derived from 2m T / 2m Td / surface pressure with Bolton
# (1980) — the standard equivalent-potential-temperature approximation
# (accurate to <0.4 K), computed in numpy (see python block below).
#
# Required env: R2_BUCKET, R2_ENDPOINT, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY
# Required tools: curl, gdal (translate/calc/warp/dem), yq, rclone, python3+numpy

set -euo pipefail

MODEL="RTMA"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CONFIG="${REPO_ROOT}/config/products.yml"
COLOR_TABLES="${REPO_ROOT}/config/color_tables"

S3_BASE="https://noaa-rtma-pds.s3.amazonaws.com"

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
BBOX=$(yq -r ".models.${MODEL}.bbox_lonlat | join(\" \")" "$CONFIG")
IMG_W=$(yq -r ".models.${MODEL}.image_size[0]" "$CONFIG")
IMG_H=$(yq -r ".models.${MODEL}.image_size[1]" "$CONFIG")

# ── 1. Find the newest published analysis (RU first, hourly fallback) ──
# Candidates newest-first; the first whose .idx HEADs 200 wins. RU is
# normally ~20-30 min behind real time; hourly RTMA ~50 min.
NOW_EPOCH=$(date -u +%s)
FOUND_KEY=""
FOUND_STAMP=""
FOUND_SOURCE=""

Q_EPOCH=$(( NOW_EPOCH - NOW_EPOCH % 900 ))
for i in $(seq 0 10); do    # 150 min of quarter-hours
  T=$(( Q_EPOCH - i * 900 ))
  D=$(date -u -d "@${T}" +%Y%m%d)
  HM=$(date -u -d "@${T}" +%H%M)
  KEY="rtma2p5_ru.${D}/rtma2p5_ru.t${HM}z.2dvaranl_ndfd.grb2"
  if curl -sfI "${S3_BASE}/${KEY}.idx" > /dev/null; then
    FOUND_KEY="$KEY"; FOUND_STAMP="${D}${HM}"; FOUND_SOURCE="rtma2p5_ru"
    break
  fi
done

if [ -z "${FOUND_KEY}" ]; then
  H_EPOCH=$(( NOW_EPOCH - NOW_EPOCH % 3600 ))
  for i in $(seq 0 3); do
    T=$(( H_EPOCH - i * 3600 ))
    D=$(date -u -d "@${T}" +%Y%m%d)
    H=$(date -u -d "@${T}" +%H)
    KEY="rtma2p5.${D}/rtma2p5.t${H}z.2dvaranl_ndfd.grb2_wexp"
    if curl -sfI "${S3_BASE}/${KEY}.idx" > /dev/null; then
      FOUND_KEY="$KEY"; FOUND_STAMP="${D}${H}00"; FOUND_SOURCE="rtma2p5"
      break
    fi
  done
fi

if [ -z "${FOUND_KEY}" ]; then
  echo "==> no RTMA analysis available in lookback window (transient upstream gap)"
  exit 0
fi
echo "==> newest analysis: ${FOUND_SOURCE} ${FOUND_STAMP}Z (${FOUND_KEY})"

# ── 2. Idempotency: run dir already on R2 → nothing to do ──────────────
# One product's frame stands in for the whole run (they render together);
# checked against a listing of just that product's prefix, so an idle
# pass is one LIST call.
FIRST_PRODUCT=$(echo "$PRODUCTS" | head -1)
if [ -z "${FORCE_RERENDER:-}" ] && \
   rclone lsf "r2:${R2_BUCKET}/v1/${MODEL}/${FIRST_PRODUCT}/${FOUND_STAMP}/" \
     --files-only 2>/dev/null | grep -q '^F000\.png$'; then
  echo "==> ${FOUND_STAMP} already rendered; nothing to do"
  exit 0
fi

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# ── 3. Fetch idx once; byte-range fetch each needed message ────────────
GRIB_URL="${S3_BASE}/${FOUND_KEY}"
curl -sf "${GRIB_URL}.idx" -o "${WORK}/file.idx"

# Same range logic as decode_pipeline.sh (subfield-safe, dedup by offset).
compute_ranges() {
  MATCH_RE="$1" IDX_FILE="${WORK}/file.idx" python3 - <<'PY'
import os, re
match_re = re.compile(os.environ["MATCH_RE"])
lines = open(os.environ["IDX_FILE"]).read().splitlines()
parsed = []
for ln in lines:
    parts = ln.split(":")
    if len(parts) >= 3:
        try:
            parsed.append((float(parts[0]), int(parts[1]), ln))
        except ValueError:
            pass
parsed.sort()
offsets = sorted({off for _, off, _ in parsed})
range_end = {
    off: (offsets[i + 1] - 1 if i + 1 < len(offsets) else "")
    for i, off in enumerate(offsets)
}
emitted = set()
for _num, offset, ln in parsed:
    if match_re.search(ln) and offset not in emitted:
        emitted.add(offset)
        print(f"{offset}-{range_end[offset]}")
PY
}

# var → (idx regex). GUST sits at "10 m above ground" in RTMA (vs
# ":GUST:surface:" in HRRR) — match either so an upstream level rename
# degrades gracefully.
fetch_var() {
  local var="$1" regex="$2"
  local ranges
  mapfile -t ranges < <(compute_ranges "${regex}")
  if [ "${#ranges[@]}" -eq 0 ]; then
    echo "  ${var}: no matching idx message" >&2
    return 1
  fi
  : > "${WORK}/${var}.grib2"
  local r
  for r in "${ranges[@]}"; do
    curl -sf -r "${r}" "${GRIB_URL}" >> "${WORK}/${var}.grib2"
  done
  # Float32 GTiff preserves the Lambert SRS for gdalwarp (see
  # decode_pipeline.sh's "read GRIB directly with gdal" note).
  gdal_translate -q -of GTiff -ot Float32 -b 1 \
    "${WORK}/${var}.grib2" "${WORK}/${var}.tif"
  echo "  ${var}: $(stat -c%s "${WORK}/${var}.grib2") bytes"
}

echo "==> fetching fields"
fetch_var TMP  ':TMP:2 m above ground:'
fetch_var DPT  ':DPT:2 m above ground:'
fetch_var UGRD ':UGRD:10 m above ground:'
fetch_var VGRD ':VGRD:10 m above ground:'
fetch_var GUST ':GUST:(10 m above ground|surface):'
fetch_var PRES ':PRES:surface:'

# ── 4. Per-product display-unit fields ─────────────────────────────────
# GDAL's GRIB driver auto-normalizes temperature-typed fields (TMP, DPT)
# K → Celsius on read — same behavior the HRRR t2m/td2m products rely on
# (see their products.yml notes). PRES stays Pa.
echo "==> computing products"
gdal_calc.py --quiet -A "${WORK}/TMP.tif" --outfile="${WORK}/calc_t2m.tif" \
  --calc="A*1.8+32" --type=Float32 --overwrite
gdal_calc.py --quiet -A "${WORK}/DPT.tif" --outfile="${WORK}/calc_td2m.tif" \
  --calc="A*1.8+32" --type=Float32 --overwrite
gdal_calc.py --quiet -A "${WORK}/UGRD.tif" -B "${WORK}/VGRD.tif" \
  --outfile="${WORK}/calc_wind10m.tif" \
  --calc="sqrt(A*A+B*B)*1.94384" --type=Float32 --overwrite
gdal_calc.py --quiet -A "${WORK}/GUST.tif" --outfile="${WORK}/calc_gust10m.tif" \
  --calc="A*1.94384" --type=Float32 --overwrite

# Theta-e (K), Bolton 1980 eqn 39: too many intermediates for a readable
# gdal_calc one-liner, so numpy + a GTiff copy that keeps georeferencing.
python3 - "${WORK}" <<'PY'
import sys
import numpy as np
from osgeo import gdal

gdal.UseExceptions()
work = sys.argv[1]

def read(name):
    ds = gdal.Open(f"{work}/{name}.tif")
    return ds, ds.GetRasterBand(1).ReadAsArray().astype("float64")

ds_t, t_c = read("TMP")     # °C (GDAL-normalized)
_, td_c = read("DPT")       # °C
_, p_pa = read("PRES")      # Pa

tk = t_c + 273.15
tdk = np.minimum(td_c, t_c) + 273.15          # guard Td > T artifacts
e = 6.112 * np.exp(17.67 * (tdk - 273.15) / ((tdk - 273.15) + 243.5))  # hPa
p_hpa = p_pa / 100.0
r = 0.622 * e / np.maximum(p_hpa - e, 1.0)     # mixing ratio kg/kg
tl = 56.0 + 1.0 / (1.0 / (tk - 56.0) + np.log(tk / tdk) / 800.0)
theta_e = (
    tk
    * (1000.0 / p_hpa) ** (0.2854 * (1.0 - 0.28 * r))
    * np.exp((3376.0 / tl - 2.54) * r * (1.0 + 0.81 * r))
)

drv = gdal.GetDriverByName("GTiff")
out = drv.Create(f"{work}/calc_thetae.tif", ds_t.RasterXSize, ds_t.RasterYSize,
                 1, gdal.GDT_Float32)
out.SetGeoTransform(ds_t.GetGeoTransform())
out.SetProjection(ds_t.GetProjection())
out.GetRasterBand(1).WriteArray(theta_e.astype("float32"))
out.FlushCache()
print(f"  thetae: min={np.nanmin(theta_e):.1f} max={np.nanmax(theta_e):.1f} K")
PY

# ── 5. Warp + colorize + value-PNG + upload, per product ───────────────
RENDERED=0
for product in $PRODUCTS; do
  clr_file=$(yq -r ".products.${product}.clr" "$CONFIG")
  gd_min=$(yq -r ".products.${product}.gpu_data.min // \"\"" "$CONFIG")
  gd_max=$(yq -r ".products.${product}.gpu_data.max // \"\"" "$CONFIG")
  out_rel="v1/${MODEL}/${product}/${FOUND_STAMP}/F000.png"

  echo "[${product}]"
  # shellcheck disable=SC2086
  gdalwarp -q -overwrite -t_srs EPSG:3857 -te_srs EPSG:4326 -te ${BBOX} \
    -ts "${IMG_W}" "${IMG_H}" -r cubic -dstnodata -9999 \
    "${WORK}/calc_${product}.tif" "${WORK}/merc.tif"

  gdaldem color-relief -q -alpha -nearest_color_entry \
    "${WORK}/merc.tif" "${COLOR_TABLES}/${clr_file}" "${WORK}/rgba.tif"
  gdal_translate -q -of PNG -co ZLEVEL=9 "${WORK}/rgba.tif" "${WORK}/F000.png"

  # Frames are immutable (unique run stamp per analysis) → long edge
  # cache; the 60 s manifest is the freshness gate.
  rclone copyto "${WORK}/F000.png" "r2:${R2_BUCKET}/${out_rel}" \
    --s3-no-check-bucket --no-traverse \
    --header-upload "Cache-Control: public, max-age=86400, immutable"
  echo "  uploaded ${out_rel} ($(stat -c%s "${WORK}/F000.png") bytes)"

  # Value PNG for the crosshair inspector — same encode as
  # render_mrms_qpe.sh (gray 1..255 = gd_min..gd_max, 0 = nodata;
  # --hideNoData so the where() guards see the raw -9999s).
  if [ -n "${gd_min}" ] && [ -n "${gd_max}" ]; then
    if gdal_calc.py --quiet -A "${WORK}/merc.tif" --outfile="${WORK}/ga.tif" \
         --calc="where(A==-9999,0,minimum(255,maximum(1,1+round((A-(${gd_min}))*254.0/((${gd_max})-(${gd_min}))))))" \
         --calc="where(A==-9999,0,255)" \
         --type=Byte --hideNoData --overwrite \
       && gdal_translate -q -of PNG -co ZLEVEL=9 "${WORK}/ga.tif" "${WORK}/F000.data.png"; then
      rclone copyto "${WORK}/F000.data.png" "r2:${R2_BUCKET}/${out_rel%.png}.data.png" \
        --s3-no-check-bucket --no-traverse \
        --header-upload "Cache-Control: public, max-age=86400, immutable"
      echo "  uploaded ${out_rel%.png}.data.png (inspector values)"
    else
      echo "  value-PNG encode FAILED (non-fatal, fill unaffected)"
    fi
  fi
  RENDERED=$((RENDERED + 1))
done

# ── 6. Prune + manifest ─────────────────────────────────────────────────
echo ""
echo "==> Pruning ${MODEL} to last ${RETAIN} analyses"
python3 "${SCRIPT_DIR}/prune_old_runs.py" "${MODEL}" "${RETAIN}"

echo "==> Rebuilding ${MODEL} manifest"
python3 "${SCRIPT_DIR}/build_manifest.py" "${MODEL}"

echo ""
echo "==> RTMA surface render complete (${RENDERED} products @ ${FOUND_STAMP}Z, ${FOUND_SOURCE})"
