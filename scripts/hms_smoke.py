#!/usr/bin/env python3
"""hms_smoke.py — NOAA HMS smoke plumes for Spotter Tools Pro's Air Quality
layer.

The Hazard Mapping System publishes analyst-drawn smoke polygons (Light /
Medium / Heavy density) from GOES imagery as a daily shapefile, revised
through the (US-daytime) analysis day. This bake fetches the freshest day,
clips + simplifies it for map drape, normalises the attributes, and uploads
one GeoJSON the app draws directly:

  airquality/v1/smoke.json    FeatureCollection; per-feature properties:
                              d = light|medium|heavy, s/e = analysis window
                              (epoch seconds), sat = source satellite.
                              Foreign members: schemaVersion, generated,
                              day, count.

Runs as its own (continue-on-error) step of the airquality workflow: a bad
HMS publish must never take down the AQI bake, and a failed fetch leaves the
last good smoke.json in place rather than blanking the layer.

Env:  R2_BUCKET  (rclone "r2:" remote configured by the workflow -> B2)
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

# Same render-area bbox the AQI contours use (W, S, E, N). HMS covers all
# of North America; clipping to the app's service area keeps the file small
# while retaining cross-border plumes drifting toward CONUS.
BBOX = [-134.0, 21.0, -60.0, 53.0]

# Douglas-Peucker tolerance in degrees (~550 m). Smoke polygons are
# analyst-drawn regions viewed at national/state zoom — sub-km vertices are
# dead weight.
SIMPLIFY_DEG = 0.005

SHP_URL = (
    "https://satepsanone.nesdis.noaa.gov/pub/FIRE/web/HMS/Smoke_Polygons/"
    "Shapefile/{y}/{m:02d}/hms_smoke{y}{m:02d}{d:02d}.zip"
)

OUT_KEY = "airquality/v1/smoke.json"
SCHEMA_VERSION = 1

DENSITY = {"light": "light", "medium": "medium", "heavy": "heavy",
           # Legacy numeric encoding seen in older HMS files.
           "5": "light", "16": "medium", "27": "heavy"}


def _fetch_zip(day: dt.date, dest: Path) -> bool:
    url = SHP_URL.format(y=day.year, m=day.month, d=day.day)
    req = urllib.request.Request(
        url, headers={"User-Agent": "spotter-hms-smoke-renderer/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            dest.write_bytes(resp.read())
        return True
    except urllib.error.HTTPError as exc:
        print(f"  {day} HTTP {exc.code}", file=sys.stderr)
        return False


def _parse_hms_time(raw: str | None) -> int | None:
    """HMS Start/End are 'YYYYDDD HHMM' (UTC, DDD = day-of-year)."""
    if not raw:
        return None
    parts = raw.split()
    try:
        base = dt.datetime.strptime(parts[0], "%Y%j").replace(
            tzinfo=dt.timezone.utc)
        if len(parts) > 1 and len(parts[1]) == 4:
            base = base.replace(hour=int(parts[1][:2]),
                                minute=int(parts[1][2:]))
        return int(base.timestamp())
    except (ValueError, IndexError):
        return None


def _rclone_upload(local: Path, key: str, cache_seconds: int) -> None:
    bucket = os.environ["R2_BUCKET"]
    subprocess.check_call([
        "rclone", "copyto", str(local), f"r2:{bucket}/{key}",
        "--s3-no-check-bucket", "--no-traverse",
        "--header-upload", f"Cache-Control: public, max-age={cache_seconds}",
    ])


def main() -> int:
    today = dt.datetime.now(dt.timezone.utc).date()
    tmp = Path(tempfile.mkdtemp(prefix="hms_"))
    zip_path = tmp / "hms.zip"

    # Today's file appears late morning ET and is revised through the day;
    # before then, yesterday's final analysis is the freshest there is.
    day = None
    for cand in (today, today - dt.timedelta(days=1)):
        if _fetch_zip(cand, zip_path):
            day = cand
            break
    if day is None:
        # Leave the last good smoke.json in place.
        print("no HMS smoke file for today or yesterday", file=sys.stderr)
        return 1
    print(f"==> HMS smoke {day} ({zip_path.stat().st_size} bytes)")

    with zipfile.ZipFile(zip_path) as z:
        z.extractall(tmp)
    shps = list(tmp.glob("*.shp"))
    if not shps:
        print("zip held no .shp", file=sys.stderr)
        return 1

    # Clip to the service area + simplify + trim coordinate precision.
    raw_geojson = tmp / "raw.geojson"
    w, s, e, n = BBOX
    subprocess.check_call([
        "ogr2ogr", "-f", "GeoJSON",
        "-clipsrc", str(w), str(s), str(e), str(n),
        "-simplify", str(SIMPLIFY_DEG),
        "-lco", "COORDINATE_PRECISION=3",
        str(raw_geojson), str(shps[0]),
    ])

    fc = json.loads(raw_geojson.read_text())
    feats = []
    for f in fc.get("features", []):
        geom = f.get("geometry")
        if not geom or geom.get("type") not in ("Polygon", "MultiPolygon"):
            continue
        props = f.get("properties") or {}
        d = DENSITY.get(str(props.get("Density", "")).strip().lower())
        if d is None:
            # Unknown density — draw it at the cautious end rather than
            # dropping an analyst-flagged plume.
            d = "light"
        feats.append({
            "type": "Feature",
            "properties": {
                "d": d,
                "s": _parse_hms_time(props.get("Start")),
                "e": _parse_hms_time(props.get("End")),
                "sat": props.get("Satellite") or None,
            },
            "geometry": geom,
        })

    out = tmp / "smoke.json"
    out.write_text(json.dumps({
        "type": "FeatureCollection",
        "schemaVersion": SCHEMA_VERSION,
        "generated": int(dt.datetime.now(dt.timezone.utc).timestamp()),
        "day": day.isoformat(),
        "count": len(feats),
        "features": feats,
    }, separators=(",", ":")))
    _rclone_upload(out, OUT_KEY, 900)
    by_d = {k: sum(1 for f in feats if f["properties"]["d"] == k)
            for k in ("light", "medium", "heavy")}
    print(f"  uploaded smoke.json ({len(feats)} plumes, {by_d})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
