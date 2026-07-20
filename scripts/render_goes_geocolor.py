#!/usr/bin/env python3
"""render_goes_geocolor.py — rolling windows of GOES-East GeoColor frames,
per REGION, for the app's satellite animation. Served from B2 behind
models.dgwaynes.com.

Why regions
-----------
A single ImageSource is capped at ~4096 px, so one box can't be both wide and
crisp. Instead each region is its own bounded box, rendered at GIBS-native
detail (~1.2 km); the app shows/animates ONE region at a time (CONUS, Gulf,
Western Atlantic, Caribbean). Small boxes stay razor sharp AND light, and the
user picks the area they're watching. See the app's satellite notes.

Why a separate script (not part of render_goes.py)
--------------------------------------------------
GeoColor is a true-color RGB composite with NO IEM single-band source, so it
comes pre-reprojected from NASA GIBS instead of the IEM-archive pipeline the
other products use. Its own script + its own manifest (v1/SAT/geocolor.json)
means render_goes.py and the shared v1/SAT/manifest.json (ir/wv/vis) are left
byte-for-byte untouched, so ALREADY-INSTALLED apps are unaffected.

Source
------
GIBS WMS GetMap (the same endpoint + TIME dimension the app already uses), one
EPSG:3857 JPEG per (region, 10-min slot). GIBS serves EPSG:3857 at our exact
bbox, so no gdal step is needed — download, sanity-check, upload to
v1/SAT/geocolor/<region>/<YYYYMMDDHHMM>.jpg. Then rebuild geocolor.json.

Manifest (schema v2): { schemaVersion, generatedAt, stepMinutes,
regions:{ <region>:{ label, bbox:[W,S,E,N] lon/lat, frames:[{t,url}] } } }.

Idempotent: skips (region, slot) pairs already on B2 and re-scans every window
each run, so a delayed/skipped tick is harmlessly backfilled next time.
FORCE_RERENDER purges the geocolor tree first, then rebuilds — use it after a
bbox / dimension change so no stale-geometry frame lingers.

Env: R2_BUCKET  (rclone "r2:" remote — points at B2, configured by the
workflow exactly like the other GOES jobs).
"""

from __future__ import annotations

import concurrent.futures as cf
import datetime as dt
import json
import math
import os
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

# ── Regions (lon/lat W,S,E,N). Everything else is derived. ─────────────
# CONUS is the wide overview; the rest are focused oceanic/tropical boxes
# that come out sharper (they don't hit the pixel cap). Adding a region is
# one line here — the manifest + frame tree follow automatically.
REGIONS: dict[str, dict] = {
    "conus":     {"label": "CONUS",            "bounds": [-125.0, 22.0, -66.0, 50.0]},
    "gulf":      {"label": "Gulf",             "bounds": [-98.0, 17.5, -80.0, 31.0]},
    "atlantic":  {"label": "Western Atlantic", "bounds": [-82.0, 24.0, -58.0, 45.0]},
    "caribbean": {"label": "Caribbean",        "bounds": [-89.0, 9.0, -58.0, 24.0]},
}

GIBS_WMS = "https://gibs.earthdata.nasa.gov/wms/epsg3857/best/wms.cgi"
LAYER = "GOES-East_ABI_GeoColor"

# Pixel budget. Target ~1.2 km/px (GIBS-native), capped so the biggest box
# (CONUS) lands at 3072 px / ~2.1 km — the fast overview the app already
# shipped — while the smaller boxes get their full 1.2 km at < 3072 px.
TARGET_MPP = 1200.0
MAX_PX = 3072
MIN_PX = 1024
A = 6378137.0  # WGS84 semi-major axis (EPSG:3857)

# Rolling window (minutes) + cadence. GIBS publishes GeoColor on a 10-min grid
# and runs ~40 min behind wall clock. 720 min = 12 h covers the app's longest
# loop; at 10 min that's up to 73 candidate slots per region.
WINDOW_MIN = 720
STEP_MIN = 10
LATENCY_MIN = 40

# Download parallelism across all (region, slot) tasks. GIBS tolerates a small
# burst; 6 keeps a cold multi-region backfill well under the job timeout.
CONCURRENCY = 6

R2_PREFIX = "v1/SAT"
GEO_PREFIX = f"{R2_PREFIX}/geocolor"
MANIFEST_KEY = f"{R2_PREFIX}/geocolor.json"


# ── Geometry ──────────────────────────────────────────────────────────
def _merc_x(lon: float) -> float:
    return lon * math.pi / 180.0 * A


def _merc_y(lat: float) -> float:
    return math.log(math.tan((90.0 + lat) * math.pi / 360.0)) * A


def _region_geom(bounds: list[float]) -> tuple[str, int, int]:
    """(bbox3857 string, width, height) for a lon/lat [W,S,E,N] box. Width
    targets ~1.2 km/px capped to [MIN_PX, MAX_PX]; height keeps square pixels
    (also capped). Square pixels + an EPSG:3857 bbox mean the app pins an
    ImageSource to the lon/lat corners with no projection mismatch."""
    w, s, e, n = bounds
    xw = _merc_x(e) - _merc_x(w)
    yh = _merc_y(n) - _merc_y(s)
    width = min(MAX_PX, max(MIN_PX, round(xw / TARGET_MPP)))
    height = min(MAX_PX, round(width * yh / xw))
    bbox = f"{_merc_x(w):.4f},{_merc_y(s):.4f},{_merc_x(e):.4f},{_merc_y(n):.4f}"
    return bbox, width, height


def _blank_threshold(width: int, height: int) -> int:
    """Min bytes for a real frame at this size. GIBS's uniform "no data" render
    is ~0.006 B/px; real GeoColor (day or night) is ~0.13 B/px+. 0.03 B/px sits
    ~5x above blank and ~4x below the sparsest real frame across every region;
    a 40 KB floor guards the smallest boxes. WMS XML exceptions (~700 B) are
    caught separately by the JPEG magic-byte check."""
    return max(40_000, round(width * height * 0.03))


# ── Time grid ─────────────────────────────────────────────────────────
def _slots(now: dt.datetime) -> list[dt.datetime]:
    """10-min-aligned UTC slots across the window, newest last, anchored at
    now - latency (floored to the grid)."""
    latest_raw = now - dt.timedelta(minutes=LATENCY_MIN)
    latest = latest_raw.replace(
        minute=(latest_raw.minute // STEP_MIN) * STEP_MIN, second=0, microsecond=0)
    return [latest - dt.timedelta(minutes=m)
            for m in range(WINDOW_MIN, -1, -STEP_MIN)]


def _time_iso(slot: dt.datetime) -> str:
    return slot.strftime("%Y-%m-%dT%H:%M:00Z")


def _gibs_url(bbox3857: str, width: int, height: int, slot: dt.datetime) -> str:
    return (
        f"{GIBS_WMS}?service=WMS&request=GetMap&version=1.3.0"
        f"&layers={LAYER}&styles=&format=image/jpeg"
        f"&width={width}&height={height}"
        f"&crs=EPSG:3857&bbox={bbox3857}&time={_time_iso(slot)}"
    )


def _key(region: str, slot: dt.datetime) -> str:
    return f"{GEO_PREFIX}/{region}/{slot:%Y%m%d%H%M}.jpg"


# ── B2 (rclone "r2:" remote) ──────────────────────────────────────────
def _list_existing(bucket: str) -> set[str]:
    try:
        out = subprocess.check_output(
            ["rclone", "lsf", "--recursive", "--files-only",
             f"r2:{bucket}/{GEO_PREFIX}/"], text=True)
    except subprocess.CalledProcessError:
        return set()
    return {f"{GEO_PREFIX}/{ln.strip()}"
            for ln in out.splitlines() if ln.strip()}


def _purge(bucket: str) -> None:
    subprocess.call(["rclone", "delete", f"r2:{bucket}/{GEO_PREFIX}/",
                     "--rmdirs"])


def _download(url: str, dest: Path, min_bytes: int) -> bool:
    """Fetch one frame. False for network errors, non-JPEG bodies (WMS XML
    exceptions), and the uniform "no data" render (below min_bytes)."""
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "storm_spotter_geocolor/1.0"})
        with urllib.request.urlopen(req, timeout=90) as r:
            data = r.read()
    except Exception as exc:  # noqa: BLE001 — best-effort; retried/backfilled
        print(f"  fetch error {url[-40:]}: {exc}", file=sys.stderr)
        return False
    if len(data) < min_bytes or data[:2] != b"\xff\xd8":  # JPEG SOI
        return False
    dest.write_bytes(data)
    return True


def _upload(src: Path, key: str, bucket: str) -> None:
    subprocess.check_call([
        "rclone", "copyto", str(src), f"r2:{bucket}/{key}",
        "--s3-no-check-bucket", "--no-traverse",
        "--header-upload", "Cache-Control: public, max-age=900",
    ])


class _Task:
    __slots__ = ("region", "slot", "url", "key", "min_bytes")

    def __init__(self, region, slot, url, key, min_bytes):
        self.region = region
        self.slot = slot
        self.url = url
        self.key = key
        self.min_bytes = min_bytes


def _fetch(task: _Task, bucket: str) -> str | None:
    """Download + upload one (region, slot); one retry (GIBS's on-demand WMS can
    return a transient blank/XML on the first hit even when the frame exists).
    Never raises — any error leaves the slot un-stored for a later idempotent
    run. Returns the key on success, else None."""
    try:
        for _ in range(2):
            with tempfile.TemporaryDirectory() as td:
                dest = Path(td) / "frame.jpg"
                if _download(task.url, dest, task.min_bytes):
                    _upload(dest, task.key, bucket)
                    return task.key
    except Exception as exc:  # noqa: BLE001 — upload/IO error; retried next run
        print(f"  {task.region} {task.slot:%Y%m%d%H%M} failed: {exc}",
              file=sys.stderr)
    return None


# ── Manifest ──────────────────────────────────────────────────────────
def _write_manifest(bucket: str, keys: set[str], now: dt.datetime) -> None:
    """Rebuild v1/SAT/geocolor.json (schema v2) from the B2 key set. Every
    region gets its lon/lat bbox (ImageSource corners) + its sorted frame
    list. A SEPARATE file — the shared SAT manifest is never touched."""
    frames: dict[str, list[dict]] = {r: [] for r in REGIONS}
    for key in keys:
        parts = key.split("/")  # v1/SAT/geocolor/<region>/<stamp>.jpg
        if len(parts) != 5:
            continue
        region, fname = parts[3], parts[4]
        if region not in REGIONS:
            continue
        stamp = fname.removesuffix(".jpg")
        try:
            kt = dt.datetime.strptime(stamp, "%Y%m%d%H%M").replace(
                tzinfo=dt.timezone.utc)
        except ValueError:
            continue
        frames[region].append(
            {"t": kt.isoformat().replace("+00:00", "Z"), "url": key})
    for r in frames:
        frames[r].sort(key=lambda f: f["t"])

    manifest = {
        "schemaVersion": 2,
        "generatedAt": now.replace(microsecond=0).isoformat().replace(
            "+00:00", "Z"),
        "stepMinutes": STEP_MIN,
        "regions": {
            r: {"label": REGIONS[r]["label"], "bbox": REGIONS[r]["bounds"],
                "frames": frames[r]}
            for r in REGIONS
        },
    }
    out = Path(tempfile.gettempdir()) / "geocolor_manifest.json"
    out.write_text(json.dumps(manifest, separators=(",", ":")))
    subprocess.check_call([
        "rclone", "copyto", str(out), f"r2:{bucket}/{MANIFEST_KEY}",
        "--s3-no-check-bucket", "--no-traverse",
        "--header-upload", "Cache-Control: public, max-age=60",
    ])
    print(f"  manifest: {{{', '.join(f'{r}: {len(frames[r])}' for r in REGIONS)}}}")


# ── Main ──────────────────────────────────────────────────────────────
def main() -> int:
    bucket = os.environ["R2_BUCKET"]
    force = bool(os.environ.get("FORCE_RERENDER"))
    now = dt.datetime.now(dt.timezone.utc)
    slots = _slots(now)

    if force:
        print("==> FORCE: purging geocolor tree for a clean rebuild")
        _purge(bucket)
        existing: set[str] = set()
    else:
        existing = _list_existing(bucket)

    # Per-region geometry (bbox / dims / blank threshold), computed once.
    geom = {r: _region_geom(REGIONS[r]["bounds"]) for r in REGIONS}
    for r, (_, w, h) in geom.items():
        print(f"    {r}: {w}x{h}  min_real={_blank_threshold(w, h)} B")

    tasks = []
    for region in REGIONS:
        bbox, width, height = geom[region]
        min_bytes = _blank_threshold(width, height)
        for slot in slots:
            key = _key(region, slot)
            if key in existing:
                continue
            tasks.append(_Task(region, slot,
                               _gibs_url(bbox, width, height, slot),
                               key, min_bytes))
    print(f"==> GeoColor render: {len(REGIONS)} regions x {len(slots)} slots; "
          f"{len(existing)} on B2, {len(tasks)} to fetch")

    rendered = 0
    if tasks:
        with cf.ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
            for key in pool.map(lambda t: _fetch(t, bucket), tasks):
                if key:
                    existing.add(key)
                    rendered += 1

    # Prune anything older than the window so storage stays bounded.
    cutoff = now - dt.timedelta(minutes=LATENCY_MIN + WINDOW_MIN + STEP_MIN)
    pruned = 0
    for key in list(existing):
        stamp = key.rsplit("/", 1)[-1].removesuffix(".jpg")
        try:
            kt = dt.datetime.strptime(stamp, "%Y%m%d%H%M").replace(
                tzinfo=dt.timezone.utc)
        except ValueError:
            continue
        if kt < cutoff:
            subprocess.call(["rclone", "deletefile", f"r2:{bucket}/{key}",
                             "--s3-no-check-bucket"])
            existing.discard(key)
            pruned += 1

    _write_manifest(bucket, existing, now)
    print(f"==> done: {rendered} new frames, {pruned} pruned")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
