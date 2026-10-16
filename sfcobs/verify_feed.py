#!/usr/bin/env python3
"""verify_feed.py — audit the LIVE Surface Obs feed (stdlib only).

  python3 sfcobs/verify_feed.py [STATION_ID ...]

Fetches models.dgwaynes.com/sfcobs/v1/{manifest.json,stations.json.gz} and
the MADIS public station table, then checks:
  * freshness: manifest generatedAt age and the newest ob in the file
  * licensing: every station's provider is in the bake's allow list, and no
    station id is tagged in the MADIS station table with a restricted
    network (West Texas Mesonet = WTEXAS, the other SYNOPTIC_N category-4
    subproviders) or carries a restricted provider (OK-Meso, WT-Meso, NYSM)
  * the requested stations: present, with their current values
Exit 1 when a licensing check fails.
"""

from __future__ import annotations

import collections
import datetime as dt
import gzip
import json
import sys
import time
import urllib.request

BASE = "https://models.dgwaynes.com/sfcobs/v1"
STNTBL = "https://madis-data.ncep.noaa.gov/madisPublic1/data/stations/stntbl.txt"
RESTRICTED_PROVIDERS = {"OK-Meso", "WT-Meso", "NYSM", "SYNOPTIC_N", "CAIC", "CO_E-470",
                        "AWS", "UrbaNet", "GPSMET", "KYMN", "NDAWN", "NE-MESO",
                        "SDSMeso", "WSAgMet", "WxFlow", "PSU-PATC", "NYDOT"}
RESTRICTED_TAGS = {"WTEXAS", "DAVIS", "APS", "ISWS", "KSU-MESO"}


def get(url: str) -> bytes:
    req = urllib.request.Request(f"{url}?_v={int(time.time())}",
                                 headers={"User-Agent": "sfcobs-verify", "Cache-Control": "no-cache"})
    return urllib.request.urlopen(req, timeout=120).read()


def main() -> int:
    man = json.loads(get(f"{BASE}/manifest.json"))
    gz = get(f"{BASE}/stations.json.gz")
    data = json.loads(gzip.decompress(gz))
    gen = dt.datetime.fromisoformat(man["generatedAt"].replace("Z", "+00:00"))
    age = (dt.datetime.now(dt.timezone.utc) - gen).total_seconds() / 60
    print(f"manifest generatedAt {man['generatedAt']} ({age:.1f} min old), host {man.get('host')}")
    print(f"stations.json.gz {len(gz):,} bytes ({len(gz) / 1024:.0f} KB), "
          f"raw {man.get('rawBytes', 0):,} bytes, {data['count']:,} stations, newest ob {man.get('newestObs')}")
    rows = [dict(zip(data["fields"], r)) for r in data["rows"]]
    print("by type:", dict(collections.Counter(r["type"] for r in rows)))
    print("by rank:", dict(sorted(collections.Counter(r["rank"] for r in rows).items())))

    fail = False
    provs = collections.Counter(r["net"] for r in rows)
    bad_p = {p: n for p, n in provs.items() if p in RESTRICTED_PROVIDERS}
    print(f"providers ({len(provs)}):", ", ".join(f"{p} {n}" for p, n in provs.most_common()))
    print("restricted providers present:", bad_p or "none")
    fail |= bool(bad_p)

    tags = collections.defaultdict(set)
    for line in get(STNTBL).decode("latin1").splitlines():
        p = line.split("|")
        if len(p) > 7:
            tags[p[0].strip()].add(p[6].strip())
    hits = sorted((r["id"], sorted(tags[r["id"]] & RESTRICTED_TAGS)) for r in rows
                  if tags[r["id"]] & RESTRICTED_TAGS)
    print(f"ids tagged with a restricted network in the MADIS station table: {len(hits)}", hits[:10])
    fail |= bool(hits)

    want = [a.upper() for a in sys.argv[1:]]
    by_id = {r["id"]: r for r in rows}
    now = time.time()
    for sid in want:
        r = by_id.get(sid)
        if r is None:
            why = sorted(tags.get(sid, set()) - {""})
            print(f"  {sid}: ABSENT (station-table network tags: {why or 'none'})")
            continue
        print(f"  {sid}: {r['name']} [{r['net']}] ob {(now - r['obs']) / 60:.0f} min ago  "
              f"T {r['t']}C Td {r['td']}C RH {r['rh']}% wind {r['wd']}/{r['ws']}kt G{r['wg']} "
              f"precip {r['pr']} ({r['pp']}) rank {r['rank']}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
