# Air Quality (EPA AirNow) feed + animation

EPA AirNow AQI for Spotter Tools Pro, built on the **same** B2 bucket +
domain + secrets as the model renderer. Adds one new secret (`AIRNOW_API_KEY`).

## What it is

- **Source:** EPA AirNow `aq/data` API — regulatory-grade AQI from ~1,900
  state/local/tribal monitors across CONUS. Free, but key-gated and rate
  limited, so the key lives here (a GitHub secret), never in the app.
- **Why the renderer (not a Cloudflare Worker):** assembling a 24 h
  nationwide series + rasterising 72 contour frames blows past the Workers
  free CPU limit. Actions runners have no cap; public-repo minutes + B2
  (free uploads, free egress) keep it $0.
- **Cadence:** a 20-min cron. AirNow publishes hourly, so that's as fresh
  as the data ever gets; a skipped tick just means the next catches up.

## Outputs (on `models.dgwaynes.com`)

| Key | Role |
|-----|------|
| `airquality/v1/conus.json` | Latest AQI per monitor+pollutant — the live dots layer |
| `airquality/v1/loop24.json` | Per-monitor 24 h hourly series — the animated dots |
| `airquality/v1/contours/<view>/F<idx>.png` | Hourly shaded-surface PNGs (`view` = combined / pm25 / ozone) |
| `airquality/v1/loop_manifest.json` | frames / hours / bbox + all the URLs above |

Contours are an inverse-distance interpolation (`gdal_grid`) of the point
AQI, colour-relieved with `config/color_tables/aqi.clr` and reprojected to
the same EPSG:3857 CONUS bbox as the model frames, so the app drapes them
exactly like a model frame.

## Pieces

| File | Role |
|------|------|
| `scripts/airquality.py` | Fetch AirNow 24 h, build JSON feeds, render + upload contours |
| `config/color_tables/aqi.clr` | EPA AQI category colour ramp |
| `.github/workflows/airquality.yml` | 20-min cron; gdal + rclone; runs the script |

## Deploy

1. **Add the AirNow key as a secret** (the only new setup — the four B2
   secrets are already shared with the renderer). Use the **same free key**
   you used for the app's `airnow-worker`, from
   <https://docs.airnowapi.org/account/request/>:
   ```bash
   gh secret set AIRNOW_API_KEY -R Dgwayne/storm-spotter-models-renderer
   # paste the key when prompted (it goes straight to GitHub, never echoed)
   ```
   (Or GitHub → repo → Settings → Secrets and variables → Actions → New
   secret, name `AIRNOW_API_KEY`.)

2. Commit + push `scripts/airquality.py`, `config/color_tables/aqi.clr`,
   and `.github/workflows/airquality.yml` to `main`.

3. GitHub → Actions → **Air Quality (AirNow)** → **Run workflow** to kick
   the first bake (or wait for the next 20-min tick).

4. After ~1 min, verify:
   ```bash
   curl https://models.dgwaynes.com/airquality/v1/loop_manifest.json | head -c 400
   curl -sI https://models.dgwaynes.com/airquality/v1/contours/combined/F23.png | head -1
   ```
   The manifest should list 24 `hours` and non-null contour URLs; the PNG
   HEAD should be `200`.

## Retiring the standalone Worker (optional)

Once this is live, `airquality/v1/conus.json` here fully replaces the
`airnow-worker` live feed. Point the app's `AppConstants.airQualityUrl` at
`https://models.dgwaynes.com/airquality/v1/conus.json` and the Worker (and
its separate AirNow key) can be deleted — one pipeline, one key.

## Tuning knobs (top of `scripts/airquality.py`)

- `GRID_ALG` — `gdal_grid` IDW params. `radius` (degrees) bounds how far a
  monitor's influence reaches; raise it to fill more of the sparse West,
  lower it to hug the monitors more tightly.
- `IMG_W`/`IMG_H` — contour resolution. Smooth surfaces, so 1024² is plenty.
- `VIEWS` — drop `pm25`/`ozone` to bake `combined` only (8× fewer frames).
- `WINDOW_HOURS` — history requested before deduping to 24 hourly buckets.

## Notes

- AirNow AQI is the reported/hourly value (a close stand-in for the NowCast
  weighted average AirNow's own loop uses; matching NowCast exactly is a
  small formula refinement if ever wanted).
- Contours paint only within `radius` of a monitor, so sparse-West gaps are
  transparent by design rather than extrapolated — honest coverage.
