#!/usr/bin/env python3
"""lightning_glm.py — build a compact near-real-time lightning flash feed
from the GOES Geostationary Lightning Mapper (GLM) and push it to R2.

Why GLM
-------
GLM is the only *free, public-domain, commercial-use-OK* real-time
lightning source. (Blitzortung/LightningMaps explicitly forbid commercial
use and storm-warning use even via third parties, so it's off the table
for Spotter Tools Pro.) GLM data lives on NOAA's open AWS buckets — same
legal footing as the NEXRAD/SPC data the app already uses — readable
anonymously with no key.

GLM detects *total* lightning (in-cloud + cloud-to-ground) optically from
geostationary orbit. Caveats vs a ground CG network: slight parallax
offset, marginally lower daytime efficiency, flash-level (not precise CG
strike points). For "where is the lightning / is this storm ramping," it's
excellent.

What this does
--------------
Every invocation rebuilds a rolling WINDOW_MIN window of recent flashes
from scratch (stateless — safe to run in a tight loop):

  1. List GLM-L2-LCFA granules from the East + West satellites for the
     current + previous UTC hour.
  2. Keep granules whose scan-start time is within the window.
  3. Download new granules to a /tmp cache (reused across loop iterations
     within one workflow run, so we never re-fetch the same granule).
  4. Parse flash_lat / flash_lon; stamp every flash in a granule with the
     granule's scan-start epoch (20 s granularity — plenty for age bins).
  5. Split East/West by longitude so the ~CONUS overlap isn't doubled,
     then clip to the app's US service area (COVERAGE_LAT/LON) so the feed
     and the MAX_FLASHES cap only carry flashes a US user can see.
  6. Grid-dedup (GRID_DEG + 1-min bucket) to bound payload size during
     big outbreaks while keeping visual coverage.
  7. Write flashes.json and rclone it to R2 at OUT_KEY.

Env:  R2_BUCKET  (rclone "r2:" remote is configured by the workflow)
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import boto3
import numpy as np
from botocore import UNSIGNED
from botocore.config import Config
from netCDF4 import Dataset

# ── Config ────────────────────────────────────────────────────────────
# Satellite buckets, newest-generation first. We probe candidates in
# order and use the first that has recent data, so an East/West satellite
# swap (GOES-16→19, GOES-17→18, future 19→20…) degrades gracefully
# instead of going dark.
EAST_BUCKETS = ["noaa-goes19", "noaa-goes16"]
WEST_BUCKETS = ["noaa-goes18", "noaa-goes17"]

# Clean longitude split between the two satellites so a flash in the
# overlap region is taken from exactly one satellite (no double-dots).
# East keeps lon >= cutoff (covers all of CONUS well); West keeps
# lon < cutoff (better toward the Pacific / AK / HI).
CUTOFF_LON = -106.0

# ── Parallax de-shift ────────────────────────────────────────────────
# GLM navigates each flash to an assumed "lightning ellipsoid" height. When
# the true optical-emission height is higher than that assumption, the
# plotted point sits too far from the sub-satellite point along the look
# direction — a SYSTEMATIC offset (same direction every time at a given
# spot) that grows with view angle, so it's biggest over the western US and
# ~zero near each bird's nadir. We measure the residual with GLM stereo:
# the same flash seen by BOTH birds, navigated twice to the same assumed
# height, disagrees by d = dh·(tanZ_E·û_E − tanZ_W·û_W); one dot product per
# pair recovers dh (the height error). A single fitted dh collapsed the raw
# ~10 km East/West disagreement by ~64% over 3701 pairs on 2026-08-27, and
# cut the plotted-position error near Lovington NM from ~4.5 km to ~0.6 km
# vs the stereo ground point. This shifts each served flash back toward its
# bird's sub-point by dh·tanZ. Safe by construction: it only slides a bolt
# along the axis the parallax pushed it out on, so an atypically low storm
# is neutral, never meaningfully worse. Retune DEPARALLAX_DH_KM as more
# stereo pairs accumulate (see ~/stp-stereo on the render box); set
# DEPARALLAX_ENABLED=False to serve raw GLM positions. The RANDOM error
# (optical centroid vs CG ground contact, ~8 km pixel) is a sensor floor no
# static shift can remove; this only takes out the systematic parallax part.
DEPARALLAX_ENABLED = True
DEPARALLAX_DH_KM = 4.0
_R_EARTH_KM = 6378.137
_R_GEO_KM = 42164.16
_KM_PER_DEG_LAT = 110.574
# Nominal sub-satellite longitudes per bucket (a 0.2° error here moves the
# correction by millimetres, so the retired birds share their slot's value).
_SUBPOINT_LON = {
    "noaa-goes19": -75.0, "noaa-goes16": -75.0,   # East
    "noaa-goes18": -137.0, "noaa-goes17": -137.0,  # West
}

# Coverage clip. GLM sees the whole hemisphere (Argentina to Canada,
# mid-Pacific to mid-Atlantic), but the app serves the US. Emitting all of
# it ~doubled the feed (to ~1.7 MB) and let a busy day over South America
# fill the MAX_FLASHES cap with lightning no US spotter can see, crowding
# out the older US flashes the app's radar-loop sync needs. So every flash
# is clipped to this box BEFORE dedup/cap. INDEPENDENT of the E/W split and
# the single-satellite outage fallback: it applies in every coverage mode,
# trimming only the far-hemisphere edges of whatever the live satellite(s)
# deliver, so it never shrinks US coverage during an outage. Box = CONUS +
# Gulf + Caribbean + near-offshore; Alaska/Hawaii fall OUTSIDE it, so widen
# if the user base grows there.
COVERAGE_LAT = (15.0, 55.0)
COVERAGE_LON = (-130.0, -60.0)

# Rolling window the feed carries. The APP picks a shorter display window
# (5/10/20/30 min) client-side and, when the single-site radar animation
# plays, slices this by each frame's scan time to sync lightning with the
# storm — so the feed must carry enough history to cover the largest app
# window + the oldest radar frame in a loop. A 10-frame volume-scan loop
# reaches ~45-60 min back, so the feed carries a full hour.
WINDOW_MIN = 60

# Dedup grid: collapse flashes sharing a ~1.1 km cell within the same
# 1-minute bucket to a single representative point. Bounds payload size
# during outbreaks (where GLM can report tens of thousands of flashes)
# without thinning the visible footprint.
GRID_DEG = 0.01
# Per-cell temporal dedup. 30 s keeps a busy cell from collapsing to one
# flash/min (which made the app's continuous reveal blink), while still
# bounding count.
TIME_BUCKET_SEC = 30

# Flashes older than this only ever feed the app's radar-loop slicing
# (the live layer shows at most the newest 30 min), and that slicing bins
# by radar frame, so the older half of the window is thinned with a much
# coarser grid. The time bucket stays modest — coarsening TIME instead
# would clump each cell's survivors near bucket ends and make short
# per-frame windows strobe during playback. 0.08° (~9 km) is inside
# GLM's own ~8-14 km footprint, so nothing real is lost, and on an
# active night it keeps the old half small enough that the MAX_FLASHES
# cap never eats the oldest frames' data (observed: 0.02° truncated the
# window to ~47 min; 0.05° still sat exactly at the cap).
RECENT_MIN = 30
GRID_DEG_OLD = 0.08
TIME_BUCKET_OLD_SEC = 120

# Hard safety cap on emitted points (keep newest). Grid-dedup normally
# keeps us well under this; the cap just guarantees a bounded file.
MAX_FLASHES = 60000

OUT_KEY = "lightning/v1/flashes.json"
SCHEMA_VERSION = 1

CACHE_DIR = Path(tempfile.gettempdir()) / "glm_cache"

_S3 = boto3.client(
    "s3",
    config=Config(signature_version=UNSIGNED, retries={"max_attempts": 2}),
    region_name="us-east-1",
)


# ── Granule-time parsing ──────────────────────────────────────────────
def _start_epoch_from_key(key: str) -> int | None:
    """Parse the scan-start epoch (UTC seconds) from a GLM L2 filename.

    Example basename:
      OR_GLM-L2-LCFA_G19_s20251692000000_e20251692000200_c....nc
    The `_s` token is YYYYDDDHHMMSSt (t = tenths of a second).
    """
    base = key.rsplit("/", 1)[-1]
    for tok in base.split("_"):
        if tok.startswith("s") and tok[1:].isdigit() and len(tok) >= 14:
            s = tok[1:]
            year = int(s[0:4])
            doy = int(s[4:7])
            hh, mm, ss = int(s[7:9]), int(s[9:11]), int(s[11:13])
            base_dt = dt.datetime(year, 1, 1, tzinfo=dt.timezone.utc) + dt.timedelta(
                days=doy - 1, hours=hh, minutes=mm, seconds=ss
            )
            return int(base_dt.timestamp())
    return None


def _hour_prefixes(now: dt.datetime) -> list[str]:
    """GLM-L2-LCFA hour prefixes covering the rolling window, oldest first.
    Derived from WINDOW_MIN (plus one spare hour) so bumping the window
    can't silently under-list granules across hour boundaries."""
    hours_back = WINDOW_MIN // 60 + 1
    prefixes = []
    for back in range(hours_back, -1, -1):  # oldest hour first, then newer
        t = now - dt.timedelta(hours=back)
        doy = t.timetuple().tm_yday
        prefixes.append(f"GLM-L2-LCFA/{t.year}/{doy:03d}/{t.hour:02d}/")
    return prefixes


def _pick_bucket(candidates: list[str], prefixes: list[str], cutoff_epoch: int) -> str | None:
    """Return the first candidate bucket that has at least one granule
    newer than cutoff_epoch under the given prefixes."""
    for bucket in candidates:
        for prefix in prefixes:
            try:
                resp = _S3.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=400)
            except Exception:
                continue
            for obj in resp.get("Contents", []):
                e = _start_epoch_from_key(obj["Key"])
                if e and e >= cutoff_epoch:
                    return bucket
    return None


def _list_window_keys(bucket: str, prefixes: list[str], cutoff_epoch: int) -> list[tuple[str, int]]:
    """List (key, start_epoch) for granules in the window for one bucket."""
    out: list[tuple[str, int]] = []
    for prefix in prefixes:
        token = None
        while True:
            kwargs = {"Bucket": bucket, "Prefix": prefix, "MaxKeys": 1000}
            if token:
                kwargs["ContinuationToken"] = token
            try:
                resp = _S3.list_objects_v2(**kwargs)
            except Exception as exc:
                print(f"  list failed {bucket}/{prefix}: {exc}", file=sys.stderr)
                break
            for obj in resp.get("Contents", []):
                e = _start_epoch_from_key(obj["Key"])
                if e and e >= cutoff_epoch:
                    out.append((obj["Key"], e))
            if resp.get("IsTruncated"):
                token = resp.get("NextContinuationToken")
            else:
                break
    return out


def _deparallax(
    lat: np.ndarray, lon: np.ndarray, sub_lon: float, dh_km: float
) -> tuple[np.ndarray, np.ndarray]:
    """Shift GLM flashes back toward the sub-satellite point by dh·tanZ to
    remove the systematic parallax offset (see the DEPARALLAX_* block).

    û is the horizontal unit vector pointing AWAY from the satellite at the
    flash; the plotted point is displaced along +û, so the true ground point
    is plotted − dh·tanZ·û. tanZ (satellite zenith tangent) comes from the
    geocentric angle γ between the flash and the sub-point:
        tanZ = sinγ / (cosγ − R_earth/R_geo).
    """
    phi = np.radians(lat)
    dlam = np.radians(sub_lon - lon)
    cosg = np.cos(phi) * np.cos(dlam)
    sing = np.sqrt(np.clip(1.0 - cosg * cosg, 0.0, 1.0))
    tanz = sing / (cosg - _R_EARTH_KM / _R_GEO_KM)
    az = np.arctan2(np.sin(dlam), -np.sin(phi) * np.cos(dlam))  # toward sat
    ue, un = -np.sin(az), -np.cos(az)  # away-from-sat = toward-sat + 180°
    klon = 111.320 * np.cos(phi)
    klon = np.where(np.abs(klon) < 1e-6, 1e-6, klon)
    lat2 = lat - dh_km * tanz * un / _KM_PER_DEG_LAT
    lon2 = lon - dh_km * tanz * ue / klon
    return lat2, lon2


def _local_path(key: str) -> Path:
    return CACHE_DIR / key.rsplit("/", 1)[-1]


def _ensure_granule(bucket: str, key: str) -> Path | None:
    """Download a granule to the /tmp cache if not already present."""
    path = _local_path(key)
    if path.exists() and path.stat().st_size > 0:
        return path
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        _S3.download_file(bucket, key, str(path))
        return path
    except Exception as exc:
        print(f"  download failed {bucket}/{key}: {exc}", file=sys.stderr)
        if path.exists():
            path.unlink(missing_ok=True)
        return None


def _flashes_from_granule(
    path: Path, granule_epoch: int
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return (lat, lon, epoch) finite arrays for one granule. `epoch` is the
    ACTUAL per-flash UTC Unix second — the granule start time
    (`granule_epoch`, from the `_s...` filename) plus
    `flash_time_offset_of_first_event`, which netCDF4 decodes to seconds
    within the ~20 s granule. This spreads flashes across the granule so the
    app reveals them at their true times instead of in a granule-sized clump.
    Falls back to the granule start when the offset is missing or implausible
    (e.g. a file that encoded it as absolute seconds-since-J2000 instead)."""
    try:
        with Dataset(str(path)) as ds:
            if "flash_lat" not in ds.variables:
                return np.array([]), np.array([]), np.array([])
            lat = np.ma.filled(ds.variables["flash_lat"][:], np.nan).astype("float64")
            lon = np.ma.filled(ds.variables["flash_lon"][:], np.nan).astype("float64")
            if "flash_time_offset_of_first_event" in ds.variables:
                off = np.ma.filled(
                    ds.variables["flash_time_offset_of_first_event"][:], np.nan
                ).astype("float64")
            else:
                off = np.full(lat.shape, np.nan)
    except Exception as exc:
        # None (not empty arrays) so the sidecar cache never freezes a
        # transient parse failure as a permanently-empty granule.
        print(f"  parse failed {path.name}: {exc}", file=sys.stderr)
        return None
    epoch = float(granule_epoch) + off
    # Offsets should land within ~25 s of the granule start; anything else
    # (masked NaN, or a file encoding absolute J2000 seconds) falls back to
    # the granule time so the flash keeps a sane absolute timestamp.
    bad = ~np.isfinite(epoch) | (np.abs(off) > 25)
    epoch = np.where(bad, float(granule_epoch), epoch)
    good = np.isfinite(lat) & np.isfinite(lon)
    return lat[good], lon[good], epoch[good]


# Bump when _flashes_from_granule's extraction changes — stale sidecars
# then reparse instead of serving the old scheme.
_PARSE_CACHE_VERSION = 1


def _flashes_from_granule_cached(
    path: Path, granule_epoch: int
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Memoized _flashes_from_granule via an .npz sidecar beside the granule.

    Granule files are immutable, yet every ~20 s pass re-parsed the WHOLE
    rolling window (~hundreds of netCDF opens) to rebuild the feed — that
    redundant parsing was nearly all of the ~14 s pass time. With sidecars a
    pass parses only the 1-3 genuinely new granules and loads the rest in
    milliseconds, so the loop can tick every few seconds and flashes reach
    the map ~8-12 s sooner on average. Parse FAILURES are never cached (see
    _flashes_from_granule), and a torn/version-stale sidecar reparses.
    """
    side = Path(str(path) + ".flashes.npz")
    if side.exists():
        try:
            with np.load(side) as z:
                if int(z["v"]) == _PARSE_CACHE_VERSION:
                    return z["lat"], z["lon"], z["epoch"]
        except Exception:
            side.unlink(missing_ok=True)
    res = _flashes_from_granule(path, granule_epoch)
    if res is None:
        return np.array([]), np.array([]), np.array([])
    lat, lon, epoch = res
    try:
        tmp = Path(f"{side}.{os.getpid()}.tmp")
        with open(tmp, "wb") as f:
            np.savez(f, v=_PARSE_CACHE_VERSION, lat=lat, lon=lon, epoch=epoch)
        tmp.replace(side)  # atomic: a killed pass never leaves a torn sidecar
    except Exception:
        pass  # cache is an optimization — never let it fail a pass
    return lat, lon, epoch


def _prune_cache(cutoff_epoch: int) -> None:
    """Drop cached granules (and their parse sidecars) older than the window
    so /tmp stays bounded."""
    if not CACHE_DIR.exists():
        return
    for f in list(CACHE_DIR.glob("*.nc")) + list(CACHE_DIR.glob("*.flashes.npz")):
        e = _start_epoch_from_key(f.name)
        if e is None or e < cutoff_epoch:
            f.unlink(missing_ok=True)


def _in_coverage(lat: np.ndarray, lon: np.ndarray) -> np.ndarray:
    """Boolean mask: flashes inside the app's US service area (COVERAGE_*).
    Combined with the East/West split so neither the feed nor the
    MAX_FLASHES cap ever carries hemisphere lightning a US user can't see."""
    return (
        (lat >= COVERAGE_LAT[0])
        & (lat <= COVERAGE_LAT[1])
        & (lon >= COVERAGE_LON[0])
        & (lon <= COVERAGE_LON[1])
    )


def main() -> int:
    bucket_r2 = os.environ["R2_BUCKET"]
    now = dt.datetime.now(dt.timezone.utc)
    cutoff_epoch = int(now.timestamp()) - WINDOW_MIN * 60
    recent_cutoff = int(now.timestamp()) - RECENT_MIN * 60
    prefixes = _hour_prefixes(now)

    east = _pick_bucket(EAST_BUCKETS, prefixes, cutoff_epoch)
    west = _pick_bucket(WEST_BUCKETS, prefixes, cutoff_epoch)
    print(f"==> GLM east={east} west={west} window={WINDOW_MIN}m")

    # Per-satellite (bucket, keep_predicate). keep returns a boolean mask
    # over a granule's flash longitudes.
    #
    # Both satellites live: split at CUTOFF_LON so the ~CONUS overlap isn't
    # doubled (East takes lon >= cutoff, West takes lon < cutoff).
    #
    # One satellite down (e.g. the 2026-07 GOES-19 GLM outage that blacked
    # out the eastern US): don't black out that satellite's half. The
    # surviving bird's GLM sees well past the cutoff. GOES-18 (West) detects
    # lightning east to about -80 (most of CONUS bar the far East seaboard
    # and FL), and GOES-19 (East) reaches similarly far west, so let the
    # survivor cover the whole disk it can see until the other recovers.
    # Reverts to the clean split automatically the next pass once both
    # buckets return fresh data, so this needs no cleanup when the outage ends.
    keep_all = lambda lon: np.ones(lon.shape, dtype=bool)
    plan = []
    if east and west:
        plan.append((east, lambda lon: lon >= CUTOFF_LON))
        plan.append((west, lambda lon: lon < CUTOFF_LON))
        mode = "split (both satellites live)"
    elif east:
        plan.append((east, keep_all))
        mode = "EAST-ONLY fallback (West satellite down)"
    elif west:
        plan.append((west, keep_all))
        mode = "WEST-ONLY fallback (East satellite down)"
    else:
        print("  no GLM data available right now (satellite swap or outage?)", file=sys.stderr)
        return 0  # don't fail the loop; next pass may recover
    print(f"  coverage mode: {mode}")

    # (recent-tier, grid-lat, grid-lon, time-bucket) -> (lat, lon, epoch);
    # newer epoch wins for the same cell.
    dedup: dict[tuple[bool, int, int, int], tuple[float, float, int]] = {}

    for bucket, keep in plan:
        keys = _list_window_keys(bucket, prefixes, cutoff_epoch)
        for key, epoch in keys:
            path = _ensure_granule(bucket, key)
            if path is None:
                continue
            lat, lon, fep = _flashes_from_granule_cached(path, epoch)
            if lat.size == 0:
                continue
            # East/West split (or fallback keep_all) AND the US service-area
            # clip are both spatial masks, so combine them in one pass.
            sel = keep(lon) & _in_coverage(lat, lon)
            lat, lon, fep = lat[sel], lon[sel], fep[sel]
            # De-parallax with THIS granule's satellite (the bucket owns the
            # look geometry). Done after the split so each flash is corrected
            # for the bird that actually reported it; the sub-km shift can't
            # move a flash meaningfully across the CUTOFF_LON boundary.
            if DEPARALLAX_ENABLED and lat.size:
                sub = _SUBPOINT_LON.get(bucket)
                if sub is not None:
                    lat, lon = _deparallax(lat, lon, sub, DEPARALLAX_DH_KM)
            for la, lo, e in zip(lat, lon, fep):
                ei = int(e)
                # Two-tier dedup: fine for the recent half (live layer's
                # continuous reveal needs the density), coarse for the
                # older half (only sliced per radar frame). The tier flag
                # in the key keeps the tiers from colliding.
                recent = ei >= recent_cutoff
                grid = GRID_DEG if recent else GRID_DEG_OLD
                tb = TIME_BUCKET_SEC if recent else TIME_BUCKET_OLD_SEC
                gk = (recent, int(round(la / grid)), int(round(lo / grid)), ei // tb)
                prev = dedup.get(gk)
                if prev is None or ei > prev[2]:
                    dedup[gk] = (la, lo, ei)

    flashes = sorted(dedup.values(), key=lambda f: f[2])  # oldest -> newest
    if len(flashes) > MAX_FLASHES:
        flashes = flashes[-MAX_FLASHES:]

    payload = {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAt": now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "windowMinutes": WINDOW_MIN,
        "count": len(flashes),
        "flashes": [[round(la, 3), round(lo, 3), ep] for (la, lo, ep) in flashes],
    }

    _prune_cache(cutoff_epoch)

    out_path = Path(tempfile.gettempdir()) / "flashes.json"
    out_path.write_text(json.dumps(payload, separators=(",", ":")))

    # Short cache so the app sees fresh data within the loop cadence while
    # still letting Cloudflare's edge absorb repeated polls. 6 s (not 15)
    # keeps edge staleness well under the ~15-20 s rebuild cadence; origin
    # hits stay bounded by one per POP per 6 s only while users are
    # actively polling, comfortably inside B2's free class-B tier.
    subprocess.check_call(
        [
            "rclone",
            "copyto",
            str(out_path),
            f"r2:{bucket_r2}/{OUT_KEY}",
            "--s3-no-check-bucket",
            "--no-traverse",
            "--header-upload",
            "Cache-Control: public, max-age=6",
        ]
    )
    print(f"  uploaded {OUT_KEY}: {len(flashes)} flashes ({out_path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
