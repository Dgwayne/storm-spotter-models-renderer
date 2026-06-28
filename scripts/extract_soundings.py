#!/usr/bin/env python3
"""extract_soundings.py - HRRR vertical profile per NEXRAD site -> R2.

For every CONUS NEXRAD radar in config/nexrad_sites.csv, pull the HRRR
analysis temperature, dewpoint, geopotential height and wind at isobaric
levels (plus a 2 m / 10 m surface level), interpolate to the radar point,
and upload one small JSON to R2 at v1/soundings/<SITE>.json.

Two consumers:
  * The velocity dealiaser uses the wind (hgt_msl_m / u_ms / v_ms) as a
    trusted reference profile to unfold aliased Doppler velocity.
  * The Soundings feature (skew-T / hodograph / analysis) uses the full
    profile (pres_hpa / temp_c / dewpoint_c + wind).

Reads the HRRR wrfprs 3D pressure file (25 mb levels) via the .idx
byte-range trick - only the ~100 matching messages are downloaded, not the
whole GRIB. Point extraction uses gdal (gdallocationinfo), not wgrib2 -lon,
because the conda-forge wgrib2 build mishandles the HRRR Lambert Conformal
geolocation. $0/month on the existing Actions cron + R2 setup.

Env: R2_BUCKET, R2_ENDPOINT, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY
Tools: gdal (gdallocationinfo), rclone, python3 (stdlib only)
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SITES_CSV = REPO_ROOT / "config" / "nexrad_sites.csv"

S3_BUCKET = "noaa-hrrr-bdp-pds"
# Isobaric levels to pull (mb). 50 mb spacing from 1000->100 keeps a clean
# skew-T while limiting the number of GRIB messages we decode per site.
LEVELS = list(range(1000, 99, -50))  # 1000,950,...,150,100  (19 levels)
ISOBARIC_VARS = ("UGRD", "VGRD", "HGT", "TMP", "DPT")
LOOKBACK_HOURS = 6
BUCKET = os.environ["R2_BUCKET"]


def classify(var: str, lvl: str) -> "str | None":
    """Map a GRIB message (var, level) to a stable key, or None to skip."""
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


def _head_ok(url: str) -> bool:
    req = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status == 200
    except Exception:
        return False


def _get(url: str, byte_range=None) -> bytes:
    req = urllib.request.Request(url)
    if byte_range:
        req.add_header("Range", f"bytes={byte_range}")
    with urllib.request.urlopen(req, timeout=180) as r:
        return r.read()


def find_latest_run():
    now = dt.datetime.now(dt.timezone.utc)
    for off in range(1, LOOKBACK_HOURS + 1):
        t = (now - dt.timedelta(hours=off)).replace(minute=0, second=0, microsecond=0)
        d, h = t.strftime("%Y%m%d"), t.strftime("%H")
        url = (
            f"https://{S3_BUCKET}.s3.amazonaws.com/"
            f"hrrr.{d}/conus/hrrr.t{h}z.wrfprsf00.grib2"
        )
        if _head_ok(url + ".idx"):
            return url, t
    return None


def fetch_subset(grib_url: str, work: Path):
    """Byte-range GET the matching messages. Returns (grib_path, band_keys)
    where band_keys[i] is the key for GRIB band i+1, in file order."""
    idx = _get(grib_url + ".idx").decode()
    parsed = []
    for line in idx.splitlines():
        a = line.split(":")
        if len(a) >= 5:
            try:
                parsed.append((int(a[0]), int(a[1]), a[3], a[4]))
            except ValueError:
                pass
    parsed.sort()
    ranges = []
    band_keys = []
    for i, (_, off, var, lvl) in enumerate(parsed):
        key = classify(var, lvl)
        if key is None:
            continue
        end = parsed[i + 1][1] - 1 if i + 1 < len(parsed) else ""
        ranges.append(f"{off}-{end}")
        band_keys.append(key)
    if not ranges:
        return None
    grib = work / "in.grib2"
    with open(grib, "wb") as f:
        for r in ranges:
            f.write(_get(grib_url, r))
    print(f"  fetched {grib.stat().st_size} bytes, {len(ranges)} messages")
    return grib, band_keys


def extract_site(grib: Path, band_keys, lon: float, lat: float, debug: bool = False):
    """gdallocationinfo point extraction -> {key: value}."""
    try:
        out = subprocess.check_output(
            ["gdallocationinfo", "-valonly", "-wgs84", str(grib), str(lon), str(lat)],
            text=True,
            stderr=subprocess.STDOUT,
        )
    except subprocess.CalledProcessError as e:
        if debug:
            print(f"  [debug] gdallocationinfo failed: {e.output!r}")
        return {}
    vals = out.split()
    if debug:
        print(f"  [debug] lon={lon} lat={lat} -> {len(vals)} vals for {len(band_keys)} bands")
    d = {}
    for key, vs in zip(band_keys, vals):
        try:
            val = float(vs)
        except ValueError:
            continue
        if abs(val) > 9.9e19:  # GRIB missing/fill
            continue
        d[key] = val
    return d


def _to_c(v: float) -> float:
    """Temperature to Celsius. The conda-forge gdal GRIB driver returns HRRR
    TMP/DPT already in Celsius (not Kelvin), so only subtract 273.15 when the
    value is clearly Kelvin (atmospheric K is always > 100; C is always < 100).
    Robust to either gdal behaviour."""
    return v - 273.15 if v > 100.0 else v


def build_profile(d):
    """Assemble the profile (surface-first), dropping below-ground isobaric
    levels. Returns None if fewer than 4 usable levels."""
    psfc = d.get("PRES:sfc")          # Pa
    hsfc = d.get("HGT:sfc")           # m
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
        levels.append((
            float(mb),
            h,
            tc,
            min(_to_c(td), tc),
            d.get(f"UGRD:{mb}", 0.0),
            d.get(f"VGRD:{mb}", 0.0),
        ))

    if len(levels) < 4:
        return None

    levels.sort(key=lambda x: -x[0])  # surface (highest pressure) first
    elev = round(hsfc, 1) if hsfc is not None else round(levels[0][1], 1)
    return {
        "elev_m": elev,
        "pres_hpa": [round(L[0], 1) for L in levels],
        "hgt_msl_m": [round(L[1], 1) for L in levels],
        "temp_c": [round(L[2], 2) for L in levels],
        "dewpoint_c": [round(L[3], 2) for L in levels],
        "u_ms": [round(L[4], 2) for L in levels],
        "v_ms": [round(L[5], 2) for L in levels],
    }


def main() -> int:
    run = find_latest_run()
    if run is None:
        print("no published HRRR wrfprs f00 found in lookback window; exit 0")
        return 0
    grib_url, run_dt = run
    run_iso = run_dt.isoformat()
    print(f"==> HRRR run {run_dt:%Y%m%d%H}Z: {grib_url}")

    work = Path(tempfile.mkdtemp())
    sub = fetch_subset(grib_url, work)
    if sub is None:
        print("no matching messages in idx; exit 0")
        return 0
    grib, band_keys = sub

    sites = []
    with open(SITES_CSV) as f:
        for row in csv.DictReader(f):
            sites.append((row["id"], float(row["lat"]), float(row["lon"])))

    out_dir = work / "out"
    out_dir.mkdir()
    n_ok = 0
    n_levels_total = 0
    for idx_site, (sid, lat, lon) in enumerate(sites):
        d = extract_site(grib, band_keys, lon, lat, debug=(idx_site == 0))
        prof = build_profile(d)
        if prof is None:
            continue
        payload = {
            "site": sid,
            "model": "HRRR",
            "run": run_iso,
            "lat": lat,
            "lon": lon,
        }
        payload.update(prof)
        (out_dir / f"{sid}.json").write_text(json.dumps(payload, separators=(",", ":")))
        n_ok += 1
        n_levels_total += len(prof["pres_hpa"])

    if n_ok == 0:
        print("extracted 0 site profiles - see [debug] above; exit 1")
        return 1
    print(f"  built {n_ok}/{len(sites)} site profiles ({n_levels_total / n_ok:.1f} levels avg)")

    manifest = {
        "schemaVersion": 2,
        "model": "HRRR",
        "run": run_iso,
        "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "levelsMb": LEVELS,
        "sites": n_ok,
    }
    (out_dir / "manifest.json").write_text(json.dumps(manifest))

    subprocess.check_call(
        [
            "rclone",
            "copy",
            str(out_dir),
            f"r2:{BUCKET}/v1/soundings/",
            "--s3-no-check-bucket",
            "--no-traverse",
            "--header-upload",
            "Cache-Control: public, max-age=600",
        ]
    )
    print(f"==> uploaded {n_ok} soundings + manifest to v1/soundings/")
    return 0


if __name__ == "__main__":
    sys.exit(main())
