#!/usr/bin/env python3
"""MRMS 3D reflectivity mosaic -> voxel tiles for the app's 3D radar volume.

The app's "3D" radar chip ray-marches a uint8 voxel brick (app repo,
lib/data/nexrad/gpu/radar_volume_brick.dart, wire format 'RVB2'). Phases 1
and 2 built that brick on-device from one site's Level II volume. This is
the phase 3 producer: it cuts the SAME voxel encodings from NOAA's national
MRMS 3D mosaic on noaa-mrms-pds, so the volume works anywhere in CONUS, off
any radar product, with no site selection and no wait for scan depth.

Source (all ~2-min cadence; 0.01 deg CONUS grid, 7000 x 3500 cells,
lon -130..-60, lat 55..20 north to south):
  CONUS/MergedReflectivityQC_<lvl>/    33 CAPPIs 0.5..19 km   -> REF plane
  CONUS/MergedRhoHV_<lvl>/             same levels, used to 8 km -> CC plane
  CONUS/MergedAzShear_0-2kmAGL_00.50/  layer-max azimuthal shear -> rotation
  CONUS/MergedAzShear_3-6kmAGL_00.50/    plane, extruded over its own layer

Output (B2 behind models.dgwaynes.com, rclone remote "r2:"):
  v1/VOL3D/latest.json                 newest stamp + per-tile summary
  v1/VOL3D/<stamp>/index.json          the same, immutable per stamp (loops)
  v1/VOL3D/<stamp>/r<RR>c<CC>.rvt.gz   one 2x2 deg tile, 200x200x38 voxels
                                       per channel, gzip. EMPTY TILES ARE
                                       NOT WRITTEN: absent means no echo.

Tile wire format 'RVT1', little-endian (decoded by the app's
radar_volume_mosaic.dart; keep the two in lock-step):
   0 u32 magic 'RVT1' (0x31545652)
   4 u32 nx | 8 u32 ny | 12 u32 nz
  16 f64 lonWest | 24 f64 latNorth       tile NW corner, degrees
  32 f64 dLonDeg | 40 f64 dLatDeg        cell size; rows run SOUTH
  48 f32 z0Metres | 52 f32 dzMetres      level k spans z0+k*dz .. z0+(k+1)*dz
  56 u32 channelMask (1 REF, 2 CC, 4 rotation)
  60 u32 headerBytes (80)
  64 u64 valid time, epoch milliseconds
  72 u32 reserved | 76 u32 reserved
  80 planes in mask order, nx*ny*nz bytes each: X fastest (west -> east),
     then Y (north -> south), then Z (ground up)

Voxel encodings are byte-identical to the on-device brick, so the shader,
its thresholds and the status text need no mosaic-specific path:
  REF  0 = none, else round((dBZ + 32) * 2) + 2, clamped 2..255
  CC   round((1.05 - CC) * 200), 0 = clean / none (inverted so the hosts'
       linear texture filtering can only FADE a signature, never invent one)
  ROT  128 + round(shear[1e-3 1/s] * 5), 128 = none

Vertical: 38 uniform 500 m levels from the ground, because the shader
samples the 3D texture linearly in z and the on-device brick is uniform
too. Level centres sit at 250 m, 750 m, 1250 m, ...: below 3 km the odd
ones coincide with MRMS's own 250 m CAPPIs, the rest interpolate linearly
between the two bracketing CAPPIs (in the moment's own units, a missing
CAPPI reading as -32 dBZ / clean / no shear so echo fades toward it instead
of being cut), and below 500 m the lowest CAPPI extends to the ground, as
the single-site brick's lowest tilt does.

Debris rule (the same three legs the on-device builder applies, done here
so a tile's CC plane is already "debris only"): a column keeps its CC plane
only if its LOWEST level has CC < 0.8 inside REF >= 30 dBZ (lofted debris is
grounded; low CC that starts aloft is a hail core) AND 0-2 km azimuthal
shear >= 0.010 1/s lies within 5 cells (~5 km): the couplet leg that
separates debris from wet hail reaching the ground. Without the AzShear
product the rule falls back to grounded-only, as the app does for a volume
with no velocity.

Usage:
  mrms_volume_tiles.py --publish            # one tick: fetch, cut, upload
  mrms_volume_tiles.py --out-dir DIR        # cut to a local dir, no upload
  mrms_volume_tiles.py --selftest           # numpy-only unit checks

Env (all optional): R2_BUCKET (required to publish), VOL3D_STATE_DIR
(default ~/stp-vol3d/state), VOL3D_RETAIN_MIN (stamps kept on B2, default
120), VOL3D_JOBS (download/decode threads, default 6), VOL3D_PREFIX
(default v1/VOL3D; a shadow run points this elsewhere).
Needs numpy + osgeo (GDAL with its GRIB driver); --selftest needs numpy only.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import struct
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import numpy as np

# ── Source ────────────────────────────────────────────────────────────────

S3 = "https://noaa-mrms-pds.s3.amazonaws.com"
REF_DIR = "MergedReflectivityQC"
CC_DIR = "MergedRhoHV"
AZ02_DIR = "MergedAzShear_0-2kmAGL_00.50"
AZ36_DIR = "MergedAzShear_3-6kmAGL_00.50"

# The 33 CAPPI heights (metres) MRMS publishes the 3D mosaic at.
NATIVE_M: list[int] = (
    list(range(500, 3001, 250))          # 0.5 .. 3.0 km every 250 m (11)
    + list(range(3500, 9001, 500))       # 3.5 .. 9.0 km every 500 m (12)
    + list(range(10000, 19001, 1000))    # 10 .. 19 km every 1 km (10)
)
assert len(NATIVE_M) == 33

# RhoHV is fetched only this high. Debris is a low-level signature (the rule
# above requires it grounded and beside 0-2 km shear), so CAPPIs above this
# would cost ~40% of the CC download for planes the rule always clears.
CC_TOP_M = 8000

# ── Grid ─────────────────────────────────────────────────────────────────

LON0, LAT0, CELL_DEG = -130.0, 55.0, 0.01
COLS, ROWS = 7000, 3500
TILE_DEG = 2.0
TILE_CELLS = 200
TILE_COLS = 35   # -130 .. -60
TILE_ROWS = 18   # 55 .. 19 (the last row is half outside the grid; padded)
NZ = 38
DZ_M = 500.0
Z0_M = 0.0

MAGIC = 0x31545652  # 'RVT1'
HEADER_BYTES = 80
CH_REF, CH_CC, CH_ROT = 1, 2, 4

# ── Encodings (must match RadarVolumeBrickBuilder in the app) ─────────────

REF_NONE = 0
REF_FADE = 2                      # code of -32 dBZ: what a missing CAPPI reads as
ROT_NONE = 128
ROT_CODE_SCALE = 5.0              # per 1e-3 1/s

# Thresholds, in code units, shared with RadarVolumeHighlights in the app.
CODE_REF5 = 2 + round((5.0 + 32.0) * 2)      # 76: crop floor / rotation echo
CODE_REF18 = 2 + round((18.0 + 32.0) * 2)    # 102: echo-top convention
CODE_REF30 = 2 + round((30.0 + 32.0) * 2)    # 126: debris echo floor
CODE_CC08 = round((1.05 - 0.8) * 200)        # 50: debris = CC code ABOVE this
CODE_ROT6 = round(6.0 * ROT_CODE_SCALE)      # 30: rotation display grade
DEBRIS_ROT_MIN = 0.010                       # 1/s: mesocyclone grade couplet
DEBRIS_ROT_RADIUS = 5                        # cells (~5 km)


def encode_dbz(a: np.ndarray) -> np.ndarray:
    """float dBZ (NaN = none) -> uint8 code."""
    valid = np.isfinite(a) & (a >= -31.5)
    code = np.rint((np.where(valid, a, 0.0) + 32.0) * 2.0) + 2.0
    code = np.clip(code, 2, 255)
    return np.where(valid, code, 0).astype(np.uint8)


def encode_cc(a: np.ndarray) -> np.ndarray:
    """float CC (NaN = none) -> inverted uint8 code, 0 = clean."""
    valid = np.isfinite(a)
    code = np.rint((1.05 - np.where(valid, a, 1.05)) * 200.0)
    code = np.clip(code, 0, 255)
    return np.where(valid, code, 0).astype(np.uint8)


def encode_rot(a: np.ndarray) -> np.ndarray:
    """float azimuthal shear, 1/s (NaN = none) -> uint8 code, 128 = none."""
    valid = np.isfinite(a)
    code = ROT_NONE + np.rint(np.where(valid, a, 0.0) * 1000.0 * ROT_CODE_SCALE)
    code = np.clip(code, 0, 255)
    return np.where(valid, code, ROT_NONE).astype(np.uint8)


def mask_sentinels(a: np.ndarray, floor: float) -> np.ndarray:
    """MRMS sentinels (-999 no coverage, -99 missing) -> NaN, in place."""
    a[a < floor] = np.nan
    return a


# ── Vertical plan ─────────────────────────────────────────────────────────

def level_plan(native_m: list[int], nz: int = NZ, dz: float = DZ_M,
               z0: float = Z0_M) -> list[tuple[int, int, float] | None]:
    """For each output level: (lowerNative, upperNative, t), or None when
    the level centre lies above the highest native."""
    plan: list[tuple[int, int, float] | None] = []
    for k in range(nz):
        h = z0 + (k + 0.5) * dz
        if h <= native_m[0]:
            plan.append((0, 0, 0.0))
            continue
        if h > native_m[-1]:
            plan.append(None)
            continue
        i = 0
        while native_m[i + 1] < h:
            i += 1
        lo, hi = native_m[i], native_m[i + 1]
        t = 0.0 if hi == lo else (h - lo) / (hi - lo)
        if abs(h - lo) < 1e-6:
            plan.append((i, i, 0.0))
        elif abs(h - hi) < 1e-6:
            plan.append((i + 1, i + 1, 0.0))
        else:
            plan.append((i, i + 1, t))
    return plan


def interp_codes(ca: np.ndarray | None, cb: np.ndarray | None, t: float,
                 none: int, fade: int) -> np.ndarray | None:
    """Linear interpolation between two code planes.

    Code is affine in the moment for every channel, so interpolating codes
    is interpolating the moment. A `none` voxel on one side reads as `fade`
    (REF: -32 dBZ; CC and ROT: `fade == none`) so echo fades toward a
    missing CAPPI; `none` on both sides stays `none`.
    """
    if ca is None and cb is None:
        return None
    if ca is None:
        ca = np.full_like(cb, none)
    if cb is None:
        cb = np.full_like(ca, none)
    if t <= 0.0:
        return ca
    if t >= 1.0:
        return cb
    za = ca == none
    zb = cb == none
    fa = ca.astype(np.float32)
    fb = cb.astype(np.float32)
    if fade != none:
        fa[za] = fade
        fb[zb] = fade
    v = np.rint(fa + np.float32(t) * (fb - fa))
    v = np.clip(v, 0, 255).astype(np.uint8)
    if fade != none:
        # Exactly the fade code is "-32 dBZ", which the on-device encoder
        # never emits (it returns none below -31.5 dBZ); keep the tails
        # identical.
        v[(za & zb) | (v == fade)] = none
    return v


class NativeStream:
    """Decoded native CAPPIs, in ascending order, with bounded look-ahead.

    A CONUS CAPPI is 24.5 MB as codes and ~100 MB as float32; 33 of each
    resident at once is what turned a 2 GB job into a 4 GB one on paper.
    The output levels are built bottom-up and only ever need two adjacent
    natives, so this decodes ahead by a few and drops what is behind.
    """

    def __init__(self, loaders, pool: ThreadPoolExecutor, lookahead: int = 3):
        # loaders[i]: callable -> uint8 plane or None (absent product).
        self._loaders = loaders
        self._pool = pool
        self._look = lookahead
        self._fut: dict[int, object] = {}
        self._done: dict[int, np.ndarray | None] = {}
        self._next = 0

    def _submit_through(self, i: int) -> None:
        while self._next <= min(i + self._look, len(self._loaders) - 1):
            j = self._next
            self._fut[j] = self._pool.submit(self._loaders[j])
            self._next += 1

    def get(self, i: int) -> np.ndarray | None:
        if i in self._done:
            return self._done[i]
        self._submit_through(i)
        self._done[i] = self._fut.pop(i).result()
        return self._done[i]

    def release_below(self, i: int) -> None:
        for j in [j for j in self._done if j < i]:
            del self._done[j]


def build_volume(stream: NativeStream, plan, nz_out: int, none: int,
                 fade: int, shape=(ROWS, COLS)) -> np.ndarray:
    out = np.full((nz_out,) + shape, none, dtype=np.uint8)
    for k in range(nz_out):
        p = plan[k]
        if p is None:
            continue
        a, b, t = p
        ca = stream.get(a)
        cb = ca if b == a else stream.get(b)
        v = interp_codes(ca, cb, t, none, fade)
        if v is not None:
            out[k] = v
        stream.release_below(a)
    return out


# ── Debris / rotation planes ──────────────────────────────────────────────

def dilate(mask: np.ndarray, r: int) -> np.ndarray:
    """Square (Chebyshev) dilation by r cells, separable, numpy only."""
    acc = mask.copy()
    for d in range(1, r + 1):
        acc[:, d:] |= mask[:, :-d]
        acc[:, :-d] |= mask[:, d:]
    out = acc.copy()
    for d in range(1, r + 1):
        out[d:, :] |= acc[:-d, :]
        out[:-d, :] |= acc[d:, :]
    return out


def apply_debris_rule(ref_vol: np.ndarray, cc_vol: np.ndarray,
                      az02: np.ndarray | None) -> np.ndarray:
    """Clear the CC plane of every column that is not a debris candidate.
    Returns the per-column keep mask (for the log line)."""
    grounded = (ref_vol[0] >= CODE_REF30) & (cc_vol[0] > CODE_CC08)
    if az02 is not None:
        strong = np.isfinite(az02) & (np.abs(az02) >= DEBRIS_ROT_MIN)
        keep = grounded & dilate(strong, DEBRIS_ROT_RADIUS)
    else:
        keep = grounded
    cc_vol[:, ~keep] = 0
    return keep


def rotation_levels(nz: int = NZ, dz: float = DZ_M, z0: float = Z0_M):
    """Output levels whose centres fall inside each AzShear layer."""
    lo = [k for k in range(nz) if 0.0 <= z0 + (k + 0.5) * dz < 2000.0]
    mid = [k for k in range(nz) if 3000.0 <= z0 + (k + 0.5) * dz < 6000.0]
    return lo, mid


# ── Tiles ─────────────────────────────────────────────────────────────────

def tile_id(tr: int, tc: int) -> str:
    return f"r{tr:02d}c{tc:02d}"


def tile_header(nx: int, ny: int, nz: int, lon_w: float, lat_n: float,
                mask: int, valid_ms: int, dlon: float = CELL_DEG,
                dlat: float = CELL_DEG, z0: float = Z0_M,
                dz: float = DZ_M) -> bytes:
    hdr = struct.pack(
        "<IIIIddddffIIQII",
        MAGIC, nx, ny, nz, lon_w, lat_n, dlon, dlat, z0, dz,
        mask, HEADER_BYTES, valid_ms, 0, 0,
    )
    assert len(hdr) == HEADER_BYTES
    return hdr


def tile_stats(ref_t: np.ndarray, cc_t: np.ndarray | None,
               rot_t: np.ndarray | None, dz: float = DZ_M) -> list:
    """[maxDbz, echoTopKm, topClipped, debrisVoxels, rotationVoxels] with
    the on-device conventions: the 18 dBZ top over the STRONGEST column
    (a domain max would report the geometry of the far edge), voxel counts
    with the shader's own thresholds."""
    nz = ref_t.shape[0]
    flat = int(np.argmax(ref_t))
    max_code = int(ref_t.reshape(-1)[flat])
    _, j, i = np.unravel_index(flat, ref_t.shape)
    col = ref_t[:, j, i]
    above = np.nonzero(col >= CODE_REF18)[0]
    top_k = int(above[-1]) if above.size else -1
    top_km = (top_k + 1) * dz / 1000.0
    clipped = 1 if top_k >= nz - 1 else 0
    max_dbz = (max_code - 2) / 2.0 - 32.0 if max_code >= 2 else None
    debris = 0
    if cc_t is not None:
        debris = int(np.count_nonzero((ref_t >= CODE_REF30) & (cc_t > CODE_CC08)))
    rot = 0
    if rot_t is not None:
        mag = np.abs(rot_t.astype(np.int16) - ROT_NONE)
        rot = int(np.count_nonzero((ref_t >= CODE_REF5) & (mag >= CODE_ROT6)))
    return [max_dbz, round(top_km, 2), clipped, debris, rot]


def cut_tiles(ref_vol: np.ndarray, cc_vol: np.ndarray | None,
              rot02: np.ndarray | None, rot36: np.ndarray | None,
              valid_ms: int, out_dir: Path, *, nz: int = NZ,
              tile_cells: int = TILE_CELLS, tile_rows: int = TILE_ROWS,
              tile_cols: int = TILE_COLS, lon0: float = LON0,
              lat0: float = LAT0, cell: float = CELL_DEG) -> dict:
    """Write every non-empty tile; return {tileId: stats}."""
    rows, cols = ref_vol.shape[1], ref_vol.shape[2]
    lo_levels, mid_levels = rotation_levels(nz)
    has_cc = cc_vol is not None
    has_rot = rot02 is not None or rot36 is not None
    mask = CH_REF | (CH_CC if has_cc else 0) | (CH_ROT if has_rot else 0)
    tiles: dict[str, list] = {}
    out_dir.mkdir(parents=True, exist_ok=True)
    for tr in range(tile_rows):
        r0 = tr * tile_cells
        r1 = min(rows, r0 + tile_cells)
        if r1 <= r0:
            continue
        pad = tile_cells - (r1 - r0)
        for tc in range(tile_cols):
            c0 = tc * tile_cells
            c1 = min(cols, c0 + tile_cells)
            ref_t = ref_vol[:, r0:r1, c0:c1]
            if int(ref_t.max()) < CODE_REF5:
                continue  # nothing above the crop floor: no tile
            if pad:
                ref_t = np.pad(ref_t, ((0, 0), (0, pad), (0, 0)))
            cc_t = None
            if has_cc:
                cc_t = cc_vol[:, r0:r1, c0:c1]
                if cc_t.shape[0] < nz:
                    cc_t = np.pad(cc_t, ((0, nz - cc_t.shape[0]), (0, 0), (0, 0)))
                if pad:
                    cc_t = np.pad(cc_t, ((0, 0), (0, pad), (0, 0)))
            rot_t = None
            if has_rot:
                rot_t = np.full((nz, tile_cells, tile_cells), ROT_NONE, np.uint8)
                if rot02 is not None:
                    sl = rot02[r0:r1, c0:c1]
                    for k in lo_levels:
                        rot_t[k, : r1 - r0, :] = sl
                if rot36 is not None:
                    sl = rot36[r0:r1, c0:c1]
                    for k in mid_levels:
                        rot_t[k, : r1 - r0, :] = sl
                # Rotation only means anything inside echo (the shader
                # applies the same floor); resting it outside echo is what
                # lets a clear-air tile compress to nothing.
                rot_t[ref_t < CODE_REF5] = ROT_NONE
            tid = tile_id(tr, tc)
            tiles[tid] = tile_stats(ref_t, cc_t, rot_t)
            hdr = tile_header(tile_cells, tile_cells, nz,
                              lon0 + tc * tile_cells * cell,
                              lat0 - tr * tile_cells * cell, mask, valid_ms,
                              dlon=cell, dlat=cell)
            parts = [hdr, np.ascontiguousarray(ref_t).tobytes()]
            if cc_t is not None:
                parts.append(np.ascontiguousarray(cc_t).tobytes())
            if rot_t is not None:
                parts.append(rot_t.tobytes())
            blob = gzip.compress(b"".join(parts), compresslevel=6, mtime=0)
            (out_dir / f"{tid}.rvt.gz").write_bytes(blob)
    return tiles


def grid_json() -> dict:
    return {
        "lon0": LON0, "lat0": LAT0, "cellDeg": CELL_DEG, "tileDeg": TILE_DEG,
        "tileCells": TILE_CELLS, "tileCols": TILE_COLS, "tileRows": TILE_ROWS,
        "nz": NZ, "dzM": DZ_M, "z0M": Z0_M,
    }


# ── S3 listing / fetch ────────────────────────────────────────────────────

_KEY_RE = re.compile(r"<Key>([^<]+)</Key>")
_STAMP_RE = re.compile(r"_(\d{8}-\d{6})\.grib2\.gz$")


def _http_get(url: str, timeout: int = 60, tries: int = 3) -> bytes:
    last: Exception | None = None
    for attempt in range(tries):
        try:
            req = Request(url, headers={"User-Agent": "stp-renderer/1.0"})
            with urlopen(req, timeout=timeout) as r:
                return r.read()
        except (HTTPError, URLError, TimeoutError, OSError) as e:  # noqa: PERF203
            last = e
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"GET failed after {tries} tries: {url}: {last}")


def stamp_dt(stamp: str) -> datetime:
    return datetime.strptime(stamp, "%Y%m%d-%H%M%S").replace(tzinfo=timezone.utc)


def list_recent(dirname: str, since: datetime) -> dict[str, str]:
    """{stamp: key} for objects of CONUS/<dirname>/ newer than `since`.

    Keys sort by stamp, so `start-after` on the day directory keeps each
    response to a handful of objects instead of the day's ~720; both the
    since-day and today are listed when they differ (the 00Z rollover)."""
    now = datetime.now(timezone.utc)
    out: dict[str, str] = {}
    days = []
    for d in (since.date(), now.date()):
        if d not in days:
            days.append(d)
    for d in days:
        day = d.strftime("%Y%m%d")
        prefix = f"CONUS/{dirname}/{day}/"
        url = f"{S3}/?list-type=2&prefix={prefix}&max-keys=1000"
        if d == since.date():
            after = f"{prefix}MRMS_{dirname}_{since.strftime('%Y%m%d-%H%M%S')}"
            url += f"&start-after={after}"
        try:
            body = _http_get(url, timeout=30).decode("utf-8", "replace")
        except RuntimeError as e:
            print(f"  WARN list {dirname}/{day}: {e}", file=sys.stderr)
            continue
        for key in _KEY_RE.findall(body):
            m = _STAMP_RE.search(key)
            if m:
                out[m.group(1)] = key
    return out


def level_tag(m: int) -> str:
    return f"{m / 1000:05.2f}"


def ref_dir(m: int) -> str:
    return f"{REF_DIR}_{level_tag(m)}"


def cc_dir(m: int) -> str:
    return f"{CC_DIR}_{level_tag(m)}"


def choose_stamp(listings: dict[str, dict[str, str]], ref_dirs: list[str],
                 forced: str | None = None) -> str | None:
    """Newest stamp present in EVERY reflectivity level (the 33 CAPPIs
    publish together but not atomically; a level missing at the newest
    stamp means the cycle is still landing, and the next tick catches it
    whole)."""
    if forced:
        return forced
    common: set[str] | None = None
    for d in ref_dirs:
        s = set(listings.get(d, {}))
        common = s if common is None else common & s
    if not common:
        return None
    return max(common)


def pick_companion(listing: dict[str, str], stamp: str, *,
                   after_s: int = 90, before_s: int = 360) -> str | None:
    """Newest key of a companion product whose stamp is within
    [stamp - before_s, stamp + after_s]. RhoHV levels and AzShear publish
    a little staggered from the reflectivity CAPPIs (measured: up to ~5 min
    apart on a quiet night), and a slightly older CC plane is far better
    than none."""
    t = stamp_dt(stamp)
    best = None
    for s, key in listing.items():
        dt = stamp_dt(s)
        if t - timedelta(seconds=before_s) <= dt <= t + timedelta(seconds=after_s):
            if best is None or s > best[0]:
                best = (s, key)
    return best[1] if best else None


def download(key: str, dest: Path) -> int:
    data = _http_get(f"{S3}/{key}")
    dest.write_bytes(data)
    return len(data)


def decimate2_mean(a: np.ndarray) -> np.ndarray:
    """2:1 decimation by the mean of the finite cells of each 2x2 block
    (all-NaN stays NaN)."""
    r, c = a.shape[0] // 2, a.shape[1] // 2
    v = a[: 2 * r, : 2 * c].reshape(r, 2, c, 2)
    fin = np.isfinite(v)
    s = np.where(fin, v, np.float32(0.0)).sum(axis=(1, 3), dtype=np.float32)
    n = fin.sum(axis=(1, 3))
    out = np.full((r, c), np.nan, np.float32)
    np.divide(s, n, out=out, where=n > 0)
    return out


def smooth3x3(a: np.ndarray) -> np.ndarray:
    """Mean over the 3x3 neighbourhood, NaN read as 0 (clear air), a NaN
    centre kept NaN. Separable, numpy only.

    Why the shear is smoothed at all: MRMS AzShear is the raw per-cell
    LLSD output and lights single cells at 0.006 1/s all over any echo
    (the first live cut had 90k "rotation" voxels in one tile, cyan
    confetti on screen). The on-device builder samples its shear through
    a 3-radial x 5-gate box mean, which is what keeps single-gate noise
    under the display grade there; 2x2 mean + 3x3 mean here is a box of
    about the same size (~3 km), so the same threshold means the same
    thing on both sources. A real couplet is several cells wide and keeps
    most of its peak through it."""
    z = np.where(np.isfinite(a), a, np.float32(0.0)).astype(np.float32)
    acc = z.copy()
    acc[:, 1:] += z[:, :-1]
    acc[:, :-1] += z[:, 1:]
    out = acc.copy()
    out[1:, :] += acc[:-1, :]
    out[:-1, :] += acc[1:, :]
    out /= np.float32(9.0)
    out[~np.isfinite(a)] = np.nan
    return out


def read_grib_float32(path: Path, allow_half_cell: bool = False) -> np.ndarray:
    """Decode one gzipped GRIB2 field to float32 on the 0.01 deg CONUS grid
    (3500 x 7000), asserting the grid the whole pipeline assumes.

    The CAPPIs and RhoHV are published on that grid. The AzShear layer
    products are on the 0.005 deg grid (14000 x 7000); with
    `allow_half_cell` those are decimated 2:1 by `decimate2_maxabs`."""
    from osgeo import gdal  # local import: --selftest runs without GDAL

    gdal.UseExceptions()
    ds = gdal.Open(f"/vsigzip/{path}")
    gt = ds.GetGeoTransform()
    half = ds.RasterXSize == 2 * COLS and ds.RasterYSize == 2 * ROWS
    full = ds.RasterXSize == COLS and ds.RasterYSize == ROWS
    if not full and not (half and allow_half_cell):
        raise RuntimeError(f"{path.name}: grid {ds.RasterXSize}x{ds.RasterYSize}, "
                           f"expected {COLS}x{ROWS}")
    cell = CELL_DEG / 2 if half else CELL_DEG
    if abs(gt[0] - LON0) > 1e-6 or abs(gt[3] - LAT0) > 1e-6 or \
            abs(gt[1] - cell) > 1e-9 or abs(gt[5] + cell) > 1e-9:
        raise RuntimeError(f"{path.name}: unexpected geotransform {gt}")
    band = ds.GetRasterBand(1)
    arr = band.ReadAsArray(buf_type=gdal.GDT_Float32)
    # The AzShear products are published in [0.001/s] as integers (-27..37
    # measured); everything downstream is in 1/s. Read the unit off the
    # GRIB rather than assuming, so a product that changes its unit shows
    # up as a wrong picture instead of a silently 1000x wrong one.
    unit = (band.GetMetadata() or {}).get("GRIB_UNIT", "")
    ds = None
    if half:
        # Shear: sentinels out first (a -999 in a mean poisons the block),
        # then reduce + smooth. Comes back NaN-masked; the caller's
        # sentinel mask is a no-op on it.
        arr[arr < -90.0] = np.nan
        arr = smooth3x3(decimate2_mean(arr))
    if "0.001" in unit:
        sentinel = ~np.isfinite(arr) | (arr < -90.0)
        arr *= np.float32(1e-3)
        arr[sentinel] = np.nan
    return arr


# ── State ─────────────────────────────────────────────────────────────────

class State:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self._f = root / "stamps.json"
        try:
            self.stamps: list[str] = json.loads(self._f.read_text())
        except Exception:
            self.stamps = []

    @property
    def last(self) -> str | None:
        return self.stamps[0] if self.stamps else None

    def record(self, stamp: str, retain_min: int) -> list[str]:
        s = sorted(set(self.stamps) | {stamp}, reverse=True)
        cutoff = stamp_dt(stamp) - timedelta(minutes=retain_min)
        s = [x for x in s if stamp_dt(x) >= cutoff]
        self.stamps = s
        tmp = self._f.with_suffix(".tmp")
        tmp.write_text(json.dumps(s))
        tmp.replace(self._f)
        return s


# ── Publish ───────────────────────────────────────────────────────────────

def rclone(*args: str) -> None:
    subprocess.check_call(["rclone", *args, "--s3-no-check-bucket"])


def publish(bucket: str, prefix: str, stamp: str, stamp_dir: Path,
            latest: Path) -> None:
    # Tiles first, then the pointer: latest.json must never name a tile
    # that is not there yet. Per-stamp keys are immutable, so they can
    # sit at the edge for a day; the pointer turns over every cycle.
    rclone("copy", str(stamp_dir), f"r2:{bucket}/{prefix}/{stamp}/",
           "--transfers", "16", "--no-traverse",
           "--header-upload", "Cache-Control: public, max-age=86400")
    rclone("copyto", str(latest), f"r2:{bucket}/{prefix}/latest.json",
           "--no-traverse", "--header-upload", "Cache-Control: public, max-age=20")


def prune(bucket: str, prefix: str, keep: set[str], retain_min: int,
          newest: str) -> int:
    """Delete stamp directories older than the retention window. One
    listing per tick (free on B2, and the prefix holds ~60 directories)."""
    try:
        out = subprocess.check_output(
            ["rclone", "lsf", "--dirs-only", f"r2:{bucket}/{prefix}/",
             "--s3-no-check-bucket"], text=True)
    except subprocess.CalledProcessError as e:
        print(f"  WARN prune listing failed: {e}", file=sys.stderr)
        return 0
    cutoff = stamp_dt(newest) - timedelta(minutes=retain_min)
    removed = 0
    for line in out.splitlines():
        name = line.strip().rstrip("/")
        if not re.fullmatch(r"\d{8}-\d{6}", name) or name in keep:
            continue
        if stamp_dt(name) < cutoff:
            subprocess.call(["rclone", "purge", f"r2:{bucket}/{prefix}/{name}",
                             "--s3-no-check-bucket"])
            removed += 1
    return removed


# ── One tick ──────────────────────────────────────────────────────────────

def run_tick(args) -> int:
    t_start = time.time()
    jobs = int(os.environ.get("VOL3D_JOBS", "6"))
    retain_min = int(os.environ.get("VOL3D_RETAIN_MIN", "120"))
    prefix = os.environ.get("VOL3D_PREFIX", "v1/VOL3D").strip("/")
    state = State(Path(os.environ.get("VOL3D_STATE_DIR",
                                      str(Path.home() / "stp-vol3d" / "state"))))
    bucket = os.environ.get("R2_BUCKET", "")
    if args.publish and not bucket:
        print("FATAL: --publish needs R2_BUCKET", file=sys.stderr)
        return 2

    ref_dirs = [ref_dir(m) for m in NATIVE_M]
    cc_levels = [m for m in NATIVE_M if m <= CC_TOP_M]
    cc_dirs = [cc_dir(m) for m in cc_levels]
    since = datetime.now(timezone.utc) - timedelta(minutes=args.lookback_min)

    # 1. What is newest, and is it complete?
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        names = ref_dirs + cc_dirs + [AZ02_DIR, AZ36_DIR]
        listed = list(pool.map(lambda d: list_recent(d, since), names))
    listings = dict(zip(names, listed))
    stamp = choose_stamp(listings, ref_dirs, args.stamp)
    if stamp is None:
        print(f"TICK vol3d status=idle reason=no-complete-cycle elapsed={time.time() - t_start:.0f}s")
        return 0
    if not args.force and state.last == stamp:
        print(f"TICK vol3d status=idle reason=unchanged stamp={stamp} elapsed={time.time() - t_start:.0f}s")
        return 0
    valid = stamp_dt(stamp)
    valid_ms = int(valid.timestamp() * 1000)

    ref_keys = [listings[d].get(stamp) for d in ref_dirs]
    if any(k is None for k in ref_keys):
        print(f"TICK vol3d status=idle reason=forced-stamp-incomplete stamp={stamp}")
        return 0
    cc_keys = [pick_companion(listings[d], stamp) for d in cc_dirs]
    az02_key = pick_companion(listings[AZ02_DIR], stamp)
    az36_key = pick_companion(listings[AZ36_DIR], stamp)

    work = Path(tempfile.mkdtemp(prefix="vol3d-"))
    try:
        # 2. Download everything the cycle needs, in parallel.
        wanted: list[tuple[str, Path]] = []
        for i, k in enumerate(ref_keys):
            wanted.append((k, work / f"ref{i:02d}.grib2.gz"))
        for i, k in enumerate(cc_keys):
            if k:
                wanted.append((k, work / f"cc{i:02d}.grib2.gz"))
        if az02_key:
            wanted.append((az02_key, work / "az02.grib2.gz"))
        if az36_key:
            wanted.append((az36_key, work / "az36.grib2.gz"))
        t0 = time.time()
        with ThreadPoolExecutor(max_workers=jobs) as pool:
            sizes = list(pool.map(lambda kp: download(kp[0], kp[1]), wanted))
        bytes_in = sum(sizes)
        t_dl = time.time() - t0

        # 3. Reflectivity volume, streamed bottom-up.
        t0 = time.time()
        plan = level_plan(NATIVE_M)

        def ref_loader(i: int):
            def load():
                a = read_grib_float32(work / f"ref{i:02d}.grib2.gz")
                return encode_dbz(mask_sentinels(a, -90.0))
            return load

        with ThreadPoolExecutor(max_workers=max(2, jobs // 2)) as pool:
            ref_vol = build_volume(
                NativeStream([ref_loader(i) for i in range(len(NATIVE_M))], pool),
                plan, NZ, REF_NONE, REF_FADE)
        t_ref = time.time() - t0

        # 4. CC volume up to CC_TOP_M, then the debris rule.
        t0 = time.time()
        cc_plan = level_plan(cc_levels)
        nz_cc = sum(1 for p in cc_plan if p is not None)

        def cc_loader(i: int):
            def load():
                p = work / f"cc{i:02d}.grib2.gz"
                if not p.exists():
                    return None
                a = read_grib_float32(p)
                return encode_cc(mask_sentinels(a, -1.0))
            return load

        az02 = az36 = None
        if az02_key:
            az02 = read_grib_float32(work / "az02.grib2.gz", allow_half_cell=True)
            mask_sentinels(az02, -1.0)
        if az36_key:
            az36 = read_grib_float32(work / "az36.grib2.gz", allow_half_cell=True)
            mask_sentinels(az36, -1.0)
        cc_vol = None
        kept_cols = 0
        if any(cc_keys):
            with ThreadPoolExecutor(max_workers=max(2, jobs // 2)) as pool:
                cc_vol = build_volume(
                    NativeStream([cc_loader(i) for i in range(len(cc_levels))], pool),
                    cc_plan, nz_cc, 0, 0)
            kept_cols = int(np.count_nonzero(apply_debris_rule(ref_vol, cc_vol, az02)))
        rot02 = encode_rot(az02) if az02 is not None else None
        rot36 = encode_rot(az36) if az36 is not None else None
        del az02, az36
        t_cc = time.time() - t0

        # 5. Tiles + index.
        t0 = time.time()
        stamp_dir = work / "out" / stamp
        tiles = cut_tiles(ref_vol, cc_vol, rot02, rot36, valid_ms, stamp_dir)
        bytes_out = sum(p.stat().st_size for p in stamp_dir.glob("*.rvt.gz"))
        has_cc = cc_vol is not None
        has_rot = rot02 is not None or rot36 is not None
        del ref_vol, cc_vol
        index = {
            "version": 1,
            "stamp": stamp,
            "valid": valid.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "generatedAt": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "grid": grid_json(),
            "channels": CH_REF | (CH_CC if has_cc else 0) | (CH_ROT if has_rot else 0),
            "tiles": tiles,
        }
        (stamp_dir / "index.json").write_text(json.dumps(index, separators=(",", ":")))
        t_tiles = time.time() - t0

        # 6. Publish (or keep locally).
        if args.out_dir:
            dest = Path(args.out_dir) / stamp
            if dest.exists():
                import shutil
                shutil.rmtree(dest)
            dest.parent.mkdir(parents=True, exist_ok=True)
            import shutil
            shutil.copytree(stamp_dir, dest)
            recent = state.record(stamp, retain_min) if args.record_state else [stamp]
            latest = dict(index, recent=recent)
            (Path(args.out_dir) / "latest.json").write_text(
                json.dumps(latest, separators=(",", ":")))
        t0 = time.time()
        removed = 0
        if args.publish:
            recent = state.record(stamp, retain_min)
            latest = dict(index, recent=recent)
            latest_path = work / "latest.json"
            latest_path.write_text(json.dumps(latest, separators=(",", ":")))
            publish(bucket, prefix, stamp, stamp_dir, latest_path)
            if not args.no_prune:
                removed = prune(bucket, prefix, set(recent), retain_min, stamp)
        t_pub = time.time() - t0

        debris_tiles = sum(1 for s in tiles.values() if s[3] >= 6)
        rot_tiles = sum(1 for s in tiles.values() if s[4] >= 6)
        print(
            f"TICK vol3d status=ok stamp={stamp} tiles={len(tiles)} "
            f"cc={'y' if has_cc else 'n'} rot={'y' if has_rot else 'n'} "
            f"debris_cols={kept_cols} debris_tiles={debris_tiles} rot_tiles={rot_tiles} "
            f"in_mb={bytes_in / 1e6:.1f} out_mb={bytes_out / 1e6:.1f} "
            f"pruned={removed} dl={t_dl:.0f}s ref={t_ref:.0f}s cc={t_cc:.0f}s "
            f"tiles={t_tiles:.0f}s pub={t_pub:.0f}s elapsed={time.time() - t_start:.0f}s"
        )
        return 0
    finally:
        if not args.keep_work:
            import shutil
            shutil.rmtree(work, ignore_errors=True)
        else:
            print(f"  work dir kept: {work}")


# ── Self-test (numpy only) ────────────────────────────────────────────────

def selftest() -> int:
    fails = 0

    def check(cond: bool, what: str) -> None:
        nonlocal fails
        print(("  ok   " if cond else "  FAIL ") + what)
        if not cond:
            fails += 1

    # Encodings, against the on-device constants.
    a = np.array([np.nan, -999.0, -99.0, -32.0, -31.5, 0.0, 30.0, 94.5, 120.0], np.float32)
    check(list(encode_dbz(a)) == [0, 0, 0, 0, 3, 66, 126, 255, 255], "encode_dbz (-31.5 dBZ is code 3, 2 is never emitted)")
    c = np.array([np.nan, 1.05, 1.0, 0.8, 0.5, 0.2, -99.0], np.float32)
    check(list(encode_cc(c)) == [0, 0, 10, 50, 110, 170, 255], "encode_cc (sentinel masked separately)")
    r = np.array([np.nan, 0.0, 0.006, -0.006, 0.01, 0.03, -0.03], np.float32)
    check(list(encode_rot(r)) == [128, 128, 158, 98, 178, 255, 0], "encode_rot")
    check(CODE_REF5 == 76 and CODE_REF18 == 102 and CODE_REF30 == 126
          and CODE_CC08 == 50 and CODE_ROT6 == 30, "threshold codes")

    # Vertical plan: 38 levels, exact hits below 3 km, interpolation above.
    plan = level_plan(NATIVE_M)
    check(len(plan) == NZ and all(p is not None for p in plan), "plan covers every level")
    check(plan[0] == (0, 0, 0.0), "level 0 extends the 500 m CAPPI to the ground")
    check(plan[1] == (1, 1, 0.0) and plan[5] == (9, 9, 0.0), "750 m and 2750 m are exact CAPPIs")
    check(plan[6] == (10, 11, 0.5), "3250 m = midpoint of 3.0/3.5 km")
    check(plan[37][0] == 31 and plan[37][1] == 32 and abs(plan[37][2] - 0.75) < 1e-9,
          "18750 m = 3/4 of the way from 18 to 19 km")
    cc_plan = level_plan([m for m in NATIVE_M if m <= CC_TOP_M])
    check(sum(1 for p in cc_plan if p is not None) == 16, "CC plane carries 16 levels (to 7.75 km)")

    # Interpolation semantics.
    ca = np.array([[0, 0, 66, 100]], np.uint8)
    cb = np.array([[0, 66, 0, 200]], np.uint8)
    v = interp_codes(ca, cb, 0.5, REF_NONE, REF_FADE)
    check(list(v[0]) == [0, 34, 34, 150], "REF interp: none+none=none, none reads as -32 dBZ, linear")
    v = interp_codes(np.array([[0, 100]], np.uint8), np.array([[0, 0]], np.uint8), 0.25, 0, 0)
    check(list(v[0]) == [0, 75], "CC interp fades toward clean")
    v = interp_codes(np.array([[128, 178]], np.uint8), np.array([[128, 128]], np.uint8), 0.5, ROT_NONE, ROT_NONE)
    check(list(v[0]) == [128, 153], "ROT interp fades toward none")
    check(interp_codes(None, None, 0.5, 0, 2) is None, "both natives absent -> level absent")

    # Streamed volume build with synthetic natives: a 2-cell world.
    natives = [np.full((1, 2), 0, np.uint8) for _ in NATIVE_M]
    for i, m in enumerate(NATIVE_M):
        if m <= 6000:
            natives[i][0, 0] = encode_dbz(np.array([50.0 - m / 1000.0], np.float32))[0]
    with ThreadPoolExecutor(max_workers=2) as pool:
        vol = build_volume(NativeStream([(lambda n=n: n) for n in natives], pool),
                           plan, NZ, REF_NONE, REF_FADE, shape=(1, 2))
    check(vol.shape == (NZ, 1, 2), "volume shape")
    check(vol[0, 0, 0] == encode_dbz(np.array([49.5], np.float32))[0], "ground level = 500 m CAPPI")
    check(vol[11, 0, 0] == encode_dbz(np.array([44.25], np.float32))[0], "5750 m interpolates 5.5/6.0 km")
    check(vol[12, 0, 0] < vol[11, 0, 0] and vol[13, 0, 0] == 0, "fades above the top CAPPI, then none")
    check(int(vol[:, 0, 1].max()) == 0, "clear column stays none")

    # Debris rule: grounded + couplet.
    ref_vol = np.zeros((2, 20, 20), np.uint8)
    cc_vol = np.zeros((2, 20, 20), np.uint8)
    ref_vol[:, 5, 5] = CODE_REF30 + 10; cc_vol[:, 5, 5] = 100     # grounded, near shear
    ref_vol[:, 15, 15] = CODE_REF30 + 10; cc_vol[:, 15, 15] = 100  # grounded, no shear
    ref_vol[1, 5, 12] = CODE_REF30 + 10; cc_vol[1, 5, 12] = 100    # aloft only
    az = np.zeros((20, 20), np.float32); az[8, 8] = 0.02          # 3 cells from (5,5)
    keep = apply_debris_rule(ref_vol, cc_vol, az)
    check(bool(keep[5, 5]) and not keep[15, 15] and not keep[5, 12], "keep mask: grounded AND couplet")
    check(cc_vol[0, 5, 5] == 100 and cc_vol[:, 15, 15].max() == 0 and cc_vol[1, 5, 12] == 0,
          "CC plane cleared outside candidates")
    cc_vol[:, 15, 15] = 100
    keep = apply_debris_rule(ref_vol, cc_vol, None)
    check(bool(keep[15, 15]), "no AzShear -> grounded-only fallback")
    d = np.array([[0.02, np.nan, 0.001, 0.0],
                  [-0.004, 0.0, -0.03, 0.002],
                  [np.nan, np.nan, 0.0, 0.0],
                  [np.nan, np.nan, 0.0, 0.0]], np.float32)
    dd = decimate2_mean(d)
    check(dd.shape == (2, 2) and abs(dd[0, 0] - 0.016 / 3) < 1e-6
          and abs(dd[0, 1] - (-0.027 / 4)) < 1e-6 and np.isnan(dd[1, 0]) and dd[1, 1] == 0.0,
          "AzShear 2:1 mean decimation: finite cells only, all-NaN stays NaN")
    sm = smooth3x3(np.array([[np.nan, 0.0, 0.0], [0.0, 0.09, 0.0], [0.0, 0.0, 0.0]], np.float32))
    check(np.isnan(sm[0, 0]) and abs(sm[1, 1] - 0.01) < 1e-6 and abs(sm[0, 1] - 0.01) < 1e-6
          and abs(sm[2, 2] - 0.01) < 1e-6,
          "3x3 smoothing spreads a lone cell to a ninth, keeps a NaN centre NaN")
    lo, mid = rotation_levels()
    check(lo == [0, 1, 2, 3] and mid == [6, 7, 8, 9, 10, 11], "AzShear layers -> levels")

    # Tile cut + header + stats on a small synthetic grid (4 tiles of 2x2).
    ref = np.zeros((NZ, 4, 4), np.uint8)
    ref[:20, 1, 1] = CODE_REF30 + 20     # a 10 km column in tile r00c00
    ref[0, 3, 3] = CODE_REF5 - 1          # below the crop floor: tile r01c01 stays empty
    cc = np.zeros((16, 4, 4), np.uint8); cc[:4, 1, 1] = 100
    rot02 = np.full((4, 4), ROT_NONE, np.uint8); rot02[1, 1] = ROT_NONE + 60
    with tempfile.TemporaryDirectory() as td:
        tiles = cut_tiles(ref, cc, rot02, None, 1700000000000, Path(td), nz=NZ,
                          tile_cells=2, tile_rows=2, tile_cols=2, lon0=-100.0,
                          lat0=40.0, cell=0.5)
        check(set(tiles) == {"r00c00"}, f"only the lit tile is written ({sorted(tiles)})")
        st = tiles["r00c00"]
        check(st[0] == 40.0 and st[1] == 10.0 and st[2] == 0, f"stats max/top/clipped {st}")
        check(st[3] == 4 and st[4] == 4, f"debris/rotation voxel counts {st}")
        raw = gzip.decompress((Path(td) / "r00c00.rvt.gz").read_bytes())
        h = struct.unpack("<IIIIddddffIIQII", raw[:HEADER_BYTES])
        check(h[0] == MAGIC and h[1:4] == (2, 2, NZ), "header magic + dims")
        check(h[4] == -100.0 and h[5] == 40.0 and h[6] == 0.5 and h[7] == 0.5, "header corner + cell")
        check(h[8] == Z0_M and h[9] == DZ_M and h[10] == 7 and h[11] == 80 and h[12] == 1700000000000,
              "header z/mask/valid")
        plane = 2 * 2 * NZ
        check(len(raw) == HEADER_BYTES + 3 * plane, "three planes follow the header")
        refp = np.frombuffer(raw[HEADER_BYTES:HEADER_BYTES + plane], np.uint8).reshape(NZ, 2, 2)
        check(refp[0, 1, 1] == CODE_REF30 + 20 and refp[19, 1, 1] != 0 and refp[20, 1, 1] == 0,
              "REF plane order z, y, x")
        rotp = np.frombuffer(raw[HEADER_BYTES + 2 * plane:], np.uint8).reshape(NZ, 2, 2)
        check(rotp[0, 1, 1] == ROT_NONE + 60 and rotp[4, 1, 1] == ROT_NONE and rotp[0, 0, 0] == ROT_NONE,
              "rotation extruded over 0-2 km, rested outside echo")

    # Companion selection window.
    lst = {"20260908-055641": "z", "20260908-055841": "a", "20260908-060040": "b"}
    check(pick_companion(lst, "20260908-055841") == "a", "companion: same stamp wins, +119 s is too new")
    check(pick_companion({"20260908-055641": "z", "20260908-055920": "n"}, "20260908-055841") == "n",
          "companion: +39 s accepted over an older one")
    check(pick_companion({"20260908-054000": "old"}, "20260908-055841") is None,
          "companion: nothing inside the window -> absent")
    check(choose_stamp({"x": {"1": "k", "2": "k"}, "y": {"1": "k"}}, ["x", "y"]) == "1",
          "stamp = newest common to every level")
    check(level_tag(500) == "00.50" and level_tag(10000) == "10.00" and level_tag(19000) == "19.00",
          "level tags")

    print("SELFTEST " + ("PASS" if fails == 0 else f"FAIL ({fails})"))
    return 0 if fails == 0 else 1


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    p.add_argument("--publish", action="store_true", help="upload to B2 (needs R2_BUCKET)")
    p.add_argument("--out-dir", help="write tiles + latest.json under this local dir")
    p.add_argument("--stamp", help="force a source stamp (YYYYMMDD-HHMMSS)")
    p.add_argument("--force", action="store_true", help="rebuild even if the stamp is unchanged")
    p.add_argument("--no-prune", action="store_true")
    p.add_argument("--keep-work", action="store_true", help="keep the scratch dir")
    p.add_argument("--record-state", action="store_true",
                   help="with --out-dir: also update the state dir (default: leave it)")
    p.add_argument("--lookback-min", type=int, default=25,
                   help="how far back to list source objects (default 25)")
    p.add_argument("--selftest", action="store_true")
    args = p.parse_args()
    if args.selftest:
        return selftest()
    if not args.publish and not args.out_dir:
        p.error("one of --publish / --out-dir / --selftest is required")
    return run_tick(args)


if __name__ == "__main__":
    sys.exit(main())
