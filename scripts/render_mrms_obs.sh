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
# marker, the tick skips the product entirely. build_manifest.py also reads
# those markers to publish each product's REAL valid time (srcTimes), since
# the hourly run stamp alone can't distinguish 19:02 data from 19:58.
#
# Two env knobs, both used by render_mrms_severe.yml:
#   OBS_FAST_ONLY  — render ONLY products flagged `fast_cadence: true` in
#                    products.yml. The full 92-product sweep can't run more
#                    often than ~15 min, but the handful of products a
#                    warning decision turns on (rotation, hail size,
#                    lowest-altitude reflectivity) publish upstream every
#                    ~2 min, and at a 15-min render cadence they were
#                    reaching the app up to ~17 min old — visibly trailing
#                    the storm they describe.
#   OBS_SKIP_PRUNE — skip the retention prune. It only needs to run on the
#                    full sweep; doing it on every fast tick would triple
#                    this model's LIST calls for nothing.
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

# ── Idempotency state ──────────────────────────────────────────────────
# The full sweep pre-lists the bucket once and answers "already rendered?"
# from that listing. The FAST tick must not: v1/OBS/ is ~7,000 objects = 8
# LIST pages, and doing that twelve times an hour is the single thing that
# would push this model past B2's free Class C allowance (measured: it would
# have taken the model from ~1,500 to ~6,100 LIST calls/day against a 2,500
# free tier). It reads the manifest off the public CDN instead — Cloudflare
# serves it, B2 never sees the request, and the srcTimes already in there
# answer exactly the same question.
EXISTING_KEYS_FILE=$(mktemp)
FAST_SRC_FILE=$(mktemp)
trap 'rm -f "$EXISTING_KEYS_FILE" "$FAST_SRC_FILE"' EXIT
: > "$EXISTING_KEYS_FILE"
: > "$FAST_SRC_FILE"

if [ -n "${OBS_FAST_ONLY:-}" ]; then
  echo "==> Fast tick: reading current source stamps from the CDN manifest"
  MODELS_BASE_URL="${MODELS_BASE_URL:-https://models.dgwaynes.com/v1}"
  if curl -sf -H 'Cache-Control: no-cache' -A 'stp-renderer/1.0' \
       "${MODELS_BASE_URL}/${MODEL}/manifest.json?_fast=$(date +%s)" \
       -o "${FAST_SRC_FILE}.json"; then
    python3 - "${FAST_SRC_FILE}.json" > "$FAST_SRC_FILE" <<'PY'
import json, sys
# "<product> <runStamp> <HHMMSS>" per line, for the awk lookup below.
try:
    m = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    sys.exit(0)
for run in m.get("runs") or []:
    stamp = run.get("runStamp")
    for code, iso in (run.get("srcTimes") or {}).items():
        # 2026-08-16T19:33:20+00:00 -> 193320
        t = iso[11:19].replace(":", "")
        if stamp and len(t) == 6:
            print(f"{code} {stamp} {t}")
PY
    echo "  $(wc -l < "$FAST_SRC_FILE") known source stamps"
  else
    echo "  manifest unavailable; this tick re-renders (uploads are free)"
  fi
else
  echo "==> Pre-listing R2 contents under v1/${MODEL}/"
  if rclone lsf --recursive "r2:${R2_BUCKET}/v1/${MODEL}/" \
      --files-only 2>/dev/null > "$EXISTING_KEYS_FILE"; then
    echo "  $(wc -l < "$EXISTING_KEYS_FILE") existing keys cached"
  else
    echo "  (no existing keys, fresh bucket)"
    : > "$EXISTING_KEYS_FILE"
  fi
fi

has_key() { grep -qxF "$1" "$EXISTING_KEYS_FILE"; }

# Source stamp the manifest says this product/run already holds (fast tick).
manifest_src() {
  awk -v p="$1" -v r="$2" '$1==p && $2==r {print $3; exit}' "$FAST_SRC_FILE"
}

# Products this tick rendered, as "<code>=<HHMMSS>" for the manifest patch.
FAST_PATCH_ARGS=()
FAST_RUN_STAMP=""

# Newest .grib2.gz key under CONUS/<dir>/<date>/ (S3 lists ascending; a
# 2-min product day is ~720 objects — one 1000-key page). Empty if none.
# The || true absorbs the grep no-match exit status: right after 00Z the
# new day's directory is empty for slower products, and under
# `set -euo pipefail` the bare pipeline would kill the whole script from
# inside the callers' command substitutions (nightly 00:00-01:00 UTC
# failure runs) before their [ -z ] yesterday-fallback could fire. A
# transient curl failure is absorbed the same way — empty means "none".
newest_key() {
  local dir="$1" date="$2"
  { curl -sf "https://noaa-mrms-pds.s3.amazonaws.com/?list-type=2&prefix=CONUS/${dir}/${date}/&max-keys=1000" \
    | grep -oE "<Key>[^<]+</Key>" | sed -e 's|</\?Key>||g' \
    | grep '\.grib2\.gz$' | tail -1; } || true
}

RENDERED_ANY=false

for product in $ALL_PRODUCTS; do
  mrms_dir=$(yq -r ".products.${product}.mrms_dir // \"\"" "$CONFIG")
  # QPE products (mrms_product / Pass cycle) belong to render_mrms_qpe.sh.
  [ -z "${mrms_dir}" ] && continue

  # Fast tick: only the severe subset. Filtering here rather than rebuilding
  # the product list keeps one code path — and the flag lives in
  # products.yml, so tuning the subset never touches the workflow.
  if [ -n "${OBS_FAST_ONLY:-}" ]; then
    fast=$(yq -r ".products.${product}.fast_cadence // false" "$CONFIG")
    [ "${fast}" = "true" ] || continue
  fi

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

  if [ -z "${FORCE_RERENDER:-}" ]; then
    if [ -n "${OBS_FAST_ONLY:-}" ]; then
      # Manifest-based skip. Worst case the CDN copy is up to 60 s stale and
      # a product re-renders once for nothing — uploads are free, so that is
      # the cheap side of the trade.
      [ "$(manifest_src "${product}" "${stamp}")" = "${vtime}" ] && continue
    elif has_key "${marker_rel#v1/${MODEL}/}"; then
      continue  # slot already holds exactly this source file
    fi
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

  # Source marker: still written on every tick — it is what build_manifest.py
  # reads to publish srcTimes, so the full sweep stays authoritative either
  # way. The cleanup of superseded markers needs the bucket listing, so a
  # fast tick leaves its markers behind; they are zero-byte, and the next
  # sweep sweeps them (parse_src_times takes the newest, so an extra one is
  # never read as the current value).
  if [ -z "${OBS_FAST_ONLY:-}" ]; then
    while IFS= read -r old_marker; do
      rclone deletefile "r2:${R2_BUCKET}/v1/${MODEL}/${old_marker}" 2>/dev/null || true
    done < <(grep -E "^${product}/${stamp}/F000\.src-" "$EXISTING_KEYS_FILE" || true)
  fi
  : > "${work}/marker"
  rclone copyto "${work}/marker" "r2:${R2_BUCKET}/${marker_rel}" \
    --s3-no-check-bucket --no-traverse
  RENDERED_ANY=true
  FAST_PATCH_ARGS+=("${product}=${vtime}")
  FAST_RUN_STAMP="${stamp}"
  rm -rf "${work}"
done

# ── Prune + manifest (only when something changed — the QPE script
# already rebuilds the manifest every tick regardless) ──────────────────
if [ "${RENDERED_ANY}" = true ] || [ -n "${FORCE_RERENDER:-}" ]; then
  echo ""
  if [ -n "${OBS_FAST_ONLY:-}" ]; then
    # Fast tick: patch the timestamps into the published manifest instead of
    # rebuilding it. A rebuild lists the whole prefix; this reads the CDN
    # copy and writes it back, so the whole tick costs B2 nothing but free
    # Class A writes. The 15-min sweep still does the authoritative rebuild,
    # including prune and the full availability map.
    # FORCE_RERENDER can reach here with nothing actually rendered; an
    # empty array would also trip `set -u` on older bash.
    if [ ${#FAST_PATCH_ARGS[@]} -gt 0 ] && [ -n "${FAST_RUN_STAMP}" ]; then
      echo "==> Patching srcTimes into the published manifest"
      python3 "${SCRIPT_DIR}/patch_obs_srctimes.py" \
        "${FAST_RUN_STAMP}" "${FAST_PATCH_ARGS[@]}"
    else
      echo "==> Nothing rendered; manifest left alone"
    fi
  else
    echo "==> Pruning OBS to last ${RETAIN} valid hours"
    python3 "${SCRIPT_DIR}/prune_old_runs.py" "${MODEL}" "${RETAIN}"
    echo "==> Rebuilding OBS manifest"
    python3 "${SCRIPT_DIR}/build_manifest.py" "${MODEL}"
  fi
else
  echo "==> No new MRMS observation frames this tick"
fi

echo ""
echo "==> MRMS observation catalog render complete"
