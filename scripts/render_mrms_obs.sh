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
# ── Env knobs ──────────────────────────────────────────────────────────
# Every one of these is optional and defaults to the pre-VPS behaviour, so
# an invocation with none of them set behaves exactly as the GitHub
# workflows have always run it.
#
#   OBS_PREFIX     — the R2/B2 path prefix this run writes to, WITHOUT the
#                    leading v1/. Default "OBS" (live). THIS VARIABLE IS THE
#                    CUTOVER SWITCH: the VPS shadows on "OBS-shadow" and
#                    goes live by changing this one value, so no other file
#                    has to be edited when the switch is thrown. The MODEL
#                    used for config lookup stays OBS regardless.
#   OBS_TIER       — render only products whose `cadence_tier` in
#                    products.yml matches: fast (~2 min at source, 41
#                    products), mid (~10 min, 9), slow (>=30 min, 37).
#                    Unset renders every mrms_dir product, as before.
#   OBS_JOBS       — how many products render concurrently. Default 1,
#                    which reproduces the old serial loop exactly. The VPS
#                    runs 4; the loop body is per-product independent
#                    (own temp dir, own uploads), so the only shared state
#                    is the read-only config and the results dir.
#   OBS_STATE_DIR  — directory holding local idempotency state, one file
#                    per product: "<runStamp> <HHMMSS>". When set, the tick
#                    answers "already rendered?" from local disk and needs
#                    NO bucket listing and NO CDN manifest read at all. On
#                    a box we control this is both cheaper and more
#                    accurate than either remote source.
#   OBS_RETAIN     — prune retention override, honoured ONLY when
#                    OBS_PREFIX is not the live prefix. A shadow run keeps
#                    a handful of runs so its listing stays one page; the
#                    guard means forgetting to unset it at cutover can
#                    never prune live history down to the shadow depth.
#   OBS_SKIP_PRUNE — skip the retention prune (and, with it, any delete
#                    against the bucket). Set on every shadow unit.
#   OBS_FAST_ONLY  — LEGACY, GitHub only: render only the six products
#                    flagged `fast_cadence: true`. Predates cadence_tier
#                    and is deliberately NOT redefined in terms of it —
#                    render_mrms_severe.yml is the live fallback until the
#                    cutover holds, and it must keep selecting exactly the
#                    same six products it does today. Retire both together.
#   FORCE_RERENDER — bypass the idempotency check entirely.
set -euo pipefail

MODEL="OBS"
# The prefix is a separate concept from the model: config is always read
# under `models.OBS`, only the bucket path moves.
PREFIX="${OBS_PREFIX:-$MODEL}"
LIVE_PREFIX="OBS"
JOBS="${OBS_JOBS:-1}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
CONFIG="${REPO_ROOT}/config/products.yml"
COLOR_TABLES="${REPO_ROOT}/config/color_tables"

ALL_PRODUCTS=$(yq -r ".models.${MODEL}.products[]" "$CONFIG")
RETAIN=$(yq -r ".models.${MODEL}.retain_runs" "$CONFIG")
BBOX=$(yq -r ".models.${MODEL}.bbox_lonlat | join(\" \")" "$CONFIG")
IMG_W=$(yq -r ".models.${MODEL}.image_size[0]" "$CONFIG")
IMG_H=$(yq -r ".models.${MODEL}.image_size[1]" "$CONFIG")

# Retention override, shadow-only by construction (see OBS_RETAIN above).
if [ -n "${OBS_RETAIN:-}" ] && [ "${PREFIX}" != "${LIVE_PREFIX}" ]; then
  RETAIN="${OBS_RETAIN}"
fi

echo "==> MRMS observation catalog: prefix=v1/${PREFIX}/ tier=${OBS_TIER:-all} jobs=${JOBS}"

# ── Idempotency state ──────────────────────────────────────────────────
# Three sources, in descending order of both accuracy and cheapness:
#
#   1. OBS_STATE_DIR — local disk. Zero network, always exactly describes
#      what this box last uploaded.
#   2. the CDN manifest (fast ticks) — Cloudflare serves it, B2 never sees
#      the read. Up to 60 s stale, so a product can re-render once for
#      nothing; uploads are free, so that is the cheap side of the trade.
#   3. one recursive bucket listing (full sweep).
#
# What (2) and (3) exist to avoid: v1/OBS/ is ~7,000 objects = 8 LIST
# pages, and listing that twelve times an hour is the single thing that
# would push this model past B2's free Class C allowance (measured: it
# would have taken the model from ~1,500 to ~6,100 LIST calls/day against
# a 2,500 free tier).
EXISTING_KEYS_FILE=$(mktemp)
FAST_SRC_FILE=$(mktemp)
RESULTS_DIR=$(mktemp -d)
trap 'rm -rf "$EXISTING_KEYS_FILE" "$FAST_SRC_FILE" "${FAST_SRC_FILE}.json" "$RESULTS_DIR"' EXIT
: > "$EXISTING_KEYS_FILE"
: > "$FAST_SRC_FILE"

if [ -n "${OBS_STATE_DIR:-}" ]; then
  mkdir -p "${OBS_STATE_DIR}/${PREFIX}"
  echo "==> Idempotency from local state (${OBS_STATE_DIR}/${PREFIX})"
elif [ -n "${OBS_FAST_ONLY:-}" ]; then
  echo "==> Fast tick: reading current source stamps from the CDN manifest"
  MODELS_BASE_URL="${MODELS_BASE_URL:-https://models.dgwaynes.com/v1}"
  if curl -sf -H 'Cache-Control: no-cache' -A 'stp-renderer/1.0' \
       "${MODELS_BASE_URL}/${PREFIX}/manifest.json?_fast=$(date +%s)" \
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
  echo "==> Pre-listing R2 contents under v1/${PREFIX}/"
  if rclone lsf --recursive "r2:${R2_BUCKET}/v1/${PREFIX}/" \
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

# ── Per-product render ─────────────────────────────────────────────────
# Everything below runs in a background subshell under `set -e`, one per
# product, up to OBS_JOBS at a time. It touches no shared mutable state:
# each call gets its own temp dir, and its outcome reaches the parent as
# a file in RESULTS_DIR rather than a variable (a `FAST_PATCH_ARGS+=()`
# in a subshell would be discarded when it exits). stdout is buffered to
# a per-product log and replayed in product order at the end, so parallel
# output stays as readable as the serial version's.
render_one() {
  local product="$1"
  local mrms_dir="$2"
  local log="${RESULTS_DIR}/${product}.log"
  # Bash resets inherited traps in an asynchronous subshell, but say so
  # explicitly: if the parent's EXIT cleanup ever did fire here, the first
  # product to finish would delete RESULTS_DIR out from under the other 40.
  trap - EXIT
  exec > "$log" 2>&1

  # Newest published object — today first, yesterday around 00Z.
  local TODAY YESTERDAY key
  TODAY=$(date -u +%Y%m%d)
  YESTERDAY=$(date -u -d "yesterday" +%Y%m%d)
  key=$(newest_key "${mrms_dir}" "${TODAY}")
  [ -z "${key}" ] && key=$(newest_key "${mrms_dir}" "${YESTERDAY}")
  if [ -z "${key}" ]; then
    echo "[${product}] no published files today/yesterday; skip"
    return 0
  fi

  # MRMS_<dir>_<YYYYMMDD>-<HHMMSS>.grib2.gz → run stamp + source stamp.
  local fname src_stamp vdate vtime vhour stamp
  fname="${key##*/}"
  src_stamp=$(echo "${fname}" | grep -oE '[0-9]{8}-[0-9]{6}' | head -1)
  if [ -z "${src_stamp}" ]; then
    echo "[${product}] unparseable filename ${fname}; skip"
    return 0
  fi
  vdate="${src_stamp%-*}"
  vtime="${src_stamp#*-}"
  vhour="${vtime:0:2}"
  stamp="${vdate}${vhour}"

  local out_rel json_rel marker_rel state_file=""
  out_rel="v1/${PREFIX}/${product}/${stamp}/F000.png"
  json_rel="${out_rel%.png}.json"
  marker_rel="${out_rel%.png}.src-${vtime}"
  [ -n "${OBS_STATE_DIR:-}" ] && state_file="${OBS_STATE_DIR}/${PREFIX}/${product}"

  local prev_stamp="" prev_vtime=""
  if [ -n "${state_file}" ] && [ -f "${state_file}" ]; then
    read -r prev_stamp prev_vtime < "${state_file}" || true
  fi

  if [ -z "${FORCE_RERENDER:-}" ]; then
    if [ -n "${OBS_STATE_DIR:-}" ]; then
      [ "${prev_stamp}" = "${stamp}" ] && [ "${prev_vtime}" = "${vtime}" ] && return 0
    elif [ -n "${OBS_FAST_ONLY:-}" ]; then
      [ "$(manifest_src "${product}" "${stamp}")" = "${vtime}" ] && return 0
    elif has_key "${marker_rel#v1/${PREFIX}/}"; then
      return 0  # slot already holds exactly this source file
    fi
  fi

  echo "[${product}] newest=${src_stamp} → run ${stamp}Z"

  # Read the product's render parameters only now that we know we are going
  # to render it. These are ten yq invocations, and the overwhelmingly
  # common tick is one where the source has published nothing new: at 41
  # products on a 2-minute timer that was 410 process spawns an hour to
  # answer a question the idempotency check above had already answered.
  local scale sentinel_lt units_out clr_file dmin dmax point_values decimals grid_w grid_h
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

  local work gz url grib
  work="$(mktemp -d)"
  gz="${work}/in.grib2.gz"
  url="https://noaa-mrms-pds.s3.amazonaws.com/${key}"
  if ! curl -sf "${url}" -o "${gz}"; then
    echo "  download failed (${url}); skip"
    rm -rf "${work}"
    return 0
  fi
  gunzip -f "${gz}"
  grib="${work}/in.grib2"

  # GRIB → warped PNG (+ optional value grid). One GDAL process when
  # OBS_SINGLE_PASS is on, the historical five otherwise — see
  # mrms_render_one.py for what the single pass replaces and why the two
  # are byte-for-byte interchangeable.
  # Single pass covers the DATA-PNG path, which is every one of the 87
  # mrms_dir products. Anything without data_png would need the
  # color-relief branch, so it keeps the classic chain rather than an
  # untested reimplementation.
  if [ -n "${OBS_SINGLE_PASS:-}" ] && [ -n "${dmin}" ]; then
    local pass_args=()
    # merc.tif is an intermediate the classic chain writes either way; the
    # single pass only needs it on disk for products that publish a value
    # grid, so the other products skip a ~44 MB write per tick.
    [ "${point_values}" = "true" ] && pass_args+=(--warped-tif "${work}/merc.tif")
    # shellcheck disable=SC2086
    python3 "${REPO_ROOT}/scripts/mrms_render_one.py" \
      --grib "${grib}" --out "${work}/F000.png" \
      --bbox ${BBOX} --size "${IMG_W}" "${IMG_H}" \
      --scale "${scale}" --sentinel-lt "${sentinel_lt}" \
      --data-min "${dmin}" --data-max "${dmax}" "${pass_args[@]}"
  else
    render_gdal_classic "${work}" "${grib}" "${scale}" "${sentinel_lt}" \
      "${dmin}" "${dmax}" "${clr_file}"
  fi

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

  rclone copyto "${work}/F000.png" "r2:${R2_BUCKET}/${out_rel}" \
    --s3-no-check-bucket --no-traverse \
    --header-upload "Cache-Control: public, max-age=300"
  echo "  uploaded ${out_rel} ($(stat -c%s "${work}/F000.png" 2>/dev/null || echo '?') bytes)"

  # Source marker: still written on every tick — it is what build_manifest.py
  # reads to publish srcTimes, so the full sweep stays authoritative either
  # way. Superseded markers are cleaned up when we know which one to remove:
  # local state names it exactly (one deletefile, no listing), and the full
  # sweep finds it in the listing it already has. A fast tick with neither
  # leaves its markers behind; they are zero-byte, and parse_src_times takes
  # the newest, so an extra one is never read as the current value.
  if [ -n "${OBS_STATE_DIR:-}" ]; then
    if [ -n "${prev_vtime}" ] && [ "${prev_stamp}" = "${stamp}" ] \
       && [ "${prev_vtime}" != "${vtime}" ]; then
      rclone deletefile \
        "r2:${R2_BUCKET}/v1/${PREFIX}/${product}/${prev_stamp}/F000.src-${prev_vtime}" \
        2>/dev/null || true
    fi
  elif [ -z "${OBS_FAST_ONLY:-}" ]; then
    while IFS= read -r old_marker; do
      rclone deletefile "r2:${R2_BUCKET}/v1/${PREFIX}/${old_marker}" 2>/dev/null || true
    done < <(grep -E "^${product}/${stamp}/F000\.src-" "$EXISTING_KEYS_FILE" || true)
  fi
  : > "${work}/marker"
  rclone copyto "${work}/marker" "r2:${R2_BUCKET}/${marker_rel}" \
    --s3-no-check-bucket --no-traverse

  # Outcome for the parent: presence of the file means "rendered", and the
  # contents feed the manifest patch.
  printf '%s %s\n' "${stamp}" "${vtime}" > "${RESULTS_DIR}/${product}.done"
  if [ -n "${OBS_STATE_DIR:-}" ]; then
    printf '%s %s\n' "${stamp}" "${vtime}" > "${state_file}"
  fi
  rm -rf "${work}"
}

# The historical five-spawn GDAL chain, kept verbatim as the fallback path
# and the reference the single pass is validated against.
render_gdal_classic() {
  local work="$1" grib="$2" scale="$3" sentinel_lt="$4"
  local dmin="$5" dmax="$6" clr_file="$7"

  # GRIB → Float32 GTiff (applies any packing scale/offset).
  gdal_translate -q -of GTiff -ot Float32 -b 1 "${grib}" "${work}/native.tif"

  # Longitude guard: 0-360 grids shift west by 360 (same as QPE script).
  local west
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
  local resample="cubic"
  [ -n "${dmin}" ] && resample="near"
  # shellcheck disable=SC2086
  gdalwarp -q -overwrite -t_srs EPSG:3857 -te_srs EPSG:4326 -te ${BBOX} \
    -ts "${IMG_W}" "${IMG_H}" -r "${resample}" -dstnodata -9999 \
    "${work}/raw.tif" "${work}/merc.tif"

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
}

# ── Product selection ──────────────────────────────────────────────────
SELECTED=()
for product in $ALL_PRODUCTS; do
  mrms_dir=$(yq -r ".products.${product}.mrms_dir // \"\"" "$CONFIG")
  # QPE products (mrms_product / Pass cycle) belong to render_mrms_qpe.sh.
  [ -z "${mrms_dir}" ] && continue

  # Legacy GitHub severe subset — deliberately still the six fast_cadence
  # products, not the 41 in the fast tier (see OBS_FAST_ONLY above).
  if [ -n "${OBS_FAST_ONLY:-}" ]; then
    fast=$(yq -r ".products.${product}.fast_cadence // false" "$CONFIG")
    [ "${fast}" = "true" ] || continue
  fi

  # Tier selection. Filtering here rather than rebuilding the product list
  # keeps one code path — and the tier lives in products.yml, so retuning
  # which products ride which timer never touches a systemd unit.
  if [ -n "${OBS_TIER:-}" ]; then
    tier=$(yq -r ".products.${product}.cadence_tier // \"slow\"" "$CONFIG")
    [ "${tier}" = "${OBS_TIER}" ] || continue
  fi

  SELECTED+=("${product}:${mrms_dir}")
done
echo "==> ${#SELECTED[@]} products selected"

# ── Render, up to JOBS at a time ───────────────────────────────────────
running=0
for entry in "${SELECTED[@]}"; do
  render_one "${entry%%:*}" "${entry#*:}" &
  running=$((running + 1))
  if [ "${running}" -ge "${JOBS}" ]; then
    # `|| true`: wait -n reports the finished job's exit status, and a
    # product that failed its own way must not take errexit's word for it
    # and kill the other 40.
    wait -n || true
    running=$((running - 1))
  fi
done
wait || true

# Replay per-product logs in selection order so parallel output reads like
# the serial version's did.
for entry in "${SELECTED[@]}"; do
  log="${RESULTS_DIR}/${entry%%:*}.log"
  [ -s "${log}" ] && cat "${log}"
done

# ── Collect outcomes ───────────────────────────────────────────────────
FAST_PATCH_ARGS=()
FAST_RUN_STAMP=""
RENDERED_ANY=false
for entry in "${SELECTED[@]}"; do
  product="${entry%%:*}"
  done_file="${RESULTS_DIR}/${product}.done"
  [ -s "${done_file}" ] || continue
  read -r r_stamp r_vtime < "${done_file}" || continue
  RENDERED_ANY=true
  FAST_PATCH_ARGS+=("${product}=${r_vtime}")
  FAST_RUN_STAMP="${r_stamp}"
done

# ── Prune + manifest (only when something changed — the QPE script
# already rebuilds the manifest every tick regardless) ──────────────────
#
# Which path a tick takes is about COST, not about which tier ran: a full
# rebuild lists the whole prefix, so only the tick that also prunes should
# pay for it. Ticks that skip the prune patch the published manifest
# instead — read over the CDN, written back as a free Class A write, no
# LIST calls at all.
#
# OBS_LOCK_FILE serialises the manifest step across tiers. On GitHub the
# tiers are separate workflows on disjoint minutes and there is no shared
# filesystem to lock on, so it stays unset and nothing changes. On one box
# running three timers, a 2-minute patch and a 20-minute rebuild WILL
# overlap, and both do read-modify-write on the same object — without the
# lock the rebuild's fresh `available` map can land on top of a patch, or
# vice versa. The render itself is deliberately outside the lock: only the
# manifest is contended.
manifest_step() {
  if [ -n "${OBS_SKIP_PRUNE:-}" ] || [ -n "${OBS_FAST_ONLY:-}" ]; then
    # FORCE_RERENDER can reach here with nothing actually rendered; an
    # empty array would also trip `set -u` on older bash.
    if [ ${#FAST_PATCH_ARGS[@]} -gt 0 ] && [ -n "${FAST_RUN_STAMP}" ]; then
      echo "==> Patching srcTimes into the published manifest"
      OBS_PREFIX="${PREFIX}" python3 "${SCRIPT_DIR}/patch_obs_srctimes.py" \
        "${FAST_RUN_STAMP}" "${FAST_PATCH_ARGS[@]}"
    else
      echo "==> Nothing rendered; manifest left alone"
    fi
  else
    echo "==> Pruning ${PREFIX} to last ${RETAIN} valid hours"
    OBS_PREFIX="${PREFIX}" python3 "${SCRIPT_DIR}/prune_old_runs.py" "${MODEL}" "${RETAIN}"
    echo "==> Rebuilding ${PREFIX} manifest"
    OBS_PREFIX="${PREFIX}" python3 "${SCRIPT_DIR}/build_manifest.py" "${MODEL}"
  fi
}

if [ "${RENDERED_ANY}" = true ] || [ -n "${FORCE_RERENDER:-}" ]; then
  echo ""
  if [ -n "${OBS_LOCK_FILE:-}" ]; then
    # -w, not -n: the frames are already uploaded, so the only thing at
    # stake is publishing their timestamps. Waiting out a rebuild costs
    # seconds; giving up loses the patch until the next tick.
    if ! flock -w 180 9; then
      echo "==> manifest lock busy for 180s; skipping (next tick re-patches)"
    else
      manifest_step
    fi 9>"${OBS_LOCK_FILE}"
  else
    manifest_step
  fi
else
  echo "==> No new MRMS observation frames this tick"
fi

echo ""
echo "==> MRMS observation catalog render complete"
