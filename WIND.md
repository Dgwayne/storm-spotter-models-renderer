# Wind Field (RTMA + GFS) — runbook

Feeds the app's animated wind-particle map layer.

## What it publishes

- `https://models.dgwaynes.com/wind/v1/latest.json` — manifest:
  analysis time, source label (`rtma2p5_ru+gfs` / `rtma2p5+gfs`, or
  `gfs` when RTMA is briefly absent), `gfsTime`, PNG key, grid dims,
  lonlat + mercator bounds, quantization (`scale`, `bias`, m/s).
- `https://models.dgwaynes.com/wind/v1/uv_<stamp>_<src>.png` —
  RGBA field in EPSG:3857 over `FIELD_BBOX` (-140, 5, -15, 55).
  R = U component, G = V component (`value = (byte - bias) * scale`
  m/s), A = 0 marks no-data. Immutable per analysis; old ones pruned
  (keep 8).

The app is fully data-driven — it reads bounds/dims/quantization from
the manifest and advects particles through whatever field it points at.
Widening the field is a renderer-only change; the app needs nothing.
Particle *density* is tied to screen area, not field extent, so a bigger
field does not thin out the flow.

## Data sources — RTMA over land, GFS over the ocean

The published field is a composite of two analyses:

- **RTMA** = NCEP's 2.5 km CONUS analysis of *observations* (METARs,
  mesonets, satellite winds) — actual current wind. The detailed land
  field; takes precedence wherever it has data. RU lands ~20-30 min
  behind valid time.
- **GFS f000 10 m wind** = the 0.25° global analysis, used to FILL
  everything RTMA doesn't cover — Gulf, Caribbean, Atlantic MDR (Cape
  Verde), East Pacific — so ocean hurricanes render. GFS refreshes every
  6 h; the synoptic ocean flow evolves slowly, so the cadence mismatch
  with 15-min RTMA is invisible.

Both are public domain, anonymous reads from `noaa-rtma-pds` /
`noaa-gfs-bdp-pds` on AWS Open Data.

**Accuracy note:** GFS is ~25 km — it renders a hurricane's circulation
and steering flow correctly but SMOOTHS the eyewall, so particle speed
near the core reads milder than the real storm. Treat the layer as a
flow visualization; NHC / the app's Tropical layer stays the source of
truth for category and position. The RTMA↔GFS seam over CONUS coastal
water is a hard cut (no feathering yet) — see Future.

## Why wgrib2 sits in the middle

RTMA's Lambert grid stores GRID-relative U/V. `wgrib2 -new_grid_winds
earth -new_grid latlon ...` rotates to true east/north while regridding;
skipping it would skew particle directions increasingly toward the
coasts. gdalwarp then reprojects the earth-relative scalars to web
mercator so the client's texture lookup is linear in mercator space. GFS
is already on an earth-relative lat-lon grid, so the same rotation step
is a harmless identity for it — both sources share one regrid path and
land on the identical `-te`/`-ts` target, so compositing is a plain
pixel-aligned `np.where` (RTMA wins where valid, GFS fills the rest).

## Ops

- Workflow: `.github/workflows/wind.yml` — hourly cron restarts a
  ~55-min loop (pass every 3 min; idle pass ≈ 2 s). Same
  GitHub-short-crons-are-unreliable mitigation as `lightning.yml`. No
  new tooling for GFS (same wgrib2/gdal/rclone stack).
- Manual run: Actions → "Wind Field (RTMA)" → Run workflow, or
  `gh workflow run wind.yml`.
- Health check: `curl https://models.dgwaynes.com/wind/v1/latest.json`
  — `analysisTime` within ~45 min of now (RTMA), `gfsTime` within ~6-10 h
  (GFS cycle + f000 latency), `boundsLonLat` = the wide box above.
- Script: `scripts/wind_field.py` (stateless; safe to run repeatedly).

## Future

- **Feather the RTMA↔GFS seam:** blend a few px across the RTMA coverage
  edge so the 2.5 km→25 km detail change isn't a visible line in the
  flow. Hard cut is fine for a first pass.
- **Realistic core intensity:** GFS can't resolve an eyewall. HAFS
  (NOAA's operational hurricane model, ~2 km moving nest per storm) would
  make particle speed near the center honest — but it's per-storm
  plumbing that only exists while a storm is active. Big lift.
- **Alaska/Hawaii:** `akrtma`/`hirtma` products exist in the same bucket;
  GFS already covers those longitudes as the base if the box is widened.
- **Measured ocean wind reference:** ASCAT scatterometer is real
  measured surface wind, but swath-gapped (~twice-daily passes), so it's
  a truth-check, not a continuous advection field.
