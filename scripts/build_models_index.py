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

with CONFIG.open(encoding="utf-8") as f:
    cfg = yaml.safe_load(f)

entries = []
for key, mc in cfg["models"].items():
    group = mc.get("picker_group")
    if not group:
        continue  # OBS/RTMA/SAT pseudo-models never reach the picker
    entry = {
        "key": key,
        "display": mc.get("display", key),
        "group": group,
        "order": mc.get("picker_order", 999),
    }
    # min_schema gates the entry to app builds that understand the
    # model's manifest schema (2 = sub-hourly minute frames). The app's
    # index provider DROPS entries above its capability, so a fleet
    # that predates a frame-scheme change never lists a model it can't
    # scrub. Absent = schema 1 = every build.
    ms = mc.get("min_schema")
    if ms:
        entry["minSchema"] = int(ms)
    entries.append(entry)

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

# ── Soundings picker index (v1/soundings/index.json) ────────────────
# Same server-driven contract as models.json, for the sounding model
# dropdown. Sourced from the `sounding_models` roster in products.yml so
# adding/pulling a sounding model is a config edit + push, no release.
sound = []
for e in cfg.get("sounding_models") or []:
    key = e.get("key")
    if not key:
        continue
    sound.append({
        "key": key,
        "display": e.get("display", key.upper()),
        "prefix": e.get("prefix", ""),
        "order": e.get("order", 999),
    })
sound.sort(key=lambda e: e["order"])
for e in sound:
    del e["order"]

if sound:
    sidx = Path("/tmp/soundings_index.json")
    sidx.write_text(json.dumps({
        "schemaVersion": 1,
        "generatedAt": dt.datetime.now(dt.timezone.utc).isoformat(),
        "models": sound,
    }, indent=2))
    subprocess.check_call([
        "rclone", "copyto", str(sidx),
        f"r2:{bucket}/v1/soundings/index.json",
        "--s3-no-check-bucket", "--no-traverse",
        "--header-upload", "Cache-Control: public, max-age=300",
    ])
    print(f"soundings/index.json uploaded ({len(sound)} models)")
else:
    print("no sounding_models roster in products.yml; soundings index skipped")
