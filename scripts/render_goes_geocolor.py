#!/usr/bin/env python3
"""render_goes_geocolor.py — rolling window of GOES-East GeoColor frames for
the app's satellite ANIMATION, served from B2 behind models.dgwaynes.com.

Why a separate script (not part of render_goes.py)
--------------------------------------------------
GeoColor is a true-color RGB composite with NO IEM single-band source, so it
can't ride the IEM-archive -> gdalwarp -> color-table pipeline the other
products use. It comes pre-reprojected from NASA GIBS instead. Keeping it in
its own script + its own manifest (v1/SAT/geocolor.json) means:
  * render_goes.py and the shared v1/SAT/manifest.json (ir/wv/vis) are left
    byte-for-byte untouched, so ALREADY-INSTALLED apps are unaffected — they
    read the shared manifest (water vapor) and never see this file.
  * A new app version opts in by reading geocolor.json; if it's ever missing
    or stale the app falls back to its live GIBS loop.

Source
------
GIBS WMS GetMap (the same endpoint + TIME dimension the app's live/animation
GIBS path already uses), one full-CONUS EPSG:3857 JPEG per 10-min slot:
  https://gibs.earthdata.nasa.gov/wms/epsg3857/best/wms.cgi
    ?layers=GOES-East_ABI_GeoColor&format=image/jpeg&crs=EPSG:3857
    &bbox=<conus 3857>&width=3072&height=1840&time=<YYYY-MM-DDTHH:MM:00Z>
Because GIBS already serves EPSG:3857 at our exact bbox, no gdal step is
needed — download, sanity-check, upload. 3072px over the CONUS box is
~2.1 km/px, a large jump over the old 0.6-scale loop (~4.3 km/px) and close
to the live GIBS view (GIBS caps GeoColor at Level 7, ~1.2 km; see the app's
satellite notes).

Pipeline per slot
-----------------
  1. GetMap the 10-min slot -> JPEG.
  2. Reject the uniform "no data" render (tiny byte size) so blanks never
     enter the loop; a genuinely missing slot is just skipped and backfilled
     by a later run.
  3. Upload to v1/SAT/geocolor/<YYYYMMDDHHMM>.jpg.
Then rebuild v1/SAT/geocolor.json from the B2 key set.

Idempotent: skips slots already on B2 and re-scans the whole window each run,
so a delayed/skipped cron tick is harmlessly backfilled next time.

Env: R2_BUCKET  (rclone "r2:" remote — points at B2, configured by the
workflow exactly like the other GOES jobs).
"""

from __future__ import annotations

import concurrent.futures as cf
import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────
GIBS_WMS = "https://gibs.earthdata.nasa.gov/wms/epsg3857/best/wms.cgi"
LAYER = "GOES-East_ABI_GeoColor"

# Fixed output extent, identical to render_goes.py's CONUS box so the app can
# pin an ImageSource to [W,S,E,N] with no projection mismatch. The 3857 twin
# is precomputed (matches AppConstants.satelliteAnimBbox3857 in the app).
BBOX = [-125.0, 22.0, -66.0, 50.0]  # W, S, E, N (lon/lat)
BBOX_3857 = "-13914936.3492,2511525.2348,-7347086.3924,6446275.8410"
# 3072 wide; height derived from the 3857 aspect (yrange/xrange = 0.5991) so
# pixels stay square. ~2.1 km/px over CONUS — ~2x sharper than the old
# 0.6-scale GIBS loop, at roughly half the data/decode of a 4096 frame.
OUT_WIDTH = 3072
OUT_HEIGHT = 1840

# Rolling window (minutes) + frame cadence. GIBS publishes GeoColor on a
# 10-min grid and runs ~40 min behind wall clock. 720 min = 12 h keeps enough
# history for the app's longest loop; at 10 min that's up to 73 frames.
WINDOW_MIN = 720
STEP_MIN = 10
LATENCY_MIN = 40

# Blank/real separation, calibrated against live GIBS responses at 3072x1840:
#   * real frame (day or night) ....... 0.7-1.1 MB JPEG
#   * "no data" render (uniform black) . EXACTLY 33,404 bytes
#   * WMS ServiceException (msDrawMap) . ~700 B of XML (caught by the JPEG
#                                        magic-byte check in _download)
# 150 KB sits ~5x below the smallest real frame and ~4.5x above the black
# render, so it rejects blanks with margin without risking a genuinely sparse
# (clear-sky) real frame. (At 4096 the black render is 59,420 B, so re-check
# this if OUT_WIDTH changes.)
MIN_REAL_BYTES = 150_000

# Download parallelism for a cold window (73 on-demand GIBS renders). GIBS
# tolerates a small burst; 4 keeps the first fill well under the job timeout
# without hammering the renderer.
CONCURRENCY = 4

R2_PREFIX = "v1/SAT"
PRODUCT = "geocolor"
MANIFEST_KEY = f"{R2_PREFIX}/{PRODUCT}.json"


# ── Time grid ─────────────────────────────────────────────────────────
def _slots(now: dt.datetime) -> list[dt.datetime]:
    """10-min-aligned UTC slots across the window, newest last. Anchored at
    now - latency (floored to the grid), matching the app's expected-latest
    heuristic."""
    latest_raw = now - dt.timedelta(minutes=LATENCY_MIN)
    latest = latest_raw.replace(
        minute=(latest_raw.minute // STEP_MIN) * STEP_MIN, second=0, microsecond=0)
    return [latest - dt.timedelta(minutes=m)
            for m in range(WINDOW_MIN, -1, -STEP_MIN)]


def _time_iso(slot: dt.datetime) -> str:
    return slot.strftime("%Y-%m-%dT%H:%M:00Z")


def _gibs_url(slot: dt.datetime) -> str:
    return (
        f"{GIBS_WMS}?service=WMS&request=GetMap&version=1.3.0"
        f"&layers={LAYER}&styles=&format=image/jpeg"
        f"&width={OUT_WIDTH}&height={OUT_HEIGHT}"
        f"&crs=EPSG:3857&bbox={BBOX_3857}"
        f"&time={_time_iso(slot)}"
    )


def _key(slot: dt.datetime) -> str:
    return f"{R2_PREFIX}/{PRODUCT}/{slot:%Y%m%d%H%M}.jpg"


# ── B2 (rclone "r2:" remote) ──────────────────────────────────────────
def _list_existing(bucket: str) -> set[str]:
    try:
        out = subprocess.check_output(
            ["rclone", "lsf", "--recursive", "--files-only",
             f"r2:{bucket}/{R2_PREFIX}/{PRODUCT}/"], text=True)
    except subprocess.CalledProcessError:
        return set()
    return {f"{R2_PREFIX}/{PRODUCT}/{ln.strip()}"
            for ln in out.splitlines() if ln.strip()}


def _download(url: str, dest: Path) -> bool:
    """Fetch a slot. Returns False for network errors, non-JPEG bodies, and
    the tiny uniform 'no data' render (so blanks never reach the manifest)."""
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "storm_spotter_geocolor/1.0"})
        with urllib.request.urlopen(req, timeout=90) as r:
            data = r.read()
    except Exception as exc:  # noqa: BLE001 — best-effort; retried/backfilled
        print(f"  fetch error {url[-32:]}: {exc}", file=sys.stderr)
        return False
    if len(data) < MIN_REAL_BYTES or data[:2] != b"\xff\xd8":  # JPEG SOI
        return False
    dest.write_bytes(data)
    return True


def _upload(src: Path, key: str) -> None:
    subprocess.check_call([
        "rclone", "copyto", str(src), f"r2:{os.environ['R2_BUCKET']}/{key}",
        "--s3-no-check-bucket", "--no-traverse",
        "--header-upload", "Cache-Control: public, max-age=900",
    ])


def _fetch_slot(slot: dt.datetime) -> str | None:
    """Download + upload one slot; one retry (GIBS's on-demand WMS can return
    a transient blank/XML on the first hit even when the frame exists — the
    same flakiness the app's loader retries around). Never raises: any error
    leaves the slot un-stored to be backfilled by a later idempotent run.
    Returns the key on success, else None."""
    key = _key(slot)
    try:
        for _ in range(2):
            with tempfile.TemporaryDirectory() as td:
                dest = Path(td) / "frame.jpg"
                if _download(_gibs_url(slot), dest):
                    _upload(dest, key)
                    return key
    except Exception as exc:  # noqa: BLE001 — upload/IO error; retried next run
        print(f"  slot {slot:%Y%m%d%H%M} failed: {exc}", file=sys.stderr)
    return None


# ── Manifest ──────────────────────────────────────────────────────────
def _write_manifest(bucket: str, keys: set[str], now: dt.datetime) -> None:
    """Rebuild v1/SAT/geocolor.json from the B2 key set. Same schema shape as
    the shared SAT manifest (bbox / stepMinutes / products) so the app can
    reuse its manifest loader, but a SEPARATE file — the shared manifest is
    never touched."""
    frames: list[dict] = []
    for key in keys:
        stamp = key.rsplit("/", 1)[-1].removesuffix(".jpg")
        try:
            kt = dt.datetime.strptime(stamp, "%Y%m%d%H%M").replace(
                tzinfo=dt.timezone.utc)
        except ValueError:
            continue
        frames.append({"t": kt.isoformat().replace("+00:00", "Z"), "url": key})
    frames.sort(key=lambda f: f["t"])

    manifest = {
        "schemaVersion": 1,
        "generatedAt": now.replace(microsecond=0).isoformat().replace(
            "+00:00", "Z"),
        # ImageSource corners: [W, S, E, N] lon/lat of the mercator frame.
        "bbox": BBOX,
        "stepMinutes": STEP_MIN,
        "products": {PRODUCT: frames},
    }
    out = Path(tempfile.gettempdir()) / "geocolor_manifest.json"
    out.write_text(json.dumps(manifest, separators=(",", ":")))
    subprocess.check_call([
        "rclone", "copyto", str(out), f"r2:{bucket}/{MANIFEST_KEY}",
        "--s3-no-check-bucket", "--no-traverse",
        "--header-upload", "Cache-Control: public, max-age=60",
    ])
    print(f"  manifest: {{'{PRODUCT}': {len(frames)}}}")


# ── Main ──────────────────────────────────────────────────────────────
def main() -> int:
    bucket = os.environ["R2_BUCKET"]
    force = bool(os.environ.get("FORCE_RERENDER"))
    now = dt.datetime.now(dt.timezone.utc)
    slots = _slots(now)
    existing = _list_existing(bucket)
    print(f"==> GeoColor render: {len(slots)} slots; "
          f"{len(existing)} already on B2")

    todo = [s for s in slots if force or _key(s) not in existing]
    rendered = 0
    if todo:
        with cf.ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
            for key in pool.map(_fetch_slot, todo):
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
