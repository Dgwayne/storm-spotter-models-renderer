#!/usr/bin/env python3
"""REFS ensemble probabilities for the app's box meteogram.

Reads NOAA's ready-made REFS probability files (14 members early, thinning to
~6 by f60; the idx says "prob fcst 0/N") and writes small per-column
probability tiles to v1/soundings/refs/, on the same 24 km column lattice and
tile chunking as the point-sounding tiles. The app aggregates whatever columns
fall inside a drawn box (max and mean probability per hour).

Only ~11 messages (~270 KB each) are byte-ranged out of each ~42 MB hourly
prob file, so a whole 60-hour cycle is ~180 MB of downloads and a few CPU
minutes. Every value is the BLOCK MAX of the column's stride block: these are
already neighborhood probabilities ("within ~25 mi of a point"), and the
block max keeps a narrow corridor of high probability from slipping between
lattice points.

Freshness: dispatched hourly (stp-models-cron Worker). Each sweep picks the
newest cycle that has reached f18 (else the newest with anything), re-reads
every published hour of it and republishes when the CDN copy has fewer hours
or an older run. Stateless by design: a missed sweep just means the next one
does more. The same script runs as Astrol's dead-man fallback with
--if-stale MINUTES (exits at once while the CDN copy is fresh).

Usage: extract_refs_box.py [--if-stale MINUTES] [--dry-run]
"""
from __future__ import annotations

import datetime as dt
import json
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import extract_soundings as es  # noqa: E402  (shared fetch / grid / tile helpers)

BASE = "https://noaa-rrfs-ops-pds.s3.amazonaws.com"
PREFIX = "refs/"
CYCLES = (0, 6, 12, 18)
FHOURS = list(range(1, 61))
MIN_HOURS_TO_SWITCH = 18  # a new cycle replaces the old once it reaches f18

# key -> (GRIB var, level, probability text). Values are percent 0-100.
FIELDS = {
    "uh75": ("MXUPHL", "5000-2000 m above ground", "prob >75"),
    "uh150": ("MXUPHL", "5000-2000 m above ground", "prob >150"),
    "refl40": ("MAXREF", "1000 m above ground", "prob >40"),
    "refl50": ("MAXREF", "1000 m above ground", "prob >50"),
    "cape1000": ("CAPE", "90-0 mb above ground", "prob >1000"),
    "cape2000": ("CAPE", "90-0 mb above ground", "prob >2000"),
    "cin50": ("CIN", "90-0 mb above ground", "prob <-50"),
    "srh200": ("HLCY", "3000-0 m above ground", "prob >200"),
    "rain1": ("APCP", "surface", "prob >25.4"),   # 1 h step only, see below
    "wind50": ("WIND", "10 m above ground", "prob >25.72"),  # 50 kt
    "ltng": ("LTNG", "entire atmosphere", "prob >0.08"),
}


def prob_url(run: dt.datetime, fh: int) -> str:
    d, h = run.strftime("%Y%m%d"), run.strftime("%H")
    return f"{BASE}/refs.{d}/{h}/ensprod/refs.t{h}z.prob.f{fh:02d}.conus.grib2"


def published_hours(run: dt.datetime) -> list[int]:
    """Forecast hours whose idx exists. NOAA publishes slightly out of order
    (f49 before f48), so every hour is probed, in parallel."""
    with ThreadPoolExecutor(max_workers=16) as ex:
        ok = list(ex.map(lambda fh: es._head_ok(prob_url(run, fh) + ".idx"),
                         FHOURS))
    return [fh for fh, good in zip(FHOURS, ok) if good]


def pick_run():
    """(run, hours): newest cycle at >= f18, else the newest with any hour."""
    now = dt.datetime.now(dt.timezone.utc)
    t = now.replace(minute=0, second=0, microsecond=0)
    t -= dt.timedelta(hours=t.hour % 6)
    fallback = None
    for _ in range(5):  # 24 h of cycles
        hrs = published_hours(t)
        if len(hrs) >= MIN_HOURS_TO_SWITCH:
            return t, hrs
        if hrs and fallback is None:
            fallback = (t, hrs)
        t -= dt.timedelta(hours=6)
    return fallback or (None, [])


def fetch_hour(run: dt.datetime, fh: int, work: Path):
    """Byte-range this hour's FIELDS messages -> (grib path, keys, members)."""
    url = prob_url(run, fh)
    idx = es._get(url + ".idx").decode()
    rows = []
    for line in idx.splitlines():
        a = line.split(":")
        if len(a) >= 7:
            try:
                rows.append((int(a[0]), int(a[1]), a[3], a[4], a[5], a[6],
                             a[7] if len(a) > 7 else ""))
            except ValueError:
                pass
    rows.sort()
    want, members = [], None
    for i, (_, off, var, lvl, step, prob, ens) in enumerate(rows):
        for key, (v, l, p) in FIELDS.items():
            if var != v or lvl != l or prob != p:
                continue
            # Rain: the 1-hour bucket only (the file also has 3/6/24 h).
            if key == "rain1" and step != f"{fh - 1}-{fh} hour acc fcst":
                continue
            if key in (k for k, _, _ in want):
                continue
            end = rows[i + 1][1] - 1 if i + 1 < len(rows) else None
            want.append((key, off, end))
            if members is None and "/" in ens:
                try:
                    members = int(ens.split("/")[-1])
                except ValueError:
                    pass
    if not want:
        return None
    with ThreadPoolExecutor(max_workers=11) as ex:
        parts = list(ex.map(
            lambda w: es._get(url, f"{w[1]}-{'' if w[2] is None else w[2]}"),
            want))
    grib = work / f"f{fh:02d}.grib2"
    with open(grib, "wb") as f:
        for p in parts:
            f.write(p)
    return grib, [w[0] for w in want], members


def read_blocks(grib: Path, keys: list[str]):
    from osgeo import gdal
    import numpy as np

    ds = gdal.Open(str(grib))
    out = {}
    for i, key in enumerate(keys):
        arr = ds.GetRasterBand(i + 1).ReadAsArray()
        if arr is None:
            continue
        a = arr.astype(np.float32)
        a = np.where(np.abs(a) > 9.9e19, np.float32(0), a)
        out[key] = es._block_reduce(np.clip(a, 0, 100), es.TILE_STRIDE, "max")
    ds = None
    return out


def write_tiles(run, by_hour, members, lats, lons, out_dir: Path) -> int:
    import numpy as np

    ny, nx = lats.shape
    tiles_dir = out_dir / "tiles"
    tiles_dir.mkdir(parents=True)
    pad_lat = 0.75 * float(np.abs(np.diff(lats, axis=0)).max())
    pad_lon = 0.75 * float(np.abs(np.diff(lons, axis=1)).max())
    hours = sorted(by_hour)
    index = []
    C = es.TILE_COLS
    for ty0 in range(0, ny, C):
        for tx0 in range(0, nx, C):
            sl = (slice(ty0, min(ty0 + C, ny)), slice(tx0, min(tx0 + C, nx)))
            tid = f"{tx0 // C}_{ty0 // C}"
            tlat, tlon = lats[sl], lons[sl]
            hrs = []
            for fh in hours:
                g = by_hour[fh]
                h = {"fhour": fh,
                     "valid": (run + dt.timedelta(hours=fh)).isoformat(),
                     "members": members.get(fh)}
                for key in FIELDS:
                    if key in g:
                        h[key] = [int(v) for v in
                                  np.rint(g[key][sl]).astype(int).ravel()]
                hrs.append(h)
            (tiles_dir / f"{tid}.json").write_text(json.dumps({
                "model": "REFS", "run": run.isoformat(),
                "lat": np.round(tlat.astype(np.float64), 4).ravel().tolist(),
                "lon": np.round(tlon.astype(np.float64), 4).ravel().tolist(),
                "hours": hrs,
            }, separators=(",", ":")))
            index.append({
                "id": tid,
                "minLat": round(float(tlat.min()) - pad_lat, 3),
                "maxLat": round(float(tlat.max()) + pad_lat, 3),
                "minLon": round(float(tlon.min()) - pad_lon, 3),
                "maxLon": round(float(tlon.max()) + pad_lon, 3),
            })
    (out_dir / "tiles.json").write_text(json.dumps({
        "schemaVersion": 1, "model": "REFS", "run": run.isoformat(),
        "strideKm": es.TILE_STRIDE * 3, "hours": hours, "tiles": index,
    }, separators=(",", ":")))
    return len(index)


def _upload(src: Path, cache: str) -> None:
    dest = f"r2:{es.BUCKET}/v1/soundings/{PREFIX}"
    if src.is_dir():
        dest += src.name + "/"
    subprocess.check_call([
        "rclone", "copy", str(src), dest, "--s3-no-check-bucket",
        "--no-traverse", "--header-upload", f"Cache-Control: {cache}",
    ])


def main(argv) -> int:
    dry = "--dry-run" in argv
    cdn = es.cdn_manifest(PREFIX)
    if "--if-stale" in argv:
        limit = float(argv[argv.index("--if-stale") + 1])
        gen = (cdn or {}).get("generatedAt")
        if gen:
            age = (dt.datetime.now(dt.timezone.utc)
                   - dt.datetime.fromisoformat(gen)).total_seconds() / 60
            if age < limit:
                print(f"REFS box: CDN copy {age:.0f} min old (< {limit:.0f}); "
                      "nothing to do")
                return 0
            print(f"REFS box: CDN copy {age:.0f} min old; taking over")

    run, hours = pick_run()
    if run is None:
        print("REFS box: no cycle published in the last 24 h")
        return 1
    if (cdn and cdn.get("run") == run.isoformat()
            and cdn.get("forecastHours") == hours):
        # Touch generatedAt so it measures OUR liveness (monitor + Astrol's
        # --if-stale), not NOAA's 6-hourly cadence.
        print(f"REFS box: CDN already has {run:%Y%m%d%H}Z f{hours[0]}-"
              f"f{hours[-1]}; manifest touched, extract skipped")
        if not dry:
            cdn["generatedAt"] = dt.datetime.now(dt.timezone.utc).isoformat()
            m = Path(tempfile.mkdtemp()) / "manifest.json"
            m.write_text(json.dumps(cdn))
            _upload(m, "public, max-age=300")
        return 0
    print(f"==> REFS {run:%Y%m%d%H}Z: {len(hours)} hours "
          f"(f{hours[0]}-f{hours[-1]})")

    work = Path(tempfile.mkdtemp())
    t0 = time.monotonic()
    by_hour, members, lats, lons = {}, {}, None, None
    for fh in hours:
        try:
            sub = fetch_hour(run, fh, work)
        except Exception as e:  # one bad hour must not sink the cycle
            print(f"  f{fh:02d}: fetch failed {e!r}")
            continue
        if sub is None:
            continue
        grib, keys, n = sub
        if lats is None:
            lats, lons = es.grid_latlon(grib)
        by_hour[fh] = read_blocks(grib, keys)
        members[fh] = n
        grib.unlink(missing_ok=True)
    if not by_hour or lats is None:
        print("REFS box: nothing decoded")
        return 1
    got = sorted(by_hour)
    print(f"  decoded {len(got)} hours in {time.monotonic() - t0:.0f}s, "
          f"members f{got[0]}={members.get(got[0])} f{got[-1]}={members.get(got[-1])}")

    out = work / "out" / PREFIX
    n = write_tiles(run, by_hour, members, lats, lons, out)
    (out / "manifest.json").write_text(json.dumps({
        "schemaVersion": 1, "model": "REFS", "run": run.isoformat(),
        "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "forecastHours": got, "fields": list(FIELDS), "pointTiles": n,
    }))
    sizes = sorted(p.stat().st_size for p in (out / "tiles").glob("*.json"))
    print(f"  wrote {n} tiles, raw KB min/med/max "
          f"{sizes[0] // 1024}/{sizes[len(sizes) // 2] // 1024}/{sizes[-1] // 1024}")
    if dry:
        print(f"  dry run: left in {out}")
        return 0
    # Tiles first, manifest last, so a reader never sees a manifest that
    # points at tiles from a different run.
    for src, cache in ((out / "tiles", "public, max-age=600"),
                       (out / "tiles.json", "public, max-age=600"),
                       (out / "manifest.json", "public, max-age=300")):
        _upload(src, cache)
    print(f"==> uploaded REFS box tiles ({len(got)} h)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
