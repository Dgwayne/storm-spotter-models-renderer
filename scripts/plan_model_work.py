#!/usr/bin/env python3
"""plan_model_work.py — decide whether a model workflow tick has any real work.

Ran by each render workflow's `plan` job BEFORE fanning out render-group
matrix jobs. The old plan job emitted the full group matrix every tick, so
an idle tick (nothing new published upstream) still spun up plan + N render
groups + finalize — each paying ~1-2 min of checkout/micromamba setup. At
q15min cadences across six model workflows that alone saturated the free
plan's 20 concurrent runners and queued real work behind idle scaffolding
(measured 2026-08-19: render jobs waiting 20-60 min for a slot).

What it does, in one pass:
  1. One recursive bucket listing under v1/<MODEL>/  -> what is rendered.
  2. Candidate runs, enumerated exactly like the sweep scripts do.
  3. Expected forecast hours per run from config/products.yml (same rules
     as build_manifest.py's expected_fhs: segments > synoptic(runs) >
     default), narrowed per product by fh_cap / fh_min / fh_step and by
     accumulation windows implied by {fh_minus_N} in the match templates
     (an "APCP:{fh_minus_1}-{fh} hour acc" product has no f00 message, so
     probing/pending f00 forever would defeat the gate).
  4. HEAD-probe the source idx ONLY for (run, fh) tuples some product still
     needs. RRFS needs no probes at all: its slim mirror lives in the same
     bucket (v1/RRFS/_src/), so the listing already says what is published.
  5. Emit GITHUB_OUTPUT:
       has_work  = 'true' | 'false'
       matrix    = JSON [{group, products}] for ONLY the groups with work
       published = "RUNSTAMP:fh,fh,...;RUNSTAMP:..." — consumed by the
                   sweep scripts as PUBLISHED_FH_SPEC so they never spawn
                   decode_pipeline.sh for unpublished tuples.

FORCE_RERENDER env set -> emit every group, empty published spec (sweeps
then behave exactly as before the gate existed).

The run-enumeration table below mirrors constants that live in the sweep
scripts (HOURS_BACK / CYCLES_BACK / synoptic-only bridges). If you change
one there, change it here.

Usage:  plan_model_work.py <MODEL> [--listing-file F] [--dry-run]
Env:    R2_BUCKET (+ a working `rclone` r2: remote), FORCE_RERENDER opt.
"""

from __future__ import annotations

import concurrent.futures
import datetime as dt
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

import yaml  # PyYAML

# ── Per-model run enumeration (KEEP IN SYNC with the sweep scripts) ──────
#   mode "hourly": offsets 0..span hours back from now (render_hrrr.sh /
#     render_rrfs.sh), optionally filtered to a set of allowed run hours
#     (the RRFS bridge renders synoptic cycles only — delete that filter
#     here when render_rrfs.sh drops its own at the NOMADS cutover).
#   mode "cycle":  snap now to the interval, step back 0..span cycles
#     (render_nam.sh / render_gfs.sh / render_gefs_mean.sh /
#      render_aifs.sh / render_ecmwf.sh).
SWEEP_RUNS = {
    "HRRR": ("hourly", 4, None),
    "RRFS": ("hourly", 9, {0, 6, 12, 18}),
    "NAM": ("cycle", 2, 6),
    "GFS": ("cycle", 2, 6),
    "GEFS": ("cycle", 2, 6),
    "AIFS": ("cycle", 2, 6),
    "ECMWF": ("cycle", 2, 12),
}

PROBE_TIMEOUT_S = 8
PROBE_WORKERS = 16
# Every probe target is a public bucket/CDN; models.dgwaynes.com 403s
# urllib's default User-Agent, so always send a real one.
PROBE_UA = "stp-plan-gate"

FRAME_RE = re.compile(r"^(?P<prod>[^/]+)/(?P<run>\d{10})/F(?P<fh>\d{3})\.png$")
MIRROR_IDX_RE = re.compile(r"^_src/(?P<run>\d{10})/F(?P<fh>\d{3})\.grib2\.idx$")
FH_MINUS_RE = re.compile(r"\{fh_minus_(\d+)\}")


def log(msg: str) -> None:
    print(msg, flush=True)


def expected_fhs(model_cfg: dict, run_hour: int) -> list[int]:
    """Canonical expected forecast hours — same rules as build_manifest.py."""
    segs = model_cfg.get("forecast_hours_segments")
    if segs:
        hours: set[int] = set()
        for s in segs:
            hours.update(range(s["start"], s["end"] + 1, s.get("step", 1)))
        return sorted(hours)
    syn = model_cfg.get("forecast_hours_synoptic")
    if syn and run_hour in syn.get("runs", []):
        return list(range(syn["start"], syn["end"] + 1, syn.get("step", 1)))
    d = model_cfg["forecast_hours_default"]
    return list(range(d["start"], d["end"] + 1, d.get("step", 1)))


def eff_fh_min(pc: dict) -> int:
    """Smallest fh at which the product's source message can exist.

    An {fh_minus_N} accumulation window means fh < N has no message —
    the sweep's decode spawn would just hit "no matching messages in idx"
    forever, and the gate would see permanent pending work.
    """
    floor = int(pc.get("fh_min") or 0)
    match_srcs = [str(pc.get("wgrib2_match") or ""), str(pc.get("ecmwf_match") or "")]
    match_srcs += [str(v) for v in (pc.get("inputs") or {}).values()]
    for s in match_srcs:
        for m in FH_MINUS_RE.finditer(s):
            floor = max(floor, int(m.group(1)))
    return floor


def admitted_fhs(pc: dict, expected: list[int], plan_knobs: dict) -> list[int]:
    """Forecast hours this product can actually render, gate view.

    plan_knobs = per-MODEL gate-only overrides from products.yml
    (plan_fh_mins / plan_fh_caps / plan_fh_skip): product codes are shared
    across models, so a per-product fh_min can't express "TCDC has no f00
    in GEFS geavg but every hour in HRRR". decode_pipeline.sh still
    self-gates on the real idx content — these knobs only stop the gate
    from seeing permanent pending work where a message never exists.
    """
    cap = pc.get("fh_cap")
    step = pc.get("fh_step")
    floor = eff_fh_min(pc)
    floor = max(floor, int(plan_knobs.get("mins", {}).get(pc["_code"], 0)))
    plan_cap = plan_knobs.get("caps", {}).get(pc["_code"])
    skips = plan_knobs.get("skips", {}).get(pc["_code"], [])
    out = []
    for fh in expected:
        if fh < floor:
            continue
        if cap is not None and fh > int(cap):
            continue
        if plan_cap is not None and fh > int(plan_cap):
            continue
        if step and fh % int(step) != 0:
            continue
        if any(lo <= fh <= hi for lo, hi in skips):
            continue
        out.append(fh)
    return out


def candidate_runs(model: str, now_epoch: int) -> list[str]:
    mode, span, extra = SWEEP_RUNS[model]
    runs: list[str] = []
    if mode == "hourly":
        for off in range(span + 1):
            t = dt.datetime.fromtimestamp(now_epoch - off * 3600, dt.timezone.utc)
            if extra and t.hour not in extra:
                continue
            runs.append(t.strftime("%Y%m%d%H"))
    else:
        interval = extra * 3600
        base = (now_epoch // interval) * interval
        for off in range(span + 1):
            t = dt.datetime.fromtimestamp(base - off * interval, dt.timezone.utc)
            runs.append(t.strftime("%Y%m%d%H"))
    return runs


def idx_url(model_cfg: dict, run: str, fh: int) -> str:
    """Source idx/index URL for one (run, fh) — mirrors decode_pipeline.sh."""
    key = model_cfg["s3_key_template"].format(
        date=run[:8], run=int(run[8:10]), fh=fh
    )
    base = model_cfg.get("base_url")
    if base:
        grib = f"{base}/{key}"
    else:
        grib = f"https://{model_cfg['s3_bucket']}.s3.amazonaws.com/{key}"
    if model_cfg.get("index_format") == "ecmwf":
        return grib[: -len(".grib2")] + ".index" if grib.endswith(".grib2") else grib + ".index"
    return grib + ".idx"


def probe_one(url: str) -> bool:
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": PROBE_UA})
    try:
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT_S) as resp:
            return 200 <= resp.status < 300
    except Exception:
        # Unpublished, transient error, whatever — treated as "not there
        # yet"; the next tick re-probes. Never let a probe kill the plan.
        return False


def frontier_published(model_cfg: dict, run: str, expected: list[int]) -> set[int]:
    """Published fh for one ECMWF-family run via binary search.

    data.ecmwf.int rate-limits aggressively (blocks read as 404 — observed
    2026-08-19 after ~120 HEADs), so per-fh probing is off the table for
    ECMWF/AIFS. Their runs publish essentially as a batch in step order,
    so the highest published step pins the whole set: ~2-7 probes per run
    instead of one per needed fh.
    """
    if not expected:
        return set()
    if probe_one(idx_url(model_cfg, run, expected[-1])):
        return set(expected)
    if not probe_one(idx_url(model_cfg, run, expected[0])):
        return set()
    lo, hi = 0, len(expected) - 1  # expected[lo] published, expected[hi] not
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if probe_one(idx_url(model_cfg, run, expected[mid])):
            lo = mid
        else:
            hi = mid
    return set(expected[: lo + 1])


def gh_output(key: str, value: str) -> None:
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            f.write(f"{key}={value}\n")
    print(f"::output:: {key}={value if len(value) < 400 else value[:400] + '…'}")


def main() -> None:
    model = sys.argv[1]
    listing_file = None
    dry_run = False
    args = sys.argv[2:]
    while args:
        a = args.pop(0)
        if a == "--listing-file":
            listing_file = args.pop(0)
        elif a == "--dry-run":
            dry_run = True
        else:
            sys.exit(f"unknown arg: {a}")
    if dry_run:
        os.environ.pop("GITHUB_OUTPUT", None)

    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(repo_root, "config", "products.yml"), encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    model_cfg = cfg["models"][model]
    products_cfg = cfg["products"]
    products: list[str] = model_cfg["products"]
    groups: dict[str, list[str]] = model_cfg.get("render_groups") or {"all": list(products)}

    # ── Coverage validation (was the old plan step's whole job) ─────────
    if model_cfg.get("render_groups"):
        covered = [p for g in groups.values() for p in g]
        dupes = sorted({p for p in covered if covered.count(p) > 1})
        if dupes:
            print(f"::error::products listed in more than one render group: {' '.join(dupes)}")
            sys.exit(1)
        if sorted(covered) != sorted(products):
            only_g = sorted(set(covered) - set(products))
            only_p = sorted(set(products) - set(covered))
            print(
                f"::error::render_groups do not exactly cover models.{model}.products "
                f"(only-in-groups: {only_g} only-in-products: {only_p})"
            )
            sys.exit(1)

    def emit(has_work: bool, matrix: list, published: dict[str, set]) -> None:
        spec = ";".join(
            f"{run}:{','.join(str(h) for h in sorted(fhs))}"
            for run, fhs in sorted(published.items(), reverse=True)
            if fhs
        )
        gh_output("has_work", "true" if has_work else "false")
        gh_output("matrix", json.dumps(matrix, separators=(",", ":")))
        gh_output("published", spec)

    if os.environ.get("FORCE_RERENDER"):
        log("==> FORCE_RERENDER set — emitting every group, no published filter")
        emit(True, [{"group": g, "products": " ".join(ps)} for g, ps in groups.items()], {})
        return

    # ── 1. What is rendered (one recursive listing) ──────────────────────
    if listing_file:
        listing = open(listing_file, encoding="utf-8").read()
    else:
        bucket = os.environ["R2_BUCKET"]
        res = subprocess.run(
            ["rclone", "lsf", "--recursive", "--files-only", f"r2:{bucket}/v1/{model}/"],
            capture_output=True, text=True,
        )
        if res.returncode != 0:
            print(f"::error::bucket listing failed: {res.stderr.strip()[:500]}")
            sys.exit(1)
        listing = res.stdout

    rendered: dict[tuple[str, str], set[int]] = {}
    mirror_pub: dict[str, set[int]] = {}
    for line in listing.splitlines():
        m = FRAME_RE.match(line)
        if m:
            rendered.setdefault((m["prod"], m["run"]), set()).add(int(m["fh"]))
            continue
        m = MIRROR_IDX_RE.match(line)
        if m:
            mirror_pub.setdefault(m["run"], set()).add(int(m["fh"]))

    # ── 2+3. Candidate runs and per-product needs ────────────────────────
    plan_knobs = {
        "mins": model_cfg.get("plan_fh_mins") or {},
        "caps": model_cfg.get("plan_fh_caps") or {},
        "skips": model_cfg.get("plan_fh_skip") or {},
    }
    now = int(time.time())
    runs = candidate_runs(model, now)
    admitted_cache: dict[tuple[str, int], list[int]] = {}
    # need[run] = {fh: [products still missing it]}
    need: dict[str, dict[int, list[str]]] = {r: {} for r in runs}
    for run in runs:
        run_hour = int(run[8:10])
        expected = expected_fhs(model_cfg, run_hour)
        for prod in products:
            key = (prod, run_hour)
            if key not in admitted_cache:
                pc = dict(products_cfg[prod], _code=prod)
                admitted_cache[key] = admitted_fhs(pc, expected, plan_knobs)
            have = rendered.get((prod, run), set())
            for fh in admitted_cache[key]:
                if fh not in have:
                    need[run].setdefault(fh, []).append(prod)

    # ── 4. Which needed (run, fh) tuples are actually published ─────────
    published: dict[str, set[int]] = {r: set() for r in runs}
    if model == "RRFS":
        # Slim mirror lives in this same bucket — the listing IS the
        # publish state; no HTTP probes at all.
        for run in runs:
            published[run] = {fh for fh in need[run] if fh in mirror_pub.get(run, set())}
    elif model_cfg.get("index_format") == "ecmwf":
        # Rate-limited host: binary-search the publish frontier per run
        # (batch publishing makes this exact) instead of per-fh probes.
        for run in runs:
            if not need[run]:
                continue
            expected = expected_fhs(model_cfg, int(run[8:10]))
            pub = frontier_published(model_cfg, run, expected)
            published[run] = pub & set(need[run])
            log(f"  frontier {run}: published through "
                f"{max(pub) if pub else 'nothing'}")
    else:
        targets = [(run, fh) for run in runs for fh in sorted(need[run])]
        if targets:
            urls = {t: idx_url(model_cfg, t[0], t[1]) for t in targets}
            with concurrent.futures.ThreadPoolExecutor(PROBE_WORKERS) as ex:
                results = dict(zip(targets, ex.map(lambda t: probe_one(urls[t]), targets)))
            for (run, fh), ok in results.items():
                if ok:
                    published[run].add(fh)
        log(f"==> probed {len(targets)} source idx URLs")

    # ── 5. Pending work per group ────────────────────────────────────────
    pending_products: set[str] = set()
    pending_counts: dict[str, int] = {}
    for run in runs:
        for fh, prods in need[run].items():
            if fh in published[run]:
                for p in prods:
                    pending_products.add(p)
                    pending_counts[p] = pending_counts.get(p, 0) + 1

    active = [
        {"group": g, "products": " ".join(ps)}
        for g, ps in groups.items()
        if any(p in pending_products for p in ps)
    ]

    for run in runs:
        pub = sorted(published[run])
        needed = len(need[run])
        log(f"  run {run}: {needed} needed fh, {len(pub)} published{' -> ' + str(pub[:12]) if pub else ''}")
    if pending_counts:
        top = sorted(pending_counts.items(), key=lambda kv: -kv[1])[:15]
        log("  pending frames by product: " + ", ".join(f"{p}={n}" for p, n in top))
    log(f"==> {model}: {len(active)}/{len(groups)} groups have work "
        f"({len(pending_products)} products, {sum(pending_counts.values())} frames)")

    emit(bool(active), active, {r: published[r] for r in runs})


if __name__ == "__main__":
    main()
