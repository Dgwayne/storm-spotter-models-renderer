#!/usr/bin/env python3
"""extract_soundings.py — HRRR wind profile per NEXRAD site -> R2.

For every CONUS NEXRAD radar in config/nexrad_sites.csv, pull the HRRR
analysis wind (UGRD/VGRD) and geopotential height (HGT) at the mandatory
isobaric levels, interpolate to the radar point, and upload one tiny JSON
to R2 at v1/soundings/<SITE>.json.

The Storm Spotter Tools Pro velocity dealiaser fetches these as a trusted
*reference wind* (independent of the aliased Doppler data) to unfold
far-range / low-Nyquist velocity correctly — the part a sounding fixes
that a self-contained VAD profile cannot.

Reuses the renderer's pattern: public NOAA HRRR S3 + the .idx byte-range
trick (download only the ~16 matching messages, not the whole GRIB), and
rclone -> Cloudflare R2. Point extraction uses gdal (gdallocationinfo),
not wgrib2 -lon, because the conda-forge wgrib2 build mishandles HRRR's
Lambert Conformal grid geolocation (same reason decode_pipeline.sh reads
GRIB with gdal). $0/month on the existing Actions cron + R2 setup.

Env: R2_BUCKET, R2_ENDPOINT, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY
Tools: gdal (gdallocationinfo), rclone, python3 (stdlib only)
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SITES_CSV = REPO_ROOT / "config" / "nexrad_sites.csv"

S3_BUCKET = "noaa-hrrr-bdp-pds"
# Mandatory isobaric levels carried in the HRRR 2D (wrfsfcf) file — the
# same file `wind500` already reads. ~0–10 km; coarse but plenty to
# anchor the global fold direction the dealiaser needs.
LEVELS = [1000, 925, 850, 700, 500, 250]
MATCH = re.compile(r":(UGRD|VGRD|HGT):(%s) mb:" % "|".join(map(str, LEVELS)))
LOOKBACK_HOURS = 6
BUCKET = os.environ["R2_BUCKET"]


def _head_ok(url: str) -> bool:
    req = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status == 200
    except Exception:
        return False


def _get(url: str, byte_range: str | None = None) -> bytes:
    req = urllib.request.Request(url)
    if byte_range:
        req.add_header("Range", f"bytes={byte_range}")
    with urllib.request.urlopen(req, timeout=120) as r:
        return r.read()


def find_latest_run() -> tuple[str, dt.datetime] | None:
    now = dt.datetime.now(dt.timezone.utc)
    for off in range(1, LOOKBACK_HOURS + 1):
        t = (now - dt.timedelta(hours=off)).replace(minute=0, second=0, microsecond=0)
        d, h = t.strftime("%Y%m%d"), t.strftime("%H")
        url = (
            f"https://{S3_BUCKET}.s3.amazonaws.com/"
            f"hrrr.{d}/conus/hrrr.t{h}z.wrfsfcf00.grib2"
        )
        if _head_ok(url + ".idx"):
            return url, t
    return None


def fetch_subset(grib_url: str, work: Path) -> tuple[Path, list[tuple[str, int]]] | None:
    """Byte-range GET the matching messages. Returns (grib_path, band_meta)
    where band_meta[i] = (var, level_mb) for GRIB band i+1, in file order
    (== the order gdal enumerates bands)."""
    idx = _get(grib_url + ".idx").decode()
    parsed = []
    for line in idx.splitlines():
        a = line.split(":")
        if len(a) >= 5:
            try:
                parsed.append((int(a[0]), int(a[1]), a[3], a[4], line))
            except ValueError:
                pass
    parsed.sort()
    ranges: list[str] = []
    band_meta: list[tuple[str, int]] = []
    for i, (_, off, var, lvl, line) in enumerate(parsed):
        if MATCH.search(line):
            end = parsed[i + 1][1] - 1 if i + 1 < len(parsed) else ""
            ranges.append(f"{off}-{end}")
            band_meta.append((var, int(lvl.split()[0])))  # "500 mb" -> 500
    if not ranges:
        return None
    grib = work / "in.grib2"
    with open(grib, "wb") as f:
        for r in ranges:
            f.write(_get(grib_url, r))
    print(f"  fetched {grib.stat().st_size} bytes, {len(ranges)} messages")
    print(f"  bands: {band_meta}")
    return grib, band_meta


def extract_site(
    grib: Path, band_meta: list[tuple[str, int]], lon: float, lat: float, debug: bool = False
) -> dict[int, dict[str, float]]:
    """gdallocationinfo point extraction -> {level_mb: {UGRD,VGRD,HGT}}."""
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
        print(f"  [debug] lon={lon} lat={lat} -> {len(vals)} vals: {vals[:6]}...")
    prof: dict[int, dict[str, float]] = {}
    for (var, lvl), vs in zip(band_meta, vals):
        try:
            val = float(vs)
        except ValueError:
            continue
        if abs(val) > 9.9e19:
            continue
        prof.setdefault(lvl, {})[var] = val
    return prof


def main() -> int:
    run = find_latest_run()
    if run is None:
        print("no published HRRR f00 found in lookback window; exit 0")
        return 0
    grib_url, run_dt = run
    run_iso = run_dt.isoformat()
    print(f"==> HRRR run {run_dt:%Y%m%d%H}Z: {grib_url}")

    work = Path(tempfile.mkdtemp())
    sub = fetch_subset(grib_url, work)
    if sub is None:
        print("no matching messages in idx; exit 0")
        return 0
    grib, band_meta = sub

    sites = []
    with open(SITES_CSV) as f:
        for row in csv.DictReader(f):
            sites.append((row["id"], float(row["lat"]), float(row["lon"])))

    out_dir = work / "out"
    out_dir.mkdir()
    n_ok = 0
    n_levels_total = 0
    for idx_site, (sid, lat, lon) in enumerate(sites):
        prof = extract_site(grib, band_meta, lon, lat, debug=(idx_site == 0))
        levels = sorted(
            lvl for lvl, d in prof.items() if {"UGRD", "VGRD", "HGT"} <= d.keys()
        )
        if len(levels) < 2:
            continue
        payload = {
            "site": sid,
            "model": "HRRR",
            "run": run_iso,
            "levels_mb": levels,
            "hgt_msl_m": [round(prof[l]["HGT"], 1) for l in levels],
            "u_ms": [round(prof[l]["UGRD"], 2) for l in levels],
            "v_ms": [round(prof[l]["VGRD"], 2) for l in levels],
        }
        (out_dir / f"{sid}.json").write_text(json.dumps(payload, separators=(",", ":")))
        n_ok += 1
        n_levels_total += len(levels)

    if n_ok == 0:
        print("extracted 0 site profiles — see [debug] above; exit 1")
        return 1
    print(f"  built {n_ok}/{len(sites)} site profiles ({n_levels_total / n_ok:.1f} levels avg)")

    manifest = {
        "schemaVersion": 1,
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
