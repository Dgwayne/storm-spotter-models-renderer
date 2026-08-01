#!/usr/bin/env python3
"""render_goes_geocolor.py — rolling windows of GOES-East + GOES-West GeoColor
frames, per REGION, for the app's satellite animation. Served from B2 behind
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
import io
import json
import math
import os
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

from PIL import Image

import slider_source  # CIRA SLIDER geostationary tiles -> EPSG:3857
import star_source    # NESDIS STAR full disk -> EPSG:3857

# ── Regions (lon/lat W,S,E,N + GOES bird). Everything else is derived. ──
# Each region names its satellite ("sat"): East boxes pull
# GOES-East_ABI_GeoColor, West boxes GOES-West_ABI_GeoColor. CONUS / Western US
# are the wide overviews; the rest are focused boxes that come out sharper
# (they don't hit the pixel cap). Adding a region is one line here — the
# manifest + frame tree follow automatically.
REGIONS: dict[str, dict] = {
    # GOES-East
    "conus":     {"label": "CONUS",            "sat": "GOES-East", "bounds": [-125.0, 22.0, -66.0, 50.0]},
    "gulf":      {"label": "Gulf",             "sat": "GOES-East", "bounds": [-98.0, 17.5, -80.0, 31.0]},
    "atlantic":  {"label": "Western Atlantic", "sat": "GOES-East", "bounds": [-82.0, 24.0, -58.0, 45.0]},
    "caribbean": {"label": "Caribbean",        "sat": "GOES-East", "bounds": [-89.0, 9.0, -58.0, 24.0]},
    # GOES-West
    "west":      {"label": "Western US",       "sat": "GOES-West", "bounds": [-135.0, 28.0, -104.0, 52.0]},
    "pacific":   {"label": "North Pacific",    "sat": "GOES-West", "bounds": [-160.0, 20.0, -118.0, 50.0]},
    # East Pacific hurricane basin — Baja + the Mexican coast + the open water
    # storms track through. The North Pacific box stops at 20 N and -118, so
    # nothing covered this until now.
    "epac":      {"label": "E Pacific Tropics", "sat": "GOES-West", "bounds": [-135.0, 8.0, -95.0, 32.0]},
    # Central Pacific basin (CPHC's water) + the approach to Hawaii. Overlaps
    # epac's west edge by 3 deg so a storm crossing 140 W stays in frame
    # through the handoff; the Hawaii box stays the tight island zoom.
    "cpac":      {"label": "Central Pacific",   "sat": "GOES-West", "bounds": [-175.0, 8.0, -132.0, 32.0]},
    "hawaii":    {"label": "Hawaii",           "sat": "GOES-West", "bounds": [-161.5, 18.0, -154.0, 23.0]},
    "alaska":    {"label": "Alaska",           "sat": "GOES-West", "bounds": [-172.0, 51.0, -129.0, 63.0]},
    # Himawari-9 (140.7 E) via CIRA SLIDER: the Western Pacific typhoon basin.
    # GOES cannot serve this: GOES-West's usable limb stops near 142 E, and even
    # where it does reach (a storm at 160 E sits 62 deg off nadir) GeoColor's
    # Rayleigh correction breaks down and renders washed out and brown-tinted.
    # GIBS has no Himawari GeoColor at all, so this one is warped from SLIDER's
    # geostationary tiles (see slider_source.py). Deliberately a BASIN, not a
    # storm box: it holds a typhoon from the open Pacific through the approach
    # to Japan and the East China Sea, and serves every future storm too.
    # Sized to land just under the 4096 single-texture cap, so it renders at its
    # full ~1.2 km without being clipped the way CONUS is.
    # ── NESDIS STAR, GOES-East (G19) ──────────────────────────────────
    # Cut from the STAR full disk rather than GIBS. STAR publishes a finished
    # whole-sector image per timestamp or nothing, so it cannot emit the
    # partial renders that put black rectangles in GIBS frames. One 18 MB full
    # disk per slot serves every region below, so upstream load does not grow
    # with the region count. See star_source.py for the fixed-grid constants.
    #
    # The GIBS regions above are deliberately LEFT IN PLACE: shipped apps read
    # them out of geocolor.json by id and cannot switch until they update.
    #
    # Boxes are checked against the visible disk (limb = 81.3 deg from the
    # sub-point). Everything here is on-disk; "oblique" corners over open ocean
    # are inherent to the geometry and are what STAR shows too.
    "conus_e":  {"label": "CONUS",              "sat": "G19", "source": "star", "bounds": [-125.0, 22.0, -66.0, 50.0]},
    "fd_e":     {"label": "Full Disk",          "sat": "G19", "source": "star", "bounds": [-135.0, -60.0, -5.0, 60.0]},
    "nr":       {"label": "Northern Rockies",   "sat": "G19", "source": "star", "bounds": [-118.0, 41.0, -101.0, 52.0]},
    "umv":      {"label": "Upper Mississippi Valley", "sat": "G19", "source": "star", "bounds": [-100.0, 38.0, -84.0, 49.0]},
    "cgl":      {"label": "Great Lakes",        "sat": "G19", "source": "star", "bounds": [-93.0, 40.0, -74.0, 50.0]},
    "ne":       {"label": "Northeast",          "sat": "G19", "source": "star", "bounds": [-82.0, 37.0, -66.0, 48.0]},
    "sr":       {"label": "Southern Rockies",   "sat": "G19", "source": "star", "bounds": [-115.0, 31.0, -100.0, 42.0]},
    "sp":       {"label": "Southern Plains",    "sat": "G19", "source": "star", "bounds": [-105.0, 28.0, -90.0, 40.0]},
    "smv":      {"label": "Southern Mississippi Valley", "sat": "G19", "source": "star", "bounds": [-97.0, 28.0, -82.0, 39.0]},
    "se":       {"label": "Southeast",          "sat": "G19", "source": "star", "bounds": [-92.0, 24.0, -75.0, 37.0]},
    "eus":      {"label": "U.S. East Coast",    "sat": "G19", "source": "star", "bounds": [-84.0, 30.0, -66.0, 45.0]},
    # NW corner pulled in from [-142, 70]: that reached 82.3 deg, past the limb.
    "can":      {"label": "Canada",             "sat": "G19", "source": "star", "bounds": [-132.0, 42.0, -52.0, 67.0]},
    "na":       {"label": "Northern Atlantic Ocean", "sat": "G19", "source": "star", "bounds": [-80.0, 25.0, -20.0, 55.0]},
    "car":      {"label": "Caribbean",          "sat": "G19", "source": "star", "bounds": [-90.0, 8.0, -58.0, 27.0]},
    "ga":       {"label": "Gulf of America",    "sat": "G19", "source": "star", "bounds": [-98.0, 17.0, -80.0, 31.0]},
    "pr":       {"label": "Puerto Rico",        "sat": "G19", "source": "star", "bounds": [-70.0, 15.0, -62.0, 20.0]},
    "taw":      {"label": "Tropical Atlantic Ocean", "sat": "G19", "source": "star", "bounds": [-70.0, 0.0, -10.0, 30.0]},
    "eep":      {"label": "Eastern Pacific Ocean", "sat": "G19", "source": "star", "bounds": [-140.0, 0.0, -85.0, 30.0]},
    "mex":      {"label": "Mexico",             "sat": "G19", "source": "star", "bounds": [-118.0, 13.0, -85.0, 33.0]},
    "cam":      {"label": "Central America",    "sat": "G19", "source": "star", "bounds": [-95.0, 5.0, -75.0, 20.0]},
    "nsa":      {"label": "South America (north)", "sat": "G19", "source": "star", "bounds": [-82.0, -15.0, -34.0, 13.0]},
    "ssa":      {"label": "South America (south)", "sat": "G19", "source": "star", "bounds": [-80.0, -56.0, -34.0, -15.0]},
    # ── NESDIS STAR, GOES-West (G18) ──────────────────────────────────
    # Same full-disk cut as GOES-East, one 18 MB disk per slot for all of them.
    # ⚠ GOES-West's disk reaches past the antimeridian (its limb sits near
    # 142 E), but a Mapbox ImageSource box is [W,S,E,N] with W < E and cannot
    # cross +/-180. Every box below therefore stops at -180 and the far-western
    # limb is simply not offered; the Himawari `wpac` region covers that water
    # far better anyway.
    "pacus":    {"label": "PACUS",              "sat": "G18", "source": "star", "bounds": [-140.0, 22.0, -90.0, 52.0]},
    # East corners pulled in from -60: those reached 83.6 deg, past the limb.
    "fd_w":     {"label": "Full Disk",          "sat": "G18", "source": "star", "bounds": [-180.0, -55.0, -70.0, 55.0]},
    "pnw":      {"label": "Pacific Northwest",  "sat": "G18", "source": "star", "bounds": [-130.0, 41.0, -110.0, 52.0]},
    "psw":      {"label": "Pacific Southwest",  "sat": "G18", "source": "star", "bounds": [-126.0, 31.0, -108.0, 43.0]},
    "wus":      {"label": "U.S. West Coast",    "sat": "G18", "source": "star", "bounds": [-130.0, 30.0, -112.0, 50.0]},
    "ak":       {"label": "Alaska",             "sat": "G18", "source": "star", "bounds": [-172.0, 51.0, -129.0, 72.0]},
    "cak":      {"label": "Central Alaska",     "sat": "G18", "source": "star", "bounds": [-160.0, 58.0, -140.0, 69.0]},
    "sea":      {"label": "Southeastern Alaska", "sat": "G18", "source": "star", "bounds": [-145.0, 54.0, -128.0, 62.0]},
    "np":       {"label": "Northern Pacific Ocean", "sat": "G18", "source": "star", "bounds": [-180.0, 30.0, -120.0, 60.0]},
    "hi":       {"label": "Hawaii",             "sat": "G18", "source": "star", "bounds": [-162.0, 17.0, -152.0, 24.0]},
    "tpw":      {"label": "Tropical Pacific Ocean", "sat": "G18", "source": "star", "bounds": [-180.0, -5.0, -110.0, 25.0]},
    "tsp":      {"label": "Southern Pacific Ocean", "sat": "G18", "source": "star", "bounds": [-180.0, -40.0, -100.0, -5.0]},
    "wpac":      {"label": "W Pacific",        "sat": "Himawari",  "bounds": [128.0, 8.0, 172.0, 36.0],
                  "source": "slider", "slider_sat": "himawari",
                  "sector": "full_disk", "product": "geocolor",
                  # At the default 1.2 km this box is 11.6 MP / ~3 MB a frame,
                  # roughly double the CONUS payload. Matching CONUS's own
                  # effective resolution puts it back in line with the rest.
                  "target_mpp": 1600.0,
                  # Started at 360 while the source was unproven. Raised to the
                  # default 12 h once the storage report showed what it really
                  # costs: 0.54 MB a frame on average (most of a day here is
                  # night over open ocean, which compresses to very little), so
                  # a full window is ~40 MB. Steady-state CIRA load is
                  # unchanged either way, since that is one new frame per slot
                  # regardless of how far back the window reaches; only the
                  # one-time backfill is longer. Anything shorter than the
                  # app's longest loop choice makes its 12 h picker dishonest.
                  # (No window_min key at all: _window_for falls back to the
                  # default. WINDOW_MIN is defined below this table, so naming
                  # it here would be a NameError at import.)
                  },
}

GIBS_WMS = "https://gibs.earthdata.nasa.gov/wms/epsg3857/best/wms.cgi"


def _source_of(region: str) -> str:
    """'gibs' (GOES, EPSG:3857 straight from a GetMap) or 'slider' (Himawari,
    geostationary tiles we reproject ourselves)."""
    return REGIONS[region].get("source", "gibs")


def _window_for(region: str) -> int:
    """Rolling window in minutes. Per-region so an expensive source can start
    with a short history without shortening everyone else's."""
    return int(REGIONS[region].get("window_min", WINDOW_MIN))


def _layer_for(region: str) -> str:
    return f"{REGIONS[region]['sat']}_ABI_GeoColor"

# Pixel budget. Target ~1.2 km/px (GIBS-native), capped at the 4096-px GPU
# single-texture limit. The smaller boxes get their full 1.2 km well under the
# cap; only CONUS is wide enough to hit it, landing at 4096 px / ~1.6 km — as
# sharp as one texture allows over the lower-48 box (the app reads each frame's
# size from its JPEG, so a dims change needs no app update).
TARGET_MPP = 1200.0
MAX_PX = 4096
# Floor so a tiny box (Hawaii) still fills a high-DPI phone screen without the
# client upscaling a too-small frame into mush. Above ~1.2 km this over-samples
# GIBS's native data, but that reads far cleaner than a stretched 1024-px frame.
MIN_PX = 2048
A = 6378137.0  # WGS84 semi-major axis (EPSG:3857)

# Rolling window (minutes) + cadence. GIBS publishes GeoColor on a 10-min grid
# and runs ~40 min behind wall clock. 720 min = 12 h covers the app's longest
# loop; at 10 min that's up to 73 candidate slots per region.
WINDOW_MIN = 720
STEP_MIN = 10
LATENCY_MIN = 40

# Download parallelism across all (region, slot) tasks. GIBS tolerates a small
# burst; 8 keeps a cold 9-region backfill well under the job timeout (steady-
# state runs only fetch the newest slot or two per region).
CONCURRENCY = 8

# ── SLIDER politeness (Himawari regions) ──────────────────────────────
# GIBS is NASA infrastructure built for programmatic access; CIRA SLIDER is a
# research web app, and one frame there costs a few dozen tile requests instead
# of one GetMap. So slider regions run in their own serialized pass: at most
# SLIDER_TILE_CONCURRENCY connections open at any moment (NOT multiplied by the
# outer pool), and at most SLIDER_MAX_FRAMES_PER_RUN new frames per run. A cold
# start therefore fills in gradually over successive runs instead of arriving as
# one burst — the whole pipeline is idempotent, so that costs nothing but time.
SLIDER_TILE_CONCURRENCY = 4
SLIDER_MAX_FRAMES_PER_RUN = 6
# We encode these ourselves (GIBS hands us a finished JPEG). 82 is where cloud
# texture still holds up but the payload stops ballooning.
SLIDER_JPEG_QUALITY = 82
# SLIDER publishes well ahead of GIBS (observed ~10-20 min vs GIBS's 40-76),
# so its regions look for newer slots than the GOES ones.
SLIDER_LATENCY_MIN = 20

# ── STAR (GOES regions) ───────────────────────────────────────────────
# One full disk per (satellite, slot) is decoded ONCE and every region for that
# satellite is cut from it, so 22 regions cost 22 warps but only ONE 18 MB
# download. The per-run slot cap bounds a cold start: at 4 slots a run the 12 h
# window fills in about 3 h, and steady state only ever needs 1.
STAR_KM = 1000                  # full-disk tier: 1 km (10848 px, ~18 MB)
STAR_MAX_SLOTS_PER_RUN = 4
STAR_JPEG_QUALITY = 82
# STAR publishes ~15-20 min behind wall clock, far ahead of GIBS's 40-76.
STAR_LATENCY_MIN = 25

R2_PREFIX = "v1/SAT"
GEO_PREFIX = f"{R2_PREFIX}/geocolor"
MANIFEST_KEY = f"{R2_PREFIX}/geocolor.json"


# ── Geometry ──────────────────────────────────────────────────────────
def _merc_x(lon: float) -> float:
    return lon * math.pi / 180.0 * A


def _merc_y(lat: float) -> float:
    return math.log(math.tan((90.0 + lat) * math.pi / 360.0)) * A


def _region_geom(bounds: list[float],
                 target_mpp: float = TARGET_MPP) -> tuple[str, int, int]:
    """(bbox3857 string, width, height) for a lon/lat [W,S,E,N] box. Width
    targets ~1.2 km/px capped to [MIN_PX, MAX_PX]; height keeps square pixels
    (also capped). Square pixels + an EPSG:3857 bbox mean the app pins an
    ImageSource to the lon/lat corners with no projection mismatch.

    target_mpp is per-region because frame BYTES scale with box area at a fixed
    m/px: a basin-sized box at the default 1.2 km would be ~2x the payload of
    the CONUS frames we already ship. Coarsening such a region to CONUS's own
    effective resolution keeps the whole set consistent AND affordable. Note the
    tile fetch is unaffected — we still pull the finer zoom and downsample it,
    which anti-aliases better than fetching the coarser one."""
    w, s, e, n = bounds
    xw = _merc_x(e) - _merc_x(w)
    yh = _merc_y(n) - _merc_y(s)
    width = min(MAX_PX, max(MIN_PX, round(xw / target_mpp)))
    height = min(MAX_PX, round(width * yh / xw))
    bbox = f"{_merc_x(w):.4f},{_merc_y(s):.4f},{_merc_x(e):.4f},{_merc_y(n):.4f}"
    return bbox, width, height


def _has_nodata_void(data: bytes, n: int = 224) -> bool:
    """True when GIBS returned a PARTIALLY rendered frame.

    GIBS composes a GetMap from source granules. When some are unpublished for
    that timestamp it still returns 200 with the missing granules as hard-edged
    black rectangles cut into otherwise perfect imagery. Measured on the live
    feed: 7.4% of stored frames, up to 23% for a single region. These are what
    flash past as black boxes mid-loop.

    The blank check below cannot catch them: it only fires under 2% lit, and a
    frame that is 79% real imagery sails through.

    Two signatures separate a void from night, which is the thing that makes
    this hard (dark ocean at night is legitimately near-black, and rejecting it
    would gut every night loop):

      * WHAT BORDERS THE DARK. A void is bounded by bright daylight imagery;
        night fades into more darkness. Measured: voids 53-127 mean boundary
        luminance, night and terminator 13-38.
      * WHETHER THE EDGE IS STRAIGHT. A missing granule is an axis-aligned
        rectangle; a terminator is a smooth curve. Measured as the longest run
        of rows sharing one transition column (or vice versa): voids 22-58% of
        the frame, night and terminator 0-6%.

    Both must hold, so a merely dark frame is never dropped.
    """
    try:
        import numpy as np
    except ImportError:
        return False  # detection unavailable: keep the frame, never crash a run
    try:
        im = Image.open(io.BytesIO(data))
        im.draft("RGB", (n * 2, n * 2))
        # NEAREST on purpose: smooth resampling blurs the hard edge we detect.
        a = np.asarray(im.convert("RGB").resize((n, n), Image.NEAREST))
        lum = a.max(axis=2)
        blk = lum <= 6
        frac = float(blk.mean())
        if frac < 0.005 or frac > 0.97:
            return False  # nothing dark, or fully blank (the other check owns that)

        ring = blk.copy()
        for _ in range(3):
            ring = (ring | np.roll(ring, 1, 0) | np.roll(ring, -1, 0)
                    | np.roll(ring, 1, 1) | np.roll(ring, -1, 1))
        ring = ring & ~blk
        # The frame border is not evidence of anything; a real region can sit
        # flush against it.
        ring[:4, :] = ring[-4:, :] = ring[:, :4] = ring[:, -4:] = False
        if ring.sum() < 40:
            return False
        if float(lum[ring].mean()) < 45.0:
            return False  # darkness bounded by darkness = night, keep it

        def _longest(col) -> int:
            best = cur = 0
            for v in col:
                cur = cur + 1 if v else 0
                best = max(best, cur)
            return best

        vt = blk[:, :-1] != blk[:, 1:]
        ht = blk[:-1, :] != blk[1:, :]
        straight = max(
            max((_longest(vt[:, x]) for x in range(vt.shape[1])), default=0),
            max((_longest(ht[y, :]) for y in range(ht.shape[0])), default=0),
        ) / n
        return straight >= 0.12
    except Exception:  # noqa: BLE001 - undecodable: let the caller keep it
        return False


def _reject_frame(data: bytes) -> bool:
    """True when a JPEG should be dropped for quality. One reduced-scale decode
    catches two failure modes:
      * BLANK — GIBS's uniform no-data render. Byte size can't tell it from a
        real dark-ocean night frame (both compress tiny), so we require >=2% of
        pixels to carry real luminance (max(r,g,b) >= 16). Clouds + city lights
        light up far more than that; no-data is uniform black. Mirrors the app's
        isBlankGibsFrame check.
      * BAND — a GOES scan artifact: a run of rows tinted bright cyan when a
        colour channel (the red band) drops out of a scan swath, seen
        transiently on GOES-West. We count rows that are a full-width bright-cyan
        band (B high, G high, R well below B); a healthy scene has none (deep
        ocean is dark, turquoise coast is localized, cloud is white)."""
    try:
        im = Image.open(io.BytesIO(data))
        im.draft("RGB", (96, 96))  # fast reduced-scale JPEG decode
        im = im.convert("RGB").resize((96, 96))
        px = im.load()
        lit = band = 0
        for y in range(96):
            cyan = 0
            for x in range(96):
                r, g, b = px[x, y]
                if r >= 16 or g >= 16 or b >= 16:
                    lit += 1
                if b > 180 and g > 150 and r < b - 60:
                    cyan += 1
            if cyan > 96 * 0.4:  # a full-width cyan row
                band += 1
        if lit < 96 * 96 * 0.02 or band >= 5:
            return True
        return _has_nodata_void(data)
    except Exception:  # noqa: BLE001 — undecodable: let the caller keep it
        return False


# ── Time grid ─────────────────────────────────────────────────────────
def _slots(now: dt.datetime, window_min: int = WINDOW_MIN,
           latency_min: int = LATENCY_MIN) -> list[dt.datetime]:
    """10-min-aligned UTC slots across the window, newest last, anchored at
    now - latency (floored to the grid). SLIDER publishes far sooner than GIBS,
    so its regions pass a smaller latency and pick up frames GIBS has not
    rendered yet."""
    latest_raw = now - dt.timedelta(minutes=latency_min)
    latest = latest_raw.replace(
        minute=(latest_raw.minute // STEP_MIN) * STEP_MIN, second=0, microsecond=0)
    return [latest - dt.timedelta(minutes=m)
            for m in range(window_min, -1, -STEP_MIN)]


def _time_iso(slot: dt.datetime) -> str:
    return slot.strftime("%Y-%m-%dT%H:%M:00Z")


def _gibs_url(layer: str, bbox3857: str, width: int, height: int,
              slot: dt.datetime) -> str:
    return (
        f"{GIBS_WMS}?service=WMS&request=GetMap&version=1.3.0"
        f"&layers={layer}&styles=&format=image/jpeg"
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


def _download(url: str) -> bytes | None:
    """Fetch one GIBS frame. None for network errors and non-JPEG bodies (WMS
    XML exceptions)."""
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "storm_spotter_geocolor/1.0"})
        with urllib.request.urlopen(req, timeout=90) as r:
            data = r.read()
    except Exception as exc:  # noqa: BLE001 — best-effort; retried/backfilled
        print(f"  fetch error {url[-40:]}: {exc}", file=sys.stderr)
        return None
    if len(data) < 1000 or data[:2] != b"\xff\xd8":  # empty / XML exception
        return None
    return data


def _upload(src: Path, key: str, bucket: str) -> None:
    subprocess.check_call([
        "rclone", "copyto", str(src), f"r2:{bucket}/{key}",
        "--s3-no-check-bucket", "--no-traverse",
        "--header-upload", "Cache-Control: public, max-age=900",
    ])


class _Task:
    __slots__ = ("region", "slot", "key", "url", "bounds", "width", "height")

    def __init__(self, region, slot, key, url=None,
                 bounds=None, width=0, height=0):
        self.region = region
        self.slot = slot
        self.key = key
        self.url = url          # gibs regions
        self.bounds = bounds    # slider regions
        self.width = width
        self.height = height


def _render(task: _Task) -> bytes | None:
    """Produce one frame's JPEG bytes from whichever source the region uses."""
    if _source_of(task.region) != "slider":
        return _download(task.url)
    cfg = REGIONS[task.region]
    return slider_source.render(
        task.bounds, task.width, task.height, task.slot,
        sat=cfg["slider_sat"], sector=cfg["sector"], product=cfg["product"],
        concurrency=SLIDER_TILE_CONCURRENCY, jpeg_quality=SLIDER_JPEG_QUALITY)


def _fetch(task: _Task, bucket: str) -> str | None:
    """Build + upload one (region, slot); one retry (GIBS's on-demand WMS can
    return a transient blank/XML on the first hit even when the frame exists,
    and a SLIDER tile can drop out mid-mosaic). Never raises: any error leaves
    the slot un-stored for a later idempotent run. Returns the key on success."""
    try:
        for _ in range(2):
            data = _render(task)
            if data is None:
                continue
            # blank no-data render, or a cyan scan-band artifact
            if _reject_frame(data):
                continue
            with tempfile.TemporaryDirectory() as td:
                dest = Path(td) / "frame.jpg"
                dest.write_bytes(data)
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


# ── Repair ────────────────────────────────────────────────────────────
def _repair(bucket: str, existing: set[str], geom: dict) -> None:
    """Re-fetch frames ALREADY on B2 that fail today's quality gate.

    Needed because the pipeline is idempotent: it skips any key already
    present, so frames stored before a detector improvement would otherwise sit
    in the loop until they age out.

    NON-DESTRUCTIVE. A frame is only ever overwritten by a replacement that
    passes the gate; if GIBS still serves a bad render for that timestamp the
    existing frame is left exactly as it was. Nothing is deleted.

    Only GIBS regions are scanned. The SLIDER path cannot store a partial
    frame in the first place (it refuses to assemble an incomplete mosaic).
    """
    targets = [k for k in existing
               if len(k.split("/")) == 5
               and k.split("/")[3] in REGIONS
               and _source_of(k.split("/")[3]) == "gibs"]
    if not targets:
        print("==> repair: nothing to scan")
        return
    print(f"==> repair: pulling {len(targets)} stored frames to re-check")
    with tempfile.TemporaryDirectory() as td:
        local = Path(td)
        subprocess.check_call([
            "rclone", "copy", f"r2:{bucket}/{GEO_PREFIX}/", str(local),
            "--transfers", "16", "--checkers", "16", "--s3-no-check-bucket",
        ])
        bad = []
        for key in sorted(targets):
            p = local / key[len(GEO_PREFIX) + 1:]
            if not p.is_file():
                continue
            try:
                if _reject_frame(p.read_bytes()):
                    bad.append(key)
            except Exception:  # noqa: BLE001 - unreadable: leave it alone
                continue
        print(f"==> repair: {len(bad)} of {len(targets)} fail the gate")

        fixed = stubborn = 0
        for key in bad:
            parts = key.split("/")
            region, stamp = parts[3], parts[4].removesuffix(".jpg")
            try:
                slot = dt.datetime.strptime(stamp, "%Y%m%d%H%M").replace(
                    tzinfo=dt.timezone.utc)
            except ValueError:
                continue
            bbox, width, height = geom[region]
            url = _gibs_url(_layer_for(region), bbox, width, height, slot)
            data = None
            for _ in range(2):  # GIBS often serves a good render on a re-ask
                got = _download(url)
                if got is not None and not _reject_frame(got):
                    data = got
                    break
            if data is None:
                stubborn += 1
                print(f"    {region} {stamp}: still bad upstream, left as-is")
                continue
            dest = local / "repair.jpg"
            dest.write_bytes(data)
            _upload(dest, key, bucket)
            fixed += 1
            print(f"    {region} {stamp}: replaced")
        print(f"==> repair: {fixed} replaced, {stubborn} still bad upstream "
              f"(those age out of the window on their own)")
        if fixed:
            print("    NOTE: Cloudflare may serve the previous copy of a "
                  "replaced frame until its edge TTL expires.")


# ── Main ──────────────────────────────────────────────────────────────
def main() -> int:
    bucket = os.environ["R2_BUCKET"]
    force = bool(os.environ.get("FORCE_RERENDER"))
    now = dt.datetime.now(dt.timezone.utc)

    if force:
        print("==> FORCE: purging geocolor tree for a clean rebuild")
        _purge(bucket)
        existing: set[str] = set()
    else:
        existing = _list_existing(bucket)

    # Per-region geometry (bbox / dims), computed once.
    geom = {r: _region_geom(REGIONS[r]["bounds"],
                            REGIONS[r].get("target_mpp", TARGET_MPP))
            for r in REGIONS}
    for r, (_, w, h) in geom.items():
        print(f"    {r}: {w}x{h}")

    gibs_tasks: list[_Task] = []
    slider_tasks: list[_Task] = []
    # STAR work is keyed by (satellite, slot): every region for that pair comes
    # out of ONE decoded full disk.
    star_pending: dict[tuple[str, dt.datetime], list[_Task]] = {}
    star_times: dict[str, set[str]] = {}
    for region in REGIONS:
        bbox, width, height = geom[region]
        src = _source_of(region)
        slider = src == "slider"
        latency = (SLIDER_LATENCY_MIN if slider
                   else STAR_LATENCY_MIN if src == "star" else LATENCY_MIN)
        slots = _slots(now, _window_for(region), latency)
        if src == "star":
            cfg = REGIONS[region]
            sat = cfg["sat"]
            if sat not in star_times:
                star_times[sat] = star_source.available_times(
                    sat, star_source.FD_PX[STAR_KM])
            have = star_times[sat]
            for slot in slots:
                key = _key(region, slot)
                if key in existing:
                    continue
                if have and star_source._stamp(slot) not in have:
                    continue
                star_pending.setdefault((sat, slot), []).append(
                    _Task(region, slot, key, bounds=cfg["bounds"],
                          width=width, height=height))
            continue
        if slider:
            # Ask SLIDER what it actually holds before spending tile requests on
            # slots that do not exist.
            cfg = REGIONS[region]
            have = slider_source.available_times(
                cfg["slider_sat"], cfg["sector"], cfg["product"])
            pending = [s for s in slots
                       if _key(region, s) not in existing
                       and (not have or f"{s:%Y%m%d%H%M}00" in have)]
            # Newest first: if the per-run cap bites, the freshest frames are
            # the ones that land.
            for slot in sorted(pending, reverse=True)[:SLIDER_MAX_FRAMES_PER_RUN]:
                slider_tasks.append(_Task(region, slot, _key(region, slot),
                                          bounds=cfg["bounds"],
                                          width=width, height=height))
            if len(pending) > SLIDER_MAX_FRAMES_PER_RUN:
                print(f"    {region}: {len(pending)} slots pending, taking "
                      f"{SLIDER_MAX_FRAMES_PER_RUN} this run (backfills over "
                      f"later runs)")
            continue
        layer = _layer_for(region)
        for slot in slots:
            key = _key(region, slot)
            if key in existing:
                continue
            gibs_tasks.append(_Task(region, slot, key,
                                    url=_gibs_url(layer, bbox, width, height,
                                                  slot)))
    # Newest slots first, so if the per-run cap bites the freshest imagery is
    # what lands; the rest backfills over later runs.
    star_slots = sorted(star_pending, key=lambda k: k[1], reverse=True)
    star_take = star_slots[:STAR_MAX_SLOTS_PER_RUN]
    star_n = sum(len(star_pending[k]) for k in star_take)
    if len(star_slots) > STAR_MAX_SLOTS_PER_RUN:
        print(f"    STAR: {len(star_slots)} slots pending, taking "
              f"{STAR_MAX_SLOTS_PER_RUN} this run (backfills over later runs)")
    print(f"==> GeoColor render: {len(REGIONS)} regions; {len(existing)} on B2, "
          f"{len(gibs_tasks)} GIBS + {len(slider_tasks)} SLIDER + "
          f"{star_n} STAR frames from {len(star_take)} full disks")

    rendered = 0
    if gibs_tasks:
        with cf.ThreadPoolExecutor(max_workers=CONCURRENCY) as pool:
            for key in pool.map(lambda t: _fetch(t, bucket), gibs_tasks):
                if key:
                    existing.add(key)
                    rendered += 1
    # Serialized on purpose: _render already opens SLIDER_TILE_CONCURRENCY
    # connections per frame, and running frames in parallel too would multiply
    # them into a burst against CIRA.
    for task in slider_tasks:
        key = _fetch(task, bucket)
        if key:
            existing.add(key)
            rendered += 1

    # One download + one decode per (satellite, slot), then cut every region.
    for sat, slot in star_take:
        tasks = star_pending[(sat, slot)]
        fd = star_source.FullDisk.fetch(sat, slot, STAR_KM)
        if fd is None:
            print(f"    STAR {sat} {slot:%Y%m%d%H%M}: full disk unavailable")
            continue
        made = 0
        for task in tasks:
            try:
                data = fd.crop(task.bounds, task.width, task.height,
                               STAR_JPEG_QUALITY)
                if data is None or _reject_frame(data):
                    continue
                with tempfile.TemporaryDirectory() as td:
                    dest = Path(td) / "frame.jpg"
                    dest.write_bytes(data)
                    _upload(dest, task.key, bucket)
                existing.add(task.key)
                rendered += 1
                made += 1
            except Exception as exc:  # noqa: BLE001 - retried next run
                print(f"    {task.region} {slot:%Y%m%d%H%M} failed: {exc}",
                      file=sys.stderr)
        del fd  # ~350 MB of decoded disk; drop it before the next slot
        print(f"    STAR {sat} {slot:%H%M}Z: {made}/{len(tasks)} regions")

    # Prune anything older than the window so storage stays bounded.
    # Per-region, since a region may run a shorter window than the default.
    cutoffs = {r: now - dt.timedelta(
        minutes=LATENCY_MIN + _window_for(r) + STEP_MIN) for r in REGIONS}
    default_cutoff = now - dt.timedelta(
        minutes=LATENCY_MIN + WINDOW_MIN + STEP_MIN)
    pruned = 0
    for key in list(existing):
        parts = key.split("/")
        cutoff = cutoffs.get(parts[3], default_cutoff) if len(parts) == 5 \
            else default_cutoff
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

    # After the normal pass, so a repair run also picks up this tick's frames.
    if os.environ.get("REPAIR_SCAN"):
        _repair(bucket, existing, geom)

    _write_manifest(bucket, existing, now)
    print(f"==> done: {rendered} new frames, {pruned} pruned")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
