#!/usr/bin/env python3
"""ptype_refl_combine.py: build the ptypeRefl mercator raster.

Called by decode_pipeline.sh (products.yml `merc_script:`) after every input
has been warped to the shared mercator target on its own kernel
(`input_resample:`): R = 1 km AGL reflectivity (cubic), A/B/C/D = the
CRAIN/CSNOW/CICEP/CFRZR dominant-type flags (nearest).

Output value = 100 * type + dBZ (dBZ clipped 0..75), -9999 = nodata:
rain 100..175, snow 200..275, sleet 300..375, freezing rain 400..475.

Steps:
  1. type per pixel, most hazardous flag wins (freezing rain > sleet >
     snow > rain), the same priority as the ptype product.
  2. FRINGE FILL: pixels with no flag take the most hazardous type within
     --fill-px pixels. The flags sit on the model's 3 km cells (warped
     nearest, so stair-stepped), while the reflectivity is cubic and
     smooth; without the fill the flag mask cuts every echo's edge into
     blocks. With it the smooth --floor dBZ contour draws the edge. Only
     EMPTY pixels are filled, so a rain/snow boundary does not move.
  3. Echo below --floor dBZ, or with no type even after the fill (echo
     the model says never reaches the ground), is nodata.

Usage:
  ptype_refl_combine.py --out merc.tif --R r.tif --A a.tif --B b.tif \
      --C c.tif --D d.tif [--floor 0] [--fill-px 3]
"""
import argparse

import numpy as np
from osgeo import gdal

NODATA = -9999.0


def read(path):
    ds = gdal.Open(path)
    band = ds.GetRasterBand(1)
    arr = band.ReadAsArray().astype(np.float32)
    nd = band.GetNoDataValue()
    invalid = ~np.isfinite(arr)
    if nd is not None:
        invalid |= arr == nd
    return ds, arr, invalid


def dilate_max(t):
    """Max over each pixel's 3x3 neighbourhood (edges padded with 0)."""
    p = np.pad(t, 1, mode="constant", constant_values=0)
    h, w = t.shape
    out = t.copy()
    for dy in (0, 1, 2):
        for dx in (0, 1, 2):
            if dy == 1 and dx == 1:
                continue
            np.maximum(out, p[dy:dy + h, dx:dx + w], out=out)
    return out


def combine(r, a, b, c, d, invalid, floor, fill_px):
    t = np.zeros(r.shape, dtype=np.uint8)
    t[a > 0] = 1
    t[b > 0] = 2  # later assignments win: priority order
    t[c > 0] = 3
    t[d > 0] = 4
    t[invalid] = 0
    for _ in range(fill_px):
        empty = t == 0
        if not empty.any():
            break
        grown = dilate_max(t)
        t = np.where(empty & ~invalid, grown, t)
    out = np.full(r.shape, NODATA, dtype=np.float32)
    keep = (t > 0) & ~invalid & (r >= floor)
    out[keep] = 100.0 * t[keep] + np.clip(r[keep], 0.0, 75.0)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    for k in "RABCD":
        ap.add_argument(f"--{k}", required=True)
    ap.add_argument("--floor", type=float, default=0.0)
    ap.add_argument("--fill-px", type=int, default=3)
    args = ap.parse_args()

    ds_r, r, inv_r = read(args.R)
    flags = []
    invalid = inv_r.copy()
    for k in "ABCD":
        _, arr, inv = read(getattr(args, k))
        invalid |= inv
        flags.append(np.where(inv, 0, arr))
    out = combine(r, *flags, invalid=invalid, floor=args.floor,
                  fill_px=args.fill_px)

    drv = gdal.GetDriverByName("GTiff")
    dst = drv.Create(args.out, ds_r.RasterXSize, ds_r.RasterYSize, 1,
                     gdal.GDT_Float32)
    dst.SetGeoTransform(ds_r.GetGeoTransform())
    dst.SetProjection(ds_r.GetProjection())
    band = dst.GetRasterBand(1)
    band.SetNoDataValue(NODATA)
    band.WriteArray(out)
    band.FlushCache()
    dst = None


if __name__ == "__main__":
    main()
