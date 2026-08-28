#!/usr/bin/env python3
"""extract_soundings.py - model vertical profiles per NEXRAD site -> R2.

For every CONUS NEXRAD radar in config/nexrad_sites.csv, pull each MODELS
entry's temperature, dewpoint, geopotential height and wind at isobaric
levels (plus a 2 m / 10 m surface level) for a set of forecast hours,
interpolate to the radar point, and upload per-site JSON to R2. HRRR is
the default/back-compat model at v1/soundings/; RRFS mirrors the same
layout under v1/soundings/rrfs/ for the app's model picker.

Outputs (per site SID):
  * v1/soundings/<SID>.json     - the F00 analysis profile only. Back-compat
    shape for the velocity dealiaser (hgt_msl_m / u_ms / v_ms) and the app's
    single-profile fetch.
  * v1/soundings/<SID>.fc.json  - all forecast hours, for the app's forecast
    scrubber: {run, elev_m, hours:[{fhour, valid, pres_hpa, temp_c, ...}, ...]}.
  * v1/soundings/tiles/<X>_<Y>.json + tiles.json - tap-anywhere point
    soundings: the grid subsampled every TILE_STRIDE px (~24 km), chopped
    into TILE_COLS^2-column tiles, all forecast hours per tile. The app
    picks the tile from tiles.json bboxes and inverse-distance-interpolates
    the nearest columns to the tap point.

Reads the HRRR wrfprs 3D pressure file via the .idx byte-range trick (only
the ~100 matching messages per forecast hour, not the whole GRIB). Point
extraction batches every site into ONE gdallocationinfo call per forecast
hour (the GRIB is decoded once, not per-site), so adding forecast hours
stays cheap. $0/month on the existing Actions cron + R2 setup.

Each file also carries utc_offset_seconds: the site's DST-correct offset from
UTC (so the app shows true local launch time, getting Arizona/Indiana right
rather than a longitude guess). Computed via timezonefinder -> IANA zone ->
stdlib zoneinfo; falls back to a longitude estimate if the lookup is missing.

Env: R2_BUCKET, R2_ENDPOINT, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY
Tools: gdal (gdallocationinfo), rclone, python3 (+ timezonefinder)
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent.parent
SITES_CSV = REPO_ROOT / "config" / "nexrad_sites.csv"

LEVELS = list(range(1000, 99, -50))  # 1000,950,...,150,100  (19 levels)
ISOBARIC_VARS = ("UGRD", "VGRD", "HGT", "TMP", "DPT")
# Forecast hours to emit: HOURLY out to +18 h. Both HRRR and RRFS publish
# every forecast hour for every cycle, so the app's scrubber steps 1 h.
# (The original 3-hourly cut was a conservative first pass, not a data limit.)
FHOURS = list(range(0, 19))
LOOKBACK_HOURS = 8
BUCKET = os.environ["R2_BUCKET"]

# Models to extract. Each source file is fetched with the same .idx
# byte-range trick and its classify()-matched messages merged into one
# band dict per hour — HRRR carries everything in wrfprs, while RRFS splits
# the isobaric levels (prslev.3km) from the surface fields (2dfld.3km).
# `prefix` is the output subdirectory under v1/soundings/ ("" keeps HRRR at
# the root for back-compat with the dealiaser and released apps).
# ⚠ RRFS DISABLED DURING THE NOMADS BRIDGE (2026-08-13 → 2026-10-06):
# the rrfs_a feed froze at 2026-08-12 11z (SCN 26-48). The live NOMADS
# source serves no .idx, and soundings need the 591 MB prslev file — a
# whole-file bridge would add ~45 GB/day of NOMADS pulls for a secondary
# feature, so RRFS soundings stay frozen at their last extracted run and
# HRRR (the default) carries the sounding deck. RESTORE the entry below
# at cutover with the operational path (drop the rrfs_a/ prefix; verify
# against the bucket):
#     {
#         "key": "rrfs",
#         "name": "RRFS",
#         "prefix": "rrfs/",
#         "files": [
#             "https://noaa-rrfs-pds.s3.amazonaws.com/"
#             "rrfs.{d}/{h}/rrfs.t{h}z.prslev.3km.f{fh:03d}.conus.grib2",
#             "https://noaa-rrfs-pds.s3.amazonaws.com/"
#             "rrfs.{d}/{h}/rrfs.t{h}z.2dfld.3km.f{fh:03d}.conus.grib2",
#         ],
#     },
MODELS = [
    {
        "key": "hrrr",
        "name": "HRRR",
        "prefix": "",
        "files": [
            "https://noaa-hrrr-bdp-pds.s3.amazonaws.com/"
            "hrrr.{d}/conus/hrrr.t{h}z.wrfprsf{fh:02d}.grib2",
        ],
    },
    # GFS 0.25 deg — the COVERAGE model: HRRR is CONUS-only, so AK / HI /
    # PR / offshore / tropics had no soundings at all. GFS publishes
    # HOURLY files to f120, so the shared FHOURS 0-18 works unchanged.
    # Two per-model quirks handled below:
    #   - pgrb2 has no upper-level DPT (only RH) -> dpt_from_rh derives
    #     dewpoint via Magnus from TMP + RH at each level.
    #   - the global 0.25 grid at the default TILE_STRIDE would space
    #     tap-anywhere columns ~220 km apart, far outside the app's 60 km
    #     IDW gate -> tile_stride 2 (~55 km) and tile_bbox crops the tile
    #     domain to North America + adjacent oceans so the tile count
    #     stays ~HRRR-sized instead of global.
    {
        "key": "gfs",
        "name": "GFS",
        "prefix": "gfs/",
        "files": [
            "https://noaa-gfs-bdp-pds.s3.amazonaws.com/"
            "gfs.{d}/{h}/atmos/gfs.t{h}z.pgrb2.0p25.f{fh:03d}",
        ],
        "isobaric_vars": ("UGRD", "VGRD", "HGT", "TMP", "RH"),
        "dpt_from_rh": True,
        "tile_stride": 2,
        "stride_km": 55,
        "tile_bbox": (-170.0, 15.0, -50.0, 72.0),  # lonW, latS, lonE, latN
    },
]

# Point-sounding tiles: subsample the 3 km HRRR grid every TILE_STRIDE pixels
# (~24 km column spacing) and chop the subsampled grid into TILE_COLS^2-column
# tiles. Each tile file carries the full vertical columns for every forecast
# hour, so one fetch powers a tap-anywhere sounding *and* its whole scrubber.
# The app inverse-distance-interpolates the nearest columns to the tap point.
#
# TILE_COLS is a pure renderer knob — the app reads whatever tile IDs/bboxes
# tiles.json advertises and picks by containment, so re-chunking the grid
# needs no app change (old and new builds both follow the manifest). At 16, a
# tile holds 256 columns: with 19 hourly forecast hours per column that keeps
# the per-tap download near the old 7-hour/24-col size (~600 KB gzipped). The
# larger file count (~135/model vs ~60) is free on B2 (no per-PUT cost — the
# R2 Class A pricing that the old 24 was sized for is gone).
TILE_STRIDE = 8
TILE_COLS = 16
STRIDE_KM = TILE_STRIDE * 3  # per-model override via MODELS "stride_km"


def classify(var: str, lvl: str):
    if var in ISOBARIC_VARS and lvl.endswith(" mb"):
        try:
            mb = int(lvl.split()[0])
        except ValueError:
            return None
        return f"{var}:{mb}" if mb in LEVELS else None
    if var == "PRES" and lvl == "surface":
        return "PRES:sfc"
    if var == "HGT" and lvl == "surface":
        return "HGT:sfc"
    if var in ("TMP", "DPT") and lvl == "2 m above ground":
        return f"{var}:2m"
    if var in ("UGRD", "VGRD") and lvl == "10 m above ground":
        return f"{var}:10m"
    return None


def _to_c(v: float) -> float:
    """conda-forge gdal returns HRRR TMP/DPT in Celsius already; only subtract
    273.15 when the value is clearly Kelvin (>100). Robust either way."""
    return v - 273.15 if v > 100.0 else v


def _td_from_t_rh(t, rh):
    """Dewpoint (deg C) from temperature + relative humidity via Magnus
    (same 6.112/17.67/243.5 constants as the om td2m derivation). Works on
    floats and numpy arrays; `t` may be Kelvin or Celsius (normalized like
    _to_c). RH clipped to [1, 100] so ln() never sees zero."""
    import numpy as np
    t_c = np.where(np.asarray(t, dtype=float) > 100.0,
                   np.asarray(t, dtype=float) - 273.15,
                   np.asarray(t, dtype=float))
    rh_c = np.clip(np.asarray(rh, dtype=float), 1.0, 100.0)
    ln_term = np.log(rh_c / 100.0) + 17.67 * t_c / (t_c + 243.5)
    td = 243.5 * ln_term / (17.67 - ln_term)
    return float(td) if td.ndim == 0 else td.astype(np.float32)


def _derive_dpt(per_site: list, grids: dict) -> None:
    """Fill DPT:<mb> from TMP+RH for models that publish no upper-level
    dewpoint (GFS pgrb2). Values stored in deg C — _to_c passes them
    through untouched downstream."""
    for d in per_site:
        for mb in LEVELS:
            if f"DPT:{mb}" not in d and f"TMP:{mb}" in d and f"RH:{mb}" in d:
                d[f"DPT:{mb}"] = _td_from_t_rh(d[f"TMP:{mb}"], d[f"RH:{mb}"])
    for mb in LEVELS:
        if f"DPT:{mb}" not in grids and f"TMP:{mb}" in grids and f"RH:{mb}" in grids:
            grids[f"DPT:{mb}"] = _td_from_t_rh(grids[f"TMP:{mb}"], grids[f"RH:{mb}"])


def _head_ok(url: str) -> bool:
    req = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status == 200
    except Exception:
        return False


def _get(url: str, byte_range=None, attempts=4) -> bytes:
    """GET (optionally a byte range) with retry/backoff.

    The NOAA HRRR S3 bucket occasionally drops a response mid-stream
    (http.client.IncompleteRead) or times out. A single hiccup on any one of
    the ~19 forecast-hour fetches used to abort the whole hourly run; retry a
    few times before giving up. Genuine 404s are not retried.
    """
    req = urllib.request.Request(url)
    if byte_range:
        req.add_header("Range", f"bytes={byte_range}")
    last = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=180) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            if e.code == 404:
                raise
            last = e
        except Exception as e:  # IncompleteRead, socket timeout, reset, 5xx
            last = e
        if attempt < attempts - 1:
            time.sleep(2 * (attempt + 1))
    raise last


def grib_urls(model: dict, d: str, h: str, fh: int) -> list[str]:
    return [t.format(d=d, h=h, fh=fh) for t in model["files"]]


def cdn_manifest(prefix: str):
    """The manifest currently served on the CDN (dict), or None. Used to skip
    a re-extract when NOAA hasn't published a new run since the last sweep —
    Worker dispatches are fixed-cadence, not publish-aware. Cache-busted: the
    edge caches manifest.json."""
    url = (f"https://models.dgwaynes.com/v1/soundings/{prefix}manifest.json"
           f"?_={int(time.time())}")
    req = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except Exception:
        return None


def find_latest_run(model: dict):
    """Latest run whose furthest forecast hour (max FHOURS) is published for
    EVERY source file, so all the forecast hours we need exist for a single
    consistent run.

    Lookback starts at 1 h: HRRR wrfprs F18 publishes ~75-90 min after the
    run hour, so by our :50 cron the PREVIOUS hour's run is normally complete
    — starting at 2 h was wasting an hour of freshness on the profile users
    see as "now". Older offsets remain the fallback for late NOAA runs."""
    now = dt.datetime.now(dt.timezone.utc)
    maxfh = max(FHOURS)
    for off in range(1, LOOKBACK_HOURS + 1):
        t = (now - dt.timedelta(hours=off)).replace(minute=0, second=0, microsecond=0)
        d, h = t.strftime("%Y%m%d"), t.strftime("%H")
        if all(_head_ok(u + ".idx") for u in grib_urls(model, d, h, maxfh)):
            return t
    return None


def fetch_subset(url: str, work: Path):
    """Byte-range GET the matching messages -> (grib_path, band_keys)."""
    idx = _get(url + ".idx").decode()
    parsed = []
    for line in idx.splitlines():
        a = line.split(":")
        if len(a) >= 5:
            try:
                parsed.append((int(a[0]), int(a[1]), a[3], a[4]))
            except ValueError:
                pass
    parsed.sort()
    ranges, band_keys = [], []
    for i, (_, off, var, lvl) in enumerate(parsed):
        key = classify(var, lvl)
        if key is None:
            continue
        end = parsed[i + 1][1] - 1 if i + 1 < len(parsed) else ""
        ranges.append(f"{off}-{end}")
        band_keys.append(key)
    if not ranges:
        return None
    work.mkdir(parents=True, exist_ok=True)
    grib = work / "in.grib2"
    # The ~100 selected messages are fetched as individual ranged GETs. Doing
    # them sequentially made S3 round-trip latency the dominant wall-clock
    # cost once FHOURS went hourly (19 hours x ~100 ranges x 2 models); a
    # small thread pool overlaps the latency. Results are written in index
    # order, so the assembled GRIB layout (and band_keys mapping) is
    # byte-identical to the sequential version.
    with ThreadPoolExecutor(max_workers=12) as ex:
        parts = list(ex.map(lambda r: _get(url, r), ranges))
    with open(grib, "wb") as f:
        for part in parts:
            f.write(part)
    return grib, band_keys


def _vals_to_dict(band_keys, vals):
    d = {}
    for key, vs in zip(band_keys, vals):
        try:
            v = float(vs)
        except ValueError:
            continue
        if abs(v) > 9.9e19:  # GRIB missing/fill
            continue
        d[key] = v
    return d


def extract_all(grib: Path, band_keys, sites, want_grid=False):
    """Open the GRIB once and read each band a single time (one decode per
    message, not per-site), then sample every site from the in-memory array.
    ~100x cheaper than per-site gdallocationinfo. Returns {key: value} per
    site; with want_grid=True returns (per_site, grids) where grids is
    {key: float32 array} subsampled every TILE_STRIDE pixels (for the
    point-sounding tiles — same decode pass, no extra I/O)."""
    from osgeo import gdal, osr  # provided by the conda-forge gdal package
    import numpy as np

    ds = gdal.Open(str(grib))
    if ds is None:
        empty = [dict() for _ in sites]
        return (empty, {}) if want_grid else empty
    gt = ds.GetGeoTransform()
    inv = gdal.InvGeoTransform(gt)
    src = osr.SpatialReference()
    src.ImportFromEPSG(4326)
    dst = osr.SpatialReference()
    dst.ImportFromWkt(ds.GetProjection())
    for s in (src, dst):
        try:
            s.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        except Exception:
            pass
    ct = osr.CoordinateTransformation(src, dst)
    w, hgt = ds.RasterXSize, ds.RasterYSize

    pix = []
    for (_, lat, lon) in sites:
        x, y, *_ = ct.TransformPoint(lon, lat)
        px, py = gdal.ApplyGeoTransform(inv, x, y)
        px, py = int(px), int(py)
        pix.append((px, py) if (0 <= px < w and 0 <= py < hgt) else None)

    res = [dict() for _ in sites]
    grids = {}
    nb = min(ds.RasterCount, len(band_keys))
    for i in range(nb):
        arr = ds.GetRasterBand(i + 1).ReadAsArray()  # decode this message once
        if arr is None:  # undecodable message (e.g. missing GRIB codec)
            continue
        key = band_keys[i]
        for j, p in enumerate(pix):
            if p is None:
                continue
            v = float(arr[p[1], p[0]])
            if abs(v) > 9.9e19:  # GRIB missing/fill
                continue
            res[j][key] = v
        if want_grid:
            g = arr[::TILE_STRIDE, ::TILE_STRIDE].astype(np.float32)
            # HRRR prs fields are fully populated over CONUS; sanitize any
            # stray fill so json.dumps never sees NaN/inf.
            bad = np.abs(g) > 9.9e19
            if bad.any():
                g = np.where(bad, np.float32(np.nanmean(np.where(bad, np.nan, g))), g)
            grids[key] = g
    ds = None
    return (res, grids) if want_grid else res


def grid_latlon(grib: Path):
    """(lats, lons) 2D arrays (WGS84 degrees) of the subsampled pixel centers —
    the point-sounding tile column locations. Grid geometry is identical for
    every forecast hour, so this runs once per run."""
    from osgeo import gdal, osr
    import numpy as np

    ds = gdal.Open(str(grib))
    gt = ds.GetGeoTransform()
    w, hgt = ds.RasterXSize, ds.RasterYSize
    src = osr.SpatialReference()
    src.ImportFromWkt(ds.GetProjection())
    dst = osr.SpatialReference()
    dst.ImportFromEPSG(4326)
    for s in (src, dst):
        try:
            s.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
        except Exception:
            pass
    ct = osr.CoordinateTransformation(src, dst)
    xs = np.arange(0, w, TILE_STRIDE) + 0.5
    ys = np.arange(0, hgt, TILE_STRIDE) + 0.5
    lats = np.empty((len(ys), len(xs)), dtype=np.float64)
    lons = np.empty_like(lats)
    for iy, py in enumerate(ys):
        pts = [(gt[0] + px * gt[1] + py * gt[2],
                gt[3] + px * gt[4] + py * gt[5]) for px in xs]
        out = ct.TransformPoints(pts)
        for ix, (lon, lat, *_) in enumerate(out):
            lats[iy, ix] = lat
            lons[iy, ix] = lon
    ds = None
    return lats, lons


def _gdalloc_one(grib: Path, band_keys, lon: float, lat: float):
    """Single-site gdallocationinfo — used only to cross-check the GDAL-Python
    coordinate transform on the runner (gdal is unavailable locally)."""
    try:
        o = subprocess.check_output(
            ["gdallocationinfo", "-valonly", "-wgs84", str(grib), str(lon), str(lat)],
            text=True, stderr=subprocess.STDOUT,
        )
        return _vals_to_dict(band_keys, o.split())
    except subprocess.CalledProcessError:
        return {}


def build_profile(d):
    """Assemble one profile (surface-first), dropping below-ground isobaric
    levels. Returns None if fewer than 4 usable levels."""
    psfc = d.get("PRES:sfc")
    hsfc = d.get("HGT:sfc")
    levels = []
    if psfc is not None and "TMP:2m" in d and "DPT:2m" in d and hsfc is not None:
        sp = psfc / 100.0
        t2 = _to_c(d["TMP:2m"])
        td2 = min(_to_c(d["DPT:2m"]), t2)
        levels.append((sp, hsfc, t2, td2, d.get("UGRD:10m", 0.0), d.get("VGRD:10m", 0.0)))
    for mb in LEVELS:
        if psfc is not None and mb * 100.0 > psfc:  # below ground
            continue
        t = d.get(f"TMP:{mb}")
        td = d.get(f"DPT:{mb}")
        h = d.get(f"HGT:{mb}")
        if t is None or td is None or h is None:
            continue
        tc = _to_c(t)
        levels.append((float(mb), h, tc, min(_to_c(td), tc),
                       d.get(f"UGRD:{mb}", 0.0), d.get(f"VGRD:{mb}", 0.0)))
    if len(levels) < 4:
        return None
    levels.sort(key=lambda x: -x[0])
    return {
        "elev_m": round(hsfc, 1) if hsfc is not None else round(levels[0][1], 1),
        "pres_hpa": [round(L[0], 1) for L in levels],
        "hgt_msl_m": [round(L[1], 1) for L in levels],
        "temp_c": [round(L[2], 2) for L in levels],
        "dewpoint_c": [round(L[3], 2) for L in levels],
        "u_ms": [round(L[4], 2) for L in levels],
        "v_ms": [round(L[5], 2) for L in levels],
    }


def utc_offsets(sites, run_dt) -> dict[str, int]:
    """Per-site UTC offset (seconds) at run_dt, DST-correct.

    timezonefinder maps the radar lat/lon to its IANA zone; stdlib zoneinfo
    then gives the actual offset for run_dt (handling DST, and zones that
    don't observe it like Arizona). Falls back to a whole-hour longitude
    estimate if timezonefinder is unavailable or returns no zone — the app
    applies the same longitude fallback, so this just makes it server-side.
    """
    try:
        from timezonefinder import TimezoneFinder
        tf = TimezoneFinder()
    except Exception as e:  # pragma: no cover - dependency/install guard
        print(f"  [tz] timezonefinder unavailable ({e}); longitude fallback")
        tf = None

    out: dict[str, int] = {}
    misses = 0
    for (sid, lat, lon) in sites:
        off = None
        if tf is not None:
            try:
                zone = tf.timezone_at(lat=lat, lng=lon)
                if zone:
                    off = int(run_dt.astimezone(ZoneInfo(zone))
                              .utcoffset().total_seconds())
            except Exception:
                off = None
        if off is None:
            off = int(round(lon / 15.0)) * 3600  # crude whole-hour fallback
            misses += 1
        out[sid] = off
    print(f"  [tz] resolved {len(sites) - misses}/{len(sites)} site offsets "
          f"({misses} longitude fallback)")
    return out


def grid_utc_offsets(lats, lons, run_dt):
    """Per-column DST-correct UTC offsets (seconds) as an int 2D list; falls
    back to a whole-hour longitude estimate per column on any lookup miss."""
    import numpy as np
    t0 = time.monotonic()
    try:
        from timezonefinder import TimezoneFinder
        tf = TimezoneFinder(in_memory=True)
    except Exception:
        tf = None
    ny, nx = lats.shape
    out = np.empty((ny, nx), dtype=np.int64)
    zone_off: dict[str, int] = {}  # IANA zone -> offset (memoized)
    misses = 0
    for iy in range(ny):
        for ix in range(nx):
            off = None
            if tf is not None:
                try:
                    zone = tf.timezone_at(lat=float(lats[iy, ix]),
                                          lng=float(lons[iy, ix]))
                    if zone:
                        if zone not in zone_off:
                            zone_off[zone] = int(
                                run_dt.astimezone(ZoneInfo(zone))
                                .utcoffset().total_seconds())
                        off = zone_off[zone]
                except Exception:
                    off = None
            if off is None:
                off = int(round(float(lons[iy, ix]) / 15.0)) * 3600
                misses += 1
            out[iy, ix] = off
    print(f"  [tiles] tz offsets for {ny * nx} columns in "
          f"{time.monotonic() - t0:.1f}s ({misses} longitude fallback)")
    return out


def build_tiles(grids_by_hour, lats, lons, offs, run_dt, run_iso, out_dir,
                model="HRRR"):
    """Write point-sounding tile JSONs + tiles.json manifest.

    Tile file shape (all numbers; flat arrays are column-major-by-column,
    i.e. flat[c * nLevels + k] = column c, level k, levels in LEVELS order):
      { model, run, pres_hpa[19], lat[], lon[], elev_m[], utc_offset_seconds[],
        hours: [ { fhour, valid,
                   sfc_pres_hpa[], sfc_temp_c[], sfc_dewpoint_c[],
                   sfc_u_ms[], sfc_v_ms[],
                   temp_c[], dewpoint_c[], hgt_msl_m[], u_ms[], v_ms[] } ] }
    Below-ground isobaric values are NOT filtered here — each column keeps all
    19 levels and the app drops levels with p > that column's sfc pressure
    after interpolating, mirroring build_profile's semantics.
    """
    import numpy as np

    def to_c(a):  # vectorized _to_c (Kelvin-vs-Celsius guard)
        return np.where(a > 100.0, a - 273.15, a)

    def rnd(a, nd):
        # Round in float64: rounding float32 then .tolist() yields doubles
        # like 24.100000381469727, and json.dumps writes every digit (~2.4x
        # file size). float64 round -> exact short repr ("24.1").
        return np.asarray(a, dtype=np.float64).round(nd).ravel().tolist()

    ny, nx = lats.shape
    var_map = {"temp_c": "TMP", "dewpoint_c": "DPT", "hgt_msl_m": "HGT",
               "u_ms": "UGRD", "v_ms": "VGRD"}
    hour_stacks = {}   # fh -> {json_key: array (ny, nx, 19) or None}
    for fh, grids in grids_by_hour.items():
        stacks = {}
        for jk, var in var_map.items():
            bands = []
            for mb in LEVELS:
                g = grids.get(f"{var}:{mb}")
                if g is None:
                    bands = None
                    break
                bands.append(g)
            stacks[jk] = None if bands is None else np.stack(bands, axis=-1)
        hour_stacks[fh] = stacks

    # Any hour missing a full stack is dropped from the tiles entirely (the
    # scrubber hour list is per-file, so partial hours would desync tiles).
    sfc_keys = ("PRES:sfc", "HGT:sfc", "TMP:2m", "DPT:2m",
                "UGRD:10m", "VGRD:10m")
    good_hours = [fh for fh in sorted(grids_by_hour)
                  if all(s is not None for s in hour_stacks[fh].values())
                  and all(k in grids_by_hour[fh] for k in sfc_keys)]
    if not good_hours:
        print("  [tiles] no complete hours; skipping tile output")
        return 0
    f0g = grids_by_hour[good_hours[0]]

    tiles_dir = out_dir / "tiles"
    tiles_dir.mkdir()
    # Bbox padding: bboxes come from column CENTERS, so unpadded neighbors
    # leave a seam between the last column of one tile and the first of the
    # next that neither bbox contains. Pad by 0.75x the measured column
    # spacing (data-driven — covers the half-spacing seam with margin at any
    # stride/projection); the app picks the nearest-center tile among the
    # overlapping candidates.
    pad_lat = 0.75 * float(np.abs(np.diff(lats, axis=0)).max())
    pad_lon = 0.75 * float(np.abs(np.diff(lons, axis=1)).max())
    index = []
    n = 0
    for ty0 in range(0, ny, TILE_COLS):
        for tx0 in range(0, nx, TILE_COLS):
            ty1, tx1 = min(ty0 + TILE_COLS, ny), min(tx0 + TILE_COLS, nx)
            sl = (slice(ty0, ty1), slice(tx0, tx1))
            tid = f"{tx0 // TILE_COLS}_{ty0 // TILE_COLS}"
            tlat, tlon = lats[sl], lons[sl]
            hours = []
            for fh in good_hours:
                g = grids_by_hour[fh]
                st = hour_stacks[fh]
                t2 = to_c(g["TMP:2m"][sl])
                td2 = np.minimum(to_c(g["DPT:2m"][sl]), t2)
                hours.append({
                    "fhour": fh,
                    "valid": (run_dt + dt.timedelta(hours=fh)).isoformat(),
                    "sfc_pres_hpa": rnd(g["PRES:sfc"][sl] / 100.0, 1),
                    "sfc_temp_c": rnd(t2, 1),
                    "sfc_dewpoint_c": rnd(td2, 1),
                    "sfc_u_ms": rnd(g["UGRD:10m"][sl], 1),
                    "sfc_v_ms": rnd(g["VGRD:10m"][sl], 1),
                    "temp_c": rnd(to_c(st["temp_c"][sl]), 1),
                    # Td clamped to T like build_profile.
                    "dewpoint_c": rnd(np.minimum(to_c(st["dewpoint_c"][sl]),
                                                 to_c(st["temp_c"][sl])), 1),
                    "hgt_msl_m": [int(v) for v in
                                  rnd(st["hgt_msl_m"][sl], 0)],
                    "u_ms": rnd(st["u_ms"][sl], 1),
                    "v_ms": rnd(st["v_ms"][sl], 1),
                })
            (tiles_dir / f"{tid}.json").write_text(json.dumps({
                "model": model, "run": run_iso,
                "pres_hpa": [float(mb) for mb in LEVELS],
                "lat": rnd(tlat, 4),
                "lon": rnd(tlon, 4),
                "elev_m": rnd(f0g["HGT:sfc"][sl], 1),
                "utc_offset_seconds": offs[sl].ravel().tolist(),
                "hours": hours,
            }, separators=(",", ":")))
            index.append({
                "id": tid,
                "minLat": round(float(tlat.min()) - pad_lat, 3),
                "maxLat": round(float(tlat.max()) + pad_lat, 3),
                "minLon": round(float(tlon.min()) - pad_lon, 3),
                "maxLon": round(float(tlon.max()) + pad_lon, 3),
            })
            n += 1
    (out_dir / "tiles.json").write_text(json.dumps({
        "schemaVersion": 1, "model": model, "run": run_iso,
        "strideKm": STRIDE_KM, "hours": good_hours,
        "tiles": index,
    }, separators=(",", ":")))
    sizes = sorted(p.stat().st_size for p in tiles_dir.glob("*.json"))
    print(f"  [tiles] wrote {n} tiles ({len(good_hours)} hours), raw size "
          f"min/med/max = {sizes[0] // 1024}/{sizes[len(sizes) // 2] // 1024}/"
          f"{sizes[-1] // 1024} KB")
    return n


def process_model(model: dict, sites, out_root: Path, work_root: Path) -> int:
    """Extract one model end-to-end into out_root/<prefix>. Returns the number
    of site profiles written; 0 = model failed / nothing published; -1 = the
    CDN already serves the newest run (healthy no-op, manifest touched)."""
    name = model["name"]
    # Per-model knobs land in the module globals the helpers read
    # (classify -> ISOBARIC_VARS, extract_all/grid_latlon -> TILE_STRIDE,
    # build_tiles -> STRIDE_KM). Single-threaded, set fresh per model.
    global ISOBARIC_VARS, TILE_STRIDE, STRIDE_KM
    ISOBARIC_VARS = tuple(model.get("isobaric_vars", ("UGRD", "VGRD", "HGT", "TMP", "DPT")))
    TILE_STRIDE = int(model.get("tile_stride", 8))
    STRIDE_KM = int(model.get("stride_km", TILE_STRIDE * 3))
    run_dt = find_latest_run(model)
    if run_dt is None:
        print(f"==> {name}: no run with all forecast hours published; skip")
        return 0
    d, h = run_dt.strftime("%Y%m%d"), run_dt.strftime("%H")
    run_iso = run_dt.isoformat()

    # Idempotent skip: the newest complete run is already on the CDN (NOAA
    # late, or an extra dispatch). Touch generatedAt so the freshness monitor
    # measures OUR liveness rather than NOAA's publish cadence, and stage
    # only the manifest.
    cdn = cdn_manifest(model["prefix"])
    if (cdn is not None and cdn.get("run") == run_iso
            and cdn.get("forecastHours") == FHOURS):
        out_dir = out_root / model["prefix"] if model["prefix"] else out_root
        out_dir.mkdir(parents=True, exist_ok=True)
        cdn["generatedAt"] = dt.datetime.now(dt.timezone.utc).isoformat()
        (out_dir / "manifest.json").write_text(json.dumps(cdn))
        print(f"==> {name}: CDN already serves run {run_iso}; "
              "manifest touched, extract skipped")
        return -1

    print(f"==> {name} run {run_dt:%Y%m%d%H}Z, forecast hours {FHOURS}")

    offs = utc_offsets(sites, run_dt)

    # site -> {fhour: profile}
    by_site: dict[str, dict[int, dict]] = {sid: {} for (sid, _, _) in sites}
    grids_by_hour: dict[int, dict] = {}   # point-sounding tile inputs
    lats = lons = None
    for fh in FHOURS:
        # A model's fields may span several files (RRFS: prslev isobaric +
        # 2dfld surface); classify() only matches each file's relevant
        # messages, so fetch each and merge the band dicts.
        merged: list[dict] = [dict() for _ in sites]
        grids: dict = {}
        for i, url in enumerate(grib_urls(model, d, h, fh)):
            sub = fetch_subset(url, work_root / model["key"] / f"f{fh:02d}_{i}")
            if sub is None:
                continue
            grib, band_keys = sub
            dicts, g = extract_all(grib, band_keys, sites, want_grid=True)
            for md, dd in zip(merged, dicts):
                md.update(dd)
            if g:
                shapes = {a.shape for a in g.values()}
                have = {a.shape for a in grids.values()}
                if grids and shapes != have:
                    # Source files on different grids can't share one tile
                    # array — drop this file's grid (site extraction above is
                    # per-file pixel-indexed and unaffected).
                    print(f"  [tiles] {name} F{fh:02d}: grid shape mismatch "
                          f"{shapes} vs {have}; skipping grid merge")
                else:
                    grids.update(g)
            if lats is None:
                lats, lons = grid_latlon(grib)
                if model.get("tile_bbox") is not None:
                    import numpy as np
                    w0, s0, e0, n0 = model["tile_bbox"]
                    lon_n = np.where(lons > 180.0, lons - 360.0, lons)
                    inside = (lats >= s0) & (lats <= n0) & (lon_n >= w0) & (lon_n <= e0)
                    ys = np.where(inside.any(axis=1))[0]
                    xs2 = np.where(inside.any(axis=0))[0]
                    crop = (slice(ys.min(), ys.max() + 1),
                            slice(xs2.min(), xs2.max() + 1))
                    lats, lons = lats[crop], lon_n[crop]
                    model["_crop"] = crop
                    print(f"  [tiles] {name}: cropped tile grid to "
                          f"{lats.shape} for bbox {model['tile_bbox']}")
            grib.unlink(missing_ok=True)
        if model.get("dpt_from_rh"):
            _derive_dpt(merged, grids)
        if model.get("_crop") is not None and grids:
            c = model["_crop"]
            grids = {k: a[c] for k, a in grids.items()}
        if grids:
            grids_by_hour[fh] = grids
        if fh == 0 and merged:
            # Spot-check line (the WGS84→grid transform itself was validated
            # against gdallocationinfo when the osgeo path landed).
            print(f"  [check] {sites[0][0]}: TMP2m={merged[0].get('TMP:2m')} "
                  f"PRESsfc={merged[0].get('PRES:sfc')} "
                  f"TMP500={merged[0].get('TMP:500')}")
        n = 0
        for (sid, _, _), dd in zip(sites, merged):
            prof = build_profile(dd)
            if prof is not None:
                by_site[sid][fh] = prof
                n += 1
        print(f"  F{fh:02d}: {n}/{len(sites)} site profiles")

    out_dir = out_root / model["prefix"] if model["prefix"] else out_root
    out_dir.mkdir(parents=True, exist_ok=True)
    n_now = n_fc = 0
    coord = {sid: (lat, lon) for (sid, lat, lon) in sites}
    for sid, hours in by_site.items():
        if 0 not in hours:  # F00 is required (back-compat + scrubber anchor)
            continue
        lat, lon = coord[sid]
        f0 = hours[0]
        # Back-compat single-profile file (dealiaser + app default).
        (out_dir / f"{sid}.json").write_text(json.dumps(
            {"site": sid, "model": name, "run": run_iso, "lat": lat, "lon": lon,
             "utc_offset_seconds": offs[sid], **f0},
            separators=(",", ":")))
        n_now += 1
        # Forecast file with every hour.
        hrs = []
        for fh in sorted(hours):
            p = hours[fh]
            hrs.append({
                "fhour": fh,
                "valid": (run_dt + dt.timedelta(hours=fh)).isoformat(),
                "pres_hpa": p["pres_hpa"], "hgt_msl_m": p["hgt_msl_m"],
                "temp_c": p["temp_c"], "dewpoint_c": p["dewpoint_c"],
                "u_ms": p["u_ms"], "v_ms": p["v_ms"],
            })
        (out_dir / f"{sid}.fc.json").write_text(json.dumps(
            {"site": sid, "model": name, "run": run_iso, "lat": lat, "lon": lon,
             "utc_offset_seconds": offs[sid], "elev_m": f0["elev_m"], "hours": hrs},
            separators=(",", ":")))
        n_fc += 1

    if n_now == 0:
        print(f"  {name}: extracted 0 site profiles")
        return 0
    print(f"  wrote {n_now} <SID>.json + {n_fc} <SID>.fc.json")

    # Point-sounding tiles (tap-anywhere soundings).
    n_tiles = 0
    if grids_by_hour and lats is not None:
        tile_offs = grid_utc_offsets(lats, lons, run_dt)
        n_tiles = build_tiles(
            grids_by_hour, lats, lons, tile_offs, run_dt, run_iso, out_dir,
            model=name)

    manifest = {
        "schemaVersion": 3,
        "model": name,
        "run": run_iso,
        "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "levelsMb": LEVELS,
        "forecastHours": FHOURS,
        "sites": n_now,
        "pointTiles": n_tiles,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest))
    print(f"  {name}: {n_now} sites + {n_tiles} tiles staged")
    return n_now


def main() -> int:
    sites = []
    with open(SITES_CSV) as f:
        for row in csv.DictReader(f):
            sites.append((row["id"], float(row["lat"]), float(row["lon"])))

    work = Path(tempfile.mkdtemp())
    out_root = work / "out"
    out_root.mkdir()

    # A failure in one model (e.g. an rrfs_a path change at the operational
    # cutover) must not take down the other's hourly refresh.
    results: dict[str, int] = {}
    for model in MODELS:
        try:
            results[model["key"]] = process_model(model, sites, out_root, work)
        except Exception as e:
            print(f"==> {model['name']} FAILED: {e!r}; continuing")
            results[model["key"]] = 0

    if all(v == 0 for v in results.values()):
        print("no model produced any profiles; exit 1")
        return 1

    # Upload whatever was staged — full extracts and/or touched manifests
    # from idempotent skips (the skip path stages only manifest.json).
    subprocess.check_call([
        "rclone", "copy", str(out_root), f"r2:{BUCKET}/v1/soundings/",
        "--s3-no-check-bucket", "--no-traverse",
        "--header-upload", "Cache-Control: public, max-age=600",
    ])
    print("==> uploaded soundings to v1/soundings/ — " +
          ", ".join(f"{k}: {'fresh (skip)' if v < 0 else f'{v} sites'}"
                    for k, v in results.items()))
    # HRRR is the load-bearing output (dealiaser + app default): flag its
    # absence as a failure even when RRFS succeeded. -1 (already current)
    # counts as success.
    return 0 if results.get("hrrr", 0) != 0 else 1


if __name__ == "__main__":
    sys.exit(main())
