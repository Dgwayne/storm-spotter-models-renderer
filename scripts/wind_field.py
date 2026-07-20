#!/usr/bin/env python3
"""wind_field.py — build the wind-particle U/V field for Spotter Tools Pro
and push it to R2.

The app's animated wind-particle map layer needs a continuous wind vector
field to advect particles through. This publishes exactly that:

  wind/v1/uv_<stamp>.png   RGBA field. R = U (east-west), G = V (north-
                           south), both quantized m/s (see SCALE/BIAS).
                           B unused. A = 255 where valid, 0 = no data.
                           EPSG:3857 (web mercator) over FIELD_BBOX, so a
                           client texture lookup is LINEAR in mercator
                           coords — no per-sample latitude math on device.
  wind/v1/latest.json      tiny manifest: analysis time, PNG key, grid
                           dims, mercator/lonlat bounds, quantization.

Data sources — RTMA over land, GFS over the open ocean
------------------------------------------------------
Two fields are composited into one raster:

  * RTMA (Real-Time Mesoscale Analysis) — NCEP's 2.5 km CONUS *analysis
    of observations* (METARs + mesonets + satellite winds assimilated),
    i.e. actual current wind, not a forecast. This is the detailed land
    field and takes precedence wherever it has data.
  * GFS (Global Forecast System) f000 10 m wind — the 0.25° global
    analysis, used to FILL everything RTMA doesn't cover: the Gulf,
    Caribbean, Atlantic, and East Pacific where hurricanes live. RTMA's
    grid stops at the CONUS coastline, so without this base the layer is
    blank over water.

Both are free + public domain on AWS Open Data (noaa-rtma-pds /
noaa-gfs-bdp-pds), same legal footing as the NEXRAD/GLM data the app
already uses. GFS is a coarse (~25 km) synoptic field: it renders a
hurricane's circulation and steering flow correctly, but SMOOTHS the
eyewall, so particle speed near the core reads milder than reality. It's
a flow visualization, not an intensity product — NHC/the Tropical layer
stays the source of truth for category and position.

Wind-rotation GOTCHA (why wgrib2 is involved)
---------------------------------------------
RTMA's NDFD grid is Lambert conformal and its GRIB U/V are GRID-relative:
"U" points along the grid's x-axis, which only equals true east at the
grid's reference longitude. Advecting particles with raw components would
skew every trajectory (worst at the coasts). `wgrib2 -new_grid_winds
earth -new_grid latlon ...` rotates to true east/north while regridding;
gdalwarp then takes the earth-relative scalars to web mercator. (This is
the same reason decode_pipeline.sh's wind10m product only publishes the
magnitude — magnitude is rotation-invariant. Direction is not.) GFS is
already on an earth-relative lat-lon grid, so the same rotation step is a
harmless identity for it and both sources share one code path.

Stateless per invocation, cheap when idle: if the newest available RTMA +
GFS pair is already what latest.json points at, it exits in ~1 s. Safe to
run in the workflow's long loop.

Env:  R2_BUCKET   (rclone "r2:" remote configured by the workflow)
Tools: wgrib2, gdal_translate, gdalwarp (+ GDAL python), rclone
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
GFS_S3_BASE = "https://noaa-gfs-bdp-pds.s3.amazonaws.com"

# Field bbox — WIDER than the CONUS render pipeline (config/products.yml)
# on purpose: it reaches into the East Pacific, Gulf, Caribbean, and the
# Atlantic main-development region (Cape Verde) so ocean hurricanes show
# up. RTMA fills the CONUS interior; GFS fills the rest. Decoupled from
# products.yml because the model overlays stay CONUS-only.
FIELD_BBOX = (-140.0, 5.0, -15.0, 55.0)  # west, south, east, north

# Output raster width; height follows from the mercator aspect. Wind
# fields are smooth — 2048 across ~125° of longitude (~6.5 km/px) keeps
# CONUS about as crisp as the old 1024/CONUS field while covering ~2.6×
# the area, and the PNG stays a few hundred KB on the wire.
OUT_WIDTH = 2048

# Quantization: byte = value/SCALE + BIAS. 0.4 m/s per step spans
# ±51 m/s — beyond any 10 m sustained wind outside a landfalling major
# hurricane eyewall (values clamp there; direction is preserved). GFS
# never resolves winds that high anyway, so the clamp only matters if a
# high-res hurricane model is ever layered in.
SCALE = 0.4
BIAS = 128.0

# wgrib2 intermediate lat-lon grid: 0.03° ≈ 3.3 km, just above RTMA's
# native 2.5 km so the earth-wind rotation step doesn't oversample RTMA.
# GFS (0.25°) is upsampled onto the same grid — wasteful but harmless,
# and it keeps both sources on one shared regrid path.
LL_DLON = 0.03
LL_DLAT = 0.03

# How far back to look for a usable RTMA analysis before giving up. RU is
# normally ~20-30 min behind real time; hourly RTMA ~50 min.
MAX_LOOKBACK_MIN = 150
# GFS cycles every 6 h; f000 lands a few hours after cycle time. Look
# back far enough to always find a complete analysis (up to ~3 cycles).
GFS_MAX_LOOKBACK_H = 18

OUT_PREFIX = "wind/v1"
KEEP_PNGS = 8
SCHEMA_VERSION = 1


def _iso(t: dt.datetime) -> str:
    return t.replace(microsecond=0).isoformat().replace("+00:00", "Z")


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


def _fetch_range(url: str, byte_range: str) -> bytes:
    req = Request(url, headers={"Range": f"bytes={byte_range}"})
    with urlopen(req, timeout=120) as resp:
        return resp.read()


# ── RTMA analysis discovery ───────────────────────────────────────────
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


# ── GFS analysis discovery ────────────────────────────────────────────
def _find_newest_gfs_f000(now: dt.datetime) -> tuple[dt.datetime, str] | None:
    """Newest GFS cycle whose f000 0.25° file is on S3, newest-first.

    key: gfs.YYYYMMDD/HH/atmos/gfs.tHHz.pgrb2.0p25.f000
    """
    cyc = now.replace(minute=0, second=0, microsecond=0)
    cyc = cyc.replace(hour=(cyc.hour // 6) * 6)
    t = cyc
    while (now - t) <= dt.timedelta(hours=GFS_MAX_LOOKBACK_H):
        key = (
            f"gfs.{t:%Y%m%d}/{t:%H}/atmos/gfs.t{t:%H}z.pgrb2.0p25.f000"
        )
        if _http_ok(f"{GFS_S3_BASE}/{key}.idx"):
            return t, key
        t -= dt.timedelta(hours=6)
    return None


# ── GRIB fetch (idx byte-range, same trick as decode_pipeline.sh) ─────
def _fetch_uv_messages(s3_base: str, grib_key: str, work: Path, tag: str) -> Path:
    """Byte-range-fetch just the 10 m UGRD+VGRD messages of one GRIB file.

    Works for both RTMA and GFS: both expose a `.idx` sidecar and carry
    exactly one `UGRD:10 m above ground` + one `VGRD:10 m above ground`.
    `tag` namespaces the temp files so RTMA and GFS don't clobber.
    """
    idx_text = _fetch(f"{s3_base}/{grib_key}.idx").decode()
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
        raise RuntimeError(
            f"expected UGRD+VGRD in {tag} idx, got {len(ranges)}"
        )
    # Keep byte order = message order so band 1 is deterministic; record
    # which variable comes first rather than assuming.
    ranges.sort()
    order = [r[2] for r in ranges]
    grib_local = work / f"{tag}_in.grib2"
    with grib_local.open("wb") as f:
        for _off, rng, _var in ranges:
            f.write(_fetch_range(f"{s3_base}/{grib_key}", rng))
    (work / f"{tag}_band_order.json").write_text(json.dumps(order))
    return grib_local


# ── Regrid: earth-relative winds → lat-lon → web mercator ─────────────
def _regrid(grib_local: Path, work: Path, tag: str) -> tuple[Path, Path]:
    """Rotate to earth-relative U/V and reproject to the shared mercator
    target. Both sources go through the identical `-te`/`-ts` so the two
    output rasters are pixel-aligned and compositing is a plain np.where.
    """
    west, south, east, north = FIELD_BBOX
    nx = int(round((east - west) / LL_DLON)) + 1
    ny = int(round((north - south) / LL_DLAT)) + 1
    lon0 = west % 360.0  # wgrib2 wants 0-360 longitudes
    ll_grib = work / f"{tag}_ll.grib2"
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
    order = json.loads((work / f"{tag}_band_order.json").read_text())
    band_of = {var: i + 1 for i, var in enumerate(order)}
    out = []
    for var in ("UGRD", "VGRD"):
        native = work / f"{tag}_{var}_ll.tif"
        merc = work / f"{tag}_{var}_merc.tif"
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


# ── Composite + encode RGBA PNG ───────────────────────────────────────
def _read_band(path: Path) -> np.ndarray:
    ds = gdal.Open(str(path))
    return ds.GetRasterBand(1).ReadAsArray().astype("float64")


def _valid_mask(u: np.ndarray, v: np.ndarray) -> np.ndarray:
    return (u > -9000) & (v > -9000) & np.isfinite(u) & np.isfinite(v)


def _encode_png(
    work: Path,
    gfs_uv: tuple[Path, Path],
    rtma_uv: tuple[Path, Path] | None,
) -> tuple[Path, int, int, list[float]]:
    """GFS is the base fill; RTMA overwrites wherever it has data."""
    ds_ref = gdal.Open(str(gfs_uv[0]))
    gt = ds_ref.GetGeoTransform()
    w, h = ds_ref.RasterXSize, ds_ref.RasterYSize
    merc_bounds = [gt[0], gt[3] + gt[5] * h, gt[0] + gt[1] * w, gt[3]]

    u = _read_band(gfs_uv[0])
    v = _read_band(gfs_uv[1])
    valid = _valid_mask(u, v)

    if rtma_uv is not None:
        ru = _read_band(rtma_uv[0])
        rv = _read_band(rtma_uv[1])
        rtma_valid = _valid_mask(ru, rv)
        u = np.where(rtma_valid, ru, u)
        v = np.where(rtma_valid, rv, v)
        valid = valid | rtma_valid

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
        return (
            f"{meta.get('source')}|{meta.get('analysisTime')}"
            f"|{meta.get('gfsTime')}"
        )
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

    # GFS is the always-present base (ocean coverage). Without it there's
    # nothing to fill the water, so a missing GFS skips the pass.
    gfs = _find_newest_gfs_f000(now)
    if not gfs:
        print("  no GFS f000 available in lookback window", file=sys.stderr)
        return 0  # transient upstream gap; next pass may recover
    gfs_time, gfs_key = gfs
    gfs_iso = _iso(gfs_time)

    # RTMA is the CONUS enhancement — optional. If it's briefly late we
    # still publish the GFS-only field rather than blanking the map.
    rtma = _find_newest_analysis(now)
    if rtma:
        rtma_time, rtma_source, rtma_key = rtma
        analysis_iso = _iso(rtma_time)
        source_label = f"{rtma_source}+gfs"
    else:
        analysis_iso = gfs_iso
        source_label = "gfs"

    # Rebuild when EITHER source advances (source_label + analysisTime
    # cover RTMA; gfsTime covers GFS). Mirror this exact shape in
    # _current_latest_stamp so the skip check lines up.
    stamp = f"{source_label}|{analysis_iso}|{gfs_iso}"
    if _current_latest_stamp(bucket) == stamp:
        print(f"==> {stamp} already published; nothing to do")
        return 0

    print(f"==> building composite wind field ({source_label})")
    print(f"    gfs:  {GFS_S3_BASE}/{gfs_key}")
    if rtma:
        print(f"    rtma: {S3_BASE}/{rtma_key}")

    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)

        gfs_grib = _fetch_uv_messages(GFS_S3_BASE, gfs_key, work, "gfs")
        gfs_uv = _regrid(gfs_grib, work, "gfs")

        rtma_uv = None
        if rtma:
            rtma_grib = _fetch_uv_messages(S3_BASE, rtma_key, work, "rtma")
            rtma_uv = _regrid(rtma_grib, work, "rtma")

        png_path, w, h, merc = _encode_png(work, gfs_uv, rtma_uv)
        print(f"    png {w}x{h} {png_path.stat().st_size} bytes")

        # Filename keyed on the freshest analysis + a path-safe source tag
        # (no '+', which is ambiguous in URLs). Timestamp-first so the
        # lexical prune stays chronological.
        stamp_time = rtma_time if rtma else gfs_time
        src_tag = "rtmagfs" if rtma else "gfs"
        png_key = f"{OUT_PREFIX}/uv_{stamp_time:%Y%m%d%H%M}_{src_tag}.png"
        meta = {
            "schemaVersion": SCHEMA_VERSION,
            "generatedAt": _iso(now),
            "analysisTime": analysis_iso,
            "gfsTime": gfs_iso,
            "source": source_label,
            "png": png_key,
            "width": w,
            "height": h,
            "boundsLonLat": list(FIELD_BBOX),
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
