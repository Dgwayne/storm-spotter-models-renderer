#!/usr/bin/env python3
"""star_source.py: NESDIS STAR full-disk GOES imagery, cropped to any lon/lat box.

Why STAR instead of GIBS
------------------------
GIBS assembles each GetMap from source granules AT REQUEST TIME and renders
whatever it currently holds, leaving missing granules as hard-edged black
rectangles in otherwise perfect imagery. Measured 7.4% of stored frames, up to
23% for a single region. STAR instead publishes a FINISHED whole-sector image
per timestamp or nothing at all: its CONUS archive measured 2886 frames over
10.0 days at a perfect 5-min cadence with ZERO missing timestamps. Same
satellite, same data, a publishing model that cannot emit a partial.

Why the FULL DISK and not STAR's own sector crops
-------------------------------------------------
STAR's 30 named sectors carry NO GeoTIFF and publish NO bounds anywhere (only
CONUS and FD have a .tif). We pin imagery to lon/lat corners, so copying their
crops would mean reverse-engineering 30 boxes by feature matching. The full
disk removes the problem: one download per satellite per slot serves EVERY
region, the bounds are whatever we choose, and upstream load does not grow with
the number of regions.

Fixed-grid geometry (measured, do not re-derive)
------------------------------------------------
The FD JPEG is exactly the standard ABI fixed grid, half-extent
+/-5,434,894.885 m, so the 10848 px image is 1002.01 m/px (measured 1001.88 off
the image itself, confirming it).

NOTE the limb-based self-calibration used for Himawari/SLIDER does NOT work
here: the disk fills this frame edge to edge, so its limb is clipped and a
measured ellipticity comes out 0.99853 against the true 0.99662. Use the
published constant.

lon_0 was solved empirically by cross-correlating a warp against NOAA's own
georeferenced .tif:
    GOES-19 East : -75.0   (validated dx=0, dy=0 EXACT; the nominal -75.2 is 15 km off)
    GOES-18 West : -137.05 (dx=0 at 0.027 deg/px; nominal -137.0 is ~4.8 km off)
Validate against a STAR product, never GIBS: STAR-latest vs GIBS-latest are
~40 min apart, the clouds have moved, and that alone produced a spurious 8 px
offset.

GOTCHA: ABI sweeps on **x**. Himawari AHI sweeps on **y** (see slider_source).
The wrong sweep does not error, it silently skews the image.
"""

from __future__ import annotations

import datetime as dt
import io
import math
import re
import urllib.request

CDN = "https://cdn.star.nesdis.noaa.gov"
UA = "storm_spotter_geocolor/1.0 (+https://dgwaynes.com)"

# ABI fixed-grid full-disk half-extent in geos metres. The whole calibration.
FD_HALF_M = 5434894.885

SATELLITES: dict[str, dict] = {
    "G19": {"cdn": "GOES19", "lon0": -75.0, "label": "GOES-East"},
    "G18": {"cdn": "GOES18", "lon0": -137.05, "label": "GOES-West"},
}

# Full-disk pixel sizes STAR publishes, by approximate ground resolution.
FD_PX = {2000: 5424, 1000: 10848, 500: 21696}


def proj4(sat: str) -> str:
    return ("+proj=geos +h=35786023 +a=6378137 +b=6356752.31414 "
            f"+lon_0={SATELLITES[sat]['lon0']} +sweep=x +units=m +no_defs")


def _get(url: str, timeout: int = 300) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


# ── Availability ──────────────────────────────────────────────────────
def _stamp(slot: dt.datetime) -> str:
    """STAR names files with day-of-year: YYYYDDDHHMM."""
    return f"{slot:%Y}{slot.timetuple().tm_yday:03d}{slot:%H%M}"


def fd_url(sat: str, slot: dt.datetime | None, px: int) -> str:
    c = SATELLITES[sat]["cdn"]
    base = f"{CDN}/{c}/ABI/FD/GEOCOLOR"
    if slot is None:
        return f"{base}/{px}x{px}.jpg"          # newest
    return f"{base}/{_stamp(slot)}_{c}-ABI-FD-GEOCOLOR-{px}x{px}.jpg"


def available_times(sat: str, px: int) -> set[str]:
    """The YYYYDDDHHMM stamps STAR currently holds for this full disk, so we
    never spend an 18 MB download on a slot that does not exist."""
    c = SATELLITES[sat]["cdn"]
    try:
        html = _get(f"{CDN}/{c}/ABI/FD/GEOCOLOR/", 120).decode("utf8", "replace")
    except Exception:  # noqa: BLE001 - unknown: caller falls back to trying
        return set()
    return set(re.findall(
        rf"(\d{{11}})_{c}-ABI-FD-GEOCOLOR-{px}x{px}\.jpg", html))


# ── Full disk ─────────────────────────────────────────────────────────
class FullDisk:
    """One decoded full disk, cropped many times.

    Decoding a 10848 px disk costs ~350 MB and a few seconds, so a run fetches
    and decodes ONCE per (satellite, slot) and then cuts every region out of
    the same array. That is the whole reason upstream load does not scale with
    region count.
    """

    def __init__(self, sat: str, array, res: float):
        self.sat = sat
        self.array = array          # (3, n, n) uint8
        self.res = res

    @classmethod
    def fetch(cls, sat: str, slot: dt.datetime | None,
              km_res: int = 1000) -> "FullDisk | None":
        import numpy as np
        from PIL import Image
        Image.MAX_IMAGE_PIXELS = None

        px = FD_PX[km_res]
        try:
            data = _get(fd_url(sat, slot, px))
        except Exception:  # noqa: BLE001 - missing slot / transient
            return None
        if len(data) < 100_000 or data[:2] != b"\xff\xd8":
            return None
        try:
            im = Image.open(io.BytesIO(data))
            arr = np.asarray(im.convert("RGB")).transpose(2, 0, 1)
        except Exception:  # noqa: BLE001 - truncated download
            return None
        n = arr.shape[1]
        if n < 1000:
            return None
        return cls(sat, arr, 2.0 * FD_HALF_M / n)

    def crop(self, bounds: list[float], out_w: int, out_h: int,
             jpeg_quality: int = 82) -> bytes | None:
        """Warp the box to EPSG:3857 and return JPEG bytes."""
        import numpy as np
        from PIL import Image
        from rasterio.crs import CRS
        from rasterio.transform import Affine
        from rasterio.warp import reproject, Resampling

        a = 6378137.0
        mx = lambda lon: math.radians(lon) * a                    # noqa: E731
        my = lambda lat: math.log(                                 # noqa: E731
            math.tan(math.radians(90.0 + lat) / 2.0)) * a
        w, s, e, n = bounds
        src_t = Affine(self.res, 0, -FD_HALF_M, 0, -self.res, FD_HALF_M)
        dst = np.zeros((3, out_h, out_w), "uint8")
        dst_t = Affine((mx(e) - mx(w)) / out_w, 0, mx(w),
                       0, -(my(n) - my(s)) / out_h, my(n))
        try:
            reproject(self.array, dst,
                      src_transform=src_t, src_crs=CRS.from_proj4(proj4(self.sat)),
                      dst_transform=dst_t, dst_crs=CRS.from_epsg(3857),
                      resampling=Resampling.cubic, num_threads=2)
        except Exception:  # noqa: BLE001 - box off the disk
            return None
        buf = io.BytesIO()
        Image.fromarray(dst.transpose(1, 2, 0)).save(
            buf, "JPEG", quality=jpeg_quality, optimize=True)
        return buf.getvalue()

    def coverage(self, bounds: list[float], probe: int = 96) -> float:
        """Fraction of the box that carries imagery. A box reaching past the
        limb comes back partly black, which is a bounds bug, not a data gap."""
        import numpy as np
        from PIL import Image
        data = self.crop(bounds, probe, probe, jpeg_quality=60)
        if data is None:
            return 0.0
        arr = np.asarray(Image.open(io.BytesIO(data)).convert("RGB"))
        return float((arr.max(axis=2) > 6).mean())
