#!/usr/bin/env python3
"""wind_field.py — build the wind-particle U/V field for Spotter Tools Pro
and push it to R2.

The app's animated wind-particle map layer needs a continuous wind vector
field to advect particles through. This publishes exactly that:

  wind/v1/uv_<stamp>.png   RGBA field. R = U (east-west), G = V (north-
                           south), both quantized m/s (see SCALE/BIAS).
                           B unused. A = 255 where valid, 0 = no data.
                           EPSG:3857 (web mercator) over CONUS_BBOX, so a
                           client texture lookup is LINEAR in mercator
                           coords — no per-sample latitude math on device.
  wind/v1/latest.json      tiny manifest: analysis time, PNG key, grid
                           dims, mercator/lonlat bounds, quantization.

Data source — RTMA (Real-Time Mesoscale Analysis)
--------------------------------------------------
RTMA is NCEP's 2.5 km CONUS *analysis of observations* (METARs + mesonets
+ satellite winds assimilated), i.e. actual current wind, not a forecast.
Free + public domain on AWS Open Data (noaa-rtma-pds), same legal footing
as the NEXRAD/GLM data the app already uses. We prefer the Rapid-Update
cycle (:00/:15/:30/:45 each hour) and fall back to the hourly analysis if
RU is missing/late.

Wind-rotation GOTCHA (why wgrib2 is involved)
---------------------------------------------
RTMA's NDFD grid is Lambert conformal and its GRIB U/V are GRID-relative:
"U" points along the grid's x-axis, which only equals true east at the
grid's reference longitude. Advecting particles with raw components would
skew every trajectory (worst at the coasts). `wgrib2 -new_grid_winds
earth -new_grid latlon ...` rotates to true east/north while regridding;
gdalwarp then takes the earth-relative scalars to web mercator. (This is
the same reason decode_pipeline.sh's wind10m product only publishes the
magnitude — magnitude is rotation-invariant. Direction is not.)

Stateless per invocation, cheap when idle: if the newest available
analysis is already what latest.json points at, it exits in ~1 s. Safe to
run in the workflow's long loop.

Env:  R2_BUCKET   (rclone "r2:" remote configured by the workflow)
Tools: curl, wgrib2, gdal_translate, gdalwarp (+ GDAL python), rclone
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from urllib.request import urlopen, Request

import numpy as np
from osgeo import gdal

gdal.UseExceptions()

# ── Config ────────────────────────────────────────────────────────────
S3_BASE = "https://noaa-rtma-pds.s3.amazonaws.com"

# Same CONUS bbox as the HRRR/GFS render pipeline (config/products.yml),
# so the wind field lines up with every other overlay.
CONUS_BBOX = (-134.0, 21.0, -60.0, 53.0)  # west, south, east, north

# Output raster width; height follows from the mercator aspect (~535).
# Wind fields are smooth — 1024 across CONUS (~7 km/px) is plenty for
# particle advection and keeps the PNG a few hundred KB on the wire.
OUT_WIDTH = 1024

# Quantization: byte = value/SCALE + BIAS. 0.4 m/s per step spans
# ±51 m/s — beyond any 10 m sustained wind outside a landfalling major
# hurricane eyewall (values clamp there; direction is preserved).
SCALE = 0.4
BIAS = 128.0

# wgrib2 intermediate lat-lon grid: 0.03° ≈ 3.3 km, just above RTMA's
# native 2.5 km so the earth-wind rotation step doesn't oversample.
LL_DLON = 0.03
LL_DLAT = 0.03

# How far back to look for a usable analysis before giving up. RU is
# normally ~20-30 min behind real time; hourly RTMA ~50 min.
MAX_LOOKBACK_MIN = 150

OUT_PREFIX = "wind/v1"
KEEP_PNGS = 8
SCHEMA_VERSION = 1


def _http_ok(url: str) -> bool:
    try:
        req = Request(url, method="HEAD")
        with urlopen(req, timeout=20) as resp:
            return 200 <= resp.status < 300
    except Exception:
        return False


def _fetch(url: str) -> bytes:
    with urlopen(url, timeout=60) as resp:
        return resp.read()


# ── Analysis discovery ────────────────────────────────────────────────
def _candidates(now: dt.datetime) -> list[tuple[dt.datetime, str, str]]:
    """(analysis_time, source_tag, grib_key) newest-first, RU preferred.

    RU:     rtma2p5_ru.YYYYMMDD/rtma2p5_ru.tHHMMz.2dvaranl_ndfd.grb2
    hourly: rtma2p5.YYYYMMDD/rtma2p5.tHHz.2dvaranl_ndfd.grb2_wexp
    """
    out: list[tuple[dt.datetime, str, str]] = []
    # RU every 15 min — enumerate recent quarter-hours.
    q = now.replace(minute=(now.minute // 15) * 15, second=0, microsecond=0)
    t = q
    while (now - t) <= dt.timedelta(minutes=MAX_LOOKBACK_MIN):
        key = (
            f"rtma2p5_ru.{t:%Y%m%d}/rtma2p5_ru.t{t:%H%M}z.2dvaranl_ndfd.grb2"
        )
        out.append((t, "rtma2p5_ru", key))
        t -= dt.timedelta(minutes=15)
    # Hourly fallbacks, interleaved after all RU candidates.
    h = now.replace(minute=0, second=0, microsecond=0)
    t = h
    while (now - t) <= dt.timedelta(minutes=MAX_LOOKBACK_MIN + 60):
        key = f"rtma2p5.{t:%Y%m%d}/rtma2p5.t{t:%H}z.2dvaranl_ndfd.grb2_wexp"
        out.append((t, "rtma2p5", key))
        t -= dt.timedelta(hours=1)
    return out


def _find_newest_analysis(now: dt.datetime) -> tuple[dt.datetime, str, str] | None:
    for anal_time, source, key in _candidates(now):
        if _http_ok(f"{S3_BASE}/{key}.idx"):
            return anal_time, source, key
    return None


# ── GRIB fetch (idx byte-range, same trick as decode_pipeline.sh) ─────
def _fetch_uv_messages(grib_key: str, work: Path) -> Path:
    idx_text = _fetch(f"{S3_BASE}/{grib_key}.idx").decode()
    parsed = []
    for ln in idx_text.splitlines():
        parts = ln.split(":")
        if len(parts) >= 3:
            try:
                parsed.append((int(parts[0]), int(parts[1]), ln))
            except ValueError:
                pass
    parsed.sort()
    want = re.compile(r":(UGRD|VGRD):10 m above ground:")
    ranges: list[tuple[int, str, str]] = []  # (offset, range, varname)
    for i, (_msg, offset, ln) in enumerate(parsed):
        m = want.search(ln)
        if not m:
            continue
        end = str(parsed[i + 1][1] - 1) if i + 1 < len(parsed) else ""
        ranges.append((offset, f"{offset}-{end}", m.group(1)))
    if len(ranges) != 2:
        raise RuntimeError(f"expected UGRD+VGRD in idx, got {len(ranges)}")
    # Keep byte order = message order so band 1 is deterministic; record
    # which variable comes first rather than assuming.
    ranges.sort()
    order = [r[2] for r in ranges]
    grib_local = work / "in.grib2"
    with grib_local.open("wb") as f:
        for _off, rng, _var in ranges:
            f.write(_fetch_range(f"{S3_BASE}/{grib_key}", rng))
    (work / "band_order.json").write_text(json.dumps(order))
    return grib_local


def _fetch_range(url: str, byte_range: str) -> bytes:
    req = Request(url, headers={"Range": f"bytes={byte_range}"})
    with urlopen(req, timeout=120) as resp:
        return resp.read()


# ── Regrid: earth-relative winds → lat-lon → web mercator ─────────────
def _regrid(grib_local: Path, work: Path) -> tuple[Path, Path]:
    west, south, east, north = CONUS_BBOX
    nx = int(round((east - west) / LL_DLON)) + 1
    ny = int(round((north - south) / LL_DLAT)) + 1
    lon0 = west % 360.0  # wgrib2 wants 0-360 longitudes
    ll_grib = work / "ll.grib2"
    subprocess.check_call(
        [
            "wgrib2", str(grib_local),
            "-set_grib_type", "simple",
            "-new_grid_winds", "earth",
            "-new_grid_interpolation", "bilinear",
            "-new_grid", "latlon",
            f"{lon0}:{nx}:{LL_DLON}", f"{south}:{ny}:{LL_DLAT}",
            str(ll_grib),
        ],
        stdout=subprocess.DEVNULL,
    )
    order = json.loads((work / "band_order.json").read_text())
    band_of = {var: i + 1 for i, var in enumerate(order)}
    out = []
    for var in ("UGRD", "VGRD"):
        native = work / f"{var}_ll.tif"
        merc = work / f"{var}_merc.tif"
        subprocess.check_call(
            ["gdal_translate", "-q", "-of", "GTiff", "-ot", "Float32",
             "-b", str(band_of[var]), str(ll_grib), str(native)]
        )
        subprocess.check_call(
            ["gdalwarp", "-q", "-overwrite",
             "-t_srs", "EPSG:3857", "-te_srs", "EPSG:4326",
             "-te", str(west), str(south), str(east), str(north),
             "-ts", str(OUT_WIDTH), "0",
             "-r", "bilinear", "-dstnodata", "-9999",
             str(native), str(merc)]
        )
        out.append(merc)
    return out[0], out[1]


# ── Encode RGBA PNG ───────────────────────────────────────────────────
def _encode_png(u_tif: Path, v_tif: Path, work: Path) -> tuple[Path, int, int, list[float]]:
    ds_u = gdal.Open(str(u_tif))
    ds_v = gdal.Open(str(v_tif))
    u = ds_u.GetRasterBand(1).ReadAsArray().astype("float64")
    v = ds_v.GetRasterBand(1).ReadAsArray().astype("float64")
    gt = ds_u.GetGeoTransform()
    w, h = ds_u.RasterXSize, ds_u.RasterYSize
    merc_bounds = [gt[0], gt[3] + gt[5] * h, gt[0] + gt[1] * w, gt[3]]

    valid = (u > -9000) & (v > -9000) & np.isfinite(u) & np.isfinite(v)

    def q(a: np.ndarray) -> np.ndarray:
        return np.clip(np.round(a / SCALE + BIAS), 0, 255).astype("uint8")

    r = np.where(valid, q(u), 0).astype("uint8")
    g = np.where(valid, q(v), 0).astype("uint8")
    b = np.zeros_like(r)
    a = np.where(valid, 255, 0).astype("uint8")

    mem = gdal.GetDriverByName("MEM").Create("", w, h, 4, gdal.GDT_Byte)
    for i, band in enumerate((r, g, b, a), start=1):
        mem.GetRasterBand(i).WriteArray(band)
    png_path = work / "uv.png"
    gdal.GetDriverByName("PNG").CreateCopy(
        str(png_path), mem, options=["ZLEVEL=9"]
    )
    return png_path, w, h, merc_bounds


# ── R2 helpers ────────────────────────────────────────────────────────
def _r2(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["rclone", *args], capture_output=True, text=True, timeout=180
    )


def _current_latest_stamp(bucket: str) -> str | None:
    res = _r2(["cat", f"r2:{bucket}/{OUT_PREFIX}/latest.json"])
    if res.returncode != 0:
        return None
    try:
        meta = json.loads(res.stdout)
        return f"{meta.get('source')}|{meta.get('analysisTime')}"
    except Exception:
        return None


def _prune_old_pngs(bucket: str) -> None:
    res = _r2(["lsf", f"r2:{bucket}/{OUT_PREFIX}/", "--files-only"])
    if res.returncode != 0:
        return
    pngs = sorted(f for f in res.stdout.splitlines() if f.startswith("uv_"))
    for old in pngs[:-KEEP_PNGS]:
        _r2(["deletefile", f"r2:{bucket}/{OUT_PREFIX}/{old}"])


# ── Main ──────────────────────────────────────────────────────────────
def main() -> int:
    bucket = os.environ["R2_BUCKET"]
    now = dt.datetime.now(dt.timezone.utc)

    found = _find_newest_analysis(now)
    if not found:
        print("  no RTMA analysis available in lookback window", file=sys.stderr)
        return 0  # transient upstream gap; next pass may recover
    anal_time, source, grib_key = found
    anal_iso = anal_time.replace(microsecond=0).isoformat().replace("+00:00", "Z")
    stamp = f"{source}|{anal_iso}"

    if _current_latest_stamp(bucket) == stamp:
        print(f"==> {source} {anal_iso} already published; nothing to do")
        return 0

    print(f"==> building wind field from {source} {anal_iso}")
    print(f"    source: {S3_BASE}/{grib_key}")

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        grib_local = _fetch_uv_messages(grib_key, work)
        print(f"    fetched {grib_local.stat().st_size} bytes (UGRD+VGRD)")
        u_tif, v_tif = _regrid(grib_local, work)
        png_path, w, h, merc = _encode_png(u_tif, v_tif, work)
        print(f"    png {w}x{h} {png_path.stat().st_size} bytes")

        png_key = f"{OUT_PREFIX}/uv_{anal_time:%Y%m%d%H%M}_{source}.png"
        meta = {
            "schemaVersion": SCHEMA_VERSION,
            "generatedAt": now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "analysisTime": anal_iso,
            "source": source,
            "png": png_key,
            "width": w,
            "height": h,
            "boundsLonLat": list(CONUS_BBOX),
            "boundsMercator": merc,
            "scale": SCALE,
            "bias": BIAS,
            "units": "m/s",
        }
        meta_path = work / "latest.json"
        meta_path.write_text(json.dumps(meta, separators=(",", ":")))

        # PNG first (immutable, long edge cache), manifest second so the
        # app never sees a manifest pointing at a missing PNG.
        subprocess.check_call(
            ["rclone", "copyto", str(png_path), f"r2:{bucket}/{png_key}",
             "--s3-no-check-bucket", "--no-traverse",
             "--header-upload", "Cache-Control: public, max-age=86400"]
        )
        subprocess.check_call(
            ["rclone", "copyto", str(meta_path),
             f"r2:{bucket}/{OUT_PREFIX}/latest.json",
             "--s3-no-check-bucket", "--no-traverse",
             "--header-upload", "Cache-Control: public, max-age=45"]
        )
    _prune_old_pngs(bucket)
    print(f"    uploaded {png_key} + latest.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
