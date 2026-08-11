#!/usr/bin/env python3
"""wildfire.py — active wildfire feed bake for Spotter Tools Pro.

Produces everything the app's Wildfire layer needs, on the same B2 bucket +
domain + secrets as the model renderer, lightning and air-quality feeds:

  wildfire/v1/current.json      active incidents (points + card fields)
  wildfire/v1/perimeters.json   burn-area polygons (GeoJSON, simplified)
  wildfire/v1/hotspots.json     VIIRS/MODIS thermal detections, last 24 h

Sources, all free and key-less:

  * NIFC WFIGS (ArcGIS FeatureServer, native GeoJSON out) — the authoritative
    incident record, fed by IRWIN, which every federal AND state agency
    reports into. Refreshes every 5 min upstream; fall-off rules drop stale
    records hourly.
  * NASA FIRMS bulk 24 h CSVs — satellite thermal detections at 375 m (VIIRS)
    / 1 km (MODIS). These are what make the layer feel live between the
    official acreage updates: WFIGS says a fire exists and how big it was at
    last report, FIRMS shows where it is actually burning right now.

Why this lives in the renderer rather than a Cloudflare Worker: the Workers
free tier is 100,000 requests/day ACCOUNT-WIDE across every Worker on the
account, so a per-user polling proxy would draw down the same budget the
telemetry / cameras / announcements / alert-preview Workers depend on. A
baked static file on models.dgwaynes.com is served by the CDN straight from
B2 with no Worker in the path, so it costs zero invocations at any user
count. Public-repo Actions minutes + free B2 uploads + free CDN egress keep
the whole thing at $0.

Cost note: write to FIXED keys and never sweep a directory. B2 Class C (LIST)
ops are the one metered vector here — only the first 2,500/day are free.

Env:  R2_BUCKET   (the rclone "r2:" remote is configured by the workflow -> B2)
"""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import math
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────

WFIGS_BASE = (
    "https://services3.arcgis.com/T4QMspbfLg3qTGWY/arcgis/rest/services"
)
INCIDENTS_URL = f"{WFIGS_BASE}/WFIGS_Incident_Locations_Current/FeatureServer/0/query"
PERIMETERS_URL = (
    f"{WFIGS_BASE}/WFIGS_Interagency_Perimeters_Current/FeatureServer/0/query"
)

# Fields pulled for the incident points. Everything here either drives the
# map (geometry, size ramp) or shows on the tap card — WFIGS exposes 97
# fields and shipping them all would quadruple the payload for no gain.
#
# NOTE: the acreage field is `IncidentSize`. Older NIFC docs and most
# tutorials still say `DailyAcres`, which no longer exists on this service
# and 400s the whole query if you ask for it.
INCIDENT_FIELDS = ",".join([
    "IncidentName",
    "IncidentSize",
    "PercentContained",
    "FireCause",
    "FireCauseGeneral",
    "FireBehaviorGeneral",
    "FireDiscoveryDateTime",
    "ModifiedOnDateTime_dt",
    "ContainmentDateTime",
    "POOState",
    "POOCounty",
    "TotalIncidentPersonnel",
    "IncidentComplexityLevel",
    "IncidentManagementOrganization",
    "PredominantFuelGroup",
    "EstimatedCostToDate",
    "GACC",
    "IrwinID",
    "IncidentTypeCategory",
    "IncidentShortDescription",
    "POOJurisdictionalAgency",
])

# Perimeter geometry is simplified server-side. Units follow outSR, so with
# outSR=4326 this is degrees: 0.002 deg ~= 220 m, which is far finer than a
# fire perimeter is actually known to and cuts the payload by roughly an
# order of magnitude. Without it a busy season's polygons run to tens of MB.
PERIM_OFFSET = 0.002

# FIRMS bulk 24 h CSVs. No MAP_KEY needed for these static files (the
# documented /api/area/ endpoint does need one; these do not).
FIRMS_BASE = "https://firms.modaps.eosdis.nasa.gov/data/active_fire"
FIRMS_FEEDS = [
    ("noaa-20-viirs-c2", "J1_VIIRS_C2_USA_contiguous_and_Hawaii_24h.csv"),
    ("suomi-npp-viirs-c2", "SUOMI_VIIRS_C2_USA_contiguous_and_Hawaii_24h.csv"),
    ("noaa-21-viirs-c2", "J2_VIIRS_C2_USA_contiguous_and_Hawaii_24h.csv"),
    ("modis-c6.1", "MODIS_C6_1_USA_contiguous_and_Hawaii_24h.csv"),
    ("noaa-20-viirs-c2", "J1_VIIRS_C2_Alaska_24h.csv"),
    ("suomi-npp-viirs-c2", "SUOMI_VIIRS_C2_Alaska_24h.csv"),
]

# Hard ceiling on hotspots shipped. A bad smoke day across the whole west
# can push past this; keeping the highest fire-radiative-power detections
# bounds the payload without dropping anything a user would notice. Logged
# when it bites, so a silent truncation never reads as "that was everything".
HOTSPOT_CAP = 30000

# Detections older than this are dropped. The FIRMS files are NAMED "24h"
# but are a rolling window of the last 24 hours of *available* data, and
# processing lag means they routinely carry detections 30-40 h old
# (measured: 2,394 min on a live pull). This layer exists to answer "where
# is it burning right now", so a day-and-a-half-old thermal signature is
# actively misleading — the fire has either moved or been put out.
MAX_AGE_MIN = 1440

OUT_PREFIX = "wildfire/v1"
SCHEMA_VERSION = 1

# Cache lifetimes. WFIGS moves every 5 min upstream and this job runs every
# 15, so 300 s lets the CDN refresh roughly in step without ever serving
# something older than one bake. FIRMS only publishes per satellite overpass.
CACHE_INCIDENTS = 300
CACHE_HOTSPOTS = 600


# ── HTTP ──────────────────────────────────────────────────────────────

def _get(url: str, *, tries: int = 4, timeout: int = 90) -> bytes:
    """GET with backoff.

    NIFC's ArcGIS returns sporadic 503s under load — observed on roughly 1
    request in 6 during fire season, which is exactly when this job matters.
    Retrying here is what keeps a transient upstream blip from publishing a
    truncated feed.
    """
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(
                url,
                headers={
                    "User-Agent": "storm-spotter-renderer/1.0 (wildfire bake)",
                    "Accept-Encoding": "identity",
                },
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except (urllib.error.URLError, urllib.error.HTTPError, OSError) as exc:
            last = exc
            if attempt < tries - 1:
                wait = 2 ** attempt
                print(f"  retry {attempt + 1}/{tries - 1} in {wait}s ({exc})",
                      file=sys.stderr)
                time.sleep(wait)
    raise RuntimeError(f"GET failed after {tries} tries: {url} ({last})")


def _arcgis_get(url: str, *, tries: int = 5) -> dict:
    """GET an ArcGIS query and decode it, retrying its in-body errors.

    ArcGIS does NOT use HTTP status codes for throttling: a rate-limited or
    overloaded query comes back as **HTTP 200** with an `error` object in
    the JSON body, so the transport-level retry in [_get] never sees it.
    The two that matter in practice:

      429  "API calls quota exceeded (… request units) … per Minute"
           — an ORG-WIDE quota on the NIFC ArcGIS account, shared across
           every consumer of these public services worldwide. Nothing we do
           reduces our own draw enough to avoid it; we just have to back off
           and retry. It is also the single strongest argument for baking
           this feed once here rather than letting each app install query
           NIFC directly, which would multiply our share of that quota by
           the install base.
      503  transient unavailability under fire-season load.

    The 429 message asks for a 60 s wait, so the backoff starts long rather
    than burning retries on 1-2 s sleeps that cannot possibly clear a
    per-minute quota window.
    """
    last = ""
    for attempt in range(tries):
        raw = _get(url)
        try:
            doc = json.loads(raw.decode("utf-8", "replace"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"non-JSON from ArcGIS: {exc}") from exc
        err = doc.get("error") if isinstance(doc, dict) else None
        if not err:
            return doc
        code = err.get("code")
        last = f"{code}: {err.get('message')}"
        if code not in (429, 503) or attempt == tries - 1:
            raise RuntimeError(f"ArcGIS error: {last}")
        wait = 65 if code == 429 else 5 * (attempt + 1)
        print(f"  ArcGIS {code}, waiting {wait}s "
              f"(attempt {attempt + 1}/{tries - 1})", file=sys.stderr)
        time.sleep(wait)
    raise RuntimeError(f"ArcGIS error: {last}")


def _arcgis_pages(base: str, params: dict) -> list:
    """Query an ArcGIS layer as GeoJSON, following resultOffset paging.

    maxRecordCount on both WFIGS layers is 2000 and today's counts sit well
    under that, but a bad season is exactly when this would silently clip,
    so page properly rather than trusting one request to be complete.
    """
    out: list = []
    offset = 0
    page = 2000
    while True:
        q = dict(params)
        q["resultOffset"] = str(offset)
        q["resultRecordCount"] = str(page)
        url = base + "?" + urllib.parse.urlencode(q)
        doc = _arcgis_get(url)
        feats = (doc or {}).get("features") or []
        out.extend(feats)
        if len(feats) < page:
            return out
        offset += page
        if offset > 20000:  # runaway guard
            print("  WARN: paging guard hit at 20000 features", file=sys.stderr)
            return out


# ── Size ramp ─────────────────────────────────────────────────────────

def _size_for(acres: float) -> float:
    """Icon-size multiplier for a fire of `acres`, as a smooth log ramp.

    Computed HERE rather than as a Mapbox expression on the client for two
    reasons. Fire size spans six orders of magnitude (a 0.1-acre spot to a
    550,000-acre megafire), so it needs real log math, and the three
    platforms run three different renderers — the native Android and iOS
    SDKs plus Mapbox GL JS inside a webview on Windows. Baking one number
    means all three draw identically and none of them has to agree on how
    to evaluate a log expression.

    The output is a multiplier on the layer's base size, NOT a pixel value,
    so the user's size slider and the per-platform base still compose on top.

    NOTE: this is the RAW ramp — where a fire sits on the size spectrum,
    which is a property of the data. It is NOT the final drawn size. The
    app remaps this band onto a tighter display band (kWildfireMinIconScale
    .. kWildfireMaxIconScale in lib/data/models/wildfire.dart) so the
    visual cap can be retuned without re-baking every incident. Shipping
    this range straight through put a megafire at ~46x64 logical px, which
    was far too large at regional zoom. Do not "fix" apparent oversizing
    here; change the cap in the app.
    """
    a = max(float(acres or 0.0), 0.1)
    # log10: 0.1ac -> -1, 10ac -> 1, 1k -> 3, 100k -> 5, 1M -> 6
    lg = math.log10(a)
    # Map [-1, 6] onto [0.55, 1.80]. Small fires stay legible rather than
    # collapsing to a speck; the ceiling stops a megafire from swallowing
    # half a state at low zoom.
    t = (lg + 1.0) / 7.0
    return round(0.55 + (1.80 - 0.55) * max(0.0, min(1.0, t)), 3)


def _epoch_s(v) -> int | None:
    """ArcGIS date fields come through GeoJSON as epoch MILLIseconds."""
    if isinstance(v, (int, float)) and v > 0:
        return int(v / 1000)
    return None


# ── Incidents ─────────────────────────────────────────────────────────

def fetch_incidents() -> list[dict]:
    feats = _arcgis_pages(INCIDENTS_URL, {
        # Wildfires, prescribed burns, and complexes — but NOT the member
        # fires that roll up into a complex.
        #
        # A complex and its children BOTH appear in this layer with
        # overlapping acreage, so taking all of them double-counts. Live
        # example: ROWE CREEK COMPLEX reports 371,549 acres and its child
        # 0445 CROSSWHITE reports 342,681 of those same acres. Keeping the
        # parent and dropping `IsCpxChild` fires means every acre is drawn
        # exactly once, under the name the incident is actually reported
        # under by InciWeb and the news. The reverse (dropping CX, keeping
        # children) also avoids the double-draw but leaves users hunting for
        # a "Rowe Creek Complex" that the map never labels.
        "where": ("IncidentTypeCategory IN ('WF','RX','CX') "
                  "AND (IsCpxChild IS NULL OR IsCpxChild = 0)"),
        "outFields": INCIDENT_FIELDS,
        "returnGeometry": "true",
        "outSR": "4326",
        "f": "geojson",
    })
    fires: list[dict] = []
    for f in feats:
        if not isinstance(f, dict):
            continue
        geom = f.get("geometry")
        # A null-geometry record blanks the ENTIRE GeoJSON source on Android
        # GL while iOS renders straight through it (the same divergence the
        # tropical layer's wind-field records tripped). Drop them here so the
        # bad case can never reach a device.
        if not isinstance(geom, dict):
            continue
        coords = geom.get("coordinates")
        if not (isinstance(coords, list) and len(coords) >= 2):
            continue
        try:
            lon = float(coords[0])
            lat = float(coords[1])
        except (TypeError, ValueError):
            continue
        if not (-180 <= lon <= 180 and -90 <= lat <= 90):
            continue
        if lon == 0 and lat == 0:
            continue  # null-island sentinel

        p = f.get("properties") or {}
        acres = p.get("IncidentSize")
        acres = float(acres) if isinstance(acres, (int, float)) else 0.0
        contained = p.get("PercentContained")
        name = (p.get("IncidentName") or "").strip() or "Unnamed fire"

        fires.append({
            "id": (p.get("IrwinID") or f"{lat:.4f},{lon:.4f}"),
            "lat": round(lat, 5),
            "lon": round(lon, 5),
            "name": name,
            "acres": round(acres, 1),
            "sz": _size_for(acres),
            "cont": (round(float(contained), 0)
                     if isinstance(contained, (int, float)) else None),
            "cause": p.get("FireCause") or p.get("FireCauseGeneral"),
            "beh": p.get("FireBehaviorGeneral"),
            "disc": _epoch_s(p.get("FireDiscoveryDateTime")),
            "upd": _epoch_s(p.get("ModifiedOnDateTime_dt")),
            "state": (p.get("POOState") or "").replace("US-", "") or None,
            "county": p.get("POOCounty"),
            "crew": p.get("TotalIncidentPersonnel"),
            "cplx": p.get("IncidentComplexityLevel"),
            "imt": p.get("IncidentManagementOrganization"),
            "fuel": p.get("PredominantFuelGroup"),
            "cost": p.get("EstimatedCostToDate"),
            "gacc": p.get("GACC"),
            "agency": p.get("POOJurisdictionalAgency"),
            "desc": p.get("IncidentShortDescription"),
            # Card badge: prescribed burns are intentional and a complex is
            # several fires managed as one, so neither should read as a
            # plain wildfire.
            "rx": 1 if p.get("IncidentTypeCategory") == "RX" else 0,
            "cx": 1 if p.get("IncidentTypeCategory") == "CX" else 0,
        })
    # Biggest last so the symbol layer's sort key can draw megafires on top
    # of the specks rather than under them.
    fires.sort(key=lambda x: x["acres"])
    return fires


# ── Perimeters ────────────────────────────────────────────────────────

def fetch_perimeters() -> dict:
    feats = _arcgis_pages(PERIMETERS_URL, {
        "where": "1=1",
        # Perimeter fields carry `poly_` / `attr_` prefixes on this layer.
        "outFields": "poly_IncidentName,poly_GISAcres,poly_IRWINID,attr_IncidentSize",
        "returnGeometry": "true",
        "outSR": "4326",
        "maxAllowableOffset": str(PERIM_OFFSET),
        "f": "geojson",
    })
    out = []
    for f in feats:
        if not isinstance(f, dict):
            continue
        geom = f.get("geometry")
        if not isinstance(geom, dict) or not geom.get("coordinates"):
            continue  # null geometry — see the note in fetch_incidents
        p = f.get("properties") or {}
        acres = p.get("poly_GISAcres") or p.get("attr_IncidentSize") or 0
        out.append({
            "type": "Feature",
            "geometry": geom,
            "properties": {
                "id": p.get("poly_IRWINID") or "",
                "name": (p.get("poly_IncidentName") or "").strip(),
                "acres": round(float(acres), 1)
                if isinstance(acres, (int, float)) else 0.0,
            },
        })
    return {"type": "FeatureCollection", "features": out}


# ── FIRMS hotspots ────────────────────────────────────────────────────

def fetch_hotspots(now: dt.datetime) -> list[list]:
    """Thermal detections from the last 24 h, as compact rows.

    Returns [lat, lon, frp, ageMinutes, highConfidence] per detection. A
    compact array rather than GeoJSON because this is the one payload that
    can run to tens of thousands of records — the app builds the feature
    collection client-side, same as the AirNow dots.
    """
    rows: list[list] = []
    seen: set[tuple] = set()
    for sat_dir, fname in FIRMS_FEEDS:
        url = f"{FIRMS_BASE}/{sat_dir}/csv/{fname}"
        try:
            raw = _get(url, tries=3, timeout=120)
        except RuntimeError as exc:
            # One satellite being unavailable is not worth failing the bake —
            # the others still carry the picture.
            print(f"  FIRMS {fname}: {exc}", file=sys.stderr)
            continue
        text = raw.decode("utf-8", "replace")
        reader = csv.DictReader(io.StringIO(text))
        kept = 0
        for r in reader:
            try:
                lat = float(r["latitude"])
                lon = float(r["longitude"])
            except (KeyError, TypeError, ValueError):
                continue
            conf = (r.get("confidence") or "").strip().lower()
            # VIIRS ships low/nominal/high; MODIS ships 0-100. Drop the
            # low-confidence tail either way — it is mostly hot bare ground
            # and industrial heat, and it is the bulk of the row count.
            high = False
            if conf in ("low", "l"):
                continue
            if conf in ("high", "h"):
                high = True
            elif conf.isdigit():
                n = int(conf)
                if n < 50:
                    continue
                high = n >= 80
            try:
                frp = float(r.get("frp") or 0)
            except ValueError:
                frp = 0.0

            # acq_date=YYYY-MM-DD, acq_time=HHMM (UTC). A row whose stamp
            # will not parse is dropped rather than defaulted to "now" —
            # dating an unknown detection to the present would paint it as
            # the freshest thing on the map.
            try:
                d = r.get("acq_date") or ""
                t = (r.get("acq_time") or "0").zfill(4)
                stamp = dt.datetime.strptime(
                    f"{d} {t}", "%Y-%m-%d %H%M").replace(tzinfo=dt.timezone.utc)
            except ValueError:
                continue
            age_min = max(0, int((now - stamp).total_seconds() // 60))
            if age_min > MAX_AGE_MIN:
                continue

            # Round to ~11 m. The detections are 375 m pixels, so more
            # precision than this is noise that costs payload.
            key = (round(lat, 4), round(lon, 4), age_min // 30)
            if key in seen:
                continue
            seen.add(key)
            rows.append([round(lat, 4), round(lon, 4),
                         round(frp, 1), age_min, 1 if high else 0])
            kept += 1
        print(f"  FIRMS {fname}: {kept} kept")

    if len(rows) > HOTSPOT_CAP:
        rows.sort(key=lambda x: -x[2])  # highest fire radiative power first
        print(f"  NOTE: capping hotspots {len(rows)} -> {HOTSPOT_CAP} "
              f"(kept highest FRP)")
        rows = rows[:HOTSPOT_CAP]
    return rows


# ── Upload ────────────────────────────────────────────────────────────

def _rclone_upload(local: Path, key: str, cache_seconds: int) -> None:
    bucket = os.environ["R2_BUCKET"]
    subprocess.check_call([
        "rclone", "copyto", str(local), f"r2:{bucket}/{key}",
        "--s3-no-check-bucket", "--no-traverse",
        # Without this the object lands header-less and Cloudflare falls back
        # to the zone default (~4 h), which silently serves a stale feed.
        "--header-upload", f"Cache-Control: public, max-age={cache_seconds}",
    ])


def _write(tmp: Path, name: str, payload: dict, cache_s: int) -> int:
    path = tmp / name
    body = json.dumps(payload, separators=(",", ":"))
    path.write_text(body, encoding="utf-8")
    _rclone_upload(path, f"{OUT_PREFIX}/{name}", cache_s)
    return len(body)


def main() -> int:
    now = dt.datetime.now(dt.timezone.utc)
    stamp = int(now.timestamp())

    print("Fetching WFIGS incidents…")
    fires = fetch_incidents()
    print(f"  {len(fires)} incidents")
    if not fires:
        # Never publish an empty incident feed over a good one: an upstream
        # hiccup would blank the layer on every device until the next tick.
        print("ERROR: zero incidents returned — refusing to publish",
              file=sys.stderr)
        return 1

    print("Fetching WFIGS perimeters…")
    try:
        perims = fetch_perimeters()
        print(f"  {len(perims['features'])} polygons")
    except RuntimeError as exc:
        print(f"  perimeters failed, publishing points only: {exc}",
              file=sys.stderr)
        perims = None

    print("Fetching FIRMS hotspots…")
    try:
        pts = fetch_hotspots(now)
        print(f"  {len(pts)} detections")
    except RuntimeError as exc:
        print(f"  hotspots failed: {exc}", file=sys.stderr)
        pts = None

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        n = _write(tmp, "current.json", {
            "schemaVersion": SCHEMA_VERSION,
            "generated": stamp,
            "count": len(fires),
            "fires": fires,
        }, CACHE_INCIDENTS)
        print(f"  current.json {n / 1024:.1f} KB")

        if perims is not None:
            perims["schemaVersion"] = SCHEMA_VERSION
            perims["generated"] = stamp
            n = _write(tmp, "perimeters.json", perims, CACHE_INCIDENTS)
            print(f"  perimeters.json {n / 1024:.1f} KB")

        if pts is not None:
            n = _write(tmp, "hotspots.json", {
                "schemaVersion": SCHEMA_VERSION,
                "generated": stamp,
                "count": len(pts),
                # [lat, lon, frp, ageMinutes, highConfidence]
                "pts": pts,
            }, CACHE_HOTSPOTS)
            print(f"  hotspots.json {n / 1024:.1f} KB")

    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
