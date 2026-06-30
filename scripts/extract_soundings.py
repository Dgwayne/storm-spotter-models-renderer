#!/usr/bin/env python3
"""extract_soundings.py - HRRR vertical profiles per NEXRAD site -> R2.

For every CONUS NEXRAD radar in config/nexrad_sites.csv, pull the HRRR
temperature, dewpoint, geopotential height and wind at isobaric levels
(plus a 2 m / 10 m surface level) for a set of forecast hours, interpolate
to the radar point, and upload per-site JSON to R2.

Outputs (per site SID):
  * v1/soundings/<SID>.json     - the F00 analysis profile only. Back-compat
    shape for the velocity dealiaser (hgt_msl_m / u_ms / v_ms) and the app's
    single-profile fetch.
  * v1/soundings/<SID>.fc.json  - all forecast hours, for the app's forecast
    scrubber: {run, elev_m, hours:[{fhour, valid, pres_hpa, temp_c, ...}, ...]}.

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
from pathlib import Path
from zoneinfo import ZoneInfo

REPO_ROOT = Path(__file__).resolve().parent.parent
SITES_CSV = REPO_ROOT / "config" / "nexrad_sites.csv"

S3_BUCKET = "noaa-hrrr-bdp-pds"
LEVELS = list(range(1000, 99, -50))  # 1000,950,...,150,100  (19 levels)
ISOBARIC_VARS = ("UGRD", "VGRD", "HGT", "TMP", "DPT")
# Forecast hours to emit (3-hourly out to +18 h).
FHOURS = [0, 3, 6, 9, 12, 15, 18]
LOOKBACK_HOURS = 8
BUCKET = os.environ["R2_BUCKET"]


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
    the ~7 forecast-hour fetches used to abort the whole hourly run; retry a
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


def grib_url(d: str, h: str, fh: int) -> str:
    return (
        f"https://{S3_BUCKET}.s3.amazonaws.com/"
        f"hrrr.{d}/conus/hrrr.t{h}z.wrfprsf{fh:02d}.grib2"
    )


def find_latest_run():
    """Latest run whose furthest forecast hour (max FHOURS) is published, so
    every forecast hour we need exists for a single consistent run."""
    now = dt.datetime.now(dt.timezone.utc)
    maxfh = max(FHOURS)
    for off in range(2, LOOKBACK_HOURS + 1):
        t = (now - dt.timedelta(hours=off)).replace(minute=0, second=0, microsecond=0)
        d, h = t.strftime("%Y%m%d"), t.strftime("%H")
        if _head_ok(grib_url(d, h, maxfh) + ".idx"):
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
    with open(grib, "wb") as f:
        for r in ranges:
            f.write(_get(url, r))
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


def extract_all(grib: Path, band_keys, sites):
    """Open the GRIB once and read each band a single time (one decode per
    message, not per-site), then sample every site from the in-memory array.
    ~100x cheaper than per-site gdallocationinfo. Returns {key: value} per
    site."""
    from osgeo import gdal, osr  # provided by the conda-forge gdal package

    ds = gdal.Open(str(grib))
    if ds is None:
        return [dict() for _ in sites]
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
    nb = min(ds.RasterCount, len(band_keys))
    for i in range(nb):
        arr = ds.GetRasterBand(i + 1).ReadAsArray()  # decode this message once
        key = band_keys[i]
        for j, p in enumerate(pix):
            if p is None:
                continue
            v = float(arr[p[1], p[0]])
            if abs(v) > 9.9e19:  # GRIB missing/fill
                continue
            res[j][key] = v
    ds = None
    return res


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


def main() -> int:
    run_dt = find_latest_run()
    if run_dt is None:
        print("no HRRR run with all forecast hours published; exit 0")
        return 0
    d, h = run_dt.strftime("%Y%m%d"), run_dt.strftime("%H")
    run_iso = run_dt.isoformat()
    print(f"==> HRRR run {run_dt:%Y%m%d%H}Z, forecast hours {FHOURS}")

    sites = []
    with open(SITES_CSV) as f:
        for row in csv.DictReader(f):
            sites.append((row["id"], float(row["lat"]), float(row["lon"])))

    offs = utc_offsets(sites, run_dt)

    # site -> {fhour: profile}
    by_site: dict[str, dict[int, dict]] = {sid: {} for (sid, _, _) in sites}
    work = Path(tempfile.mkdtemp())
    for fh in FHOURS:
        sub = fetch_subset(grib_url(d, h, fh), work / f"f{fh:02d}")
        if sub is None:
            print(f"  F{fh:02d}: no matching messages; skip")
            continue
        grib, band_keys = sub
        dicts = extract_all(grib, band_keys, sites)
        if fh == 0 and dicts:
            r = _gdalloc_one(grib, band_keys, sites[0][2], sites[0][1])
            print(f"  [check] {sites[0][0]}: osgeo TMP2m={dicts[0].get('TMP:2m')} "
                  f"PRESsfc={dicts[0].get('PRES:sfc')} | gdalloc TMP2m={r.get('TMP:2m')} "
                  f"PRESsfc={r.get('PRES:sfc')}")
        n = 0
        for (sid, _, _), dd in zip(sites, dicts):
            prof = build_profile(dd)
            if prof is not None:
                by_site[sid][fh] = prof
                n += 1
        print(f"  F{fh:02d}: {n}/{len(sites)} site profiles")
        grib.unlink(missing_ok=True)

    out_dir = work / "out"
    out_dir.mkdir()
    n_now = n_fc = 0
    coord = {sid: (lat, lon) for (sid, lat, lon) in sites}
    for sid, hours in by_site.items():
        if 0 not in hours:  # F00 is required (back-compat + scrubber anchor)
            continue
        lat, lon = coord[sid]
        f0 = hours[0]
        # Back-compat single-profile file (dealiaser + app default).
        (out_dir / f"{sid}.json").write_text(json.dumps(
            {"site": sid, "model": "HRRR", "run": run_iso, "lat": lat, "lon": lon,
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
            {"site": sid, "model": "HRRR", "run": run_iso, "lat": lat, "lon": lon,
             "utc_offset_seconds": offs[sid], "elev_m": f0["elev_m"], "hours": hrs},
            separators=(",", ":")))
        n_fc += 1

    if n_now == 0:
        print("extracted 0 site profiles; exit 1")
        return 1
    print(f"  wrote {n_now} <SID>.json + {n_fc} <SID>.fc.json")

    manifest = {
        "schemaVersion": 3,
        "model": "HRRR",
        "run": run_iso,
        "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "levelsMb": LEVELS,
        "forecastHours": FHOURS,
        "sites": n_now,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest))

    subprocess.check_call([
        "rclone", "copy", str(out_dir), f"r2:{BUCKET}/v1/soundings/",
        "--s3-no-check-bucket", "--no-traverse",
        "--header-upload", "Cache-Control: public, max-age=600",
    ])
    print(f"==> uploaded {n_now} soundings (+forecast) + manifest to v1/soundings/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
