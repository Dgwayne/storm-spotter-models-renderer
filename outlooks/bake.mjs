// Outlook cache bake. Run by .github/workflows/outlooks.yml.
//
// For every product in products.mjs whose change probe disagrees with the
// manifest (or every product named in FORCE), this:
//   1. fetches the full source (ArcGIS query as GeoJSON, or KML converted),
//   2. writes up to three zoom tiers, simplified TOPOLOGICALLY by mapshaper so
//      neighbouring bands keep sharing one border (no gaps, no slivers),
//   3. uploads them under content-addressed keys (immutable, edge-cacheable),
//   4. rewrites outlooks/v1/manifest.json (max-age=60) to point at them,
//   5. deletes all but the current and previous generation of each product.
//
// The app reads the manifest, picks a tier by zoom, and falls back to the
// direct NOAA fetch whenever the cache is missing or stale.
//
// Env: R2_BUCKET (the rclone "r2:" remote is configured by the workflow and
// points at B2), FORCE (optional: "all" or comma-separated ids to re-bake
// even when unchanged).

import { execFileSync } from "node:child_process";
import { mkdirSync, writeFileSync, rmSync } from "node:fs";
import { dirname, join } from "node:path";
import mapshaper from "mapshaper";
import { kml as kmlToGeoJson } from "@tmcw/togeojson";
import { DOMParser } from "@xmldom/xmldom";
import { PRODUCTS, PREFIX, MANIFEST_KEY } from "./products.mjs";
import { probe, USER_AGENT } from "./probe.mjs";

// Zoom tiers. Each tier is shown from `minZoom` up to the next tier's
// minZoom, and its tolerance is at most ~0.45 screen pixels at the MOST
// zoomed-in point of that range. Metres per pixel at Mapbox zoom z (512 px
// tiles) is 78271 * cos(lat) / 2^z; latitude 25 N is the worst case in CONUS.
//   z0 tier, shown below z5: >= 2217 m/px there, so 1000 m = 0.45 px.
//   z5 tier, shown z5-z7:    >=  554 m/px there, so  250 m = 0.45 px.
//   z7 tier, shown z7 and up: the source as the app fetches it today
//                             (0.0001 deg, ~11 m), no simplification.
// Mapbox itself simplifies GeoJSON at 0.375 px per zoom before drawing, so a
// tier under half a pixel looks the same as the full data at that zoom.
// Checked by eye on 2026-09-25 against full resolution (Chesapeake, Gulf
// coast, Florida QPF); 1000 m was visibly softer at z6.3, which is why the
// national tier stops at z5.
const TIERS = [
  { name: "z0", minZoom: 0, interval: 1000, precision: 0.0005 },
  { name: "z5", minZoom: 5, interval: 250, precision: 0.0002 },
  { name: "z7", minZoom: 7, interval: 0, precision: 0.0001 },
];
// A coarser tier is only worth a separate file when it is meaningfully
// smaller than the next finer one; otherwise that finer file serves both
// ranges (typical for small SPC-style outlooks with a few hundred vertices).
const TIER_KEEP_RATIO = 0.8;

const IMMUTABLE_CC = "public, max-age=31536000, immutable";
// The manifest is the only object that changes in place. 60 s bounds how
// stale an edge copy can be; the Worker and freshness monitor cache-bust.
const MANIFEST_CC = "public, max-age=60";

// DRY_RUN=1 bakes into work/ and touches no storage (local testing without
// B2 credentials): every rclone call is skipped and the manifest starts empty.
const DRY_RUN = process.env.DRY_RUN === "1";
const BUCKET = process.env.R2_BUCKET || (DRY_RUN ? "dry-run" : "");
if (!BUCKET) throw new Error("R2_BUCKET is not set");
const FORCE = (process.env.FORCE || "").trim();
const WORK = "work/outlooks";
const STAGE = join(WORK, "upload");

function rclone(args, opts = {}) {
  if (DRY_RUN) {
    if (args[0] === "cat") throw new Error("dry run");
    console.log(`[dry-run] rclone ${args.join(" ")}`);
    return "";
  }
  return execFileSync("rclone", args, { encoding: "utf8", maxBuffer: 64 << 20, ...opts });
}

function loadManifest() {
  try {
    return JSON.parse(rclone(["cat", `r2:${BUCKET}/${MANIFEST_KEY}`], { stdio: ["ignore", "pipe", "ignore"] }));
  } catch {
    console.log("no manifest yet, starting fresh");
    return { v: 1, products: {} };
  }
}

async function fetchSource(p) {
  if (p.kind === "kml") {
    const r = await fetch(p.url, { headers: USER_AGENT });
    if (!r.ok) throw new Error(`${p.id}: HTTP ${r.status}`);
    const doc = new DOMParser().parseFromString(await r.text(), "text/xml");
    const fc = kmlToGeoJson(doc);
    // KML carries only its styles, no issuance fields. `description` is a
    // full HTML page per polygon on WPC's files (several KB each); the name
    // and style colours are what the app draws.
    fc.features = fc.features.filter((f) => f.geometry);
    for (const f of fc.features) delete f.properties?.description;
    return fc;
  }
  const url =
    `${p.url}/query?where=1%3D1&outFields=*&f=geojson&geometryPrecision=4`;
  const r = await fetch(url, { headers: USER_AGENT });
  if (!r.ok) throw new Error(`${p.id}: HTTP ${r.status}`);
  const fc = await r.json();
  if (fc.error) throw new Error(`${p.id}: ArcGIS error ${fc.error.code} ${fc.error.message}`);
  if (fc.type !== "FeatureCollection" || !Array.isArray(fc.features)) {
    throw new Error(`${p.id}: not a FeatureCollection`);
  }
  // Paging is not implemented because no outlook comes near the 2000-record
  // limit; a truncated outlook must never be published as if complete.
  if (fc.exceededTransferLimit || fc.properties?.exceededTransferLimit) {
    throw new Error(`${p.id}: exceededTransferLimit (${fc.features.length} features)`);
  }
  for (const f of fc.features) {
    if (f.properties) {
      delete f.properties["st_area(shape)"];
      delete f.properties["st_perimeter(shape)"];
      // ArcGIS Online (USDM) names these differently.
      delete f.properties["Shape__Area"];
      delete f.properties["Shape__Length"];
    }
  }
  return fc;
}

// Features without geometry (ArcGIS's "no risk areas" placeholder, dn:0) are
// kept as-is: the app's parsers already skip them, and dropping them here
// would change what an empty outlook looks like to those parsers.
async function buildTiers(fc) {
  const src = JSON.stringify(fc);
  const out = [];
  for (const t of TIERS) {
    const simplify = t.interval > 0 ? `-simplify interval=${t.interval} keep-shapes ` : "";
    const res = await mapshaper.applyCommands(
      `-i in.json ${simplify}-o out.json format=geojson precision=${t.precision}`,
      { "in.json": src },
    );
    const text = res["out.json"].toString();
    out.push({ ...t, text, bytes: Buffer.byteLength(text) });
  }
  // Drop coarse tiers that don't save enough; the next kept finer tier
  // inherits the dropped tier's minZoom so every zoom stays covered.
  const kept = [];
  for (let i = TIERS.length - 1; i >= 0; i--) {
    const t = out[i];
    const finer = kept[0];
    if (finer && t.bytes > finer.bytes * TIER_KEEP_RATIO) {
      finer.minZoom = t.minZoom;
      continue;
    }
    kept.unshift(t);
  }
  return kept;
}

async function pool(items, n, fn) {
  const results = [];
  let next = 0;
  const workers = Array.from({ length: Math.min(n, items.length) }, async () => {
    while (next < items.length) {
      const i = next++;
      results[i] = await fn(items[i]);
    }
  });
  await Promise.all(workers);
  return results;
}

async function main() {
  const t0 = Date.now();
  rmSync(WORK, { recursive: true, force: true });
  mkdirSync(STAGE, { recursive: true });

  const manifest = loadManifest();
  manifest.products ||= {};
  // id -> the probe sig a bake FAILED on. The Worker treats a probe equal to
  // this as "nothing new", so a persistently broken layer is retried when
  // NOAA publishes again (or on the hourly backstop), not every 5 minutes.
  manifest.failed ||= {};
  const forced = FORCE === "all" ? new Set(PRODUCTS.map((p) => p.id)) : new Set(FORCE.split(",").map((s) => s.trim()).filter(Boolean));

  // 1. Probe everything (cheap, parallel) and decide what to bake.
  const probed = await pool(PRODUCTS, 16, async (p) => {
    try {
      return { p, sig: await probe(p) };
    } catch (e) {
      console.log(`probe failed: ${e.message}`);
      return { p, sig: null };
    }
  });
  const todo = probed.filter(({ p, sig }) => sig && (forced.has(p.id) || manifest.products[p.id]?.sig !== sig));
  console.log(`probed ${PRODUCTS.length}, ${todo.length} to bake${forced.size ? ` (${forced.size} forced)` : ""}`);

  // 2. Fetch + tier. Four at a time: the heavy sources are 10-25 MB of JSON.
  const baked = [];
  const failed = [];
  await pool(todo, 4, async ({ p, sig }) => {
    const t1 = Date.now();
    try {
      const fc = await fetchSource(p);
      const tiers = await buildTiers(fc);
      const entryTiers = tiers.map((t) => {
        const key = `${PREFIX}/${p.id}/${sig}.${t.name}.json`;
        const path = join(STAGE, key);
        mkdirSync(dirname(path), { recursive: true });
        writeFileSync(path, t.text);
        return { minZoom: t.minZoom, key, bytes: t.bytes };
      });
      baked.push({
        id: p.id,
        prevSig: manifest.products[p.id]?.sig,
        entry: {
          sig,
          bakedAt: new Date().toISOString(),
          features: fc.features.length,
          tiers: entryTiers,
        },
      });
      console.log(
        `baked ${p.id} sig=${sig} features=${fc.features.length} ` +
          entryTiers.map((t) => `z${t.minZoom}:${(t.bytes / 1024).toFixed(0)}K`).join(" ") +
          ` ${Date.now() - t1}ms`,
      );
    } catch (e) {
      failed.push({ id: p.id, sig });
      console.log(`::warning::bake failed ${p.id}: ${e.message}`);
    }
  });

  for (const b of baked) delete manifest.failed[b.id];
  for (const f of failed) manifest.failed[f.id] = f.sig;

  if (baked.length) {
    // 3. Upload the new tier files in one rclone call (content-addressed, so
    // an existing key is already the right bytes).
    rclone([
      "copy", STAGE, `r2:${BUCKET}`,
      "--header-upload", `Cache-Control: ${IMMUTABLE_CC}`,
      "--header-upload", "Content-Type: application/json",
      "--size-only", "--transfers", "16",
    ], { stdio: "inherit" });

    // 5. Keep current + previous generation (a client holding a 60 s old
    // manifest can still load its files); delete anything older.
    const doomed = [];
    for (const b of baked) {
      const keep = new Set([b.entry.sig, b.prevSig].filter(Boolean));
      let names = [];
      try {
        names = rclone(["lsf", `r2:${BUCKET}/${PREFIX}/${b.id}/`]).split("\n").filter(Boolean);
      } catch (e) {
        console.log(`list failed for ${b.id}: ${e.message}`);
      }
      for (const n of names) {
        if (!keep.has(n.split(".")[0])) doomed.push(`${PREFIX}/${b.id}/${n}`);
      }
    }
    if (doomed.length) {
      const listPath = join(WORK, "doomed.txt");
      writeFileSync(listPath, doomed.join("\n") + "\n");
      rclone(["delete", `r2:${BUCKET}`, "--files-from", listPath], { stdio: "inherit" });
      console.log(`deleted ${doomed.length} superseded files`);
    }
  }

  // 4. Manifest, rewritten on EVERY run, including runs that baked nothing.
  // generatedAt means "the cache was last checked against NOAA at", which is
  // the app's liveness signal: past its limit the app stops trusting the
  // cache and fetches NOAA directly. The hourly backstop keeps it moving on
  // a quiet day, and the freshness monitor watches it. Written after the
  // uploads above, so it never points at a file that isn't there yet; the
  // superseded files deleted above were already unreferenced (previous
  // generation kept), so that order is safe too.
  for (const b of baked) manifest.products[b.id] = b.entry;
  manifest.v = 1;
  manifest.generatedAt = new Date().toISOString();
  if (baked.length) manifest.changedAt = manifest.generatedAt;
  const mpath = join(WORK, "manifest.json");
  writeFileSync(mpath, JSON.stringify(manifest));
  rclone([
    "copyto", mpath, `r2:${BUCKET}/${MANIFEST_KEY}`,
    "--header-upload", `Cache-Control: ${MANIFEST_CC}`,
    "--header-upload", "Content-Type: application/json",
  ], { stdio: "inherit" });

  console.log(`done: ${baked.length} baked, ${failed.length} failed, ${((Date.now() - t0) / 1000).toFixed(1)} s`);
  // One flaky NOAA layer should not email anyone; a broken pipeline should.
  if (failed.length && failed.length >= todo.length / 2) {
    throw new Error(`${failed.length}/${todo.length} bakes failed: ${failed.map((f) => f.id).join(", ")}`);
  }
}

await main();
