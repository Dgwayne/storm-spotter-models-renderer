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

# Wall-clock ceiling on the whole FIRMS stage.
#
# [main] already treats hotspots as optional and publishes incidents +
# perimeters without them, but on 2026-08-11 that path proved unreachable:
# FIRMS went unroutable (Errno 101) and every attempt hung for its full
# timeout instead of failing fast, so six feeds x (3 tries x 120 s) came to
# ~36 min against the workflow's 15 min `timeout-minutes`. The job was killed
# two feeds in, before reaching a single upload, and the layer went stale for
# 75 min when only the thermal dots were actually unavailable.
#
# A budget rather than smaller per-feed numbers because the feed list has
# grown before (the two Alaska files came later) and any fixed tries/timeout
# pair silently stops fitting when it grows again. Healthy pulls take 1-3 s
# per feed, so this never binds on a good run.
HOTSPOT_BUDGET_S = 300

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
    # The hotspots twin of this guard, and for the same reason: main() skips
    # the write only on None, so an empty FeatureCollection would publish
    # over the last good ~640 KB file. A query that succeeds and returns
    # nothing is an upstream glitch, not an empty fire season, since the
    # point layer is what says whether anything is burning.
    if not out:
        raise RuntimeError("zero perimeters returned, refusing to publish "
                           "an empty perimeter feed")
    return {"type": "FeatureCollection", "features": out}


# ── FIRMS hotspots ────────────────────────────────────────────────────

# CSV `satellite` values -> the compact code the app maps back to a name.
# Both the short and long spellings appear across FIRMS instances.
SAT_CODES = {
    "N": 0, "NPP": 0, "SUOMI NPP": 0,      # Suomi NPP  (VIIRS)
    "N20": 1, "NOAA-20": 1,                # NOAA-20/JPSS-1 (VIIRS)
    "N21": 2, "NOAA-21": 2,                # NOAA-21/JPSS-2 (VIIRS)
    "A": 3, "AQUA": 3,                     # Aqua (MODIS)
    "T": 4, "TERRA": 4,                    # Terra (MODIS)
}


def fetch_hotspots(now: dt.datetime) -> list[list]:
    """Thermal detections from the last 24 h, as compact rows.

    Returns [lat, lon, frp, ageMinutes, highConfidence, brightnessK,
    satCode, isDay, footprintKm] per detection. A compact array rather than
    GeoJSON because this is the one payload that can run to tens of
    thousands of records — the app builds the feature collection
    client-side, same as the AirNow dots.

    APPEND-ONLY format: shipped clients index positions 0-4, so new fields
    may only ever be added to the END of the row. brightnessK is 0 when the
    CSV had none (real values are always 200+ K), footprintKm 0 when scan/
    track were unparsable, satCode -1 when the satellite is unrecognised.
    """
    # Keyed by the dedupe cell; values are complete rows. A dict rather
    # than a seen-set so a collision can KEEP THE STRONGER detection —
    # first-wins silently preferred whichever satellite file happened to
    # be fetched first. Rows are replaced whole, never field-merged: a
    # max-FRP from one overpass stapled to the timestamp of another would
    # describe a detection that never happened.
    cells: dict[tuple, list] = {}
    ok_feeds = 0
    deadline = time.monotonic() + HOTSPOT_BUDGET_S
    for i, (sat_dir, fname) in enumerate(FIRMS_FEEDS):
        left = deadline - time.monotonic()
        if left <= 1:
            print(f"  FIRMS budget of {HOTSPOT_BUDGET_S}s spent, skipping "
                  f"{len(FIRMS_FEEDS) - i} remaining feed(s)", file=sys.stderr)
            break
        url = f"{FIRMS_BASE}/{sat_dir}/csv/{fname}"
        try:
            # Spend at most what is left, so one hung feed cannot starve the
            # rest: two tries plus the 1 s backoff between them lands exactly
            # on the deadline. The 90 s cap is the per-attempt allowance a
            # healthy pull gets while budget remains; the 20 s floor keeps the
            # last feed a real attempt rather than a token one.
            per_try = max(20, min(90, int((left - 1) / 2)))
            raw = _get(url, tries=2, timeout=per_try)
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

            # Fire-channel brightness temperature, Kelvin. VIIRS calls it
            # bright_ti4 (I-4 band), MODIS just `brightness` (ch 21/22) —
            # both are the ~4 µm fire channel, comparable enough to ship
            # as one field.
            bt = 0
            for kbt in ("bright_ti4", "brightness"):
                v = r.get(kbt)
                if v:
                    try:
                        bt = int(round(float(v)))
                        break
                    except ValueError:
                        pass

            sat = SAT_CODES.get((r.get("satellite") or "").strip().upper(), -1)
            day = 1 if (r.get("daynight") or "").strip().upper() == "D" else 0

            # True pixel footprint, km. The nominal "375 m" is nadir-only:
            # at the swath edge a VIIRS pixel grows to ~0.8 km and a MODIS
            # one to ~4.5 km, and drawing both as equally precise dots is
            # an accuracy claim the instrument never made. Ship the worst
            # dimension so the card can be honest about it.
            try:
                fp = round(max(float(r.get("scan") or 0),
                               float(r.get("track") or 0)), 1)
            except (TypeError, ValueError):
                fp = 0.0

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
            cur = cells.get(key)
            if cur is not None and cur[2] >= frp:
                continue
            cells[key] = [round(lat, 4), round(lon, 4), round(frp, 1),
                          age_min, 1 if high else 0, bt, sat, day, fp]
            if cur is None:
                kept += 1
        ok_feeds += 1
        print(f"  FIRMS {fname}: {kept} kept")
    rows = list(cells.values())

    # Never publish an empty hotspot feed over a good one, the same rule
    # [main] already applies to incidents. Losing every feed is an outage on
    # our side of the wire, not a report that nothing is burning: there are
    # always thousands of detections nationwide. Raising here routes into
    # main()'s existing `pts = None` branch, which skips the write entirely
    # and leaves the last good file on B2 to age out under its own cache.
    #
    # This is the specific way the 2026-08-11 fix drew blood. Returning []
    # on a total FIRMS outage was always wrong, but unreachable while the
    # job was being SIGKILLed first; bounding the stage made the write block
    # reachable and published a 61-byte hotspots.json over a 577 KB one.
    # A partial result is still published on purpose, since one satellite
    # being down genuinely does mean fewer detections.
    if not ok_feeds or not rows:
        raise RuntimeError(
            f"no usable FIRMS feed ({ok_feeds}/{len(FIRMS_FEEDS)} answered, "
            f"{len(rows)} rows), refusing to publish an empty hotspot feed")

    if len(rows) > HOTSPOT_CAP:
        rows.sort(key=lambda x: -x[2])  # highest fire radiative power first
        print(f"  NOTE: capping hotspots {len(rows)} -> {HOTSPOT_CAP} "
              f"(kept highest FRP)")
        rows = rows[:HOTSPOT_CAP]
    return rows


# ── Incident ⇄ hotspot join ───────────────────────────────────────────

def _haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    rlat1, rlat2 = math.radians(lat1), math.radians(lat2)
    dlat = rlat2 - rlat1
    dlon = math.radians(lon2 - lon1)
    a = (math.sin(dlat / 2) ** 2
         + math.cos(rlat1) * math.cos(rlat2) * math.sin(dlon / 2) ** 2)
    return 6371.0 * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def attach_activity(fires: list[dict], pts: list[list]) -> None:
    """Stamp each fire with a summary of ITS OWN satellite detections.

    WFIGS and FIRMS are fetched in the same run and answer different
    questions — WFIGS says what the incident WAS at last human report,
    FIRMS says where heat is RIGHT NOW — so relating them here is free and
    is the one thing neither source offers on its own. The summary rides
    on current.json (`hs` per fire), which the app fetches even when the
    user never turns the ~900 KB hotspot payload on: the fire card gets
    live-activity rows at zero extra download.

    Each hotspot is assigned to the nearest incident whose radius covers
    it. The radius scales with acreage (a megafire's active edge can burn
    30+ km from the incident's point coordinate; a spot fire's cannot),
    floored at 3 km for point fires and capped at 60 km so a complex never
    absorbs a neighbouring incident's detections wholesale.

    Every fire gets an `hs` when this runs — {"n": 0} is a REAL statement
    ("satellites see no heat here, likely smoldering or out"), which is
    exactly what a user watching a 90%-contained fire wants to know. When
    the FIRMS stage failed entirely, main() never calls this and the key
    is absent, so the app can tell "no heat" from "no data".
    """
    tagged = [
        (f,
         min(60.0, max(3.0, 1.2 * math.sqrt(max(f["acres"], 0.0) * 0.00404686))),
         f["lat"], f["lon"])
        for f in fires
    ]
    stats: dict[int, dict] = {}
    for p in pts:
        plat, plon = p[0], p[1]
        clat = math.cos(math.radians(plat)) * 111.0
        best_i = -1
        best_km = 1e9
        for i, (_f, r, flat, flon) in enumerate(tagged):
            # Cheap rejects first: 20k pts x ~500 fires only stays fast if
            # the common case is two float compares, not a haversine.
            if abs(plat - flat) * 111.0 > r:
                continue
            if abs(plon - flon) * clat > r:
                continue
            km = _haversine_km(plat, plon, flat, flon)
            if km <= r and km < best_km:
                best_km = km
                best_i = i
        if best_i < 0:
            continue
        s = stats.setdefault(best_i, {"n": 0, "n6": 0, "pf": 0.0,
                                      "la": 10 ** 9, "sats": set()})
        s["n"] += 1
        if p[3] <= 360:
            s["n6"] += 1
        if p[2] > s["pf"]:
            s["pf"] = p[2]
        if p[3] < s["la"]:
            s["la"] = p[3]
        s["sats"].add(p[6])
    n_hot = 0
    for i, (f, _r, _lat, _lon) in enumerate(tagged):
        s = stats.get(i)
        if s is None:
            f["hs"] = {"n": 0}
        else:
            f["hs"] = {"n": s["n"], "n6": s["n6"], "pf": round(s["pf"], 1),
                       "la": s["la"], "ns": len(s["sats"])}
            n_hot += 1
    print(f"  {n_hot}/{len(fires)} incidents show satellite heat")


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

    if pts is not None:
        # Join AFTER the cap so the summary describes what actually ships.
        attach_activity(fires, pts)

    with tempfile.TemporaryDirectory() as td:
        tmp = Path(td)
        n = _write(tmp, "current.json", {
            "schemaVersion": SCHEMA_VERSION,
            "generated": stamp,
            # ISO duplicate of `generated`. The app reads the epoch int;
            # this exists for the freshness monitor's dead-man check, which
            # parses `generatedAt` as a date string across every manifest
            # in the pipeline. Cheap to carry both rather than teach the
            # monitor a second schema.
            "generatedAt": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
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
                # [lat, lon, frp, ageMinutes, highConfidence,
                #  brightnessK, satCode, isDay, footprintKm]
                # Append-only: shipped clients index 0-4 positionally.
                "pts": pts,
            }, CACHE_HOTSPOTS)
            print(f"  hotspots.json {n / 1024:.1f} KB")

    print("Done.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
