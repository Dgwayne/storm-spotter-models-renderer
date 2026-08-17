#!/usr/bin/env python3
"""Prune R2 to the most recent N runs per product for one model.

Usage:  prune_old_runs.py <MODEL> <RETAIN>
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import yaml

from r2_listing import parse_tree, prefix_for, rclone_lsf_recursive, runs_by_product

MODEL = sys.argv[1]
RETAIN = int(sys.argv[2])
# Deletes are scoped to this prefix and nothing else, which is what makes a
# shadow run safe: with OBS_PREFIX=OBS-shadow every purge below addresses
# v1/OBS-shadow/..., so a shadowing box has no path to production data.
PREFIX = prefix_for(MODEL)

REPO_ROOT = Path(__file__).resolve().parent.parent
with (REPO_ROOT / "config" / "products.yml").open(encoding="utf-8") as f:
    cfg = yaml.safe_load(f)

products = cfg["models"][MODEL]["products"]
bucket = os.environ["R2_BUCKET"]

# One recursive listing instead of one `rclone lsf --dirs-only` per product.
runs_map = runs_by_product(parse_tree(rclone_lsf_recursive(bucket, MODEL) or "", products))

for product in products:
    runs = runs_map.get(product, [])
    if len(runs) <= RETAIN:
        continue
    to_delete = runs[:-RETAIN]
    for run in to_delete:
        prefix = f"v1/{PREFIX}/{product}/{run}/"
        print(f"  prune {prefix}")
        subprocess.run(
            ["rclone", "purge", f"r2:{bucket}/{prefix}", "--s3-no-check-bucket"],
            check=False,
        )
