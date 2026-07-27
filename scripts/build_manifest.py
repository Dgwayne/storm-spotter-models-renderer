#!/usr/bin/env python3
"""Rebuild manifest.json for one model by reconciling R2 contents with the canonical
expected forecast-hour set from config/products.yml.

Usage:  build_manifest.py <MODEL>      # MODEL in {HRRR, GFS}

Env:    R2_BUCKET, R2_ENDPOINT, AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
import sys
from pathlib import Path

import yaml  # PyYAML

from r2_listing import parse_tree, rclone_lsf_recursive
from r2_listing import all_runs as tree_all_runs

MODEL = sys.argv[1]
REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG = REPO_ROOT / "config" / "products.yml"

with CONFIG.open() as f:
    cfg = yaml.safe_load(f)

model_cfg = cfg["models"][MODEL]
products_cfg = cfg["products"]
products = model_cfg["products"]
bbox = model_cfg["bbox_lonlat"]
img_size = model_cfg["image_size"]
retain = model_cfg.get("retain_runs", 5)

bucket = os.environ["R2_BUCKET"]


def expected_fhs(model: str, run_hour: int) -> list[int]:
    """Return canonical expected forecast-hour list per model + run.

    Driven entirely by config/products.yml: forecast_hours_synoptic (with its
    `runs` list) wins for synoptic run hours, forecast_hours_default otherwise.
    Reproduces the old hardcoded HRRR (48h synoptic / 18h) and GFS (default
    only) behavior, and covers RRFS (84h synoptic / 18h) with no new branch.
    """
    syn = model_cfg.get("forecast_hours_synoptic")
    if syn and run_hour in syn.get("runs", []):
        return list(range(syn["start"], syn["end"] + 1, syn.get("step", 1)))
    d = model_cfg["forecast_hours_default"]
    return list(range(d["start"], d["end"] + 1, d.get("step", 1)))


# --- Build manifest -----------------------------------------------------------
# One recursive R2 listing replaces the old per-product list_r2_runs() +
# per-(product, run) list_r2_frames() fan-out (P*(1+retain) `rclone lsf` LIST
# calls per build). See r2_listing.parse_tree for the byte-identical semantics.
# This is a FRESH listing at build time — deliberately NOT the driver's
# tick-start EXISTING_KEYS_FILE, which predates this tick's uploads and prune
# and would hide just-rendered frames for a full tick.
r2_tree = parse_tree(rclone_lsf_recursive(bucket, MODEL) or "", products)
all_run_stamps = tree_all_runs(r2_tree)
recent_runs = all_run_stamps[-retain:] if all_run_stamps else []

# Mesoanalysis products (category: meso) publish in a SEPARATE mesoCatalog
# list: app versions that predate the Mesoanalysis layer only read
# productCatalog, so they never surface a product that renders just f00-f01
# (which would look broken in the forecast-scrub models tab).
product_catalog = []
meso_catalog = []
for code in products:
    pc = products_cfg[code]
    entry = {
        "code": code,
        "display": pc["display"],
        "units": pc.get("units_out", ""),
        "pal": pc.get("pal", ""),
        # App draws on-map numbers only for products whose frames ship a
        # F###.json value grid alongside the PNG (see decode_pipeline.sh).
        "pointValues": bool(pc.get("point_values", False)),
        # Picker section for catalogs the app groups (OBS observation
        # products). Empty for everything else; old app versions ignore
        # unknown JSON keys.
        "group": pc.get("group", ""),
    }
    # Data-PNG products (gray 1..255 = min..max linear, 0/alpha-0 =
    # nodata): the app inverts values client-side and runs its crisp
    # data-space renderer instead of showing the PNG's pixels.
    dp = pc.get("data_png")
    if isinstance(dp, dict) and "min" in dp and "max" in dp:
        entry["dataMin"] = float(dp["min"])
        entry["dataMax"] = float(dp["max"])
    # gpu_data: model products that ALSO publish a value-encoded F###.data.png
    # for the GPU model layer (separate from the OBS data_png so old apps keep
    # rendering the colored frame — see decode_pipeline.sh step 9). The encode
    # range is in DISPLAY units; the app reads these to decode + colorize.
    gd = pc.get("gpu_data")
    if isinstance(gd, dict) and "min" in gd and "max" in gd:
        entry["gpuDataMin"] = float(gd["min"])
        entry["gpuDataMax"] = float(gd["max"])
    if pc.get("category") == "meso":
        meso_catalog.append(entry)
    else:
        # point_value_fh_cap products (srh1/srh3/ehi1/ehi3) publish their
        # F###.json value grid for the first hour(s) only, while the PNG
        # covers the whole forecast range — see decode_pipeline.sh step 6b.
        # The two catalogs therefore disagree about pointValues: the meso
        # layer reads the grid at the product's first expected hour and
        # gets it, but the models tab would scrub straight off the end of
        # the grids, so it must be told there are no on-map numbers here
        # (which also matches every other forecast product — they all sit
        # at point_values: false). Copy first: one dict feeds both lists.
        meso_entry = entry
        if pc.get("point_value_fh_cap") is not None and entry["pointValues"]:
            meso_entry = dict(entry)
            entry["pointValues"] = False
        product_catalog.append(entry)
        # also_meso products render for the full forecast range (models
        # tab) AND appear in the meso catalog — the app's Mesoanalysis
        # layer just consumes their existing f00 frames.
        if pc.get("also_meso"):
            meso_catalog.append(meso_entry)


def expected_for(code: str, run_expected: list[int]) -> list[int]:
    """Clamp a run's expected hours to the product's fh_cap/fh_min/fh_step."""
    cap = products_cfg[code].get("fh_cap")
    floor = products_cfg[code].get("fh_min")
    # fh_step: product renders only every Nth hour (NAM's 3-hour APCP
    # buckets on an hourly-output model). Must mirror decode_pipeline.sh's
    # skip or the scrub bar paints the skipped hours as missing.
    step = products_cfg[code].get("fh_step")
    if cap is None and floor is None and step is None:
        return run_expected
    return [
        h
        for h in run_expected
        if (cap is None or h <= cap)
        and (floor is None or h >= floor)
        and (step is None or h % step == 0)
    ]

runs_payload = []
for run in recent_runs:
    # 10-digit stamps are hourly (YYYYMMDDHH); RTMA publishes per-analysis
    # 12-digit minute stamps (YYYYMMDDHHMM) — see render_rtma_obs.sh.
    stamp_fmt = "%Y%m%d%H%M" if len(run) == 12 else "%Y%m%d%H"
    run_hour = int(run[8:10])
    expected = expected_fhs(MODEL, run_hour)
    available = {}
    for code in products:
        available[code] = sorted(r2_tree.get(code, {}).get(run, set()))
    runs_payload.append(
        {
            "runTime": dt.datetime.strptime(run, stamp_fmt)
            .replace(tzinfo=dt.timezone.utc)
            .isoformat(),
            "runStamp": run,
            "available": available,
            "expected": {code: expected_for(code, expected) for code in products},
        }
    )

manifest = {
    "schemaVersion": 1,
    "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
    "model": MODEL,
    "bbox": bbox,
    "imageSize": img_size,
    "productCatalog": product_catalog,
    "mesoCatalog": meso_catalog,
    "runs": runs_payload,
}

out_path = Path("/tmp") / f"manifest_{MODEL}.json"
out_path.write_text(json.dumps(manifest, indent=2))

# Upload to R2 at v1/<MODEL>/manifest.json
#
# Cache-Control is CRITICAL here. Without it Cloudflare applies its default
# 4-hour Browser Cache TTL, which froze the manifest at the edge — the app
# polls every ~60 s but kept getting a HIT on a manifest hours old, so new
# runs/frames stayed invisible even though they were already on R2. The PNG
# frames don't have this problem because decode_pipeline.sh stamps them with
# max-age=300. 60 s matches the app's manifest poll cadence
# (weatherModelManifestTtlSec) and still lets the edge absorb the bulk of
# repeated polls, so R2 read cost stays low.
subprocess.check_call(
    [
        "rclone",
        "copyto",
        str(out_path),
        f"r2:{bucket}/v1/{MODEL}/manifest.json",
        "--s3-no-check-bucket",
        "--no-traverse",
        "--header-upload",
        "Cache-Control: public, max-age=60",
    ]
)

print(f"manifest uploaded: v1/{MODEL}/manifest.json ({len(runs_payload)} runs)")
