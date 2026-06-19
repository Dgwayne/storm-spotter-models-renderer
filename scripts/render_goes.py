#!/usr/bin/env python3
"""render_goes.py — build a rolling window of recent GOES satellite frames
for the app's satellite ANIMATION, served from R2.

Why this source
---------------
IEM's *live* WMS (goes_east.cgi) is current-frame only — no time dimension
— so it can't drive an animation. But IEM also ARCHIVES already-
georeferenced CONUS GeoTIFFs every 15 min:
  https://mesonet.agron.iastate.edu/archive/data/<Y>/<m>/<d>/GIS/sat/
    conus_goes_ir4km_HHMM.tif   (band 13 enhanced IR)
    conus_goes_wv4km_HHMM.tif   (water vapor)
    conus_goes_vis4km_HHMM.tif  (visible)
Because they carry an embedded SRS, gdal reprojects them in one step — far
simpler than wrangling raw ABI's geostationary fixed-grid projection, and
4 km is plenty for an animated loop.

Pipeline per (product, frame)
-----------------------------
  1. Download conus_goes_<p>4km_HHMM.tif for each 15-min slot in the window.
  2. gdalwarp  GeoTIFF (embedded SRS) -> EPSG:3857 at a FIXED lon/lat bbox
     (so every frame shares one extent the app can pin an ImageSource to).
  3. gdaldem color-relief (.clr) for ir/wv; visible uses a grayscale ramp.
  4. gdal_translate -> PNG, push to R2 at v1/SAT/<p>/<YYYYMMDDHHMM>.png.
Then rebuild v1/SAT/manifest.json from R2 contents.

Idempotent: skips frames already on R2, and re-scans the whole window each
run, so a skipped/late cron tick is backfilled by the next one.

Env: R2_BUCKET  (rclone "r2:" remote configured by the workflow)
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────
IEM_ARCHIVE = "https://mesonet.agron.iastate.edu/archive/data"

REPO_ROOT = Path(__file__).resolve().parent.parent
COLOR_TABLES = REPO_ROOT / "config" / "color_tables"

# product key -> (IEM archive infix, color table or None for grayscale)
PRODUCTS = {
    "ir": ("ir4km", "goes_ir.clr"),
    "wv": ("wv4km", "goes_wv.clr"),
    "vis": ("vis4km", "goes_vis.clr"),
}

# Rolling window of history to keep on R2 (minutes) and frame cadence.
WINDOW_MIN = 180
STEP_MIN = 15

# Fixed output extent (lon/lat). gdalwarp emits a Web-Mercator image whose
# corners unproject to exactly these lon/lat values, so the app pins an
# ImageSource to [W,S,E,N] with no projection mismatch. Kept inside the
# IEM CONUS source extent so there are no nodata edge strips.
BBOX = [-125.0, 22.0, -66.0, 50.0]  # W, S, E, N
OUT_WIDTH = 1600  # height auto-derived (gdalwarp -ts W 0) to keep aspect

R2_PREFIX = "v1/SAT"


def _run(cmd: list[str]) -> None:
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def _frame_slots(now: dt.datetime) -> list[dt.datetime]:
    """15-min-aligned UTC slots covering the rolling window, newest last."""
    aligned = now.replace(minute=(now.minute // STEP_MIN) * STEP_MIN,
                          second=0, microsecond=0)
    return [aligned - dt.timedelta(minutes=m)
            for m in range(WINDOW_MIN, -1, -STEP_MIN)]


def _archive_url(slot: dt.datetime, infix: str) -> str:
    return (f"{IEM_ARCHIVE}/{slot.year}/{slot.month:02d}/{slot.day:02d}"
            f"/GIS/sat/conus_goes_{infix}_{slot.hour:02d}{slot.minute:02d}.tif")


def _r2_key(product: str, slot: dt.datetime) -> str:
    return f"{R2_PREFIX}/{product}/{slot:%Y%m%d%H%M}.png"


def _r2_exists(keys_cache: set[str], key: str) -> bool:
    return key in keys_cache


def _list_r2(bucket: str) -> set[str]:
    try:
        out = subprocess.check_output(
            ["rclone", "lsf", "--recursive", "--files-only",
             f"r2:{bucket}/{R2_PREFIX}/"], text=True)
    except subprocess.CalledProcessError:
        return set()
    return {f"{R2_PREFIX}/{line.strip()}" for line in out.splitlines() if line.strip()}


def _download(url: str, dest: Path) -> bool:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "storm_spotter/1.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read()
        # IEM serves a tiny HTML 404 page for missing slots.
        if len(data) < 2000 or data[:2] not in (b"II", b"MM"):
            return False
        dest.write_bytes(data)
        return True
    except Exception:
        return False


def _render_frame(src_tif: Path, product: str, work: Path) -> Path:
    """gdalwarp -> color-relief -> PNG. Returns the PNG path."""
    merc = work / "merc.tif"
    _run([
        "gdalwarp", "-q", "-overwrite",
        "-t_srs", "EPSG:3857",
        "-te_srs", "EPSG:4326",
        "-te", str(BBOX[0]), str(BBOX[1]), str(BBOX[2]), str(BBOX[3]),
        "-ts", str(OUT_WIDTH), "0",
        "-r", "bilinear",
        "-dstnodata", "0",
        str(src_tif), str(merc),
    ])

    clr = COLOR_TABLES / PRODUCTS[product][1]
    rgba = work / "rgba.tif"
    _run([
        "gdaldem", "color-relief", "-q", "-alpha",
        str(merc), str(clr), str(rgba),
    ])

    png = work / "frame.png"
    _run(["gdal_translate", "-q", "-of", "PNG", "-co", "ZLEVEL=9",
          str(rgba), str(png)])
    return png


def main() -> int:
    bucket = os.environ["R2_BUCKET"]
    force = bool(os.environ.get("FORCE_RERENDER"))
    now = dt.datetime.now(dt.timezone.utc)
    slots = _frame_slots(now)
    existing = _list_r2(bucket)
    print(f"==> GOES render: {len(slots)} slots × {len(PRODUCTS)} products; "
          f"{len(existing)} already on R2")

    rendered = 0
    for product, (infix, _clr) in PRODUCTS.items():
        for slot in slots:
            key = _r2_key(product, slot)
            if not force and _r2_exists(existing, key):
                continue
            with tempfile.TemporaryDirectory() as td:
                work = Path(td)
                src = work / "src.tif"
                if not _download(_archive_url(slot, infix), src):
                    continue  # slot not published (yet) — backfilled next run
                try:
                    png = _render_frame(src, product, work)
                except subprocess.CalledProcessError as exc:
                    print(f"  render failed {product} {slot:%H%M}: "
                          f"{exc.stderr.decode()[:200] if exc.stderr else exc}",
                          file=sys.stderr)
                    continue
                subprocess.check_call([
                    "rclone", "copyto", str(png), f"r2:{bucket}/{key}",
                    "--s3-no-check-bucket", "--no-traverse",
                    "--header-upload", "Cache-Control: public, max-age=900",
                ])
                existing.add(key)
                rendered += 1

    # Prune anything older than the window so storage stays bounded.
    cutoff = (now - dt.timedelta(minutes=WINDOW_MIN + STEP_MIN))
    pruned = 0
    for key in list(existing):
        stamp = key.rsplit("/", 1)[-1].removesuffix(".png")
        try:
            kt = dt.datetime.strptime(stamp, "%Y%m%d%H%M").replace(tzinfo=dt.timezone.utc)
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


def _write_manifest(bucket: str, keys: set[str], now: dt.datetime) -> None:
    """Rebuild v1/SAT/manifest.json from the R2 key set."""
    products: dict[str, list[dict]] = {p: [] for p in PRODUCTS}
    for key in keys:
        parts = key.split("/")
        if len(parts) != 4:
            continue
        product, fname = parts[2], parts[3]
        if product not in products:
            continue
        stamp = fname.removesuffix(".png")
        try:
            kt = dt.datetime.strptime(stamp, "%Y%m%d%H%M").replace(tzinfo=dt.timezone.utc)
        except ValueError:
            continue
        products[product].append({
            "t": kt.isoformat().replace("+00:00", "Z"),
            "url": key,
        })
    for p in products:
        products[p].sort(key=lambda f: f["t"])

    manifest = {
        "schemaVersion": 1,
        "generatedAt": now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        # ImageSource corners: [W, S, E, N] lon/lat of the (mercator) frame.
        "bbox": BBOX,
        "stepMinutes": STEP_MIN,
        "products": products,
    }
    out = Path(tempfile.gettempdir()) / "sat_manifest.json"
    out.write_text(json.dumps(manifest, separators=(",", ":")))
    subprocess.check_call([
        "rclone", "copyto", str(out), f"r2:{bucket}/{R2_PREFIX}/manifest.json",
        "--s3-no-check-bucket", "--no-traverse",
        "--header-upload", "Cache-Control: public, max-age=60",
    ])
    counts = {p: len(v) for p, v in products.items()}
    print(f"  manifest: {counts}")


if __name__ == "__main__":
    sys.exit(main())
