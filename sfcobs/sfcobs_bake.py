#!/usr/bin/env python3
"""sfcobs_bake.py — Surface Obs (non-airport mesonet) feed bake for Spotter Tools Pro.

The app's METARs layer (renamed "Surface Obs") shows AWC airport METARs. This
bake adds everything else that is legal to redistribute: RAWS fire-weather
stations, NWS HADS, state DOT / agricultural / university mesonets, and CWOP
citizen stations, from the NOAA MADIS PUBLIC feed. One compact gz JSON:

  sfcobs/v1/stations.json      every station, newest ob, server-side zoom rank.
                               Stored GZIPPED with Content-Encoding: gzip (real
                               gzip, so not the 2026-07 false-gzip trap): the
                               CDN hands gzip clients the 0.9 MB body as is and
                               decodes for the rest. Named .json, not .json.gz,
                               so the zone's "Cache JSON feeds" rule covers it
                               (edge caching + bypass-if-no-header); a .gz key
                               has no rule and is exposed to the 4 h edge pin.
  sfcobs/v1/manifest.json      generatedAt + counts (freshness monitor + app)

SOURCE
  https://madis-data.ncep.noaa.gov/madisPublic1/data/LDAD/mesonet/netCDF/
  YYYYMMDD_HH00.gz, anonymous HTTPS. One gz netCDF per UTC hour (~30 MB gz,
  ~450 MB raw); the current and previous hour are rewritten every few
  minutes. The sibling LDAD/hads/netCDF/ dir is EMPTY on the public server
  and LDAD/hydro/ carries river stage + precip only (no temperature), so
  HADS weather stations are taken from the mesonet file, where MADIS files
  them under dataProvider=HADS.

LICENSING (the reason the provider filter exists)
  Only MADIS restriction category 3 ("public, full distribution") providers
  ship. The public file is NOT pre-filtered to category 3: on 2026-10-16 it
  carried CAIC (cat 4) and CO_E-470 (cat 2) records. OK Mesonet (cat 2),
  West Texas Mesonet (cat 4, incl. the WTEXAS subprovider of SYNOPTIC_N) and
  NYS Mesonet (cat 4) are absent from the public file today, but the filter
  does not rely on that. Two layers:
    1. ALLOWED_PROVIDERS below: hand-checked against the live provider table
       (madis.ncep.noaa.gov/mesonet_providers.shtml, "Restriction (Weather)"
       column) on 2026-10-16. A provider not listed here never ships, so a
       NEW provider appearing in the feed needs a human to check it first.
    2. Every run re-reads that live table (cached 6 h) and intersects: a
       provider MADIS later moves off category 3 drops out automatically,
       with no code change. A provider whose name is absent from the live
       table is dropped too.
  Credit "NOAA MADIS" in the app; MADIS asks for no key or registration.

QC
  MADIS DD summary flags: Z none, C stage 1, S stages 1-2, V stages 1-3,
  K/k stage 4, G accept list; X failed stage 1, Q failed 2 or 3, B reject
  list. Any variable flagged X/Q/B is nulled. A station with no surviving
  temperature is dropped. Citizen (APRSWXNET) stations additionally need a
  temperature that PASSED stage 2 (S/V/K/G) — the app shows them only as an
  opt-in layer and the plan says QC-passed only.

AGE
  Non-airport obs older than 90 min are dropped (the app fades them past
  60). Airports (NonFedAWOS) keep up to 180 min, the app's METAR rule.

DEDUPE
  Stations that also publish an AWC METAR are dropped (the app already
  draws them from AWC's own feed), matched by id, including 3-letter ids
  against K-prefixed ICAOs.

RANK (server-side, so every client gets identical decluttering)
  The app's METAR rank pass: rank 0..3 = first station claiming each
  4/2/1/0.5-degree cell in stationId order, rank 4 = the rest, shown at
  zoom <3 / 3 / 5 / 7 / 8. This bake re-runs that pass over AWC METARs AND
  these stations together, in priority order airports (AWC first, then
  MADIS airports) > RAWS/gov > citizen. Levels 0-1 are AWC-only, so the
  zoomed-out view keeps exactly today's station count; mesonets fill empty
  1-degree cells from the regional view (rank 2, z5) down, a 0.25-degree
  level is rank 4 (z8), and rank 5 = every remaining station (z9). The AWC
  ranks themselves are NOT published: the app keeps computing its own, and
  since AWC sorts first here the two passes agree.

Env:
  R2_BUCKET             bucket (rclone remote "r2:" is configured by the caller)
  SFCOBS_CACHE_DIR      keeps unchanged hour files + the provider table
                        between runs (default: a temp dir, i.e. no cache)
  SFCOBS_NO_UPLOAD=1    bake + write locally only (SFCOBS_OUT_DIR, default .)
"""

from __future__ import annotations

import csv
import datetime as dt
import gzip
import hashlib
import html
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import netCDF4
import numpy as np

# ── Config ────────────────────────────────────────────────────────────

MADIS_URL = ("https://madis-data.ncep.noaa.gov/madisPublic1/data/LDAD/"
             "mesonet/netCDF/{stamp}.gz")
PROVIDERS_URL = "https://madis.ncep.noaa.gov/mesonet_providers.shtml"
AWC_METARS_URL = "https://aviationweather.gov/data/cache/metars.cache.csv.gz"
UA = "SpotterToolsPro-sfcobs/1.0 (models.dgwaynes.com; spottertoolspro@gmail.com)"

OUT_PREFIX = "sfcobs/v1"
SCHEMA_VERSION = 1
CACHE_DATA_S = 60        # stations.json: rebaked every 5 min
CACHE_MANIFEST_S = 30

# Same North America bbox the app applies to AWC METARs (app_constants.dart).
MIN_LAT, MAX_LAT, MIN_LON, MAX_LON = 15.0, 72.0, -170.0, -50.0

MAX_AGE_MIN = 90          # non-airport
MAX_AGE_AIRPORT_MIN = 180
PROVIDER_TABLE_TTL_S = 6 * 3600

# MADIS provider -> station type. a = airport, r = RAWS fire weather,
# g = government / mesonet, c = citizen (CWOP).
# Category 3 per the live provider table on 2026-10-16. Names are MADIS's
# `dataProvider` strings. Providers present in the public feed that are NOT
# here, and why: CAIC (cat 4), CO_E-470 (cat 2), NRCS / AJKNDBC / MQT_Meso
# (no row in the provider table, so their category cannot be verified).
ALLOWED_PROVIDERS: dict[str, str] = {
    "APRSWXNET": "c",
    "RAWS": "r",
    "NonFedAWOS": "a",
    "HADS": "g", "MesoWest": "g", "IEM": "g", "LCRA": "g", "DEOS": "g",
    "NJWxNet": "g", "CoAgMet": "g", "MOComAgNet": "g", "NC-ECONet": "g",
    "ITD": "g", "OHDOT": "g", "MADOT": "g", "AKDOT": "g", "DEDOT": "g",
    "VTDOT": "g", "NHDOT": "g", "MEDOT": "g", "PADOT": "g", "SDDOT": "g",
    "SCDOT": "g", "AZDOT": "g", "CADOT": "g", "ILDOT": "g", "KSDOT": "g",
    "NDDOT": "g", "VADOT": "g", "NOS-NWLON": "g", "NOS-PORTS": "g",
    "CA-Hydro": "g", "MAP": "g", "SFWMD": "g", "CRN": "g", "CRN2-al": "g",
    "HCN": "g", "ARLFRD": "g", "GoMOOS": "g", "FL-Meso": "g",
    "INTERNET": "g", "MISC": "g", "NEPP": "g", "HF-METAR": "a",
}

# Display names for the tap sheet. Anything not listed shows its MADIS name.
NETWORK_NAMES: dict[str, str] = {
    "APRSWXNET": "CWOP citizen weather",
    "RAWS": "RAWS fire weather",
    "NonFedAWOS": "Non-federal AWOS",
    "HADS": "NWS HADS",
    "MesoWest": "MesoWest cooperative mesonets",
    "IEM": "Iowa Environmental Mesonet",
    "LCRA": "Lower Colorado River Authority",
    "DEOS": "Delaware Environmental Observing System",
    "NJWxNet": "New Jersey Weather Network",
    "CoAgMet": "Colorado Ag Met",
    "MOComAgNet": "Missouri Ag Weather Network",
    "NC-ECONet": "NC ECONet",
    "ITD": "Idaho DOT", "OHDOT": "Ohio DOT", "MADOT": "Massachusetts DOT",
    "AKDOT": "Alaska DOT", "DEDOT": "Delaware DOT", "VTDOT": "Vermont DOT",
    "NHDOT": "New Hampshire DOT", "MEDOT": "Maine DOT", "PADOT": "Pennsylvania DOT",
    "SDDOT": "South Dakota DOT", "SCDOT": "South Carolina DOT", "AZDOT": "Arizona DOT",
    "CADOT": "Caltrans", "ILDOT": "Illinois DOT", "KSDOT": "Kansas DOT",
    "NDDOT": "North Dakota DOT", "VADOT": "Virginia DOT",
    "NOS-NWLON": "NOAA water level network", "NOS-PORTS": "NOAA PORTS",
    "CA-Hydro": "California hydro (NWS)", "MAP": "NOAA profiler sites",
    "SFWMD": "South Florida Water Mgmt", "CRN": "US Climate Reference Network",
}

# precipAccum period per provider, from the file's own Provider-Subprovider
# Table (code1PST). Values follow the file's attribute definitions.
PRECIP_PERIODS = {1: "1h", 3: "3h", 6: "6h", 12: "12h", 18: "18h",
                  -1: "local-midnight", -2: "utc-midnight", -3: "24h",
                  -4: "5m", -5: "15m"}

QC_BAD = {"X", "Q", "B"}
QC_PASSED_STAGE2 = {"S", "V", "K", "G"}

# Rank grid: levels 0..4, rank 5 = unclaimed.
CELL_DEG = [4.0, 2.0, 1.0, 0.5, 0.25]
FIRST_MESONET_LEVEL = 2
RANK_MIN_ZOOM = {0: 0, 1: 3, 2: 5, 3: 7, 4: 8, 5: 9}

MS_TO_KT = 1.0 / 0.514444

FIELDS = ["id", "name", "type", "net", "lat", "lon", "elev", "obs",
          "t", "td", "rh", "wd", "ws", "wg", "pr", "pp", "rank"]


def log(msg: str) -> None:
    print(msg, flush=True)


def http_get(url: str, timeout: int = 120, headers: dict | None = None):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    return urllib.request.urlopen(req, timeout=timeout)


# ── Provider category check ──────────────────────────────────────────

def live_cat3_providers(cache_dir: Path) -> set[str] | None:
    """Provider names MADIS currently lists as weather restriction 3.

    Cached for 6 h. On fetch failure the last good copy is used; with no
    copy at all, None (caller falls back to ALLOWED_PROVIDERS alone)."""
    path = cache_dir / "providers_cat3.json"
    if path.exists() and time.time() - path.stat().st_mtime < PROVIDER_TABLE_TTL_S:
        return set(json.loads(path.read_text()))
    try:
        text = http_get(PROVIDERS_URL, timeout=60).read().decode("utf-8", "replace")
        cat3 = set()
        for row in re.findall(r"(?s)<tr[^>]*>(.*?)</tr>", text):
            cells = [re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", c))).strip()
                     for c in re.findall(r"(?s)<t[dh][^>]*>(.*?)</t[dh]>", row)]
            # Provider rows: Data Provider | MADIS name | sites | active |
            # coverage | restriction (weather) | restriction (pavement)
            if len(cells) == 7 and cells[5] == "3":
                cat3.add(cells[1].replace("(subproviders)", "").strip())
        if len(cat3) < 20:
            raise RuntimeError(f"only {len(cat3)} cat-3 rows parsed")
        path.write_text(json.dumps(sorted(cat3)))
        return cat3
    except Exception as exc:  # noqa: BLE001 — fail to the last good table
        log(f"  provider table fetch failed ({exc}); using cached copy")
        if path.exists():
            return set(json.loads(path.read_text()))
        return None


# ── AWC METAR ids (dedupe + rank occupancy) ──────────────────────────

def fetch_awc_metars(now: float) -> list[tuple[str, float, float]]:
    raw = gzip.decompress(http_get(AWC_METARS_URL, timeout=60).read()).decode("utf-8", "replace")
    out = []
    for row in csv.DictReader(io.StringIO(raw)):
        try:
            sid = row["station_id"].strip()
            lat, lon = float(row["latitude"]), float(row["longitude"])
            obs = dt.datetime.fromisoformat(row["observation_time"].replace("Z", "+00:00")).timestamp()
        except (KeyError, ValueError, AttributeError):
            continue
        if not (MIN_LAT <= lat <= MAX_LAT and MIN_LON <= lon <= MAX_LON):
            continue
        if now - obs > MAX_AGE_AIRPORT_MIN * 60:
            continue
        out.append((sid, lat, lon))
    return out


# ── MADIS hour files ─────────────────────────────────────────────────

def _chars(var) -> np.ndarray:
    a = var[:]
    return np.char.strip(np.char.decode(a.view(f"S{a.shape[1]}").ravel(), "latin1"))


def _flags(var) -> np.ndarray:
    return np.char.decode(var[:].astype("S1"), "latin1")


def _num(d, name: str) -> np.ndarray:
    v = d.variables[name]
    a = np.asarray(v[:], dtype="float64")
    for attr in ("_FillValue", "missing_value"):
        if attr in v.ncattrs():
            a[a == float(getattr(v, attr))] = np.nan
    a[np.abs(a) > 1e30] = np.nan
    return a


def decode_hour(blob: bytes) -> dict:
    """The newest record per station from one hour file, as plain arrays."""
    d = netCDF4.Dataset("madis.nc", memory=gzip.decompress(blob))
    d.set_auto_mask(False)
    try:
        cols = {
            "id": _chars(d.variables["stationId"]),
            "name": _chars(d.variables["stationName"]),
            "prov": _chars(d.variables["dataProvider"]),
            "lat": _num(d, "latitude"), "lon": _num(d, "longitude"),
            "elev": _num(d, "elevation"), "obs": _num(d, "observationTime"),
            "t": _num(d, "temperature"), "td": _num(d, "dewpoint"),
            "rh": _num(d, "relHumidity"), "wd": _num(d, "windDir"),
            "ws": _num(d, "windSpeed"), "wg": _num(d, "windGust"),
            "pr": _num(d, "precipAccum"),
            "rdl": _num(d, "restrictedDataLevel"),
        }
        for v, key in (("temperatureDD", "t"), ("dewpointDD", "td"),
                       ("relHumidityDD", "rh"), ("windDirDD", "wd"),
                       ("windSpeedDD", "ws"), ("windGustDD", "wg"),
                       ("precipAccumDD", "pr")):
            cols[key + "DD"] = _flags(d.variables[v])
        n = int(d.variables["numPST"][:])
        pst = dict(zip(_chars(d.variables["namePST"])[:n],
                       zip(d.variables["code1PST"][:n].tolist(),
                           d.variables["code3PST"][:n].tolist())))
    finally:
        d.close()
    # Newest ob per station within this file.
    order = np.lexsort((-np.nan_to_num(cols["obs"], nan=-1e18), cols["id"]))
    ids = cols["id"][order]
    keep = order[np.r_[True, ids[1:] != ids[:-1]]]
    out = {k: v[keep] for k, v in cols.items()}
    out["pst"] = pst
    return out


def fetch_hour(stamp: str, cache_dir: Path) -> dict | None:
    """Download (conditionally) and decode one hour file.

    The decoded arrays are cached as .npz beside the file's Last-Modified, so
    the hour file from two hours ago — which MADIS no longer rewrites — costs
    one 304 per run instead of a 30 MB download and a decode."""
    meta_p = cache_dir / f"{stamp}.meta"
    npz_p = cache_dir / f"{stamp}.npz"
    headers = {}
    if meta_p.exists() and npz_p.exists():
        headers["If-Modified-Since"] = meta_p.read_text().strip()
    try:
        resp = http_get(MADIS_URL.format(stamp=stamp), timeout=180, headers=headers)
        blob = resp.read()
        lm = resp.headers.get("Last-Modified", "")
    except urllib.error.HTTPError as exc:
        if exc.code == 304:
            z = np.load(npz_p, allow_pickle=True)
            data = {k: z[k] for k in z.files if k != "pst"}
            data["pst"] = z["pst"].item()
            return data
        if exc.code == 404:
            return None
        raise
    t0 = time.time()
    data = decode_hour(blob)
    log(f"  {stamp}: {len(blob) / 1e6:.1f} MB gz, {len(data['id'])} stations, "
        f"decoded in {time.time() - t0:.1f}s")
    if lm:
        np.savez(npz_p, **{k: v for k, v in data.items() if k != "pst"},
                 pst=np.array(data["pst"], dtype=object))
        meta_p.write_text(lm)
    return data


def prune_cache(cache_dir: Path, keep: set[str]) -> None:
    for p in cache_dir.glob("2*.*"):
        if p.name.split(".")[0] not in keep:
            p.unlink(missing_ok=True)


# ── Build ────────────────────────────────────────────────────────────

def clean_name(name: str) -> str:
    name = re.sub(r"\s+", " ", name).strip()
    return re.sub(r"\s+US$", "", name)


def r1(x: float) -> float | None:
    return None if x is None or np.isnan(x) else round(float(x), 1)


def build(now: float, hours: list[dict], awc: list[tuple[str, float, float]],
          allowed: dict[str, str]) -> tuple[list[list], dict]:
    awc_ids = {s for s, _, _ in awc}
    stats = {"dropped_provider": {}, "dropped_qc_temp": 0, "dropped_age": 0,
             "dropped_awc_dupe": 0, "dropped_citizen_qc": 0, "dropped_restricted": 0}
    # Newest ob per station across the hour files, vectorised.
    pst: dict = {}
    for h in hours:
        pst.update(h["pst"])
    keys = [k for k in hours[0] if k != "pst"]
    cat = {k: np.concatenate([h[k] for h in hours]) for k in keys}
    valid = (cat["id"] != "") & ~np.isnan(cat["obs"])
    cat = {k: v[valid] for k, v in cat.items()}
    order = np.lexsort((-cat["obs"], cat["id"]))
    ids = cat["id"][order]
    keep = order[np.r_[True, ids[1:] != ids[:-1]]]
    cat = {k: v[keep] for k, v in cat.items()}

    rows = []
    for i in range(len(cat["id"])):
        s = {k: v[i] for k, v in cat.items()}
        sid = str(s["id"])
        prov = str(s["prov"])
        s["_pst"] = pst.get(prov)
        stype = allowed.get(prov)
        if stype is None:
            stats["dropped_provider"][prov] = stats["dropped_provider"].get(prov, 0) + 1
            continue
        # Defensive: MADIS marks restricted records per ob. All -32767 today.
        if not np.isnan(s["rdl"]) and s["rdl"] >= 2:
            stats["dropped_restricted"] += 1
            continue
        lat, lon = float(s["lat"]), float(s["lon"])
        if np.isnan(lat) or np.isnan(lon) or (lat == 0 and lon == 0):
            continue
        if not (MIN_LAT <= lat <= MAX_LAT and MIN_LON <= lon <= MAX_LON):
            continue
        age_min = (now - float(s["obs"])) / 60
        if age_min > (MAX_AGE_AIRPORT_MIN if stype == "a" else MAX_AGE_MIN) or age_min < -10:
            stats["dropped_age"] += 1
            continue
        if sid in awc_ids or (len(sid) == 3 and "K" + sid in awc_ids):
            stats["dropped_awc_dupe"] += 1
            continue

        def ok(key: str) -> bool:
            return not np.isnan(s[key]) and s[key + "DD"] not in QC_BAD

        t = s["t"] - 273.15 if ok("t") and 200 < s["t"] < 340 else np.nan
        if np.isnan(t):
            stats["dropped_qc_temp"] += 1
            continue
        if stype == "c" and s["tDD"] not in QC_PASSED_STAGE2:
            stats["dropped_citizen_qc"] += 1
            continue
        td = s["td"] - 273.15 if ok("td") and 180 < s["td"] < 320 else np.nan
        if not np.isnan(td) and td > t + 0.5:
            td = np.nan
        rh = float(s["rh"]) if ok("rh") and 0 <= s["rh"] <= 105 else np.nan
        ws = s["ws"] * MS_TO_KT if ok("ws") and 0 <= s["ws"] < 100 else np.nan
        wd = float(s["wd"]) if ok("wd") and 0 <= s["wd"] <= 360 else np.nan
        wg = s["wg"] * MS_TO_KT if ok("wg") and 0 <= s["wg"] < 120 else np.nan
        if not np.isnan(wg) and not np.isnan(ws) and wg < ws:
            wg = np.nan
        pr, pp = None, None
        code = s["_pst"]
        if ok("pr") and code is not None and code[1] == 0 and code[0] in PRECIP_PERIODS \
                and 0 <= s["pr"] < 2000:
            pr, pp = round(float(s["pr"]), 1), PRECIP_PERIODS[code[0]]
        elev = None if np.isnan(s["elev"]) or abs(s["elev"]) > 9000 else int(round(float(s["elev"])))
        rows.append({
            "id": sid, "name": clean_name(str(s["name"])), "type": stype, "net": prov,
            "lat": round(lat, 4), "lon": round(lon, 4), "elev": elev,
            "obs": int(s["obs"]), "t": r1(t), "td": r1(td),
            "rh": None if np.isnan(rh) else int(round(min(rh, 100.0))),
            "wd": None if np.isnan(wd) else int(round(wd)) % 360,
            "ws": None if np.isnan(ws) else int(round(ws)),
            "wg": None if np.isnan(wg) else int(round(wg)),
            "pr": pr, "pp": pp,
        })
    assign_ranks(rows, awc)
    return rows, stats


def assign_ranks(rows: list[dict], awc: list[tuple[str, float, float]]) -> None:
    prio = {"a": 1, "r": 2, "g": 2, "c": 3}
    # (priority, id, lat, lon, row or None for AWC)
    sweep = [(0, sid, lat, lon, None) for sid, lat, lon in awc]
    sweep += [(prio[r["type"]], r["id"], r["lat"], r["lon"], r) for r in rows]
    sweep.sort(key=lambda e: (e[0], e[1]))
    ranked: set[int] = set()
    for level, cell in enumerate(CELL_DEG):
        used: set[tuple[int, int]] = set()
        for idx, (p, _sid, lat, lon, row) in enumerate(sweep):
            if idx in ranked or (level < FIRST_MESONET_LEVEL and p > 0):
                continue
            key = (int((lat + 90) // cell), int((lon + 180) // cell))
            if key not in used:
                used.add(key)
                ranked.add(idx)
                if row is not None:
                    row["rank"] = level
    for r in rows:
        r.setdefault("rank", len(CELL_DEG))


# ── Publish ──────────────────────────────────────────────────────────

def rclone_upload(local: Path, key: str, cache_s: int, content_type: str,
                  gzipped: bool = False) -> None:
    bucket = os.environ["R2_BUCKET"]
    cmd = [
        "rclone", "copyto", str(local), f"r2:{bucket}/{key}",
        "--s3-no-check-bucket", "--no-traverse",
        # Header-less objects get BYPASSed by the zone's cache rule and every
        # request falls through to B2 (freshness_monitor.yml asserts this).
        "--header-upload", f"Cache-Control: public, max-age={cache_s}",
        "--header-upload", f"Content-Type: {content_type}",
    ]
    if gzipped:
        cmd += ["--header-upload", "Content-Encoding: gzip"]
    subprocess.check_call(cmd)


def main() -> int:
    t_start = time.time()
    now = time.time()
    now_dt = dt.datetime.fromtimestamp(now, dt.timezone.utc)
    cache_root = os.environ.get("SFCOBS_CACHE_DIR")
    tmp_ctx = tempfile.TemporaryDirectory() if not cache_root else None
    cache_dir = Path(cache_root or tmp_ctx.name)
    cache_dir.mkdir(parents=True, exist_ok=True)

    live = live_cat3_providers(cache_dir)
    if live is None:
        log("  WARNING: no provider table available; using the hand-checked list alone")
        allowed = dict(ALLOWED_PROVIDERS)
    else:
        allowed = {p: t for p, t in ALLOWED_PROVIDERS.items() if p in live}
        gone = sorted(set(ALLOWED_PROVIDERS) - set(allowed))
        if gone:
            log(f"  not category 3 in today's MADIS table, dropped: {', '.join(gone)}")

    # Hours covering the 180-min airport window would mean four ~30 MB files;
    # the 90-min window covers everything but a few late airport obs, which
    # the app's AWC feed mostly duplicates anyway.
    first = now_dt - dt.timedelta(minutes=MAX_AGE_MIN)
    stamps = []
    h = first.replace(minute=0, second=0, microsecond=0)
    while h <= now_dt:
        stamps.append(h.strftime("%Y%m%d_%H00"))
        h += dt.timedelta(hours=1)
    log(f"MADIS hours: {', '.join(stamps)}")
    hours = []
    for st in stamps:
        data = fetch_hour(st, cache_dir)
        if data is None:
            log(f"  {st}: not published yet")
        else:
            hours.append(data)
    prune_cache(cache_dir, set(stamps))
    if not hours:
        log("ERROR: no MADIS hour file available — refusing to publish")
        return 1

    awc = fetch_awc_metars(now)
    log(f"AWC METARs in bbox: {len(awc)}")
    if len(awc) < 1000:
        log("ERROR: AWC METAR list looks truncated — refusing to publish")
        return 1

    rows, stats = build(now, hours, awc, allowed)
    by_type = {t: sum(1 for r in rows if r["type"] == t) for t in "argc"}
    by_rank = {k: sum(1 for r in rows if r["rank"] == k) for k in range(len(CELL_DEG) + 1)}
    log(f"stations: {len(rows)}  by type {by_type}  by rank {by_rank}")
    log(f"dropped: { {k: v for k, v in stats.items() if k != 'dropped_provider'} }")
    log(f"dropped by provider: {stats['dropped_provider']}")
    if len(rows) < 5000:
        # A normal hour is ~30k. Never replace a good feed with a gutted one.
        log("ERROR: fewer than 5000 stations — refusing to publish")
        return 1

    rows.sort(key=lambda r: (r["rank"], r["id"]))
    nets = sorted({r["net"] for r in rows})
    gen_iso = now_dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    payload = {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAt": gen_iso,
        "source": "NOAA MADIS (public, restriction category 3)",
        "count": len(rows),
        "units": {"t": "C", "td": "C", "rh": "%", "wd": "deg from", "ws": "kt",
                  "wg": "kt", "pr": "mm", "elev": "m", "obs": "unix s"},
        "types": {"a": "Airport", "r": "Fire weather (RAWS)",
                  "g": "Gov & mesonet", "c": "Citizen (CWOP)"},
        "rankMinZoom": {str(k): v for k, v in RANK_MIN_ZOOM.items()},
        "networks": {n: NETWORK_NAMES.get(n, n) for n in nets},
        "fields": FIELDS,
        "rows": [[r[f] for f in FIELDS] for r in rows],
    }
    body = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    gz = gzip.compress(body, compresslevel=9, mtime=0)
    manifest = {
        "schemaVersion": SCHEMA_VERSION,
        "generatedAt": gen_iso,
        "file": "stations.json",
        "contentEncoding": "gzip",
        "count": len(rows),
        "byType": by_type,
        "bytes": len(gz),
        "rawBytes": len(body),
        "sha1": hashlib.sha1(gz).hexdigest(),
        "madisHours": stamps,
        "newestObs": dt.datetime.fromtimestamp(max(r["obs"] for r in rows), dt.timezone.utc)
                     .strftime("%Y-%m-%dT%H:%M:%SZ"),
        "host": os.environ.get("SFCOBS_HOST", os.uname().nodename if hasattr(os, "uname") else "?"),
    }
    log(f"stations.json {len(gz) / 1024:.0f} KB gzipped (raw {len(body) / 1024:.0f} KB)")

    out_dir = Path(os.environ.get("SFCOBS_OUT_DIR", cache_dir / "out"))
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "stations.json.gz").write_bytes(gz)
    (out_dir / "manifest.json").write_text(json.dumps(manifest, separators=(",", ":")))
    if os.environ.get("SFCOBS_NO_UPLOAD") == "1":
        log(f"SFCOBS_NO_UPLOAD=1: wrote {out_dir}, skipping upload")
    else:
        # Data first, manifest second: a manifest never points at an older file.
        rclone_upload(out_dir / "stations.json.gz", f"{OUT_PREFIX}/stations.json",
                      CACHE_DATA_S, "application/json", gzipped=True)
        rclone_upload(out_dir / "manifest.json", f"{OUT_PREFIX}/manifest.json",
                      CACHE_MANIFEST_S, "application/json")
    log(f"done in {time.time() - t_start:.1f}s")
    if tmp_ctx:
        tmp_ctx.cleanup()
    return 0


if __name__ == "__main__":
    sys.exit(main())
