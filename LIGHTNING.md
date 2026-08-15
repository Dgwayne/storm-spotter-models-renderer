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
- **Output:** one small file, rebuilt every ~15-20 s:
  `https://models.dgwaynes.com/lightning/v1/flashes.json`
- **Cadence:** a 2-hourly cron starts a ~2h30m loop that rebuilds the
  feed every ~15-20 s (7 s sleep + pass work). Runs deliberately
  **overlap** (~30 min, no concurrency group): the old
  `cancel-in-progress` handoff froze the feed for the whole time the
  successor sat in the runner queue (11+ min measured). Two overlapping
  writers are harmless — each pass rebuilds the full window, last write
  wins. The `stp-models-cron` Worker dead-mans the feed on every tick
  (`generatedAt` > 5 min stale + no queued/running lightning run →
  dispatch), and `freshness_monitor.yml` emails at 30 min stale.
- **Cost:** $0. Public repo = unlimited Actions minutes; the JSON is a
  few hundred KB (up to ~0.7 MB on an active day, after the service-area
  clip) and egress is free.

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
| `.github/workflows/lightning.yml` | 2-h cron; ~2h30m internal loop; overlapping handoff |
| `cron-trigger/src/worker.js` | Dead-man: re-dispatches the loop if the feed stalls >5 min |
| `.github/workflows/freshness_monitor.yml` | Emails the owner if the feed is >30 min stale |

## Deploy

1. Commit `scripts/lightning_glm.py` and `.github/workflows/lightning.yml`
   to this repo and push to `main`.
2. The four R2 secrets are **already set** (shared with the renderer):
   `R2_BUCKET`, `R2_ENDPOINT`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`.
   Nothing to add.
3. GitHub → Actions → **Lightning (GLM)** → **Run workflow** to kick the
   first run. **After a code change:** there is no `cancel-in-progress`
   anymore — an already-running loop keeps executing OLD code until its
   END (≤2h30m). To cut over immediately: `gh run cancel <old run>`,
   then dispatch.
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
- `COVERAGE_LAT`, `COVERAGE_LON`: service-area clip applied to every flash
  before dedup/cap (default CONUS + Gulf + Caribbean + near-offshore,
  lat 15..55 / lon -130..-60). Keeps the feed and the `MAX_FLASHES` cap
  from carrying hemisphere lightning a US user can't see (the unclipped
  feed was ~1.7 MB and let far-off storms crowd out US flashes).
  Independent of the E/W split and the outage fallback; widen the box for
  Alaska/Hawaii users.
- `MAX_FLASHES` — hard safety cap on payload size.
- `EAST_BUCKETS`, `WEST_BUCKETS` — candidate buckets, newest sat first;
  survives a GOES satellite swap automatically.

## Notes / caveats

- GLM is **total** lightning (IC + CG) seen optically from orbit: great
  for "where's the lightning / is this cell intensifying," with slight
  parallax offset and somewhat lower daytime detection efficiency vs a
  ground CG network.
- The satellites see the Western Hemisphere full disk, but the feed is
  clipped to the app's US service area (`COVERAGE_LAT`/`COVERAGE_LON`):
  CONUS + Gulf + Caribbean + near-offshore. Alaska and Hawaii are outside
  the default box; widen it if the user base grows there.
- The loop is a legitimate data pipeline, not a hosted server; it stays
  well within GitHub Actions and R2 free tiers.
