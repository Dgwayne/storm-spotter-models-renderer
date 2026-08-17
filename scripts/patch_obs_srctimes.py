#!/usr/bin/env python3
"""Patch srcTimes into the live OBS manifest without rebuilding it.

The fast severe tick (render_mrms_severe.yml, every 5 min) re-renders six
products and needs the manifest to advertise their new valid times, or the
app paints fresh pixels under a stale timestamp. Running the full
build_manifest.py would be correct but expensive: it lists the whole
v1/OBS/ prefix (~7,000 objects = 8 LIST pages), and doing that twelve times
an hour is what tipped this model past B2's free Class C allowance. The
15-minute sweep still does the full authoritative rebuild; this only moves
the timestamps in between.

Cost shape, which is the whole point:
  * the manifest is READ over the public CDN, so Cloudflare serves it and
    B2 never sees the request;
  * the patched copy is written back with rclone — a Class A write, which
    B2 does not charge for;
  * no LIST calls at all.

Concurrency: the full sweep rebuilds this same file. The two are scheduled
onto disjoint minutes, and this re-fetches immediately before writing, so
the window where a rebuild could be overwritten is seconds wide — and a
rebuild fifteen minutes later repairs anything lost regardless. Only
srcTimes entries are touched; every other key is passed through untouched.

Usage:  patch_obs_srctimes.py <run_stamp> <code>=<HHMMSS> [<code>=<HHMMSS> ...]

Env:    R2_BUCKET, plus rclone's r2: remote (configured by the workflow),
        MODELS_BASE_URL (optional; defaults to the public CDN origin),
        OBS_PREFIX (optional; which prefix's manifest to patch — a shadow
        run must patch its own, never the live one),
        OBS_PATCH_CREATE_RUNS (optional; also publish a not-yet-seen run and
        claim frames into `available`, closing the hourly-rollover hole).
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

MODEL = "OBS"
# Read and write the same prefix the frames went to (see r2_listing.prefix_for).
# A shadow run must patch its own manifest, never the live one.
PREFIX = os.environ.get("OBS_PREFIX") or MODEL
# Opt-in: also publish a run the manifest has not seen yet, and claim frames
# into `available`. Off by default so the GitHub severe workflow — the
# rollback target until the VPS cutover has held — keeps behaving exactly as
# it does today. See the CREATE_RUNS branch in main() for what it fixes.
CREATE_RUNS = bool(os.environ.get("OBS_PATCH_CREATE_RUNS"))
BASE = os.environ.get("MODELS_BASE_URL", "https://models.dgwaynes.com/v1")
BUCKET = os.environ["R2_BUCKET"]


def fetch_manifest() -> dict | None:
    """Newest manifest from the CDN, cache-busted.

    Cache-busting matters twice over: the edge holds manifest.json for 60 s,
    and patching a copy that predates the last sweep would hand back a stale
    `available` map along with the fresh timestamps.
    """
    url = f"{BASE}/{PREFIX}/manifest.json?_patch={int(time.time())}"
    req = urllib.request.Request(
        url,
        headers={
            "Cache-Control": "no-cache",
            # models.dgwaynes.com 403s urllib's default UA.
            "User-Agent": "stp-renderer-patch/1.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            return json.load(r)
    except Exception as e:  # noqa: BLE001 — any failure means "skip quietly"
        print(f"  manifest fetch failed ({e}); leaving it to the next sweep")
        return None


def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    run_stamp = sys.argv[1]
    updates: dict[str, str] = {}
    for arg in sys.argv[2:]:
        code, _, hhmmss = arg.partition("=")
        if code and len(hhmmss) == 6 and hhmmss.isdigit():
            updates[code] = hhmmss
    if not updates:
        print("  nothing to patch")
        return 0

    manifest = fetch_manifest()
    if manifest is None:
        return 0  # non-fatal: the render already succeeded

    runs = manifest.get("runs")
    if not isinstance(runs, list) or not runs:
        print("  manifest has no runs; skipping patch")
        return 0

    target = next(
        (r for r in runs if isinstance(r, dict) and r.get("runStamp") == run_stamp),
        None,
    )
    if target is None:
        if not CREATE_RUNS:
            # Historical behaviour: only ever touch a run the sweep already
            # published, because a run with no `expected` map behind it would
            # make the app paint the scrub bar as permanently incomplete.
            print(f"  run {run_stamp} not in manifest yet; the sweep will publish it")
            return 0
        # The hourly-rollover hole. Products render into an hourly slot, so at
        # the top of every hour the first tick writes frames into a run stamp
        # the manifest has never heard of — and refusing to publish it means
        # the app keeps showing the PREVIOUS hour until the next full sweep.
        # At a 20-minute sweep that is up to 20 minutes of staleness handed
        # back on every hour boundary, which would eat most of what the
        # 2-minute tier just bought.
        #
        # `expected` is copied from the newest existing run rather than
        # recomputed: OBS has only forecast_hours_default (no synoptic
        # variation), so every run's expected map is identical, and copying
        # keeps the one definition in build_manifest.py. Products this tick
        # did not render simply stay absent from `available`, and the app
        # walks runs newest-first looking for one that actually has the
        # product — so a partially-populated new run degrades to exactly the
        # right answer per product. The next sweep replaces all of it.
        newest = runs[-1]
        target = {
            "runTime": dt.datetime.strptime(run_stamp, "%Y%m%d%H")
            .replace(tzinfo=dt.timezone.utc)
            .isoformat(),
            "runStamp": run_stamp,
            "available": {},
            "expected": dict(newest.get("expected") or {}),
            "srcTimes": {},
        }
        runs.append(target)
        print(f"  run {run_stamp} is new; added to the manifest")

    # A product that just rendered into this run has its frame on the bucket
    # NOW. Recording that here (not only at the next sweep) is what lets the
    # app pick up the new hour immediately — `expected` for OBS is [0], and
    # the app requires available to contain expected.first before it will use
    # a run.
    available = target.get("available")
    if not isinstance(available, dict):
        available = {}
    expected_map = target.get("expected") or {}

    src_times = target.get("srcTimes")
    if not isinstance(src_times, dict):
        src_times = {}
    patched = []
    for code, hhmmss in updates.items():
        # The marker's hour always equals the run's, so the run carries the
        # date (same derivation build_manifest.py uses).
        iso = (
            dt.datetime.strptime(run_stamp[:8] + hhmmss, "%Y%m%d%H%M%S")
            .replace(tzinfo=dt.timezone.utc)
            .isoformat()
        )
        if src_times.get(code) != iso:
            src_times[code] = iso
            patched.append(f"{code}@{hhmmss}")
        if CREATE_RUNS:
            # Frame is uploaded by the time this runs, so claim it. Use the
            # product's own expected list rather than a hardcoded [0] so this
            # stays correct if OBS ever grows a multi-hour product.
            want = (expected_map.get(code) or [0])[:1]
            have = available.get(code) or []
            if want and want[0] not in have:
                available[code] = sorted({*have, want[0]})
    if not patched:
        print("  srcTimes already current; no write")
        return 0
    target["srcTimes"] = src_times
    if CREATE_RUNS:
        target["available"] = available
        # The app reads runs newest-last. Appending is already in order for
        # the only case that produces a new run (the hour rolling forward),
        # but sorting costs nothing and keeps a late-publishing product from
        # putting the list out of order.
        runs.sort(key=lambda r: r.get("runStamp") or "")
    # generatedAt is what the dead-man monitor reads; a fast tick really did
    # regenerate this file, and leaving the old value would make a healthy
    # pipeline look stalled between sweeps.
    manifest["generatedAt"] = dt.datetime.now(dt.timezone.utc).isoformat()

    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "manifest.json"
        out.write_text(json.dumps(manifest, indent=2))
        subprocess.check_call(
            [
                "rclone",
                "copyto",
                str(out),
                f"r2:{BUCKET}/v1/{PREFIX}/manifest.json",
                "--s3-no-check-bucket",
                "--no-traverse",
                "--header-upload",
                "Cache-Control: public, max-age=60",
            ]
        )
    print(f"  manifest srcTimes patched: {', '.join(patched)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
