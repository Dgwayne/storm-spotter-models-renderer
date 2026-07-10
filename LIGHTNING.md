# Lightning (GOES GLM) feed

Near-real-time lightning for Spotter Tools Pro, built on the **same** R2
bucket + domain + secrets as the model renderer. **No new Cloudflare
setup.**

## What it is

- **Source:** GOES Geostationary Lightning Mapper (GLM) L2 flashes, read
  anonymously from NOAA's open AWS buckets (`noaa-goes19` East,
  `noaa-goes18` West). Public domain, commercial use OK, no key.
  *(Blitzortung/LightningMaps are NOT usable — they forbid commercial and
  storm-warning use, even via a proxy.)*
- **Output:** one small file, rebuilt every ~15 s:
  `https://models.dgwaynes.com/lightning/v1/flashes.json`
- **Cadence:** a 2-hourly cron restarts a long-running (~5h50m) loop that
  rebuilds the feed every ~15 s (`cancel-in-progress` handles the
  handoff). Short crons are unreliable on GitHub; the long loop keeps the
  feed fresh even if a scheduled tick is skipped.
- **Cost:** $0. Public repo = unlimited Actions minutes; the JSON is a
  few hundred KB at most and R2 egress is free.

## File format

```json
{
  "schemaVersion": 1,
  "generatedAt": "2026-06-18T20:31:05Z",
  "windowMinutes": 60,
  "count": 1234,
  "flashes": [ [39.123, -104.567, 1718742660] ]   // [lat, lon, epochSec]
}
```

Each flash carries its own UTC epoch, so the **app** does age-fading and
expiry. That means we can change backend cadence (or the window) without
shipping an app update.

## Pieces

| File | Role |
|------|------|
| `scripts/lightning_glm.py` | Fetch GLM granules, parse flashes, dedup, push to R2 |
| `.github/workflows/lightning.yml` | 2-h cron; ~5h50m internal loop; `cancel-in-progress` |

## Deploy

1. Commit `scripts/lightning_glm.py` and `.github/workflows/lightning.yml`
   to this repo and push to `main`.
2. The four R2 secrets are **already set** (shared with the renderer):
   `R2_BUCKET`, `R2_ENDPOINT`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`.
   Nothing to add.
3. GitHub → Actions → **Lightning (GLM)** → **Run workflow** to kick the
   first run (or wait for the next 2-h tick; `cancel-in-progress`
   supersedes the running loop either way).
4. After ~1 min, verify:
   ```bash
   curl https://models.dgwaynes.com/lightning/v1/flashes.json | head -c 400
   ```
   You should see `"schemaVersion":1` and a non-empty `flashes` array
   whenever there's active lightning in the Western Hemisphere. (When the
   sky is quiet nationwide, `count` can legitimately be low or 0.)

## Tuning knobs (top of `lightning_glm.py`)

- `WINDOW_MIN` — rolling window / max flash age shown (default 60, so
  radar-loop sync in the app has lightning for the oldest frames of a
  ~10-frame volume-scan loop).
- `GRID_DEG`, `TIME_BUCKET_SEC` — dedup density for the recent
  (`RECENT_MIN`) half of the window (default ~1.1 km / 30 s).
- `GRID_DEG_OLD`, `TIME_BUCKET_OLD_SEC` — coarser dedup for flashes older
  than `RECENT_MIN`; they only feed the app's per-radar-frame slicing, so
  thinning them bounds payload growth.
- `CUTOFF_LON` — East/West satellite split meridian (default -106).
- `MAX_FLASHES` — hard safety cap on payload size.
- `EAST_BUCKETS`, `WEST_BUCKETS` — candidate buckets, newest sat first;
  survives a GOES satellite swap automatically.

## Notes / caveats

- GLM is **total** lightning (IC + CG) seen optically from orbit: great
  for "where's the lightning / is this cell intensifying," with slight
  parallax offset and somewhat lower daytime detection efficiency vs a
  ground CG network.
- Coverage is the Western Hemisphere full disk — all of CONUS, AK-adjacent,
  HI, Gulf, Caribbean, oceans.
- The loop is a legitimate data pipeline, not a hosted server; it stays
  well within GitHub Actions and R2 free tiers.
