# Wind Soundings (velocity dealias reference)

`scripts/extract_soundings.py` (workflow: `Extract Soundings`, hourly)
publishes one tiny JSON per CONUS NEXRAD site to R2 under `v1/soundings/`.
The Storm Spotter Tools Pro velocity dealiaser fetches these as a trusted
**reference wind** — independent of the aliased Doppler data — to unfold
far-range / low-Nyquist velocity correctly.

It reuses the same machinery as the image renderer: the public NOAA HRRR
S3 bucket, the `.idx` byte-range trick (only the ~18 matching messages are
downloaded), `wgrib2 -lon` point extraction, and `rclone` → Cloudflare R2.
Costs $0/month on the existing Actions cron + R2 setup.

## URLs

```
https://models.dgwaynes.com/v1/soundings/manifest.json
https://models.dgwaynes.com/v1/soundings/<SITE>.json      e.g. KEAX.json
```

`Cache-Control: public, max-age=600` (10 min). CONUS sites only (HRRR
coverage); AK/HI/PR/Guam fall back to the app's self-contained VAD profile.

## Per-site schema (`KEAX.json`)

```json
{
  "site": "KEAX",
  "model": "HRRR",
  "run": "2026-06-10T10:00:00+00:00",
  "levels_mb":  [1000, 925, 850, 700, 500, 250],
  "hgt_msl_m":  [310.2, 1012.5, 1498.1, 3120.7, 5810.3, 10720.9],
  "u_ms":       [4.2, 8.1, 12.6, 18.0, 24.3, 31.5],
  "v_ms":       [3.0, 5.5, 7.2, 9.1, 6.8, 2.1]
}
```

- `hgt_msl_m` is geopotential height (metres MSL). The app converts to
  height-above-radar with `agl = hgt_msl - siteElevationM` (the radar
  altitude carried in each Level-2 volume).
- `u_ms` / `v_ms` are eastward / northward wind components (m/s). The
  expected radial wind at a gate is `(u·sinφ + v·cosφ)·cos(elev)`.
- Arrays are index-aligned with `levels_mb`, ordered low → high altitude.

## manifest.json

```json
{ "schemaVersion": 1, "model": "HRRR", "run": "...", "generatedAt": "...",
  "levelsMb": [1000,925,850,700,500,250], "sites": 146 }
```
