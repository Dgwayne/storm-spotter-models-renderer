#!/usr/bin/env python3
"""Publish v1/models.json — the app's server-driven model picker index.

Emits every model in config/products.yml that carries a `picker_group`,
sorted by picker_order. The app renders the picker grouped by `group`
(section order is the app's fixed list: CONUS, Global, Ensembles,
Tropical, Europe) and falls back to its built-in model list when this
file is unreachable, so publishing here is additive-only: a model added
to this index appears on installed apps with no release.

Usage:  build_models_index.py
Env:    R2_BUCKET (+ rclone remote `r2` configured)
"""

from __future__ import annotations

import datetime as dt
import json
import os
import subprocess
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG = REPO_ROOT / "config" / "products.yml"

with CONFIG.open() as f:
    cfg = yaml.safe_load(f)

entries = []
for key, mc in cfg["models"].items():
    group = mc.get("picker_group")
    if not group:
        continue  # OBS/RTMA/SAT pseudo-models never reach the picker
    entries.append(
        {
            "key": key,
            "display": mc.get("display", key),
            "group": group,
            "order": mc.get("picker_order", 999),
        }
    )

entries.sort(key=lambda e: e["order"])
for e in entries:
    del e["order"]

index = {
    "schemaVersion": 1,
    "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
    "models": entries,
}

out = Path("/tmp/models.json")
out.write_text(json.dumps(index, indent=2))

bucket = os.environ["R2_BUCKET"]
subprocess.check_call(
    [
        "rclone",
        "copyto",
        str(out),
        f"r2:{bucket}/v1/models.json",
        "--s3-no-check-bucket",
        "--no-traverse",
        "--header-upload",
        "Cache-Control: public, max-age=300",
    ]
)
print(f"models.json uploaded ({len(entries)} models)")
