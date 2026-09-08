# MRMS 3D volume tiles (`v1/VOL3D/`)

The app's **3D** radar chip ray-marches a uint8 voxel brick. Its phase 1+2
producer builds that brick on-device from one radar's Level II volume. This
pipeline is the phase 3 producer: the same voxel encodings, cut from NOAA's
national MRMS 3D reflectivity mosaic, so the volume works anywhere in the
lower 48, off any radar product, with no site selection and no wait for a
scan to deepen.

| | |
|---|---|
| script | `scripts/mrms_volume_tiles.py` (`--selftest` needs numpy only) |
| runs on | Box 2 (`deploy/box2/run-vol3d.sh`, user timer `stp-vol3d.timer`, every 2 min) |
| source | `noaa-mrms-pds` `CONUS/MergedReflectivityQC_<lvl>/` (33 CAPPIs 0.5–19 km), `MergedRhoHV_<lvl>/` to 8 km, `MergedAzShear_0-2kmAGL_00.50/`, `MergedAzShear_3-6kmAGL_00.50/` |
| output | `v1/VOL3D/latest.json` (pointer, `max-age=20`), `v1/VOL3D/<stamp>/index.json` + `r<RR>c<CC>.rvt.gz` (immutable, `max-age=86400`) |
| tile | 2°×2°, 200×200 cells × 38 levels of 500 m, planes REF / CC / rotation, gzip; an **empty tile is not written** |
| retention | `VOL3D_RETAIN_MIN` (120) minutes of stamps, pruned each tick |
| measured | 2026-09-08 quiet night: 44 s/tick, 3.1 GB peak RSS, 39 MB in, 16 MB out, 208 lit tiles |

The tile wire format (`'RVT1'`), the voxel encodings and the debris rule are
documented at the top of the script and mirrored by the app's
`lib/data/nexrad/gpu/radar_volume_mosaic.dart`. Keep the two in lock-step.

## Gotchas found while building it

- **AzShear is on the 0.005° grid** (14000×7000) and published in
  `[0.001/s]` as integers. The script reads the unit off the GRIB, decimates
  2:1 by mean and smooths 3×3 (~3 km box, the on-device builder's own
  3-radial × 5-gate box mean) so the 0.006 1/s display grade means the same
  thing on both sources. Raw per-cell AzShear at that grade lit 90k
  "rotation" voxels in one tile: cyan confetti on screen.
- RhoHV levels and AzShear publish a little staggered from the CAPPIs; a
  companion is accepted within −6 min / +90 s of the reflectivity stamp.
- The 33 CAPPIs of a cycle land over ~1 min. The stamp chosen is the newest
  present in **every** level, so a half-landed cycle waits one tick.
- Memory: the CONUS volume is 931 MB as uint8 (38 levels) and a decoded
  CAPPI is ~100 MB as float32 plus GDAL's own float64 buffer. The natives
  are streamed bottom-up with a 3-deep look-ahead; do not "simplify" it
  into decode-everything-then-interpolate (that is ~4 GB).

## Client side

The app polls `latest.json` every 30 s, picks the 2×2 (phone) or 3×3
(desktop) block of tiles around the camera, fetches only those, and
stitches them into one brick with a Mercator row remap (tiles are
lat-uniform, the brick is Mercator-uniform). The Source option in the 3D
chip switches between this and the radar-site volume.
