#!/usr/bin/env python3
"""Extract variables from an Open-Meteo .om spatial file into a GeoTIFF.

Usage:
    om_extract.py <file.om> <out.tif> <meta.json> <var> [<var2> ...]

Writes a Float32 GTiff with one band per requested variable, in argument
order (U before V for composite_uv products — decode_pipeline.sh reads
band 1 as U and band 2 as V, mirroring the GRIB path's idx ordering).

Georeferencing comes from <meta.json> (the model's latest.json — grid
geometry is static per model): its `crs_wkt` carries both the CRS and a
USAGE BBOX holding the LAT/Lon of the corner GRID POINTS. The corners
are transformed from WGS84 into the grid's own CRS and the pixel size
derived from the array dims. One code path covers all three grid kinds
in the bucket:

  * regular lat-lon (ICON-D2, the globals): the transform is identity;
  * Lambert Conformal (NBM, HRRR-15min): corners land in projected
    metres — validated against NBM's documented NDFD grid: derived
    dx = 2539.700 m vs the spec's 2539.703 m;
  * rotated lat-lon (HRDPS, KNMI/DMI HARMONIE): the transform rotates
    into the oblique frame where the grid is regular.

The grid must be axis-aligned and regular in its own CRS (true for
every data_spatial model — that is what a grid IS), and the om array is
south-first — flipped to north-up here. NaN (outside a nest's native
domain) maps to the pipeline's standard -9999 nodata.

⚠ THE BUG THIS DESIGN REPLACED: v1 georeferenced the source array with
the model's products.yml `bbox_lonlat` — which is the WARP TARGET box.
Correct only when the grid extent and target happen to coincide
(ICON-D2, the European regionals); for every global-source model it
squashed the whole planet into the CONUS box. Source georeferencing and
output framing are different facts and now come from different places.
"""

from __future__ import annotations

import json
import re
import sys

import numpy as np
from osgeo import gdal, osr

NODATA = -9999.0


def read_var(reader, name: str) -> np.ndarray:
    for i in range(reader.num_children):
        child = reader.get_child_by_index(i)
        if child.is_array and child.name == name:
            return child[:, :]
    # Exit 3 = "variable not in this file" — decode_pipeline.sh treats it
    # as a clean skip (the om analogue of "no matching messages in idx").
    # Open-Meteo's f00 analysis files carry fewer variables than forecast
    # steps (82 vs 93 on ICON-D2: max/accum fields like `updraft` and
    # `precipitation` only exist from f01).
    print(f"om_extract: variable {name!r} not in file", file=sys.stderr)
    sys.exit(3)


def grid_frame(meta_path: str, rows: int, cols: int):
    """(wkt, geotransform) for a south-first rows x cols array."""
    meta = json.load(open(meta_path))
    wkt = meta["crs_wkt"]
    m = re.search(r"BBOX\[([-\d.]+),([-\d.]+),([-\d.]+),([-\d.]+)\]", wkt)
    if not m:
        raise SystemExit(f"om_extract: no BBOX in {meta_path} crs_wkt")
    south, west, north, east = (float(v) for v in m.groups())

    srs = osr.SpatialReference()
    srs.ImportFromWkt(wkt)
    srs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    wgs = osr.SpatialReference()
    wgs.ImportFromEPSG(4326)
    wgs.SetAxisMappingStrategy(osr.OAMS_TRADITIONAL_GIS_ORDER)
    ct = osr.CoordinateTransformation(wgs, srs)
    x0, y0, _ = ct.TransformPoint(west, south)
    x1, y1, _ = ct.TransformPoint(east, north)

    dx = (x1 - x0) / (cols - 1)
    dy = (y1 - y0) / (rows - 1)
    # Corner points are grid-point CENTERS; the geotransform describes
    # pixel edges, hence the half-cell outward shift.
    gt = (x0 - dx / 2, dx, 0, y1 + dy / 2, 0, -dy)
    return srs.ExportToWkt(), gt


def main() -> None:
    if len(sys.argv) < 5:
        raise SystemExit(__doc__)
    om_path, out_tif, meta_path = sys.argv[1], sys.argv[2], sys.argv[3]
    var_names = sys.argv[4:]

    import omfiles

    reader = omfiles.OmFileReader.from_path(om_path)
    bands = [read_var(reader, name) for name in var_names]

    rows, cols = bands[0].shape
    for name, band in zip(var_names, bands):
        if band.shape != (rows, cols):
            raise SystemExit(f"om_extract: {name} shape {band.shape} != {(rows, cols)}")

    wkt, gt = grid_frame(meta_path, rows, cols)

    drv = gdal.GetDriverByName("GTiff")
    ds = drv.Create(out_tif, cols, rows, len(bands), gdal.GDT_Float32)
    ds.SetGeoTransform(gt)
    ds.SetProjection(wkt)
    for i, band in enumerate(bands, start=1):
        data = np.flipud(band).astype(np.float32)  # south-first -> north-up
        data = np.where(np.isnan(data), NODATA, data)
        out_band = ds.GetRasterBand(i)
        out_band.SetNoDataValue(NODATA)
        out_band.WriteArray(data)
    ds.FlushCache()
    ds = None


if __name__ == "__main__":
    main()
