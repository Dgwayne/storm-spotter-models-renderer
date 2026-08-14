#!/usr/bin/env python3
"""Extract variables from an Open-Meteo .om spatial file into a GeoTIFF.

Usage:
    om_extract.py <file.om> <out.tif> <west> <south> <east> <north> <var> [<var2> ...]

Writes a Float32 GTiff in EPSG:4326 with one band per requested variable,
in argument order (U before V for composite_uv products — decode_pipeline.sh
reads band 1 as U and band 2 as V, mirroring the GRIB path's idx ordering).

Open-Meteo data_spatial conventions handled here (see the ICOND2 model notes
in config/products.yml):
  * rows are SOUTH-first — flipped to the north-up order GeoTIFF expects;
  * the bbox in the bucket's meta.json describes grid-point CENTERS, so the
    geotransform shifts outward by half a cell to describe pixel edges;
  * NaN (outside the model's native nest inside the rectangular grid) maps
    to the pipeline's standard -9999 nodata.

The values arrive already unit-normalized by Open-Meteo (degC, m/s, hPa, mm),
so products.yml `om_convert`/`convert` expressions run in the same unit space
as the GRIB path's post-GDAL-normalization values.
"""

from __future__ import annotations

import sys

import numpy as np
from osgeo import gdal, osr

NODATA = -9999.0


def read_var(reader, name: str) -> np.ndarray:
    for i in range(reader.num_children):
        child = reader.get_child_by_index(i)
        if child.is_array and child.name == name:
            return child[:, :]
    raise SystemExit(f"om_extract: variable {name!r} not in file "
                     f"(has: {[reader.get_child_by_index(i).name for i in range(reader.num_children) if reader.get_child_by_index(i).is_array]})")


def main() -> None:
    if len(sys.argv) < 8:
        raise SystemExit(__doc__)
    om_path, out_tif = sys.argv[1], sys.argv[2]
    west, south, east, north = (float(v) for v in sys.argv[3:7])
    var_names = sys.argv[7:]

    import omfiles

    reader = omfiles.OmFileReader.from_path(om_path)
    bands = [read_var(reader, name) for name in var_names]

    rows, cols = bands[0].shape
    for name, band in zip(var_names, bands):
        if band.shape != (rows, cols):
            raise SystemExit(f"om_extract: {name} shape {band.shape} != {(rows, cols)}")

    # meta.json bbox = grid-point centers; geotransform wants pixel edges.
    xres = (east - west) / (cols - 1)
    yres = (north - south) / (rows - 1)

    drv = gdal.GetDriverByName("GTiff")
    ds = drv.Create(out_tif, cols, rows, len(bands), gdal.GDT_Float32)
    ds.SetGeoTransform((west - xres / 2, xres, 0, north + yres / 2, 0, -yres))
    srs = osr.SpatialReference()
    srs.ImportFromEPSG(4326)
    ds.SetProjection(srs.ExportToWkt())
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
