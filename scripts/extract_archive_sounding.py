#!/usr/bin/env python3
"""extract_archive_sounding.py - dated model wind profile for ONE site -> R2.

Serves the Storm Spotter velocity dealiaser's ARCHIVE mode: a loop over an
old storm needs the environmental wind for THAT date as its trusted fold
reference (live mode already has hourly HRRR via extract_soundings.py; the
archive previously ran with no reference at all).

One request = one (site, UTC hour). Sources, newest capability first:
  * HRRR wrfprsf00 (AWS NODD, 2014-07-30 ->): .idx byte-range subset,
    UGRD/VGRD/HGT/TMP/DPT at the shared mandatory levels.
  * NCEI historical/rap-130 analysis trees (2005-01 ->): the S3-compatible
    listing at www.ncei.noaa.gov/oa/prod-model, files rap_130_*/ruc2*_000
    with .inv inventories in the same offset format as .idx, so the same
    byte-range trick applies (RUC-era GRIB1 included - gdal reads both).
    Filenames are DISCOVERED from the day listing, not hardcoded: the era
    determines ruc2_252 vs ruc2anl_130 vs rap_130 and .grb vs .grb2.
  * Nothing before 2005: a small negative-marker JSON is uploaded so the
    app stops re-requesting (short cache in case the miss was an outage).

The requested hour walks +/-3 h when the exact analysis is missing (NCEI
archive gaps are common); environmental wind is smooth at that scale and
the profile carries its true `run` time.

Output: v1/soundings/archive/<SITE>/<YYYYMMDDHH>.json - the same
hgt_msl_m/u_ms/v_ms contract the dealiaser's WindSounding.tryParse reads
(temp/dewpoint included when the era provides them). Archive analyses
never change, so positives upload immutable.

Usage: extract_archive_sounding.py --site KMVX --time 2025-06-21T04
       [--out DIR]   (write JSON locally, skip CDN check + upload)
Env (upload path): R2_BUCKET, and rclone configured with an `r2` remote.
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))
import extract_soundings as live  # noqa: E402  (classify/_get/extract_all reuse)

SITES_CSV = REPO_ROOT / "config" / "nexrad_sites.csv"
HRRR_START = dt.date(2014, 7, 30)
NCEI_START = dt.date(2005, 1, 1)
NCEI = "https://www.ncei.noaa.gov/oa/prod-model"
NCEI_TREES = (
    "rapid-refresh/access/historical/analysis",
    "rapid-refresh/access/rap-130-13km/analysis",
)
CDN_BASE = "https://models.dgwaynes.com/v1/soundings/archive"

# Hour offsets tried around the requested analysis hour.
HOUR_WALK = (0, -1, 1, -2, -3, 2, 3)


def _cdn_has(site: str, stamp: str) -> bool:
    url = f"{CDN_BASE}/{site}/{stamp}.json"
    req = urllib.request.Request(url, method="HEAD")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status == 200
    except Exception:
        return False


def _list_keys(prefix: str) -> list[str]:
    """S3 ListObjectsV2 on the NCEI prod-model bucket (one page is plenty:
    a day directory holds ~50 files)."""
    url = f"{NCEI}?list-type=2&prefix={urllib.parse.quote(prefix)}&max-keys=400"
    try:
        xml = live._get(url).decode()
    except Exception:
        return []
    return re.findall(r"<Key>([^<]+)</Key>", xml)


def _subset_by_inventory(url: str, inv_url: str, work: Path):
    """Byte-range the classify()-matched messages using a wgrib-style
    inventory (msg:offset:date:VAR:LEVEL:...). Same trick as the live
    fetch_subset, parameterized on the inventory URL - NCEI serves `.inv`
    beside each GRIB instead of `.idx`.

    RAP/RUC files pack fields as SUBMESSAGES (dotted numbers `112.1`,
    `112.2` sharing one parent message's offset) - the parent decodes to
    one gdal band PER SUBMESSAGE, in inventory order. So: group entries
    by offset, fetch a parent when ANY submessage matches, and emit a
    band key (or None placeholder for unmatched submessages) for EVERY
    submessage of every fetched parent, keeping band indices aligned."""
    inv = live._get(inv_url).decode()
    entries = []  # (offset, msg_tuple, var, lvl)
    for line in inv.splitlines():
        a = line.split(":")
        if len(a) >= 5:
            try:
                msg = tuple(int(p) for p in a[0].split("."))
                entries.append((int(a[1]), msg, a[3], a[4]))
            except ValueError:
                pass
    entries.sort()
    # Group submessages by parent offset, preserving submessage order.
    groups: list[tuple[int, list]] = []
    for e in entries:
        if groups and groups[-1][0] == e[0]:
            groups[-1][1].append(e)
        else:
            groups.append((e[0], [e]))
    ranges, band_keys = [], []
    for gi, (off, subs) in enumerate(groups):
        keys = [live.classify(var, lvl) for (_, _, var, lvl) in subs]
        if not any(k is not None for k in keys):
            continue
        end = groups[gi + 1][0] - 1 if gi + 1 < len(groups) else ""
        ranges.append(f"{off}-{end}")
        band_keys.extend(keys)
    print(f"  [inv] {len(entries)} fields in {len(groups)} messages; "
          f"{len(ranges)} messages fetched, "
          f"{sum(1 for k in band_keys if k)} bands matched")
    if not ranges:
        return None
    work.mkdir(parents=True, exist_ok=True)
    grib = work / "in.grib"
    with open(grib, "wb") as f:
        for r in ranges:
            f.write(live._get(url, r))
    return grib, band_keys


def _try_hrrr(t: dt.datetime, work: Path):
    d, h = t.strftime("%Y%m%d"), t.strftime("%H")
    url = (f"https://noaa-hrrr-bdp-pds.s3.amazonaws.com/hrrr.{d}/conus/"
           f"hrrr.t{h}z.wrfprsf00.grib2")
    if not live._head_ok(url + ".idx"):
        return None
    sub = live.fetch_subset(url, work / f"hrrr{d}{h}")
    if sub is None:
        return None
    return sub[0], sub[1], "HRRR", t


def _try_ncei(t: dt.datetime, work: Path):
    d, hhmm = t.strftime("%Y%m%d"), t.strftime("%H00")
    for tree in NCEI_TREES:
        day = f"{tree}/{t.strftime('%Y%m')}/{d}/"
        keys = _list_keys(day)
        # Analysis for our hour, .grb2 preferred over GRIB1 .grb, and the
        # finer grid (130 = 13 km) over 252 (20 km) when both exist.
        cands = [k for k in keys
                 if re.search(rf"_{hhmm}_000\.(grb2|grb)$", k)]
        cands.sort(key=lambda k: (not k.endswith(".grb2"), "_130_" not in k))
        for key in cands:
            url = f"{NCEI}/{key}"
            inv = re.sub(r"\.(grb2|grb)$", ".inv", url)
            try:
                sub = _subset_by_inventory(url, inv, work / "ncei")
            except Exception as e:
                print(f"  [ncei] {key}: {e!r}")
                continue
            if sub is None:
                continue
            model = "RAP" if "rap_" in key else "RUC"
            return sub[0], sub[1], model, t
    return None


def build_wind_profile(d: dict):
    """Lean profile: every mandatory level with HGT+UGRD+VGRD. The
    dealiaser needs only wind vs height; temp/dewpoint ride along when the
    era provides them (RUC/RAP publish RH, not DPT - fine, omitted)."""
    levels = []
    for mb in live.LEVELS:
        h, u, v = d.get(f"HGT:{mb}"), d.get(f"UGRD:{mb}"), d.get(f"VGRD:{mb}")
        if h is None or u is None or v is None:
            continue
        t = d.get(f"TMP:{mb}")
        td = d.get(f"DPT:{mb}")
        levels.append((float(mb), h, u, v,
                       None if t is None else live._to_c(t),
                       None if td is None else live._to_c(td)))
    if len(levels) < 4:
        return None
    levels.sort(key=lambda x: -x[0])  # surface-first (low -> high altitude)
    prof = {
        "pres_hpa": [round(L[0], 1) for L in levels],
        "hgt_msl_m": [round(L[1], 1) for L in levels],
        "u_ms": [round(L[2], 2) for L in levels],
        "v_ms": [round(L[3], 2) for L in levels],
    }
    if all(L[4] is not None for L in levels):
        prof["temp_c"] = [round(L[4], 2) for L in levels]
    if all(L[5] is not None for L in levels):
        prof["dewpoint_c"] = [round(L[5], 2) for L in levels]
    return prof


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", required=True)
    ap.add_argument("--time", required=True,
                    help="UTC analysis hour, e.g. 2025-06-21T04")
    ap.add_argument("--out", help="write JSON to this dir instead of R2")
    args = ap.parse_args()

    site = args.site.upper()
    t0 = dt.datetime.fromisoformat(args.time).replace(
        minute=0, second=0, microsecond=0, tzinfo=dt.timezone.utc)
    stamp = t0.strftime("%Y%m%d%H")

    coord = None
    with open(SITES_CSV) as f:
        for row in csv.DictReader(f):
            if row["id"].upper() == site:
                coord = (float(row["lat"]), float(row["lon"]))
                break
    if coord is None:
        print(f"unknown site {site}")
        return 1

    if args.out is None and _cdn_has(site, stamp):
        print(f"==> {site}/{stamp}.json already on CDN; nothing to do")
        return 0

    work = Path(tempfile.mkdtemp())
    found = None
    for off in HOUR_WALK:
        t = t0 + dt.timedelta(hours=off)
        if t.date() >= HRRR_START:
            found = _try_hrrr(t, work)
        if found is None and t.date() >= NCEI_START:
            found = _try_ncei(t, work)
        if found is not None:
            break

    lat, lon = coord
    if found is None:
        doc = {"site": site, "requested": t0.isoformat(), "model": None,
               "error": "no_model_data"}
        cache = "public, max-age=3600"
        print(f"==> {site} {stamp}: no archive model source; negative marker")
    else:
        grib, band_keys, model, t = found
        vals = live.extract_all(grib, band_keys, [(site, lat, lon)])[0]
        print(f"  [extract] {len(band_keys)} bands requested, "
              f"{len(vals)} values sampled: {sorted(vals)[:8]}...")
        prof = build_wind_profile(vals)
        if prof is None:
            print(f"==> {site} {stamp}: {model} extraction yielded <4 levels")
            return 1
        doc = {"site": site, "model": model, "run": t.isoformat(),
               "requested": t0.isoformat(), "lat": lat, "lon": lon, **prof}
        cache = "public, max-age=31536000, immutable"
        print(f"==> {site} {stamp}: {model} @ {t.isoformat()} "
              f"({len(prof['hgt_msl_m'])} levels)")
        print(f"    u={prof['u_ms'][:4]}... v={prof['v_ms'][:4]}...")

    out_dir = Path(args.out) if args.out else work / "out"
    (out_dir / site).mkdir(parents=True, exist_ok=True)
    dest = out_dir / site / f"{stamp}.json"
    dest.write_text(json.dumps(doc, separators=(",", ":")))
    print(f"    wrote {dest}")
    if args.out is None:
        import os
        bucket = os.environ["R2_BUCKET"]
        subprocess.check_call([
            "rclone", "copy", str(out_dir),
            f"r2:{bucket}/v1/soundings/archive/",
            "--s3-no-check-bucket", "--no-traverse",
            "--header-upload", f"Cache-Control: {cache}",
        ])
        print(f"    uploaded to v1/soundings/archive/{site}/{stamp}.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
