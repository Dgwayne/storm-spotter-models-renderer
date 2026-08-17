#!/usr/bin/env bash
# Prove scripts/mrms_render_one.py produces the SAME PNG as the five-spawn
# GDAL chain it replaces, on real MRMS files, on this box's GDAL build.
#
#   bash deploy/vps/validate_single_pass.sh [product ...]
#
# Byte-identity is the bar, not "looks the same". The single pass moves
# rounding out of gdal_calc and into numpy, and float32-vs-float64 or
# round-half-to-even-vs-half-away would each shift a handful of pixels by
# one gray level — invisible on a map, and a real change to the values the
# app decodes and the inspector reads back.
#
# Defaults cover the shapes that differ: mm->in scaling, km->kft scaling,
# a negative sentinel floor (dBZ), a raw pass-through, and a tiny
# non-integer range.
set -euo pipefail

REPO_DIR="${REPO_DIR:-/opt/stp-renderer}"
CONFIG="${REPO_DIR}/config/products.yml"
CLR="${REPO_DIR}/config/color_tables"

PRODUCTS=("$@")
[ ${#PRODUCTS[@]} -eq 0 ] && PRODUCTS=(mesh et18 rala rotnow vil shi rate ffg01h)

BBOX=$(yq -r ".models.OBS.bbox_lonlat | join(\" \")" "$CONFIG")
IMG_W=$(yq -r ".models.OBS.image_size[0]" "$CONFIG")
IMG_H=$(yq -r ".models.OBS.image_size[1]" "$CONFIG")

pass=0; fail=0
for product in "${PRODUCTS[@]}"; do
  mrms_dir=$(yq -r ".products.${product}.mrms_dir // \"\"" "$CONFIG")
  dmin=$(yq -r ".products.${product}.data_png.min // \"\"" "$CONFIG")
  dmax=$(yq -r ".products.${product}.data_png.max // \"\"" "$CONFIG")
  scale=$(yq -r ".products.${product}.scale // 1" "$CONFIG")
  sentinel_lt=$(yq -r ".products.${product}.sentinel_lt // 0" "$CONFIG")
  if [ -z "$mrms_dir" ] || [ -z "$dmin" ]; then
    echo "SKIP ${product} (no mrms_dir or no data_png)"
    continue
  fi

  work=$(mktemp -d)
  key=$({ curl -sf "https://noaa-mrms-pds.s3.amazonaws.com/?list-type=2&prefix=CONUS/${mrms_dir}/$(date -u +%Y%m%d)/&max-keys=1000" \
        | grep -oE "<Key>[^<]+</Key>" | sed -e 's|</\?Key>||g' \
        | grep '\.grib2\.gz$' | tail -1; } || true)
  if [ -z "$key" ]; then
    echo "SKIP ${product} (nothing published today yet)"
    rm -rf "$work"; continue
  fi
  curl -sf "https://noaa-mrms-pds.s3.amazonaws.com/${key}" -o "${work}/in.grib2.gz"
  gunzip -f "${work}/in.grib2.gz"

  # ── single pass ──────────────────────────────────────────────────────
  s0=$(date +%s%N)
  python3 "${REPO_DIR}/scripts/mrms_render_one.py" \
    --grib "${work}/in.grib2" --out "${work}/new.png" \
    --bbox ${BBOX} --size "$IMG_W" "$IMG_H" \
    --scale "$scale" --sentinel-lt "$sentinel_lt" \
    --data-min "$dmin" --data-max "$dmax"
  s1=$(date +%s%N)

  # ── classic five-spawn chain, verbatim from render_mrms_obs.sh ───────
  c0=$(date +%s%N)
  gdal_translate -q -of GTiff -ot Float32 -b 1 "${work}/in.grib2" "${work}/native.tif"
  west=$(python3 -c "
import json,subprocess,sys
print(json.loads(subprocess.check_output(['gdalinfo','-json','${work}/native.tif']))['cornerCoordinates']['upperLeft'][0])")
  if python3 -c "import sys; sys.exit(0 if float('${west}') > 180.0 else 1)"; then
    python3 -c "
import json,subprocess
p='${work}/native.tif'
i=json.loads(subprocess.check_output(['gdalinfo','-json',p]))
(ulx,uly)=i['cornerCoordinates']['upperLeft']; (lrx,lry)=i['cornerCoordinates']['lowerRight']
subprocess.check_call(['gdal_edit.py','-a_ullr',str(ulx-360),str(uly),str(lrx-360),str(lry),p])"
  fi
  gdal_calc.py --quiet -A "${work}/native.tif" --outfile="${work}/raw.tif" \
    --calc="where(A<(${sentinel_lt}),-9999,A*${scale})" \
    --NoDataValue=-9999 --type=Float32 --overwrite
  gdalwarp -q -overwrite -t_srs EPSG:3857 -te_srs EPSG:4326 -te ${BBOX} \
    -ts "$IMG_W" "$IMG_H" -r near -dstnodata -9999 \
    "${work}/raw.tif" "${work}/merc.tif"
  gdal_calc.py --quiet -A "${work}/merc.tif" --outfile="${work}/ga.tif" \
    --calc="where(A==-9999,0,minimum(255,maximum(1,1+round((A-(${dmin}))*254.0/((${dmax})-(${dmin}))))))" \
    --calc="where(A==-9999,0,255)" \
    --type=Byte --hideNoData --overwrite
  gdal_translate -q -of PNG -co ZLEVEL=9 "${work}/ga.tif" "${work}/old.png"
  c1=$(date +%s%N)

  new_ms=$(( (s1-s0)/1000000 )); old_ms=$(( (c1-c0)/1000000 ))
  if cmp -s "${work}/new.png" "${work}/old.png"; then
    echo "PASS ${product}  identical  single=${new_ms}ms classic=${old_ms}ms  ($(( 100*new_ms/old_ms ))% of classic)"
    pass=$((pass+1))
  else
    # Not identical: say by how much, because "3 pixels differ by 1" and
    # "the whole grid is shifted" need very different responses.
    echo "FAIL ${product}  PNGs differ  single=${new_ms}ms classic=${old_ms}ms"
    python3 - "${work}/new.png" "${work}/old.png" <<'PY'
import sys
from osgeo import gdal
import numpy as np
a = gdal.Open(sys.argv[1]); b = gdal.Open(sys.argv[2])
if a.RasterXSize != b.RasterXSize or a.RasterYSize != b.RasterYSize:
    print(f"     size {a.RasterXSize}x{a.RasterYSize} vs {b.RasterXSize}x{b.RasterYSize}")
    sys.exit()
for i in range(1, a.RasterCount + 1):
    x = a.GetRasterBand(i).ReadAsArray().astype(int)
    y = b.GetRasterBand(i).ReadAsArray().astype(int)
    d = np.abs(x - y)
    n = int((d > 0).sum())
    print(f"     band {i}: {n} px differ ({100.0*n/d.size:.6f}%), max delta {int(d.max())}")
PY
    fail=$((fail+1))
    cp "${work}/new.png" "/tmp/spv-${product}-new.png"
    cp "${work}/old.png" "/tmp/spv-${product}-old.png"
    echo "     kept /tmp/spv-${product}-{new,old}.png"
  fi
  rm -rf "$work"
done

echo
echo "single-pass validation: ${pass} identical, ${fail} differing"
[ "$fail" -eq 0 ]
