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
            # Skip convert() when the JPEG is already RGB. At the 0.5 km tier
            # the decoded disk is ~1.4 GB, and a needless convert doubles peak
            # memory for no gain.
            if im.mode != "RGB":
                im = im.convert("RGB")
            arr = np.asarray(im).transpose(2, 0, 1)
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

    # ── Meso sectors ──────────────────────────────────────────────────
    def solve_meso_center(self, tag: str, meso: int,
                          stamp: str, search_px: int = 80):
        """Exact fixed-grid centre of a meso sector, in geos metres.

        STAR names the sector directory with a lat/lon ROUNDED TO WHOLE
        DEGREES ("43N-93W"), which is a label, not georeferencing: measured up
        to 33 km out. So the label only seeds a search, and the true centre
        comes from template-matching the meso frame inside this full disk.

        Both are crops of the SAME fixed grid at the same tier, so the match is
        a pure translation with no scaling: a meso is 1000 px at the 1 km tier
        and the disk is 1002.01 m/px, so their pixels are identical by
        construction.

        Cheap enough to run once per position DIRECTORY, not per frame: the
        sector is stationary until operations moves it, and a move creates a
        new directory.
        """
        import numpy as np
        from PIL import Image
        from rasterio.crs import CRS
        from rasterio.warp import transform as rtransform

        # Always solve at the 1 km tier, whatever disk is loaded. The match is
        # O(template area x search area), so a 2000 px template at the 0.5 km
        # tier is ~8x the work for precision we do not need: the label it is
        # correcting is tens of km out, and the answer comes back in geos
        # metres, which applies to every tier.
        try:
            data = _get(meso_url(self.sat, tag, meso, stamp, 1000))
            patch = np.asarray(Image.open(io.BytesIO(data)).convert("L")
                               ).astype(np.float32)
        except Exception:  # noqa: BLE001
            return None
        m = patch.shape[0]
        step = max(1, int(round(1002.01 / self.res)))
        disk = self.array[:, ::step, ::step].mean(axis=0).astype(np.float32)
        res = self.res * step
        n = disk.shape[0]

        lat, lon = parse_meso_tag(tag)
        gx, gy = rtransform(CRS.from_epsg(4326), CRS.from_proj4(proj4(self.sat)),
                            [lon], [lat])
        if not (math.isfinite(gx[0]) and math.isfinite(gy[0])):
            return None
        cx = (gx[0] + FD_HALF_M) / res
        cy = (FD_HALF_M - gy[0]) / res

        tpl = (patch - patch.mean()) / (patch.std() + 1e-6)
        best = None
        for dy in range(-search_px, search_px + 1, 2):
            for dx in range(-search_px, search_px + 1, 2):
                y0 = int(cy - m / 2 + dy)
                x0 = int(cx - m / 2 + dx)
                if y0 < 0 or x0 < 0 or y0 + m > n or x0 + m > n:
                    continue
                win = disk[y0:y0 + m, x0:x0 + m]
                c = float(((win - win.mean()) / (win.std() + 1e-6) * tpl).mean())
                if best is None or c > best[0]:
                    best = (c, dx, dy)
        if best is None or best[0] < 0.25:
            return None            # no confident lock; skip rather than guess
        _, dx, dy = best
        return ((cx + dx) * res - FD_HALF_M,
                FD_HALF_M - (cy + dy) * res)

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


# ── Meso sectors ──────────────────────────────────────────────────────
# The ABI mesoscale sectors are 1000 x 1000 km boxes that operations
# REPOSITIONS several times a day to follow storms, so they have no fixed
# bounds. STAR publishes the position in the URL and in the CDN path, which is
# why this needs no AWS netCDF: a move simply creates a new directory.
#
# Meso timestamps carry SECONDS (13 digits, YYYYDDDHHMMSS) because the sector
# scans every 30-60 s, unlike every other STAR product's 11-digit stamp.
MESO_INDEX = "https://www.star.nesdis.noaa.gov/GOES/index.php"
# Half-extent of a meso box in geos metres, at the tier its pixels match.
MESO_HALF_M = 501_005.0


def parse_meso_tag(tag: str) -> tuple[float, float]:
    """'43N-93W' -> (43.0, -93.0). Rounded to whole degrees by STAR."""
    m = re.match(r"(\d+)([NS])-(\d+)([EW])", tag)
    if not m:
        raise ValueError(tag)
    lat = float(m.group(1)) * (1 if m.group(2) == "N" else -1)
    lon = float(m.group(3)) * (1 if m.group(4) == "E" else -1)
    return lat, lon


def meso_positions() -> list[dict]:
    """Active meso sectors, straight off STAR's own nav links."""
    try:
        html = _get(MESO_INDEX, 120).decode("utf8", "replace")
    except Exception:  # noqa: BLE001
        return []
    seen, out = set(), []
    for sat, la, ns, lo, ew in re.findall(
            r"meso\.php\?sat=(G\d+)&(?:amp;)?lat=(\d+)([NS])"
            r"&(?:amp;)?lon=(\d+)([EW])", html):
        if sat not in SATELLITES:
            continue
        tag = f"{la}{ns}-{lo}{ew}"
        key = (sat, tag)
        if key in seen:
            continue
        seen.add(key)
        out.append({"sat": sat, "tag": tag})
    return out


def meso_url(sat: str, tag: str, meso: int, stamp: str, px: int) -> str:
    c = SATELLITES[sat]["cdn"]
    return (f"{CDN}/{c}/ABI/MESO/{tag}/{meso:02d}/"
            f"{stamp}_{c}-ABI-MESO-{meso:02d}-{tag}-{px}x{px}.jpg")


def meso_frames(sat: str, tag: str, meso: int, px: int) -> list[str]:
    """Available 13-digit stamps for this sector, oldest first."""
    c = SATELLITES[sat]["cdn"]
    try:
        html = _get(f"{CDN}/{c}/ABI/MESO/{tag}/{meso:02d}/", 120).decode(
            "utf8", "replace")
    except Exception:  # noqa: BLE001
        return []
    return sorted(set(re.findall(
        rf"(\d{{13}})_{c}-ABI-MESO-{meso:02d}-{tag}-{px}x{px}\.jpg", html)))


def inscribed_box(sat: str, gx: float, gy: float,
                  half_m: float = MESO_HALF_M) -> list[float] | None:
    """Largest axis-aligned lon/lat box INSIDE a meso's fixed-grid square.

    A square in the fixed grid is a curved quad in lon/lat, so its lon/lat
    bounding box would poke outside the imagery and render black edges. Taking
    the innermost edge instead guarantees every pixel of the output is real.
    """
    from rasterio.crs import CRS
    from rasterio.warp import transform as rtransform

    geos = CRS.from_proj4(proj4(sat))
    wgs = CRS.from_epsg(4326)
    steps = [i / 24.0 for i in range(25)]
    west, east, south, north = [], [], [], []
    for t in steps:
        x = gx - half_m + 2 * half_m * t
        y = gy - half_m + 2 * half_m * t
        for px, py, bucket in (
                (gx - half_m, y, west), (gx + half_m, y, east),
                (x, gy - half_m, south), (x, gy + half_m, north)):
            lon, lat = rtransform(geos, wgs, [px], [py])
            if math.isfinite(lon[0]) and math.isfinite(lat[0]):
                bucket.append(lon[0] if bucket in (west, east) else lat[0])
    if not (west and east and south and north):
        return None
    box = [max(west), max(south), min(east), min(north)]
    if box[0] >= box[2] or box[1] >= box[3]:
        return None
    return box


class MesoFrame:
    """One meso image, warped from the fixed grid to a lon/lat box."""

    def __init__(self, sat: str, array, gx: float, gy: float, half_m: float):
        self.sat = sat
        self.array = array
        self.gx, self.gy, self.half_m = gx, gy, half_m

    @classmethod
    def fetch(cls, sat: str, tag: str, meso: int, stamp: str, px: int,
              gx: float, gy: float) -> "MesoFrame | None":
        import numpy as np
        from PIL import Image
        try:
            data = _get(meso_url(sat, tag, meso, stamp, px))
        except Exception:  # noqa: BLE001
            return None
        if len(data) < 20_000 or data[:2] != b"\xff\xd8":
            return None
        try:
            im = Image.open(io.BytesIO(data))
            if im.mode != "RGB":
                im = im.convert("RGB")
            arr = np.asarray(im).transpose(2, 0, 1)
        except Exception:  # noqa: BLE001
            return None
        return cls(sat, arr, gx, gy, MESO_HALF_M)

    def crop(self, bounds: list[float], out_w: int, out_h: int,
             jpeg_quality: int = 82) -> bytes | None:
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
        res = 2 * self.half_m / self.array.shape[1]
        src_t = Affine(res, 0, self.gx - self.half_m,
                       0, -res, self.gy + self.half_m)
        dst = np.zeros((3, out_h, out_w), "uint8")
        dst_t = Affine((mx(e) - mx(w)) / out_w, 0, mx(w),
                       0, -(my(n) - my(s)) / out_h, my(n))
        try:
            reproject(self.array, dst,
                      src_transform=src_t, src_crs=CRS.from_proj4(proj4(self.sat)),
                      dst_transform=dst_t, dst_crs=CRS.from_epsg(3857),
                      resampling=Resampling.cubic, num_threads=2)
        except Exception:  # noqa: BLE001
            return None
        buf = io.BytesIO()
        Image.fromarray(dst.transpose(1, 2, 0)).save(
            buf, "JPEG", quality=jpeg_quality, optimize=True)
        return buf.getvalue()
