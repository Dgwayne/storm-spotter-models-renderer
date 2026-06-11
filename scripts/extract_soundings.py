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

Reuses the exact pattern the image renderer already uses: the public NOAA
HRRR S3 bucket, the .idx byte-range trick (so we download only the ~18
matching GRIB messages, not the whole file), wgrib2 for point extraction,
and rclone -> Cloudflare R2. Costs $0/month on the same GitHub Actions
cron + R2 setup as the rest of this repo.

Env: R2_BUCKET, R2_ENDPOINT, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY
Tools: wgrib2, rclone, python3 (stdlib only — urllib, no requests needed)
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
VAR_RE = re.compile(r":(UGRD|VGRD|HGT):(\d+) mb:")
VAL_RE = re.compile(r"val=([-\d.eE+]+|nan)")

# How many run-hours back to look for a published f00 (HRRR f00 publishes
# ~45–55 min after init; the previous run is always available as fallback).
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
    """Newest HRRR run whose f00 wrfsfcf .idx is published. (grib_url, run_dt)."""
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


def fetch_subset(grib_url: str, work: Path) -> Path | None:
    """Byte-range GET only the UGRD/VGRD/HGT mandatory-level messages."""
    idx = _get(grib_url + ".idx").decode()
    parsed = []
    for line in idx.splitlines():
        a = line.split(":")
        if len(a) >= 3:
            try:
                parsed.append((int(a[0]), int(a[1]), line))
            except ValueError:
                pass
    parsed.sort()
    ranges = []
    for i, (_, off, line) in enumerate(parsed):
        if MATCH.search(line):
            end = parsed[i + 1][1] - 1 if i + 1 < len(parsed) else ""
            ranges.append(f"{off}-{end}")
    if not ranges:
        return None
    grib = work / "in.grib2"
    with open(grib, "wb") as f:
        for r in ranges:
            f.write(_get(grib_url, r))
    print(f"  fetched {grib.stat().st_size} bytes, {len(ranges)} messages")
    return grib


def extract_site(grib: Path, lon: float, lat: float) -> dict[int, dict[str, float]]:
    """wgrib2 -lon point extraction -> {level_mb: {UGRD,VGRD,HGT}}."""
    try:
        out = subprocess.check_output(
            ["wgrib2", str(grib), "-lon", str(lon), str(lat)],
            text=True,
            stderr=subprocess.DEVNULL,
        )
    except subprocess.CalledProcessError:
        return {}
    prof: dict[int, dict[str, float]] = {}
    for line in out.splitlines():
        vm = VAR_RE.search(line)
        valm = VAL_RE.search(line)
        if not vm or not valm:
            continue
        vs = valm.group(1)
        if vs == "nan":
            continue
        val = float(vs)
        if abs(val) > 9.9e19:  # GRIB missing
            continue
        prof.setdefault(int(vm.group(2)), {})[vm.group(1)] = val
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
    grib = fetch_subset(grib_url, work)
    if grib is None:
        print("no matching messages in idx; exit 0")
        return 0

    sites = []
    with open(SITES_CSV) as f:
        for row in csv.DictReader(f):
            sites.append((row["id"], float(row["lat"]), float(row["lon"])))

    out_dir = work / "out"
    out_dir.mkdir()
    n_ok = 0
    n_levels_total = 0
    for sid, lat, lon in sites:
        prof = extract_site(grib, lon, lat)
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
        print("extracted 0 site profiles — check wrfsfcf level availability; exit 1")
        return 1
    avg_levels = n_levels_total / n_ok
    print(f"  built {n_ok}/{len(sites)} site profiles ({avg_levels:.1f} levels avg)")

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
