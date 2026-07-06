#!/usr/bin/env python3
"""Sample a decoded Float32 Web Mercator raster into a compact point-value JSON grid.

Called from decode_pipeline.sh after gdalwarp produces merc.tif (the
unit-converted, reprojected raster the PNG is color-mapped from). Downsamples
it to a small fixed grid with nearest-neighbor sampling and writes the values
as JSON, so the app can draw Pivotal-style on-map numbers that always agree
with the color fill.

Grid convention: row-major, row 0 = north (top of raster), west -> east within
each row. Points are evenly spaced in EPSG:3857 (screen space on a Mapbox
map); the app derives each point's lon/lat from bbox + grid dims. NoData
(-9999) becomes JSON null.

Usage:
  sample_point_values.py <merc_tif> <out_json> <model> <product> <run_stamp>
                         <fh> <units> <decimals> "<minLon> <minLat> <maxLon> <maxLat>"
                         <grid_w> <grid_h>

Requires gdalwarp + gdal_translate on PATH (same as decode_pipeline.sh).
Pure-stdlib Python otherwise (the runners have no numpy/rasterio).
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
from pathlib import Path

NODATA_THRESHOLD = -9998.0  # anything at/below this is treated as -9999 NoData


def main() -> int:
    (
        merc_tif,
        out_json,
        model,
        product,
        run_stamp,
        fh,
        units,
        decimals,
        bbox_str,
        grid_w,
        grid_h,
    ) = sys.argv[1:12]

    n_decimals = int(decimals)
    width = int(grid_w)
    height = int(grid_h)
    bbox = [float(t) for t in bbox_str.split()]
    if len(bbox) != 4:
        raise SystemExit(f"bad bbox: {bbox_str!r}")

    with tempfile.TemporaryDirectory() as td:
        # Nearest-neighbor pick of the source pixel at each target cell
        # center: point values, not block averages, so local extremes
        # (the 81 in a field of 75s) survive the way Pivotal's do.
        small_tif = str(Path(td) / "small.tif")
        subprocess.check_call(
            [
                "gdalwarp",
                "-q",
                "-overwrite",
                "-ts",
                str(width),
                str(height),
                "-r",
                "near",
                "-dstnodata",
                "-9999",
                merc_tif,
                small_tif,
            ]
        )

        # XYZ text export: one "x y z" line per pixel, top row (north)
        # first, west->east — exactly the row-major order we publish.
        xyz_path = str(Path(td) / "small.xyz")
        subprocess.check_call(
            ["gdal_translate", "-q", "-of", "XYZ", small_tif, xyz_path]
        )

        values: list[float | int | None] = []
        with open(xyz_path) as f:
            for line in f:
                parts = line.split()
                if len(parts) != 3:
                    continue
                v = float(parts[2])
                if v <= NODATA_THRESHOLD:
                    values.append(None)
                elif n_decimals == 0:
                    values.append(int(round(v)))
                else:
                    values.append(round(v, n_decimals))

    expected = width * height
    if len(values) != expected:
        raise SystemExit(
            f"sample count mismatch: got {len(values)}, expected {expected}"
        )

    payload = {
        "schemaVersion": 1,
        "model": model,
        "product": product,
        "runStamp": run_stamp,
        "fh": int(fh),
        "units": units,
        "bbox": bbox,
        "grid": [width, height],
        "values": values,
    }
    Path(out_json).write_text(json.dumps(payload, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    sys.exit(main())
