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
import re
import subprocess
import sys
from pathlib import Path

import yaml  # PyYAML

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


def list_r2_runs(model: str) -> list[str]:
    """Return sorted list of RunStamp directories present under v1/<model>/*/."""
    prefix = f"v1/{model}/"
    # Use rclone lsd; format is "          -1 ... <name>".
    runs: set[str] = set()
    for product in products:
        pp = f"{prefix}{product}/"
        try:
            out = subprocess.check_output(
                ["rclone", "lsf", "--dirs-only", f"r2:{bucket}/{pp}"],
                text=True,
            )
        except subprocess.CalledProcessError:
            continue
        for line in out.splitlines():
            name = line.strip().rstrip("/")
            if re.fullmatch(r"\d{10}", name):
                runs.add(name)
    return sorted(runs)


def list_r2_frames(model: str, product: str, run: str) -> set[int]:
    """Return integer set of forecast hours present on R2 for one (product, run)."""
    pp = f"v1/{model}/{product}/{run}/"
    try:
        out = subprocess.check_output(
            ["rclone", "lsf", f"r2:{bucket}/{pp}"],
            text=True,
        )
    except subprocess.CalledProcessError:
        return set()
    hours: set[int] = set()
    for line in out.splitlines():
        m = re.fullmatch(r"F(\d{3})\.png", line.strip())
        if m:
            hours.add(int(m.group(1)))
    return hours


# --- Build manifest -----------------------------------------------------------
all_runs = list_r2_runs(MODEL)
recent_runs = all_runs[-retain:] if all_runs else []

product_catalog = []
for code in products:
    pc = products_cfg[code]
    product_catalog.append(
        {
            "code": code,
            "display": pc["display"],
            "units": pc.get("units_out", ""),
            "pal": pc.get("pal", ""),
            # App draws on-map numbers only for products whose frames ship a
            # F###.json value grid alongside the PNG (see decode_pipeline.sh).
            "pointValues": bool(pc.get("point_values", False)),
        }
    )

runs_payload = []
for run in recent_runs:
    run_hour = int(run[-2:])
    expected = expected_fhs(MODEL, run_hour)
    available = {}
    for code in products:
        hours = sorted(list_r2_frames(MODEL, code, run))
        available[code] = hours
    runs_payload.append(
        {
            "runTime": dt.datetime.strptime(run, "%Y%m%d%H")
            .replace(tzinfo=dt.timezone.utc)
            .isoformat(),
            "runStamp": run,
            "available": available,
            "expected": {code: expected for code in products},
        }
    )

manifest = {
    "schemaVersion": 1,
    "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
    "model": MODEL,
    "bbox": bbox,
    "imageSize": img_size,
    "productCatalog": product_catalog,
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
