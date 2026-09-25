// Outlook cache: the ONE list of products the bake and the change probe share.
//
// Both consumers import this file: scripts/bake.mjs (GitHub Actions, Node)
// and the stp-models-cron Worker (bundled by wrangler). Keep it plain data
// with no Node or Worker APIs so it loads in both.
//
// Every product is a public NOAA (or NDMC via ArcGIS Online) source the app
// could fetch directly, and the app DOES fall back to that direct fetch when
// the cache is missing or stale. The cache exists for speed (NOAA's map
// server sends max-age=0 and takes 1-4 s per heavy query) and for
// topology-aware simplification (see bake.mjs TIERS).
//
// Product id = `<service>-<layer>` for ArcGIS layers and `<group>-<name>` for
// KML files. The id is the storage key and the app's lookup key, so NEVER
// rename one; add a new id instead.
//
// probeEvery: minutes between change probes. The Worker probes on the
// 5-minute ticks; slow products (monthly/seasonal/weekly) are probed on the
// top of the hour only, which cuts the NOAA request count by ~30%.

const MS = "https://mapservices.weather.noaa.gov/vector/rest/services";

function arcgis(folder, service, layers, probeEvery = 5, base = MS) {
  return layers.map((layer) => ({
    id: `${service}-${layer}`,
    kind: "arcgis",
    url: `${base}/${folder}/${service}/MapServer/${layer}`,
    probeEvery,
  }));
}

function kml(group, baseUrl, names, probeEvery = 5) {
  return names.map((name) => ({
    id: `${group}-${name}`,
    kind: "kml",
    url: `${baseUrl}/${name}.kml`,
    probeEvery,
  }));
}

const range = (a, b) => Array.from({ length: b - a + 1 }, (_, i) => a + i);

export const PRODUCTS = [
  // WPC Excessive Rainfall Outlook, Days 1-5.
  ...arcgis("hazards", "wpc_precip_hazards", range(0, 4)),

  // WPC QPF: Day 1/2/3, Days 4-5 and 6-7 (48 h), 2/3/5/7-day totals, and the
  // 6-hour periods (13-25 = 00-06 h through 72-78 h).
  ...arcgis("precip", "wpc_qpf", [1, 2, 3, 4, 5, 8, 9, 10, 11, ...range(13, 25)]),

  // WPC Winter Storm Severity Index: overall, snow amount, snow load, ice,
  // blowing snow; each Day 1, Day 2, Day 3, Days 1-3.
  ...arcgis("outlooks", "wpc_wssi", [
    ...range(1, 4), ...range(6, 9), ...range(11, 14), ...range(16, 19), ...range(24, 27),
  ]),

  // WPC winter probabilities: snow >4", >8", >12", freezing rain >0.25";
  // Day 1 (1-4), Day 2 (6-9), Day 3 (11-14).
  ...arcgis("precip", "wpc_prob_winter_precip", [...range(1, 4), ...range(6, 9), ...range(11, 14)]),

  // CPC 6-10 and 8-14 day (0 = temperature, 1 = precipitation), daily.
  ...arcgis("outlooks", "cpc_6_10_day_outlk", [0, 1]),
  ...arcgis("outlooks", "cpc_8_14_day_outlk", [0, 1]),

  // CPC monthly and seasonal (13 overlapping 3-month leads), monthly cadence.
  ...arcgis("outlooks", "cpc_mthly_temp_outlk", [0], 60),
  ...arcgis("outlooks", "cpc_mthly_precip_outlk", [0], 60),
  ...arcgis("outlooks", "cpc_sea_temp_outlk", range(0, 12), 60),
  ...arcgis("outlooks", "cpc_sea_precip_outlk", range(0, 12), 60),

  // CPC drought outlook: 1 = monthly, 4 = seasonal (US + PR polygons).
  ...arcgis("outlooks", "cpc_drought_outlk", [1, 4], 60),

  // Hazards Outlook Days 3-7: temperature, precipitation, wildfire/drought.
  // (This service's Days 8-14 layers are empty; those come from CPC's KML.)
  ...arcgis("hazards", "cpc_weather_hazards", [1, 4, 7]),

  // US Drought Monitor (weekly, Thursdays). ArcGIS Online, not NOAA's server.
  {
    id: "usdm_current-0",
    kind: "arcgis",
    url: "https://services5.arcgis.com/0OTVzJS4K09zlixn/arcgis/rest/services/USDM_current/FeatureServer/0",
    probeEvery: 60,
  },

  // WPC Hazards Outlook Days 3-7, the parts the ArcGIS service lacks (soils,
  // flooding) plus its own copies of the rest. KML polygons.
  ...kml("wpc_threats", "https://www.wpc.ncep.noaa.gov/threats/final", [
    "Prcp_D3_7", "Temp_D3_7", "Wildfires_D3_7", "Soils_D3_7", "FloodingHazards",
  ]),

  // CPC Hazards Outlook Days 8-14 (categorical + probabilistic). KML polygons.
  ...kml("cpc_threats", "https://www.cpc.ncep.noaa.gov/products/predictions/threats", [
    "temp_D8_14", "prcp_D8_14", "snow_D8_14", "wind_D8_14", "soils_D8_14",
    "temp_prob_D8_14", "prcp_prob_D8_14", "snow_prob_D8_14", "wind_prob_D8_14",
    "excess_heat_prob_D8_14",
  ]),
];

export const PRODUCTS_BY_ID = Object.fromEntries(PRODUCTS.map((p) => [p.id, p]));

// Storage layout on the shared B2 bucket (served at models.dgwaynes.com).
export const PREFIX = "outlooks/v1";
export const MANIFEST_KEY = `${PREFIX}/manifest.json`;
export const MANIFEST_URL = `https://models.dgwaynes.com/${MANIFEST_KEY}`;
