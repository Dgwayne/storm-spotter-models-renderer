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
        MODELS_BASE_URL (optional; defaults to the public CDN origin).
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
BASE = os.environ.get("MODELS_BASE_URL", "https://models.dgwaynes.com/v1")
BUCKET = os.environ["R2_BUCKET"]


def fetch_manifest() -> dict | None:
    """Newest manifest from the CDN, cache-busted.

    Cache-busting matters twice over: the edge holds manifest.json for 60 s,
    and patching a copy that predates the last sweep would hand back a stale
    `available` map along with the fresh timestamps.
    """
    url = f"{BASE}/{MODEL}/manifest.json?_patch={int(time.time())}"
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

    # Only ever touch a run the sweep already published. Inventing one here
    # would put a run in the manifest with no `expected` map behind it, and
    # the app would paint the scrub bar as permanently incomplete.
    target = next(
        (r for r in runs if isinstance(r, dict) and r.get("runStamp") == run_stamp),
        None,
    )
    if target is None:
        print(f"  run {run_stamp} not in manifest yet; the sweep will publish it")
        return 0

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
    if not patched:
        print("  srcTimes already current; no write")
        return 0
    target["srcTimes"] = src_times
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
                f"r2:{BUCKET}/v1/{MODEL}/manifest.json",
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
