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
  5. Split East/West by longitude so the ~CONUS overlap isn't doubled.
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

# Rolling window the feed carries. The APP picks a shorter display window
# (5/10/20/30 min) client-side and, when the single-site radar animation
# plays, slices this by each frame's scan time to sync lightning with the
# storm — so the feed must carry enough history to cover the largest app
# window + recent radar frames. 30 min does both.
WINDOW_MIN = 30

# Dedup grid: collapse flashes sharing a ~1.1 km cell within the same
# 1-minute bucket to a single representative point. Bounds payload size
# during outbreaks (where GLM can report tens of thousands of flashes)
# without thinning the visible footprint.
GRID_DEG = 0.01
# Per-cell temporal dedup. 30 s keeps a busy cell from collapsing to one
# flash/min (which made the app's continuous reveal blink), while still
# bounding count.
TIME_BUCKET_SEC = 30

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
    """GLM-L2-LCFA prefixes for the current + previous UTC hour, enough to
    cover the rolling window even across an hour boundary."""
    prefixes = []
    for back in (1, 0):  # previous hour first, then current
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


# Unix seconds at the GLM/J2000 epoch (2000-01-01 12:00:00 UTC). GLM
# flash time offsets are "seconds since" this instant.
_J2000_UNIX = 946728000


def _flashes_from_granule(
    path: Path, granule_epoch: int
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """Return (lat, lon, epoch) finite arrays for one granule. `epoch` is the
    ACTUAL per-flash UTC Unix second — from `flash_time_offset_of_first_event`
    (seconds since the J2000 epoch) — so flashes spread across the ~20 s
    granule and the app can reveal them at their true times instead of in a
    granule-sized clump. Falls back to the granule start time per-flash when
    the offset variable is missing/masked."""
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
                epoch = _J2000_UNIX + off
            else:
                epoch = np.full(lat.shape, float(granule_epoch))
    except Exception as exc:
        print(f"  parse failed {path.name}: {exc}", file=sys.stderr)
        return np.array([]), np.array([]), np.array([])
    # Replace any non-finite flash time with the granule start so a masked
    # offset doesn't silently drop an otherwise-valid flash.
    epoch = np.where(np.isfinite(epoch), epoch, float(granule_epoch))
    good = np.isfinite(lat) & np.isfinite(lon)
    return lat[good], lon[good], epoch[good]


def _prune_cache(cutoff_epoch: int) -> None:
    """Drop cached granules older than the window so /tmp stays bounded."""
    if not CACHE_DIR.exists():
        return
    for f in CACHE_DIR.glob("*.nc"):
        e = _start_epoch_from_key(f.name)
        if e is None or e < cutoff_epoch:
            f.unlink(missing_ok=True)


def main() -> int:
    bucket_r2 = os.environ["R2_BUCKET"]
    now = dt.datetime.now(dt.timezone.utc)
    cutoff_epoch = int(now.timestamp()) - WINDOW_MIN * 60
    prefixes = _hour_prefixes(now)

    east = _pick_bucket(EAST_BUCKETS, prefixes, cutoff_epoch)
    west = _pick_bucket(WEST_BUCKETS, prefixes, cutoff_epoch)
    print(f"==> GLM east={east} west={west} window={WINDOW_MIN}m")

    # (lat, lon, epoch, keep_predicate) per satellite. East keeps the
    # eastern side of the cutoff; West keeps the western side.
    plan = []
    if east:
        plan.append((east, lambda lon: lon >= CUTOFF_LON))
    if west:
        plan.append((west, lambda lon: lon < CUTOFF_LON))
    if not plan:
        print("  no GLM data available right now (satellite swap or outage?)", file=sys.stderr)
        return 0  # don't fail the loop; next pass may recover

    # grid-key -> (lat, lon, epoch); newer epoch wins for the same cell.
    dedup: dict[tuple[int, int, int], tuple[float, float, int]] = {}

    for bucket, keep in plan:
        keys = _list_window_keys(bucket, prefixes, cutoff_epoch)
        for key, epoch in keys:
            path = _ensure_granule(bucket, key)
            if path is None:
                continue
            lat, lon, fep = _flashes_from_granule(path, epoch)
            if lat.size == 0:
                continue
            sel = keep(lon)
            lat, lon, fep = lat[sel], lon[sel], fep[sel]
            for la, lo, e in zip(lat, lon, fep):
                ei = int(e)
                tbucket = ei // TIME_BUCKET_SEC
                gk = (int(round(la / GRID_DEG)), int(round(lo / GRID_DEG)), tbucket)
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
    # still letting Cloudflare's edge absorb repeated polls.
    subprocess.check_call(
        [
            "rclone",
            "copyto",
            str(out_path),
            f"r2:{bucket_r2}/{OUT_KEY}",
            "--s3-no-check-bucket",
            "--no-traverse",
            "--header-upload",
            "Cache-Control: public, max-age=15",
        ]
    )
    print(f"  uploaded {OUT_KEY}: {len(flashes)} flashes ({out_path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
