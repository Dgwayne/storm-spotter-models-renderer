#!/usr/bin/env python3
"""MRMS 2D product -> native-resolution data tiles for the app's national layer.

The OBS frame (v1/OBS/<code>/<run>/F000.png) is one 4096 px Web-Mercator
image, capped for mobile GPU textures. That is ~1.5 km cells at 35N and a
NEAREST warp that keeps only ~45% of MRMS's native 0.01 deg cells: fine at
national zoom, visibly soft zoomed into a county. These tiles carry the
native grid untouched so the app can fetch just the ones under the view.

Grid: MRMS CONUS as published, EPSG:4326, latitude-LINEAR rows, 0.01 deg
cells, 7000 x 3500, NW corner (-130, 55). The same grid, origin and cell as
the 3D volume tiles (mrms_volume_tiles.py), so the 2D layer lines up with
the volume cell for cell.

Tiles: TILE x TILE cells (default 512 = 5.12 deg), edge tiles cropped.
Encoding is exactly the OBS data PNG's (mrms_render_one.encode_gray): gray
1..255 = dataMin..dataMax linear, gray 0 + alpha 0 = no data. A tile with
no cell above dataMin is not written at all; the index lists the ones that
exist, so the app never asks for an empty one.

Output (B2 behind models.dgwaynes.com, rclone remote "r2:"):
  <prefix>/<code>/<YYYYMMDD-HHMMSS>/r<RR>c<CC>.png   immutable, one per scan
  <prefix>/<code>/<YYYYMMDD-HHMMSS>/index.json       grid + tile list
  <prefix>/<code>/latest.json                        newest + retained stamps

Two boxes render the same product (Box 2 and Oracle, last writer wins), so
latest.json is a MERGE with the published copy, never a blind overwrite:
the slower box finishing an older scan must not roll the pointer back or
drop stamps the faster box already published. Tiles go up before the
pointer, so latest.json never names a scan that is not there yet.

Usage:
  mrms_obs_tiles.py --grib IN.grib2 --code brefqc --stamp 20260929-064756
                    --data-min -30 --data-max 80 --sentinel-lt -35
                    [--scale 1] [--tile 512] [--out-dir DIR]
                    [--publish] [--prefix v1/OBSTILES] [--retain-min 120]
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
from osgeo import gdal

sys.path.insert(0, str(Path(__file__).resolve().parent))
from mrms_render_one import encode_gray, read_scaled  # noqa: E402

gdal.UseExceptions()

LON0, LAT0, CELL_DEG = -130.0, 55.0, 0.01
COLS, ROWS = 7000, 3500
STAMP_RE = re.compile(r"\d{8}-\d{6}")


def stamp_dt(stamp: str) -> datetime:
    return datetime.strptime(stamp, "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc)


def load_grid(grib: str, sentinel_lt: float, scale: float) -> np.ndarray:
    """The native grid, after checking it IS the grid the app assumes. A
    silent shift here would misplace every echo, so refuse instead."""
    _src, gt, scaled = read_scaled(grib, sentinel_lt, scale)
    rows, cols = scaled.shape
    ok = (cols == COLS and rows == ROWS
          and abs(gt[0] - LON0) < 1e-3 and abs(gt[3] - LAT0) < 1e-3
          and abs(gt[1] - CELL_DEG) < 1e-6 and abs(gt[5] + CELL_DEG) < 1e-6
          and gt[2] == 0 and gt[4] == 0)
    if not ok:
        raise SystemExit(f"FATAL: unexpected MRMS grid {cols}x{rows} gt={gt}")
    return scaled


def cut(gray: np.ndarray, alpha: np.ndarray, tile: int, out: Path) -> list[str]:
    """Write every tile holding a cell above dataMin; return their ids."""
    png = gdal.GetDriverByName("PNG")
    mem_drv = gdal.GetDriverByName("MEM")
    ids: list[str] = []
    for tr in range((ROWS + tile - 1) // tile):
        r0 = tr * tile
        for tc in range((COLS + tile - 1) // tile):
            c0 = tc * tile
            g = gray[r0:r0 + tile, c0:c0 + tile]
            # gray 1 is "valid but at/below dataMin": nothing any palette
            # paints, so a tile of only 0s and 1s is not worth a request.
            if not (g > 1).any():
                continue
            a = alpha[r0:r0 + tile, c0:c0 + tile]
            tid = f"r{tr:02d}c{tc:02d}"
            mem = mem_drv.Create("", g.shape[1], g.shape[0], 2, gdal.GDT_Byte)
            mem.GetRasterBand(1).WriteArray(g)
            mem.GetRasterBand(2).WriteArray(a)
            png.CreateCopy(str(out / f"{tid}.png"), mem, strict=0,
                           options=["ZLEVEL=9"])
            ids.append(tid)
    return ids


def rclone(*args: str, capture: bool = False) -> str:
    cmd = ["rclone", *args, "--s3-no-check-bucket",
           "--contimeout", "20s", "--timeout", "60s", "--retries", "2"]
    if capture:
        return subprocess.run(cmd, check=True, text=True,
                              capture_output=True).stdout
    subprocess.run(cmd, check=True)
    return ""


def published_latest(bucket: str, key: str) -> dict | None:
    """The live latest.json straight from B2 (the CDN copy may be minutes
    old). None when it does not exist yet or cannot be read."""
    try:
        return json.loads(rclone("cat", f"r2:{bucket}/{key}", capture=True))
    except (subprocess.CalledProcessError, json.JSONDecodeError):
        return None


def merge_latest(prev: dict | None, header: dict, stamp: str,
                 retain_min: int) -> dict:
    stamps = {stamp}
    if prev and prev.get("tile") == header["tile"]:
        stamps |= {s for s in prev.get("stamps", []) if STAMP_RE.fullmatch(s)}
    newest = max(stamps)
    cutoff = stamp_dt(newest) - timedelta(minutes=retain_min)
    kept = sorted((s for s in stamps if stamp_dt(s) >= cutoff), reverse=True)
    out = dict(header)
    out["newest"] = newest
    out["stamps"] = kept
    out["generatedAt"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return out


def prune(bucket: str, base: str, newest: str, retain_min: int) -> int:
    """Delete scan folders older than the window. One folder-only listing
    (~60 entries), so this never walks the tiles themselves. Cutoff is
    relative to the NEWEST published scan, so a box running behind can only
    ever delete less, never a scan the pointer still names."""
    try:
        out = rclone("lsf", "--dirs-only", f"r2:{bucket}/{base}/", capture=True)
    except subprocess.CalledProcessError as e:
        print(f"  WARN tile prune listing failed: {e}", file=sys.stderr)
        return 0
    cutoff = stamp_dt(newest) - timedelta(minutes=retain_min)
    removed = 0
    for line in out.splitlines():
        name = line.strip().rstrip("/")
        if STAMP_RE.fullmatch(name) and stamp_dt(name) < cutoff:
            subprocess.call(["rclone", "purge", f"r2:{bucket}/{base}/{name}",
                             "--s3-no-check-bucket"])
            removed += 1
    return removed


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--grib", required=True)
    p.add_argument("--code", required=True)
    p.add_argument("--stamp", required=True, help="source scan YYYYMMDD-HHMMSS")
    p.add_argument("--data-min", type=float, required=True)
    p.add_argument("--data-max", type=float, required=True)
    p.add_argument("--sentinel-lt", type=float, default=0.0)
    p.add_argument("--scale", type=float, default=1.0)
    p.add_argument("--tile", type=int, default=512)
    p.add_argument("--out-dir")
    p.add_argument("--publish", action="store_true")
    p.add_argument("--prefix", default="v1/OBSTILES")
    p.add_argument("--retain-min", type=int, default=120)
    args = p.parse_args()
    if not STAMP_RE.fullmatch(args.stamp):
        raise SystemExit(f"FATAL: bad --stamp {args.stamp}")

    t0 = time.time()
    scaled = load_grid(args.grib, args.sentinel_lt, args.scale)
    gray, alpha = encode_gray(scaled, args.data_min, args.data_max)
    del scaled

    work = Path(args.out_dir) if args.out_dir else Path(tempfile.mkdtemp())
    stamp_dir = work / args.stamp
    stamp_dir.mkdir(parents=True, exist_ok=True)
    ids = cut(gray, alpha, args.tile, stamp_dir)

    header = {
        "code": args.code,
        "lon0": LON0, "lat0": LAT0, "cellDeg": CELL_DEG,
        "cols": COLS, "rows": ROWS, "tile": args.tile,
        "dataMin": args.data_min, "dataMax": args.data_max,
    }
    index = dict(header, stamp=args.stamp, tiles=ids)
    (stamp_dir / "index.json").write_text(json.dumps(index, separators=(",", ":")))
    nbytes = sum(f.stat().st_size for f in stamp_dir.glob("*.png"))
    print(f"  tiles {args.code} {args.stamp}: {len(ids)} tiles, "
          f"{nbytes / 1e6:.2f} MB, cut in {time.time() - t0:.1f}s")

    if not args.publish:
        return 0
    bucket = os.environ.get("R2_BUCKET", "")
    if not bucket:
        raise SystemExit("FATAL: --publish needs R2_BUCKET")
    base = f"{args.prefix.strip('/')}/{args.code}"
    # Tiles + index first (immutable: a scan's bytes never change).
    rclone("copy", str(stamp_dir), f"r2:{bucket}/{base}/{args.stamp}/",
           "--transfers", "16", "--no-traverse",
           "--header-upload", "Cache-Control: public, max-age=86400, immutable")
    # Then the pointer, merged with whatever the other box published.
    latest = merge_latest(published_latest(bucket, f"{base}/latest.json"),
                          header, args.stamp, args.retain_min)
    lp = work / "latest.json"
    lp.write_text(json.dumps(latest, separators=(",", ":")))
    rclone("copyto", str(lp), f"r2:{bucket}/{base}/latest.json",
           "--no-traverse", "--header-upload", "Cache-Control: public, max-age=20")
    removed = prune(bucket, base, latest["newest"], args.retain_min)
    print(f"  tiles published {base}/{args.stamp} newest={latest['newest']} "
          f"stamps={len(latest['stamps'])} pruned={removed} "
          f"total {time.time() - t0:.1f}s")
    if not args.out_dir:
        subprocess.call(["rm", "-rf", str(work)])
    return 0


if __name__ == "__main__":
    sys.exit(main())
