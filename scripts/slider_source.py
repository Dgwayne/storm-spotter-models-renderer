#!/usr/bin/env python3
"""slider_source.py: fetch CIRA SLIDER geostationary imagery and reproject it
to EPSG:3857 over a lon/lat box.

Why this module exists
----------------------
NASA GIBS serves GeoColor for GOES-East and GOES-West only. There is NO
Himawari GeoColor on GIBS (its Himawari layers are Air Mass, Band13 Clean
Infrared and Band3 Red Visible 1km; checked the full 3857 capabilities). The
only source of CIRA's Himawari GeoColor is RAMMB/CIRA SLIDER, which serves
tiles in the satellite's own geostationary fixed grid. So unlike the GIBS path
(which hands us EPSG:3857 at our exact bbox and needs no warp at all), this one
has to reproject.

Self-calibrating geometry
-------------------------
SLIDER does not publish its sector navigation (/js/define-sector-products.js is
404), so we do NOT depend on any config of theirs. The Earth's limb in the
imagery has a known angular radius, which pins the projection exactly:

    measured, z2: disk centre (1376.0, 1376.5) on a 2752 canvas (dead-centred),
                  radii (1359.0, 1354.5) px -> ellipticity 0.99669
    theoretical:  b/a at the limb from Earth oblateness -> 0.99662

Agreeing to 7 parts in 100,000 confirms the model. It yields a CONSTANT disk
extent of +/-5,502,156 m in geos metres at every zoom level, with square pixels
(z2 measured 3998.66 x 3998.39 m/px). Verified against an independent
reference: the warped output cross-correlated against GIBS
Himawari_AHI_Band3_Red_Visible_1km over an identical box/time peaks at
dx=0, dy=0 (corr 0.784 across two different products), i.e. zero offset.

GOTCHA: Himawari AHI sweeps on the **y** axis; GOES ABI sweeps on **x**. The
wrong sweep does not error, it silently skews the image. Keep it per-satellite.

Tile scheme
-----------
688 px tiles; z0 is one tile holding the whole disk, doubling each level, so
canvas = 688 * 2**z and ground resolution halves per level:
    z2 ~ 4 km,  z3 ~ 2 km,  z4 ~ 1 km,  z5 ~ 0.5 km.
We fetch only the tiles intersecting the target box, at the COARSEST zoom that
still out-resolves the output, never more data than the render can show.
"""

from __future__ import annotations

import concurrent.futures as cf
import datetime as dt
import io
import json
import math
import urllib.request

# ── SLIDER endpoints ──────────────────────────────────────────────────
SLIDER_BASE = "https://rammb-slider.cira.colostate.edu"
TILE_PX = 688
MAX_ZOOM = 5
UA = "storm_spotter_geocolor/1.0 (+https://dgwaynes.com)"

# Measured disk half-extent in geos metres. Constant across zooms (see module
# docstring). This single number is the whole calibration.
DISK_HALF_M = 5502156.0

# Per-satellite fixed-grid definition. sweep is NOT interchangeable.
SATELLITES: dict[str, dict] = {
    "himawari": {
        "slider_sat": "himawari",
        "proj4": ("+proj=geos +h=35785863 +a=6378137 +b=6356752.31414 "
                  "+lon_0=140.7 +sweep=y +units=m +no_defs"),
    },
}


def _get(url: str, timeout: int = 90) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# ── Availability ──────────────────────────────────────────────────────
def available_times(sat: str, sector: str, product: str) -> set[str]:
    """The 'YYYYMMDDHHMMSS' stamps SLIDER currently holds. Checking this first
    means we never spend a tile request on a slot that does not exist."""
    slider_sat = SATELLITES[sat]["slider_sat"]
    url = (f"{SLIDER_BASE}/data/json/{slider_sat}/{sector}/{product}"
           f"/latest_times.json")
    try:
        j = json.loads(_get(url, 45))
    except Exception:  # noqa: BLE001: treat as "unknown", caller falls back
        return set()
    for key in ("timestamps_int", "timestamps"):
        if key in j:
            return {str(v) for v in j[key]}
    # unexpected shape: take the first list value we find
    for v in j.values():
        if isinstance(v, list):
            return {str(x) for x in v}
    return set()


# ── Geometry ──────────────────────────────────────────────────────────
def zoom_res_m(zoom: int) -> float:
    """Ground resolution near nadir at a zoom level, in metres per pixel."""
    return 2.0 * DISK_HALF_M / (TILE_PX * 2 ** zoom)


def pick_zoom(bounds: list[float], out_width_px: int) -> int:
    """Coarsest zoom that still out-resolves the output raster.

    The output is EPSG:3857, whose metres are inflated by 1/cos(lat); compare
    against real ground resolution at the box's mid-latitude so we do not
    over-fetch just because mercator says the pixels are big.
    """
    w, s, e, n = bounds
    a = 6378137.0
    merc_res = (math.radians(e - w) * a) / out_width_px
    ground_res = merc_res * math.cos(math.radians((s + n) / 2.0))
    for z in range(0, MAX_ZOOM + 1):
        if zoom_res_m(z) <= ground_res:
            return z
    return MAX_ZOOM


def _geos_bbox(bounds: list[float], crs_geos, transform_fn):
    """Project the box OUTLINE (not just corners) into geos and take its bbox.

    Sampling the whole boundary matters: geos is strongly curved, so an edge
    can bow well outside the quadrilateral its corners describe. Points that
    fall off the disk come back non-finite and are dropped.
    """
    w, s, e, n = bounds
    xs, ys = [], []
    for t in [i / 59.0 for i in range(60)]:
        xs += [w + (e - w) * t, w + (e - w) * t, w, e]
        ys += [s, n, s + (n - s) * t, s + (n - s) * t]
    gx, gy = transform_fn(4326, crs_geos, xs, ys)
    gx = [v for v in gx if math.isfinite(v)]
    gy = [v for v in gy if math.isfinite(v)]
    if not gx or not gy:
        raise ValueError("box does not intersect the satellite disk")
    return min(gx), min(gy), max(gx), max(gy)


def tile_span(bounds: list[float], zoom: int, sat: str):
    """(tx0, tx1, ty0, ty1, res) covering the box at this zoom."""
    from rasterio.crs import CRS
    from rasterio.warp import transform as rtransform

    geos = CRS.from_proj4(SATELLITES[sat]["proj4"])

    def tf(src_epsg, dst_crs, xs, ys):
        return rtransform(CRS.from_epsg(src_epsg), dst_crs, xs, ys)

    minx, miny, maxx, maxy = _geos_bbox(bounds, geos, tf)
    canvas = TILE_PX * 2 ** zoom
    res = 2.0 * DISK_HALF_M / canvas
    px0 = int((minx + DISK_HALF_M) / res)
    px1 = int(math.ceil((maxx + DISK_HALF_M) / res))
    py0 = int((DISK_HALF_M - maxy) / res)
    py1 = int(math.ceil((DISK_HALF_M - miny) / res))
    last = 2 ** zoom - 1
    return (max(0, px0 // TILE_PX), min(last, px1 // TILE_PX),
            max(0, py0 // TILE_PX), min(last, py1 // TILE_PX), res)


def tile_count(bounds: list[float], zoom: int, sat: str) -> int:
    tx0, tx1, ty0, ty1, _ = tile_span(bounds, zoom, sat)
    return (tx1 - tx0 + 1) * (ty1 - ty0 + 1)


# ── Fetch + warp ──────────────────────────────────────────────────────
def _tile_url(sat: str, sector: str, product: str, stamp: str,
              zoom: int, ty: int, tx: int) -> str:
    slider_sat = SATELLITES[sat]["slider_sat"]
    d = f"{stamp[0:4]}/{stamp[4:6]}/{stamp[6:8]}"
    return (f"{SLIDER_BASE}/data/imagery/{d}/{slider_sat}---{sector}/{product}"
            f"/{stamp}/{zoom:02d}/{ty:03d}_{tx:03d}.png")


def render(bounds: list[float], out_w: int, out_h: int, slot: dt.datetime,
           sat: str = "himawari", sector: str = "full_disk",
           product: str = "geocolor", zoom: int | None = None,
           concurrency: int = 4, jpeg_quality: int = 87) -> bytes | None:
    """Fetch the tiles covering `bounds`, warp to EPSG:3857, return JPEG bytes.

    Returns None if ANY tile fails, because a partial mosaic would upload as a
    frame with black holes in it, and the idempotent re-scan retries the slot
    next run anyway. Imports are local so a machine without rasterio can still run
    the GIBS-only regions.
    """
    import numpy as np
    from PIL import Image
    from rasterio.crs import CRS
    from rasterio.transform import Affine
    from rasterio.warp import reproject, Resampling

    if zoom is None:
        zoom = pick_zoom(bounds, out_w)
    stamp = f"{slot:%Y%m%d%H%M}00"
    tx0, tx1, ty0, ty1, res = tile_span(bounds, zoom, sat)
    jobs = [(ty, tx) for ty in range(ty0, ty1 + 1)
            for tx in range(tx0, tx1 + 1)]

    def one(job):
        ty, tx = job
        url = _tile_url(sat, sector, product, stamp, zoom, ty, tx)
        try:
            return job, Image.open(io.BytesIO(_get(url))).convert("RGB")
        except Exception:  # noqa: BLE001: caller retries the whole slot
            return job, None

    mosaic = Image.new("RGB",
                       ((tx1 - tx0 + 1) * TILE_PX, (ty1 - ty0 + 1) * TILE_PX))
    with cf.ThreadPoolExecutor(max_workers=concurrency) as pool:
        for (ty, tx), im in pool.map(one, jobs):
            if im is None:
                return None
            mosaic.paste(im, ((tx - tx0) * TILE_PX, (ty - ty0) * TILE_PX))

    src = np.asarray(mosaic).transpose(2, 0, 1)
    src_t = Affine(res, 0, -DISK_HALF_M + tx0 * TILE_PX * res,
                   0, -res, DISK_HALF_M - ty0 * TILE_PX * res)

    a = 6378137.0
    mx = lambda lon: math.radians(lon) * a                       # noqa: E731
    my = lambda lat: math.log(                                    # noqa: E731
        math.tan(math.radians(90.0 + lat) / 2.0)) * a
    w, s, e, n = bounds
    dst = np.zeros((3, out_h, out_w), "uint8")
    dst_t = Affine((mx(e) - mx(w)) / out_w, 0, mx(w),
                   0, -(my(n) - my(s)) / out_h, my(n))
    reproject(src, dst,
              src_transform=src_t, src_crs=CRS.from_proj4(SATELLITES[sat]["proj4"]),
              dst_transform=dst_t, dst_crs=CRS.from_epsg(3857),
              resampling=Resampling.cubic, num_threads=2)

    buf = io.BytesIO()
    Image.fromarray(dst.transpose(1, 2, 0)).save(
        buf, "JPEG", quality=jpeg_quality, optimize=True)
    return buf.getvalue()
