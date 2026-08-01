#!/usr/bin/env python3
"""storage_report.py: read-only breakdown of what the B2 bucket is holding.

Answers three questions the manifests cannot:
  1. Where is the space actually going (per prefix, not just a bucket total)?
  2. What is STALE, i.e. present on B2 but older than the window its renderer
     prunes to, and therefore about to be reclaimed?
  3. Where are we storing frames the app can never show, or offering the app a
     longer window than we actually keep?

Read-only. Lists and measures, never deletes.

Env: R2_BUCKET (rclone "r2:" remote, pointed at B2 by the workflow).
"""

from __future__ import annotations

import collections
import datetime as dt
import os
import re
import subprocess
import sys

GB = 1024 ** 3
MB = 1024 ** 2

# What each tree is pruned to by the job that writes it, so we can separate
# "live window" from "not yet reclaimed". Minutes.
WINDOWS_MIN = {
    "v1/SAT/geocolor": 720,   # render_goes_geocolor.py WINDOW_MIN
    "v1/SAT": 180,            # render_goes.py rolling window (ir/wv/vis)
}
# Per-region overrides inside the geocolor tree.
GEOCOLOR_REGION_WINDOW = {"wpac": 360}

# The loop lengths the app offers the user, longest last. Storing less than the
# longest means the app silently shows a shorter loop than its own picker says.
APP_WINDOW_CHOICES_MIN = [60, 180, 360, 720]


def _human(n: float) -> str:
    return f"{n / GB:.2f} GB" if n >= GB else f"{n / MB:.0f} MB"


def _listing(bucket: str, versions: bool = False) -> list[tuple[int, str]]:
    cmd = ["rclone", "lsf", "--recursive", "--files-only", "--format", "sp"]
    if versions:
        cmd.append("--s3-versions")
    cmd.append(f"r2:{bucket}/")
    out = subprocess.check_output(cmd, text=True, timeout=3600)
    rows = []
    for ln in out.splitlines():
        if ";" not in ln:
            continue
        size, path = ln.split(";", 1)
        try:
            rows.append((int(size), path.strip()))
        except ValueError:
            continue
    return rows


def _versions_with_age(bucket: str) -> list[tuple[int, dt.datetime | None, str]]:
    """Every stored version as (size, modtime, path)."""
    out = subprocess.check_output(
        ["rclone", "lsf", "--recursive", "--files-only", "--s3-versions",
         "--format", "stp", f"r2:{bucket}/"], text=True, timeout=3600)
    rows = []
    for ln in out.splitlines():
        parts = ln.split(";", 2)
        if len(parts) != 3:
            continue
        try:
            size = int(parts[0])
        except ValueError:
            continue
        mt = None
        try:
            mt = dt.datetime.fromisoformat(parts[1].replace("Z", "+00:00"))
            if mt.tzinfo is None:
                mt = mt.replace(tzinfo=dt.timezone.utc)
        except ValueError:
            pass
        rows.append((size, mt, parts[2].strip()))
    return rows


def _version_overhead(bucket: str, live_total: int, live_n: int) -> None:
    """Compare live objects against ALL versions.

    B2 buckets default to keeping every version of a file. Our renderers
    overwrite manifests every few minutes and prune frames constantly, and each
    of those leaves a hidden old version behind that still bills. A bucket
    total far above the live total means the space is NOT going to be reclaimed
    by our own pruning: it needs a bucket lifecycle rule.
    """
    print("--- live objects vs all versions ---")
    try:
        allv = _versions_with_age(bucket)
    except Exception as exc:  # noqa: BLE001 - diagnostic only
        print(f"  version listing unavailable: {exc}\n")
        return
    all_total = sum(s for s, _, _ in allv)
    extra = all_total - live_total
    print(f"  live     : {live_n:>7,} objects  {_human(live_total)}")
    print(f"  all vers : {len(allv):>7,} objects  {_human(all_total)}")
    print(f"  dead     : {len(allv) - live_n:>7,} objects  {_human(extra)}"
          f"  ({100 * extra / max(all_total, 1):.0f}% of billed storage)")

    # The bucket lifecycle is meant to be "keep only the last version", whose
    # floor is 1 day. So the question is NOT whether dead versions exist, it is
    # whether they AGE OUT. Anything well past a couple of days means the rule
    # is not actually being applied.
    now = dt.datetime.now(dt.timezone.utc)
    buckets = {"< 1 day": 0, "1-2 days": 0, "2-7 days": 0, "> 7 days": 0}
    oldest = None
    for size, mt, _ in allv:
        if mt is None:
            continue
        age_d = (now - mt).total_seconds() / 86400
        oldest = mt if oldest is None or mt < oldest else oldest
        key = ("< 1 day" if age_d < 1 else "1-2 days" if age_d < 2
               else "2-7 days" if age_d < 7 else "> 7 days")
        buckets[key] += size
    print("\n  age of ALL stored versions (lifecycle should age these out):")
    for k, v in buckets.items():
        print(f"    {k:>9}: {_human(v)}")
    if oldest:
        print(f"    oldest object: {oldest:%Y-%m-%d %H:%M}Z "
              f"({(now - oldest).days} days)")
    stuck = buckets["2-7 days"] + buckets["> 7 days"]
    if stuck > all_total * 0.25:
        print("\n  VERDICT: a large share sits well past the 1-day lifecycle "
              "floor,\n  so 'keep only the last version' is NOT being applied "
              "as expected.")
    else:
        print("\n  VERDICT: versions are aging out. This is the steady state "
              "of an\n  overwrite-heavy workload under a 1-day lifecycle "
              "floor, not a leak.")
    print()


def _stamp(path: str) -> dt.datetime | None:
    """UTC time encoded in a frame filename, if there is one."""
    m = re.search(r"(\d{12})(?:\.\w+)?$", path)
    if not m:
        return None
    try:
        return dt.datetime.strptime(m.group(1), "%Y%m%d%H%M").replace(
            tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def main() -> int:
    bucket = os.environ["R2_BUCKET"]
    now = dt.datetime.now(dt.timezone.utc)
    print(f"==> listing r2:{bucket} ...")
    rows = _listing(bucket)
    total = sum(s for s, _ in rows)
    print(f"==> {len(rows):,} objects, {_human(total)} total\n")

    _version_overhead(bucket, total, len(rows))

    # ── Where the space is, by prefix ─────────────────────────────────
    for depth in (2, 3):
        agg: dict[str, list[int]] = collections.defaultdict(lambda: [0, 0])
        for size, path in rows:
            parts = path.split("/")
            key = "/".join(parts[:depth]) if len(parts) > depth else "/".join(
                parts[:-1]) or "(root)"
            agg[key][0] += size
            agg[key][1] += 1
        print(f"--- by prefix (depth {depth}) ---")
        for key, (size, n) in sorted(agg.items(), key=lambda kv: -kv[1][0])[:18]:
            print(f"  {_human(size):>10}  {n:>7,} obj  {key}"
                  f"   [{100 * size / total:.1f}%]")
        print()

    # ── Stale vs live, for the trees that carry timestamps ────────────
    print("--- stale (past the prune window, awaiting reclaim) ---")
    stale_total = 0
    for tree, window in WINDOWS_MIN.items():
        buckets: dict[str, list[int]] = collections.defaultdict(lambda: [0, 0])
        for size, path in rows:
            if not path.startswith(tree + "/"):
                continue
            parts = path.split("/")
            region = parts[3] if tree.endswith("geocolor") and len(
                parts) > 4 else parts[len(tree.split("/"))]
            win = GEOCOLOR_REGION_WINDOW.get(region, window)
            t = _stamp(path)
            if t and (now - t).total_seconds() / 60 > win + 60:
                buckets[region][0] += size
                buckets[region][1] += 1
        for region, (size, n) in sorted(buckets.items(), key=lambda kv: -kv[1][0]):
            if n:
                print(f"  {_human(size):>10}  {n:>5} obj  {tree}/{region}")
                stale_total += size
    print(f"  {'':>10}  -> {_human(stale_total)} reclaimable\n" if stale_total
          else "  none: every timestamped tree is inside its window\n")

    # ── Do we store what the app can actually show? ───────────────────
    print("--- geocolor: stored window vs what the app offers ---")
    per_region: dict[str, list] = collections.defaultdict(
        lambda: [0, 0, None, None])  # size, n, oldest, newest
    for size, path in rows:
        parts = path.split("/")
        if len(parts) != 5 or not path.startswith("v1/SAT/geocolor/"):
            continue
        r = parts[3]
        t = _stamp(path)
        e = per_region[r]
        e[0] += size
        e[1] += 1
        if t:
            e[2] = t if e[2] is None or t < e[2] else e[2]
            e[3] = t if e[3] is None or t > e[3] else e[3]
    # Compare against the CONFIGURED window, not the observed span. A 12 h
    # window with a few missing slots lands just under 12 h, and judging by
    # span alone made that look like a shortfall when nothing was wrong.
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import render_goes_geocolor as R
        configured = {r: R._window_for(r) for r in R.REGIONS}
    except Exception:  # noqa: BLE001 - fall back to the local table
        configured = {}
    longest = APP_WINDOW_CHOICES_MIN[-1]
    for r, (size, n, old, new) in sorted(per_region.items()):
        span = (new - old).total_seconds() / 60 if old and new else 0
        win = configured.get(r, GEOCOLOR_REGION_WINDOW.get(r, 720))
        flag = "" if win >= longest else \
            f"  <-- stores {win // 60}h, app offers {longest // 60}h"
        avg = size / n if n else 0
        print(f"  {r:<10} {n:>3} frames  {_human(size):>8}  "
              f"avg {avg / MB:.2f} MB  span {span / 60:.1f}h{flag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
