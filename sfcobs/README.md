# Surface Obs feed (`sfcobs/v1/`)

Non-airport surface stations for the app's Surface Obs layer (formerly
METARs): RAWS, NWS HADS, state DOT / ag / university mesonets and CWOP
citizen stations, from the NOAA MADIS public feed, restriction category 3
only. The full rationale (licensing, QC, age, dedupe, rank) is the docstring
of `sfcobs_bake.py`.

| Object | Cache-Control | What |
|---|---|---|
| `models.dgwaynes.com/sfcobs/v1/stations.json.gz` | max-age=60 | real gzip (`application/gzip`, no Content-Encoding), the app gunzips it |
| `models.dgwaynes.com/sfcobs/v1/manifest.json` | max-age=30 | `generatedAt`, counts, bytes, sha1, MADIS hours used, host |

`stations.json.gz` is columnar: `fields` names the columns of every row in
`rows`:
`id, name, type, net, lat, lon, elev, obs, t, td, rh, wd, ws, wg, pr, pp, rank`.
Units are in `units` (C, %, deg from, kt, mm, m, unix seconds). `type` is
`a` airport / `r` RAWS / `g` gov & mesonet / `c` citizen. `net` keys into
`networks` (display names). `pp` is the precip period (`24h`, `1h`,
`local-midnight`, ...); `pr` is null when the provider's period is unknown.
`rank` 2..5 shows from zoom `rankMinZoom[rank]` (5, 7, 8, 9); ranks 0-1 are
reserved for AWC airports, which the app ranks itself.

## Writers

* **Primary: Astrol VPS** (199.127.60.94), system unit `stp-sfcobs.timer`
  every 5 min (`*:4/5`), `User=ubuntu`. Runs from its own sparse clone
  `~/stp-sfcobs/renderer` (this dir only), so it never moves the shared
  `~/storm-spotter-models-renderer` checkout vol3d runs from. Cache, logs and
  the last local output live in `~/stp-sfcobs/`. A warm tick is ~2-5 s; a
  cold one (new hour) ~40 s, 1.0 GB peak RSS.
* **Fallback: `.github/workflows/sfcobs.yml`**, no schedule. The
  `stp-models-cron` Worker dispatches it when the manifest is >15 min old and
  no run is queued or in progress (same dead-man as lightning).
* **Monitor:** `freshness_monitor.yml` fails past 20 min and asserts the
  data file keeps its own Cache-Control.

## Deploy / update on the box

```bash
# first time
mkdir -p ~/stp-sfcobs && cd ~/stp-sfcobs
git clone --filter=blob:none --sparse https://github.com/Dgwayne/storm-spotter-models-renderer.git renderer
git -C renderer sparse-checkout set sfcobs
cp renderer/sfcobs/env.sh.example env.sh
sudo install -m 0644 renderer/sfcobs/stp-sfcobs.{service,timer} /etc/systemd/system/
sudo systemctl daemon-reload && sudo systemctl enable --now stp-sfcobs.timer

# update
git -C ~/stp-sfcobs/renderer pull --ff-only
```

The renderer micromamba env needs `netcdf4` (added to Astrol's `renderer`
env 2026-10-16: pure addition, 6 packages, libnetcdf was already there for
GDAL).

## Checking it

```bash
tail ~/stp-sfcobs/logs/ticks.log          # one line per tick
cat ~/stp-sfcobs/logs/last.log            # full output of the last tick
python3 sfcobs/verify_feed.py PSGT2 GDBT2 # live CDN audit: freshness, licensing, stations
```

`verify_feed.py` exits 1 if any restricted provider (OK Mesonet, West Texas
Mesonet, NYS Mesonet, every other non-category-3 provider) or any station
the MADIS station table tags with a category-4 SYNOPTIC_N subprovider
(WTEXAS, DAVIS, APS, ISWS, KSU-MESO) is in the published file.

Note: PSST2 (Pine Springs, Guadalupe Mountains NP) is a **West Texas
Mesonet** station (MADIS station table network `WTEXAS`, category 4). It is
not in the public MADIS feed and must not be added from another source
either. Its four RAWS neighbours (PSGT2, GDBT2, DGCT2, PXWT2) are category 3
and ship.
