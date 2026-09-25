# Outlook cache

NOAA outlooks (WPC, CPC, NDMC) baked into zoom-tiered GeoJSON on B2, served
through Cloudflare at `models.dgwaynes.com/outlooks/v1/`. The app reads this
first and falls back to the NOAA sources directly.

## Why

NOAA's ArcGIS server sends `Cache-Control: max-age=0`, so every phone's
request makes it rebuild the answer: 1.2-3.7 s and 1.6-4 MB compressed for
the heavy layers (CPC 6-10 day, seasonal drought, 7-day QPF), measured
2026-09-25. From the edge the same bytes arrive in well under a second, and
the national-view tier is a fraction of the size.

## Pieces

| File | Role |
|---|---|
| `products.mjs` | The product list (~112 layers). Shared by the bake and the Worker. |
| `probe.mjs` | Change signature per product (attribute-only ArcGIS query, or KML HEAD). Shared. |
| `bake.mjs` | Probe, fetch, tier with mapshaper, upload, manifest, prune. |
| `../.github/workflows/outlooks.yml` | Runs the bake. Hourly native backstop. |
| `../cron-trigger/src/worker.js` `checkOutlooks` | Probes every 5 min; dispatches only on change. |

## Tiers

Each product gets up to three files, chosen by the app by map zoom:

| Tier | Zoom | Simplification |
|---|---|---|
| `z0` | below 5 | 1000 m |
| `z5` | 5 to 7 | 250 m |
| `z7` | 7 and up | none (source at 0.0001 deg, as the app fetched it before) |

Each tolerance is at most ~0.45 screen px at the most zoomed-in point of its
range, below Mapbox's own 0.375 px GeoJSON simplification, so a tier draws
the same as full detail at the zoom it is used. Simplification is
**topological** (mapshaper): a border between two bands is simplified once
and shared, so bands never gap or overlap. A coarse tier is skipped when it
would not be at least 20% smaller than the next finer one.

## Storage

`outlooks/v1/<product>/<sig>.<tier>.<content hash>.json` is immutable (max-age
1 year); the content hash makes a forced re-bake with new output land on a
new key instead of overwriting one the edge may hold for a year. A
new issuance gets a new `<sig>`. The bake keeps the current and previous
generation and deletes older ones; the bucket's keep-only-last-version
lifecycle purges them. Everything, all tiers, all products, is ~470 MB raw
(~135 MB compressed on the wire), well under a cent a month on B2.

`outlooks/v1/manifest.json` (max-age 60):

```json
{
  "v": 1,
  "generatedAt": "last time a run checked NOAA (liveness)",
  "changedAt": "last time anything was baked",
  "products": {
    "wpc_qpf-11": {
      "sig": "9f2c...", "bakedAt": "...", "features": 14,
      "tiers": [{ "minZoom": 0, "key": "outlooks/v1/wpc_qpf-11/9f2c....z0.json", "bytes": 4210000 }, ...]
    }
  },
  "failed": { "<id>": "<sig that failed to bake>" }
}
```

## Operating

- Force a re-bake: run **Outlooks** with `force` = `all` or `id1,id2`.
- Local test without credentials: `cd outlooks && npm ci && DRY_RUN=1 FORCE=all node bake.mjs`
  (writes `work/outlooks/`, touches nothing).
- Adding a product: append to `products.mjs`, commit, then `wrangler deploy`
  from `cron-trigger/` so the Worker probes it too. Ids are storage keys and
  app keys: never rename one.
- Stale manifest: the freshness monitor emails at 2 h. The app ignores the
  cache past 3 h and goes to NOAA directly.
