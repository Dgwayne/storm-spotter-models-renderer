#!/usr/bin/env python3
"""GRIB -> data-PNG in ONE GDAL process.

render_mrms_obs.sh historically spawned FIVE GDAL processes per product
(gdal_translate, gdal_calc, gdalwarp, gdal_calc, gdal_translate) plus two
more python+gdalinfo calls for the longitude guard. Measured at ~7.5
core-seconds per product, roughly 5 s of which is GDAL CPU even for a
51 KB source file — most of it process startup, driver registration and
numpy import paid over and over, plus three ~100 MB intermediate GTiffs
written to disk and read straight back.

At 41 products on a 2-minute tier that overhead is the difference between
fitting on Oracle's free 2 OCPU and having to pay for more, so it is worth
removing. This does the whole chain in-process on one numpy array.

Scope: the DATA-PNG path only. Every one of the 87 mrms_dir products in
products.yml carries `data_png`, so the gdaldem color-relief branch is
unreachable for this catalog; render_mrms_obs.sh keeps the classic
five-spawn chain and falls back to it for anything without data_png rather
than carry an untested reimplementation of color-relief here.

Byte-for-byte equivalence with the classic chain is the requirement, not
an aspiration — --compare renders both ways and diffs the PNGs, which is
how this gets validated on the box before it renders anything real.

Usage:
  mrms_render_one.py --grib IN.grib2 --out F000.png
                     --bbox W S E N --size WIDTH HEIGHT
                     --scale FLOAT --sentinel-lt FLOAT
                     --data-min FLOAT --data-max FLOAT
                     [--warped-tif merc.tif]
"""

from __future__ import annotations

import argparse
import sys

import numpy as np
from osgeo import gdal

gdal.UseExceptions()

NODATA = -9999.0


def build(args) -> tuple[np.ndarray, gdal.Dataset]:
    """Read the GRIB, mask sentinels, scale, and warp to the target grid.

    Reproduces, in order:
      gdal_translate -of GTiff -ot Float32 -b 1
      [gdal_edit.py -a_ullr ...]              (0-360 longitude guard)
      gdal_calc  where(A<sentinel,-9999,A*scale)  --NoDataValue=-9999
      gdalwarp -t_srs EPSG:3857 -te_srs EPSG:4326 -te BBOX -ts W H
               -r near -dstnodata -9999
    """
    src = gdal.Open(args.grib, gdal.GA_ReadOnly)
    band = src.GetRasterBand(1)
    # Float32 to match `gdal_translate -ot Float32`: the arithmetic below
    # must happen at the same precision the classic chain used, or values
    # sitting exactly on a quantisation boundary round the other way.
    arr = band.ReadAsArray().astype(np.float32)

    gt = list(src.GetGeoTransform())

    # Longitude guard: 0-360 grids shift west by 360 (same as QPE script).
    if gt[0] > 180.0:
        gt[0] -= 360.0

    # MRMS sentinel negatives (-1 missing / -3 no coverage) -> NoData, then
    # per-product unit scale. Every catalog product is non-negative in
    # display units, so the A<0 mask is safe across the board.
    # sentinel_lt: products whose REAL values go negative (dBZ, raw
    # azimuthal shear) set a floor below their physical range so the
    # -99/-999 sentinels are still caught without eating valid data.
    scaled = np.where(arr < np.float32(args.sentinel_lt),
                      np.float32(NODATA),
                      arr * np.float32(args.scale)).astype(np.float32)
    # gdal_calc reads bands as MASKED arrays unless --hideNoData is given,
    # and the sentinel step deliberately does not pass it: cells the GRIB
    # itself declares NoData come out as the output NoDataValue rather than
    # as scaled garbage. Reproduce that, and fold in NaN, which masks the
    # same way and would otherwise survive the `<` comparison untouched.
    src_nodata = band.GetNoDataValue()
    if src_nodata is not None:
        scaled = np.where(arr == np.float32(src_nodata), np.float32(NODATA), scaled)
    scaled = np.where(np.isnan(arr), np.float32(NODATA), scaled)

    mem = gdal.GetDriverByName("MEM").Create(
        "", src.RasterXSize, src.RasterYSize, 1, gdal.GDT_Float32
    )
    mem.SetGeoTransform(gt)
    mem.SetProjection(src.GetProjection())
    mem.GetRasterBand(1).WriteArray(scaled)
    mem.GetRasterBand(1).SetNoDataValue(NODATA)

    w, s, e, n = args.bbox
    warped = gdal.Warp(
        "",
        mem,
        format="MEM",
        dstSRS="EPSG:3857",
        outputBoundsSRS="EPSG:4326",
        outputBounds=(w, s, e, n),
        width=args.size[0],
        height=args.size[1],
        # DATA products warp with NEAREST — the app's crisp renderer
        # interpolates in data space client-side, and cubic here would
        # pre-blur real values (and invent overshoot ones).
        resampleAlg="near",
        srcNodata=NODATA,
        dstNodata=NODATA,
    )
    return warped


def to_png(warped: gdal.Dataset, out: str, dmin: float, dmax: float) -> None:
    """gray+alpha PNG, reproducing the second gdal_calc + gdal_translate.

    gray 1..255 = dmin..dmax linear (0 reserved for nodata), alpha 255 =
    valid. The app decodes values back and runs the same crisp data-space
    renderer the live reflectivity uses — colorized client-side with the
    product's legend bins.

    The classic call passes --hideNoData so the raw -9999s reach the
    expressions and the where() guards do exactly what they say (a
    separate constant-valued alpha calc was being silently zeroed by
    masked-array handling — verified live 2026-07-12). Working on the raw
    array here has the same effect.
    """
    a = warped.GetRasterBand(1).ReadAsArray()
    valid = a != np.float32(NODATA)

    # Expression order matters: ((A-dmin)*254.0)/(dmax-dmin) in float32,
    # and np.round's round-half-to-even, are what gdal_calc evaluated.
    v = (a - np.float32(dmin)) * np.float32(254.0) / np.float32(dmax - dmin)
    gray = np.where(valid, np.minimum(255, np.maximum(1, 1 + np.round(v))), 0)
    gray = gray.astype(np.uint8)
    alpha = np.where(valid, 255, 0).astype(np.uint8)

    mem = gdal.GetDriverByName("MEM").Create(
        "", warped.RasterXSize, warped.RasterYSize, 2, gdal.GDT_Byte
    )
    mem.GetRasterBand(1).WriteArray(gray)
    mem.GetRasterBand(2).WriteArray(alpha)
    png = gdal.GetDriverByName("PNG")
    png.CreateCopy(out, mem, strict=0, options=["ZLEVEL=9"])


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--grib", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--bbox", nargs=4, type=float, required=True)
    p.add_argument("--size", nargs=2, type=int, required=True)
    p.add_argument("--scale", type=float, default=1.0)
    p.add_argument("--sentinel-lt", type=float, default=0.0)
    p.add_argument("--data-min", type=float, required=True)
    p.add_argument("--data-max", type=float, required=True)
    p.add_argument(
        "--warped-tif",
        help="also write the warped Float32 grid here (sample_point_values.py "
        "reads it; skipped for products with no value grid)",
    )
    p.add_argument("--clr", help="accepted and ignored — data-PNG path only")
    args = p.parse_args()

    warped = build(args)
    if args.warped_tif:
        gdal.GetDriverByName("GTiff").CreateCopy(args.warped_tif, warped)
    to_png(warped, args.out, args.data_min, args.data_max)
    return 0


if __name__ == "__main__":
    sys.exit(main())
