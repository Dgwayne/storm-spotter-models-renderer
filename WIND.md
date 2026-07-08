# Wind Field (RTMA) — runbook

Feeds the app's animated wind-particle map layer.

## What it publishes

- `https://models.dgwaynes.com/wind/v1/latest.json` — manifest:
  analysis time, source (`rtma2p5_ru` 15-min / `rtma2p5` hourly
  fallback), PNG key, grid dims, lonlat + mercator bounds,
  quantization (`scale`, `bias`, m/s).
- `https://models.dgwaynes.com/wind/v1/uv_<stamp>_<source>.png` —
  RGBA field in EPSG:3857 over the standard CONUS bbox
  (-134, 21, -60, 53). R = U component, G = V component
  (`value = (byte - bias) * scale` m/s), A = 0 marks no-data
  (outside the RTMA NDFD grid). Immutable per analysis; old ones
  pruned (keep 8).

## Data source

RTMA = NCEP's 2.5 km analysis of *observations* (METARs, mesonets,
satellite winds) — actual current wind, not a forecast. Public domain,
anonymous reads from `noaa-rtma-pds` on AWS Open Data. RU analysis
lands ~20-30 min behind valid time.

## Why wgrib2 sits in the middle

RTMA's Lambert grid stores GRID-relative U/V. `wgrib2 -new_grid_winds
earth -new_grid latlon ...` rotates to true east/north while regridding;
skipping it would skew particle directions increasingly toward the
coasts. gdalwarp then reprojects the earth-relative scalars to web
mercator so the client's texture lookup is linear in mercator space.

## Ops

- Workflow: `.github/workflows/wind.yml` — hourly cron restarts a
  ~55-min loop (pass every 3 min; idle pass ≈ 2 s). Same
  GitHub-short-crons-are-unreliable mitigation as `lightning.yml`.
- Manual run: Actions → "Wind Field (RTMA)" → Run workflow, or
  `gh workflow run wind.yml`.
- Health check: `curl https://models.dgwaynes.com/wind/v1/latest.json`
  — `analysisTime` should be within ~45 min of now.
- Script: `scripts/wind_field.py` (stateless; safe to run repeatedly).

## Future

- Alaska/Hawaii: `akrtma`/`hirtma` products exist in the same bucket —
  add sibling fields if the app ever spawns particles there.
- Global oceans: GFS f00 10 m wind as a coarse fallback field under
  the RTMA CONUS field.
