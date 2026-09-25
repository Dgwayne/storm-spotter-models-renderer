// Change probe shared by the Worker (every 5 min) and the bake (before each
// fetch). Returns a short signature that changes whenever the product does.
//
// The two callers MUST compute the signature the same way: the bake stores
// what it saw in the manifest, and the Worker dispatches a bake whenever a
// fresh probe disagrees with the stored value. A mismatch in method would
// re-bake forever.
//
// Uses only fetch + WebCrypto, so it runs unchanged in Workers and Node 22.

// Product-token form. WPC's CloudFront 403s some free-form agents (it
// rejected "spotter-tools-pro-outlooks" on 2026-09-25 while this passed).
const UA = { "User-Agent": "SpotterToolsPro/1.0 (+https://spottertools.pro)" };

async function sha(text) {
  const buf = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(text));
  return [...new Uint8Array(buf)].slice(0, 6).map((b) => b.toString(16).padStart(2, "0")).join("");
}

// ArcGIS: the attribute table without geometry is a few hundred bytes and
// carries idp_filedate / issue_time, which move on every NOAA ingest. An
// empty outlook is still a valid answer ("no risk areas"), so it hashes too.
async function probeArcgis(p) {
  const url = `${p.url}/query?where=1%3D1&returnGeometry=false&outFields=*&f=json`;
  const r = await fetch(url, { headers: UA });
  if (!r.ok) throw new Error(`${p.id}: probe HTTP ${r.status}`);
  const j = await r.json();
  if (j.error) throw new Error(`${p.id}: probe ArcGIS error ${j.error.code} ${j.error.message}`);
  if (!Array.isArray(j.features)) throw new Error(`${p.id}: probe has no features array`);
  return sha(JSON.stringify(j.features.map((f) => f.attributes)));
}

// KML: both NOAA hosts send Last-Modified (WPC adds an ETag), so a HEAD is
// enough. Falls back to hashing the body if a host ever drops both.
async function probeKml(p) {
  const r = await fetch(p.url, { method: "HEAD", headers: UA });
  if (!r.ok) throw new Error(`${p.id}: probe HTTP ${r.status}`);
  const lm = r.headers.get("last-modified");
  const et = r.headers.get("etag");
  if (lm || et) return sha(`${lm}|${et}|${r.headers.get("content-length")}`);
  const g = await fetch(p.url, { headers: UA });
  if (!g.ok) throw new Error(`${p.id}: probe HTTP ${g.status}`);
  return sha(await g.text());
}

export function probe(p) {
  return p.kind === "kml" ? probeKml(p) : probeArcgis(p);
}

export const USER_AGENT = UA;
