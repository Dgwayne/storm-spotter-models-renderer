#!/usr/bin/env python3
"""Shadow vs live vs NOAA, sampled over time.

The question the cutover turns on is not "did the timer fire" — it is
whether the number moved. For each watched product this reads three
things:

  * NOAA's newest published key       — the floor nobody can beat
  * v1/OBS/manifest.json srcTimes     — what the app is showing today
  * v1/OBS-shadow/manifest.json       — what the box would be showing

and reports each as an age in minutes. Both manifests are read over the
CDN, so this costs B2 nothing and can be run as often as you like.

  compare_freshness.py                    # one sample, printed
  compare_freshness.py --watch 65 --every 5   # sample for 65 min, append CSV
  compare_freshness.py --summarize            # read the CSV back

Gotchas this deliberately works around:
  * models.dgwaynes.com 403s urllib's default User-Agent.
  * A bare CDN URL reports the zone rule's Cache-Control (4 h), not the
    origin's, so every request here is cache-busted.
  * A product's newest srcTime can sit in an earlier run than the last one
    right after the hour rolls, so every run is scanned, not just runs[-1].
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import statistics
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor

UA = "stp-freshness/1.0"
BASE = os.environ.get("MODELS_BASE_URL", "https://models.dgwaynes.com/v1")
CSV_PATH = os.environ.get("FRESHNESS_CSV", "/var/lib/stp-renderer/freshness.csv")
CONFIG = os.environ.get(
    "PRODUCTS_YML", "/opt/stp-renderer/config/products.yml"
)

# A handful, chosen to span the tiers and the failure modes: the four echo
# tops are the product the whole exercise was measured on, rotnow/mesh/rala
# are the six that already run fast on GitHub (so they should show the
# SMALLEST gap), and vil/posh/shi/rate are fast-at-source products that
# today ride the starved 15-minute sweep (so they should show the largest).
WATCH = ["et18", "et30", "rotnow", "rotml30", "mesh", "rala", "vil", "posh", "shi", "rate"]


def _get(url: str, timeout: int = 45) -> bytes:
    sep = "&" if "?" in url else "?"
    req = urllib.request.Request(
        f"{url}{sep}_cb={int(time.time() * 1000)}",
        headers={"User-Agent": UA, "Cache-Control": "no-cache"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def manifest_src_times(prefix: str) -> dict[str, dt.datetime]:
    """{code: newest srcTime} across every run in a manifest."""
    try:
        m = json.loads(_get(f"{BASE}/{prefix}/manifest.json"))
    except Exception as e:  # noqa: BLE001
        print(f"  ! {prefix} manifest unreadable: {e}", file=sys.stderr)
        return {}
    out: dict[str, dt.datetime] = {}
    for run in m.get("runs") or []:
        for code, iso in (run.get("srcTimes") or {}).items():
            t = dt.datetime.fromisoformat(iso)
            if code not in out or t > out[code]:
                out[code] = t
    return out


def mrms_dirs() -> dict[str, str]:
    """{code: mrms_dir} — read with a regex so this needs no pyyaml."""
    out: dict[str, str] = {}
    code = None
    with open(CONFIG, encoding="utf-8") as f:
        for line in f:
            m = re.match(r"^  ([a-z0-9_]+):\s*$", line)
            if m:
                code = m.group(1)
                continue
            m = re.match(r'^    mrms_dir:\s*"([^"]+)"', line)
            if m and code:
                out[code] = m.group(1)
                code = None
    return out


def noaa_newest(mrms_dir: str) -> dt.datetime | None:
    now = dt.datetime.now(dt.timezone.utc)
    for day in (now, now - dt.timedelta(days=1)):
        url = (
            "https://noaa-mrms-pds.s3.amazonaws.com/?list-type=2"
            f"&prefix=CONUS/{mrms_dir}/{day:%Y%m%d}/&max-keys=1000"
        )
        try:
            body = _get(url).decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            continue
        keys = [k for k in re.findall(r"<Key>([^<]+)</Key>", body)
                if k.endswith(".grib2.gz")]
        if not keys:
            continue
        m = re.search(r"(\d{8})-(\d{6})", keys[-1].rsplit("/", 1)[-1])
        if m:
            return dt.datetime.strptime(
                m.group(1) + m.group(2), "%Y%m%d%H%M%S"
            ).replace(tzinfo=dt.timezone.utc)
    return None


def sample(live_prefix: str, shadow_prefix: str) -> list[dict]:
    now = dt.datetime.now(dt.timezone.utc)
    dirs = mrms_dirs()
    with ThreadPoolExecutor(max_workers=8) as ex:
        f_live = ex.submit(manifest_src_times, live_prefix)
        f_shadow = ex.submit(manifest_src_times, shadow_prefix)
        f_noaa = {c: ex.submit(noaa_newest, dirs[c]) for c in WATCH if c in dirs}
        live, shadow = f_live.result(), f_shadow.result()
        noaa = {c: f.result() for c, f in f_noaa.items()}

    rows = []
    for code in WATCH:
        def age(t):
            return None if t is None else round((now - t).total_seconds() / 60, 1)
        rows.append({
            "t": now.replace(microsecond=0).isoformat(),
            "code": code,
            "noaa_age": age(noaa.get(code)),
            "live_age": age(live.get(code)),
            "shadow_age": age(shadow.get(code)),
        })
    return rows


def fmt(v) -> str:
    return "  --  " if v is None else f"{v:6.1f}"


def print_table(rows: list[dict]) -> None:
    print(f"\n{rows[0]['t']}   ages in minutes")
    print(f"  {'product':<9} {'NOAA':>6} {'live':>6} {'shadow':>6}   {'live lag':>8} {'shadow lag':>10}")
    print(f"  {'-'*9} {'-'*6} {'-'*6} {'-'*6}   {'-'*8} {'-'*10}")
    for r in rows:
        # Lag = how far behind the floor, which is the only number that is
        # ours to fix. Absolute age also moves with where we are in the
        # source's own publish cycle.
        ll = (None if r["live_age"] is None or r["noaa_age"] is None
              else round(r["live_age"] - r["noaa_age"], 1))
        sl = (None if r["shadow_age"] is None or r["noaa_age"] is None
              else round(r["shadow_age"] - r["noaa_age"], 1))
        print(f"  {r['code']:<9} {fmt(r['noaa_age'])} {fmt(r['live_age'])} "
              f"{fmt(r['shadow_age'])}   {fmt(ll)} {fmt(sl)}")


def append_csv(rows: list[dict]) -> None:
    new = not os.path.exists(CSV_PATH)
    os.makedirs(os.path.dirname(CSV_PATH), exist_ok=True)
    with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["t", "code", "noaa_age", "live_age", "shadow_age"])
        if new:
            w.writeheader()
        w.writerows(rows)


def summarize() -> None:
    with open(CSV_PATH, encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        print("no samples yet")
        return
    ts = sorted({r["t"] for r in rows})
    span = (dt.datetime.fromisoformat(ts[-1]) - dt.datetime.fromisoformat(ts[0]))
    print(f"{len(ts)} samples over {span} ({ts[0]} .. {ts[-1]})\n")
    print(f"  {'product':<9} {'live median':>11} {'live p90':>9} "
          f"{'shadow median':>14} {'shadow p90':>11}")
    print(f"  {'-'*9} {'-'*11} {'-'*9} {'-'*14} {'-'*11}")

    def pct(vals, p):
        if not vals:
            return None
        s = sorted(vals)
        return s[min(len(s) - 1, int(round((p / 100) * (len(s) - 1))))]

    for code in WATCH:
        sub = [r for r in rows if r["code"] == code]
        lv = [float(r["live_age"]) for r in sub if r["live_age"]]
        sv = [float(r["shadow_age"]) for r in sub if r["shadow_age"]]
        print(f"  {code:<9} {fmt(statistics.median(lv) if lv else None)}      "
              f"{fmt(pct(lv, 90))}    {fmt(statistics.median(sv) if sv else None)}        "
              f"{fmt(pct(sv, 90))}")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--live", default="OBS")
    p.add_argument("--shadow", default="OBS-shadow")
    p.add_argument("--watch", type=int, metavar="MIN",
                   help="keep sampling for this many minutes, appending to CSV")
    p.add_argument("--every", type=int, default=5, metavar="MIN")
    p.add_argument("--summarize", action="store_true")
    args = p.parse_args()

    if args.summarize:
        summarize()
        return 0

    if not args.watch:
        print_table(sample(args.live, args.shadow))
        return 0

    deadline = time.time() + args.watch * 60
    while True:
        rows = sample(args.live, args.shadow)
        append_csv(rows)
        print_table(rows)
        if time.time() >= deadline:
            break
        time.sleep(args.every * 60)
    print()
    summarize()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
