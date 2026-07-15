#!/usr/bin/env python3
"""airquality.py — EPA AirNow AQI feed + animation bake for Spotter Tools Pro.

Built on the **same** B2 bucket + domain + secrets as the model renderer and
lightning feed. Produces everything the app's Air Quality layer needs:

  airquality/v1/conus.json       latest AQI per monitor+pollutant (live layer)
  airquality/v1/loop24.json      per-monitor 24 h hourly series (animated dots)
  airquality/v1/contours/<view>/F<idx>.png   hourly shaded-surface PNGs
  airquality/v1/loop_manifest.json           frames/hours/bbox + the URLs above

`<view>` is one of: combined (worst pollutant), pm25, ozone. Contours are an
inverse-distance interpolation (gdal_grid) of the point AQI, colour-relieved
with config/color_tables/aqi.clr and reprojected to the same EPSG:3857 CONUS
bbox the model frames use, so the app drapes them exactly like a model frame.

Why this lives in the renderer (not a Cloudflare Worker): assembling a 24 h
nationwide series + rasterising 72 contour frames blows past the Workers free
CPU limit. GitHub Actions runners have no such cap, and public-repo minutes +
B2 (free uploads, free CDN egress) keep it $0.

Env:  R2_BUCKET        (rclone "r2:" remote is configured by the workflow -> B2)
      AIRNOW_API_KEY   (GitHub secret; keeps the key server-side)
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
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────
# CONUS bbox + image size match the model frames (see config/products.yml
# HRRR), so the app reuses its existing model-frame corner drape for the
# contour PNGs. Contours are smooth low-frequency surfaces, so 1024² is
# plenty and keeps each PNG tiny + the gdal sweep fast.
BBOX = [-134.0, 21.0, -60.0, 53.0]  # W, S, E, N
IMG_W, IMG_H = 1024, 1024

# AirNow query bbox — a touch tighter than the render bbox (AirNow only has
# monitors over land in/near CONUS anyway). W,S,E,N.
AIRNOW_BBOX = "-125,24,-66,50"
AIRNOW_PARAMS = "OZONE,PM25,PM10"
WINDOW_HOURS = 26  # request a little extra history so all 24 buckets fill
FRAMES = 24

# The three pollutant views the app's chip switches between. "combined" is
# the worst pollutant at each monitor (how AQI is normally reported).
VIEWS = ["combined", "pm25", "ozone"]
# AirNow Parameter strings that map to each single-pollutant view.
VIEW_PARAM = {"pm25": "PM2.5", "ozone": "OZONE"}

# gdal_grid IDW: inverse-distance-to-nearest-neighbours. `radius` (degrees)
# bounds how far a monitor's influence reaches, so remote areas with no
# nearby monitor stay nodata (transparent) instead of smearing — the AirNow
# contour behaviour.
GRID_ALG = "invdistnn:power=2.0:radius=4.0:max_points=20:min_points=1:nodata=-9999"

OUT_PREFIX = "airquality/v1"
SCHEMA_VERSION = 1
REPO_ROOT = Path(__file__).resolve().parent.parent
CLR_PATH = REPO_ROOT / "config" / "color_tables" / "aqi.clr"


def _category(aqi: int) -> int:
    if aqi <= 50:
        return 1
    if aqi <= 100:
        return 2
    if aqi <= 150:
        return 3
    if aqi <= 200:
        return 4
    if aqi <= 300:
        return 5
    return 6


# AirNow's aq/data endpoint rejects long date ranges (HTTP 400) — a 26 h
# CONUS query 400s, while a ~3 h one is fine. So fetch the window in short
# chunks and combine; the caller's hourly bucketing dedups any overlap.
CHUNK_HOURS = 3


def _fetch_airnow(start: dt.datetime, end: dt.datetime, key: str) -> list:
    """Fetch [start, end) in <=CHUNK_HOURS windows and combine. A failed
    chunk is logged and skipped rather than aborting the whole bake."""
    def fmt(d: dt.datetime) -> str:
        return d.strftime("%Y-%m-%dT%H")

    rows: list = []
    t = start
    while t < end:
        c_end = min(t + dt.timedelta(hours=CHUNK_HOURS), end)
        url = (
            "https://www.airnowapi.org/aq/data/"
            f"?startDate={fmt(t)}&endDate={fmt(c_end)}"
            f"&parameters={AIRNOW_PARAMS}&BBOX={AIRNOW_BBOX}"
            "&dataType=A&format=application/json&verbose=1"
            "&monitorType=2&includerawconcentrations=0"
            f"&API_KEY={key}"
        )
        req = urllib.request.Request(
            url, headers={"User-Agent": "spotter-airquality-renderer/1.0"}
        )
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                data = json.loads(resp.read().decode("utf-8", "replace"))
            if isinstance(data, list):
                rows.extend(data)
            else:
                print(f"  chunk {fmt(t)} non-list response", file=sys.stderr)
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")[:200]
            print(f"  chunk {fmt(t)}..{fmt(c_end)} HTTP {exc.code}: {body}",
                  file=sys.stderr)
        t = c_end
    if not rows:
        raise RuntimeError("AirNow returned no rows for any chunk")
    return rows


def _rclone_upload(local: Path, key: str, cache_seconds: int) -> None:
    bucket = os.environ["R2_BUCKET"]
    subprocess.check_call(
        [
            "rclone", "copyto", str(local), f"r2:{bucket}/{key}",
            "--s3-no-check-bucket", "--no-traverse",
            "--header-upload", f"Cache-Control: public, max-age={cache_seconds}",
        ]
    )


def _run(cmd: list) -> None:
    subprocess.check_call(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.STDOUT)


def _render_contour(points: list[tuple[float, float, int]], out_png: Path) -> bool:
    """points -> IDW grid -> EPSG:3857 CONUS -> AQI colour-relief -> PNG.
    Returns False (non-fatal) if the frame has too few points to interpolate."""
    if len(points) < 3:
        return False
    work = out_png.parent
    csv = work / "pts.csv"
    vrt = work / "pts.vrt"
    with csv.open("w") as f:
        f.write("lon,lat,aqi\n")
        for lon, lat, aqi in points:
            f.write(f"{lon},{lat},{aqi}\n")
    vrt.write_text(
        '<OGRVRTDataSource>\n'
        '  <OGRVRTLayer name="pts">\n'
        f'    <SrcDataSource relativeToVRT="1">{csv.name}</SrcDataSource>\n'
        "    <GeometryType>wkbPoint</GeometryType>\n"
        '    <LayerSRS>EPSG:4326</LayerSRS>\n'
        '    <GeometryField encoding="PointFromColumns" x="lon" y="lat"/>\n'
        "    <Field name=\"aqi\" type=\"Real\"/>\n"
        "  </OGRVRTLayer>\n"
        "</OGRVRTDataSource>\n"
    )
    grid_tif = work / "grid.tif"
    merc_tif = work / "merc.tif"
    rgba_tif = work / "rgba.tif"
    w, s, e, n = BBOX
    # 1. IDW interpolate the points to a 4326 raster over the CONUS extent.
    _run([
        "gdal_grid", "-q", "-a", GRID_ALG, "-zfield", "aqi",
        "-txe", str(w), str(e), "-tye", str(s), str(n),
        "-outsize", str(IMG_W), str(IMG_H),
        "-a_srs", "EPSG:4326", "-ot", "Float32", "-of", "GTiff",
        str(vrt), str(grid_tif),
    ])
    # 2. Reproject to EPSG:3857 with the fixed CONUS bbox (matches models).
    _run([
        "gdalwarp", "-q", "-overwrite", "-t_srs", "EPSG:3857",
        "-te_srs", "EPSG:4326", "-te", str(w), str(s), str(e), str(n),
        "-ts", str(IMG_W), str(IMG_H), "-r", "bilinear",
        "-dstnodata", "-9999", str(grid_tif), str(merc_tif),
    ])
    # 3. Colour-relief with the AQI ramp (smooth mode = AirNow contour look).
    _run([
        "gdaldem", "color-relief", "-q", "-alpha",
        str(merc_tif), str(CLR_PATH), str(rgba_tif),
    ])
    # 4. PNG.
    _run([
        "gdal_translate", "-q", "-of", "PNG", "-co", "ZLEVEL=9",
        str(rgba_tif), str(out_png),
    ])
    return out_png.exists() and out_png.stat().st_size > 0


def main() -> int:
    key = os.environ.get("AIRNOW_API_KEY")
    if not key:
        print("AIRNOW_API_KEY not set", file=sys.stderr)
        return 1

    now = dt.datetime.now(dt.timezone.utc)
    end_hour = now.replace(minute=0, second=0, microsecond=0)
    hours = [
        int((end_hour - dt.timedelta(hours=FRAMES - 1 - i)).timestamp())
        for i in range(FRAMES)
    ]
    hour0 = hours[0]

    print(f"==> AirNow fetch {WINDOW_HOURS}h window, {FRAMES} frames")
    rows = _fetch_airnow(
        end_hour - dt.timedelta(hours=WINDOW_HOURS),
        end_hour + dt.timedelta(hours=1),
        key,
    )
    print(f"  {len(rows)} rows")

    # station key -> {lat, lon, site, agency, series: {PARAM: [24]}}
    stations: dict[str, dict] = {}
    for r in rows:
        try:
            lat = float(r["Latitude"])
            lon = float(r["Longitude"])
        except (KeyError, TypeError, ValueError):
            continue
        aqi_raw = r.get("AQI", r.get("Value"))
        try:
            aqi = int(round(float(aqi_raw)))
        except (TypeError, ValueError):
            continue
        if aqi < 0:
            continue
        param = r.get("Parameter")
        if not isinstance(param, str) or not param:
            continue
        utc = str(r.get("UTC", "")).replace(" ", "T")
        try:
            t = int(
                dt.datetime.fromisoformat(utc)
                .replace(tzinfo=dt.timezone.utc)
                .timestamp()
            )
        except ValueError:
            continue
        idx = (t // 3600 * 3600 - hour0) // 3600
        if idx < 0 or idx >= FRAMES:
            continue
        skey = f"{lat:.4f},{lon:.4f}"
        st = stations.get(skey)
        if st is None:
            st = {
                "lat": round(lat, 4), "lon": round(lon, 4),
                "site": r.get("SiteName") or None,
                "agency": r.get("AgencyName") or None,
                "series": {},
            }
            stations[skey] = st
        arr = st["series"].setdefault(param, [None] * FRAMES)
        if arr[idx] is None or aqi > arr[idx]:
            arr[idx] = aqi

    st_list = [s for s in stations.values() if s["series"]]
    print(f"  {len(st_list)} monitors")

    # Carry each monitor's reading forward up to CARRY_HOURS when it misses an
    # update, so the newest frames — and the live/static contour, which is the
    # last frame — stay full instead of collapsing to a few blobs when a
    # slow-reporting pollutant (ozone especially) hasn't published the current
    # hour yet. Bounded so a monitor that genuinely went offline doesn't
    # linger a stale value all day.
    CARRY_HOURS = 3
    for s in st_list:
        for arr in s["series"].values():
            last = None
            carried = 0
            for i in range(FRAMES):
                if arr[i] is not None:
                    last = arr[i]
                    carried = 0
                elif last is not None and carried < CARRY_HOURS:
                    arr[i] = last
                    carried += 1
                else:
                    last = None
                    carried = 0

    tmp = Path(tempfile.mkdtemp(prefix="aqi_"))

    # ── conus.json — latest AQI per monitor+pollutant (live layer) ────────
    obs = []
    for s in st_list:
        for param, arr in s["series"].items():
            for i in range(FRAMES - 1, -1, -1):
                if arr[i] is not None:
                    obs.append({
                        "lat": s["lat"], "lon": s["lon"], "p": param,
                        "aqi": arr[i], "cat": _category(arr[i]),
                        "site": s["site"], "agency": s["agency"],
                        "t": hours[i],
                    })
                    break
    conus = tmp / "conus.json"
    conus.write_text(json.dumps(
        {"schemaVersion": SCHEMA_VERSION,
         "generated": int(now.timestamp()), "count": len(obs), "obs": obs},
        separators=(",", ":")))
    _rclone_upload(conus, f"{OUT_PREFIX}/conus.json", 900)
    print(f"  uploaded conus.json ({len(obs)} obs)")

    # ── loop24.json — per-monitor 24 h series (animated dots) ─────────────
    loop = tmp / "loop24.json"
    loop.write_text(json.dumps(
        {"schemaVersion": SCHEMA_VERSION,
         "generated": int(now.timestamp()), "frames": FRAMES, "hours": hours,
         "count": len(st_list), "stations": st_list},
        separators=(",", ":")))
    _rclone_upload(loop, f"{OUT_PREFIX}/loop24.json", 900)
    print(f"  uploaded loop24.json ({len(st_list)} monitors)")

    # ── Contour PNGs — per view, per hour ─────────────────────────────────
    def view_value(s: dict, view: str, i: int):
        if view == "combined":
            vals = [arr[i] for arr in s["series"].values() if arr[i] is not None]
            return max(vals) if vals else None
        arr = s["series"].get(VIEW_PARAM[view])
        return arr[i] if arr else None

    manifest_views: dict[str, list] = {v: [None] * FRAMES for v in VIEWS}
    for view in VIEWS:
        made = 0
        for i in range(FRAMES):
            pts = []
            for s in st_list:
                v = view_value(s, view, i)
                if v is not None:
                    pts.append((s["lon"], s["lat"], v))
            out_png = tmp / f"F{i:02d}.png"
            try:
                if _render_contour(pts, out_png):
                    key_rel = f"{OUT_PREFIX}/contours/{view}/F{i:02d}.png"
                    _rclone_upload(out_png, key_rel, 900)
                    manifest_views[view][i] = f"/{key_rel}"
                    made += 1
            except subprocess.CalledProcessError as exc:
                print(f"  contour {view} F{i:02d} failed (non-fatal): {exc}",
                      file=sys.stderr)
            finally:
                out_png.unlink(missing_ok=True)
        print(f"  contours[{view}]: {made}/{FRAMES} frames")

    # ── loop_manifest.json — everything the app animation needs ───────────
    base = "https://models.dgwaynes.com"
    manifest = tmp / "loop_manifest.json"
    manifest.write_text(json.dumps({
        "schemaVersion": SCHEMA_VERSION,
        "generatedAt": now.replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "frames": FRAMES,
        "hours": hours,
        "bbox": BBOX,
        "imageSize": [IMG_W, IMG_H],
        "dotsUrl": f"{base}/{OUT_PREFIX}/loop24.json",
        # per view: 24 absolute contour PNG URLs (null where a frame was empty)
        "views": {
            v: [(base + u) if u else None for u in manifest_views[v]]
            for v in VIEWS
        },
    }, separators=(",", ":")))
    _rclone_upload(manifest, f"{OUT_PREFIX}/loop_manifest.json", 900)
    print("  uploaded loop_manifest.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
