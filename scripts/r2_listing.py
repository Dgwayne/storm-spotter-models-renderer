#!/usr/bin/env python3
"""Shared R2 listing helpers for build_manifest.py and prune_old_runs.py.

Both scripts used to fan out one `rclone lsf` per product (and, in
build_manifest, one more per (product, run)) to discover what is on R2 —
P*(1+retain) ListObjectsV2 (Class A) calls per manifest build, rebuilt twice
per tick. For the OBS catalog (92 products x retain_runs 26) that is ~2,400+
LIST calls per build.

These helpers replace that with a SINGLE `rclone lsf --recursive` over the
model prefix, parsed in memory, dropping Class A LIST cost from
O(products*runs) to O(objects/1000) pages. The pure parse functions are kept
separate from the subprocess call so they can be unit-tested without rclone or
R2 (see scratchpad test_list_equivalence.py).
"""
from __future__ import annotations

import re
import subprocess

# 10 digits = hourly run stamps (YYYYMMDDHH, every forecast model + OBS);
# 12 digits = per-analysis minute stamps (YYYYMMDDHHMM, RTMA — see
# render_rtma_obs.sh). Lexical sort stays chronological within a product
# because a product's stamps are always one width.
RUN_RE = re.compile(r"\d{10}(?:\d{2})?")
PNG_RE = re.compile(r"F(\d{3})\.png")


def rclone_lsf_recursive(bucket: str, model: str) -> str | None:
    """Raw stdout of one recursive listing of v1/<model>/, or None on error."""
    try:
        return subprocess.check_output(
            ["rclone", "lsf", "--recursive", f"r2:{bucket}/v1/{model}/"],
            text=True,
        )
    except subprocess.CalledProcessError:
        return None


def parse_tree(listing: str, products) -> dict:
    """{product: {run: set(hours)}} from recursive-lsf stdout.

    Semantics are chosen to reproduce the old list_r2_runs() + list_r2_frames()
    output byte-for-byte:
      * only products in `products` (this model's config list) are kept, so
        stray or decommissioned prefixes on R2 never leak into the manifest;
      * a run is registered as soon as ANY object exists under
        <product>/<run>/ (mirrors the old `lsf --dirs-only`), so a run still
        mid-upload — F###.json already there but F###.png not yet — appears
        with an empty hour set exactly as before;
      * a forecast hour is recorded only for a F###.png leaf (mirrors the old
        list_r2_frames regex); F###.json and F###.data.png siblings are ignored.
    Assumes the fixed key layout <product>/<run>/F###.<ext> (3 path segments);
    anything else (manifest.json at the root, etc.) is skipped.
    """
    product_set = set(products)
    tree: dict = {}
    for line in listing.splitlines():
        parts = line.strip().split("/")
        if len(parts) != 3:
            continue
        product, run, fname = parts
        if product not in product_set or not RUN_RE.fullmatch(run):
            continue
        hours = tree.setdefault(product, {}).setdefault(run, set())
        m = PNG_RE.fullmatch(fname)
        if m:
            hours.add(int(m.group(1)))
    return tree


def runs_by_product(tree: dict) -> dict:
    """{product: sorted[run]} view of a parse_tree() result (for pruning)."""
    return {p: sorted(runs) for p, runs in tree.items()}


def all_runs(tree: dict) -> list:
    """Sorted union of run stamps across all products (old list_r2_runs)."""
    return sorted({run for runs in tree.values() for run in runs})
