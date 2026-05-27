#!/usr/bin/env python3
"""Prune R2 to the most recent N runs per product for one model.

Usage:  prune_old_runs.py <MODEL> <RETAIN>
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

MODEL = sys.argv[1]
RETAIN = int(sys.argv[2])

REPO_ROOT = Path(__file__).resolve().parent.parent
with (REPO_ROOT / "config" / "products.yml").open() as f:
    cfg = yaml.safe_load(f)

products = cfg["models"][MODEL]["products"]
bucket = os.environ["R2_BUCKET"]


def list_runs(product: str) -> list[str]:
    pp = f"v1/{MODEL}/{product}/"
    try:
        out = subprocess.check_output(
            ["rclone", "lsf", "--dirs-only", f"r2:{bucket}/{pp}"],
            text=True,
        )
    except subprocess.CalledProcessError:
        return []
    runs = [ln.strip().rstrip("/") for ln in out.splitlines()]
    return sorted(r for r in runs if re.fullmatch(r"\d{10}", r))


for product in products:
    runs = list_runs(product)
    if len(runs) <= RETAIN:
        continue
    to_delete = runs[:-RETAIN]
    for run in to_delete:
        prefix = f"v1/{MODEL}/{product}/{run}/"
        print(f"  prune {prefix}")
        subprocess.run(
            ["rclone", "purge", f"r2:{bucket}/{prefix}", "--s3-no-check-bucket"],
            check=False,
        )
