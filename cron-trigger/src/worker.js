// Cron-triggered Worker: dispatches the renderer's GitHub Actions workflows on
// a reliable schedule (GitHub's `on: schedule:` was dropping most sub-hourly
// ticks). See cron-trigger/wrangler.toml for the schedule and rationale.
//
// Secret required (set once via `wrangler secret put GH_DISPATCH_TOKEN`):
//   GH_DISPATCH_TOKEN — fine-grained GitHub PAT scoped to this repo with
//                       "Actions: Read and write" permission.

const OWNER = "Dgwayne";
const REPO = "storm-spotter-models-renderer";

// Map each cron expression (must match wrangler.toml exactly) to the workflow
// file(s) it should dispatch. The free plan allows exactly 5 cron triggers,
// so extra workflows piggyback on an existing slot rather than adding a 6th
// (each dispatch is just one more subrequest in the same invocation).
// An entry can also be { wf, minutes: [...] } to fire on a SUBSET of the
// slot's ticks — for workflows that want an hourly cadence but must ride a
// 15-min slot (a full soundings extract is not an idempotent sweep, so
// dispatching it 4x/hour would just queue useless 15-min runs).
// GOES rides the :02/:17/:32/:47 slot — satellite frames land in IEM's
// archive on the quarter-hours, so this catches each one a few minutes
// after it publishes (GitHub-native cron was leaving the satellite loop
// 1-3 h stale overnight).
// render_goes_geocolor.yml (added 2026-07-20, GIBS-sourced, its own
// geocolor.json manifest) was left on GitHub-native cron and hit the exact
// same failure: on 2026-07-28 its declared 10-min schedule was actually
// firing every 1-3 h, so the app's GeoColor live view served a 3 h 12 m old
// frame. It rides the same slot as render_goes.yml — both are satellite,
// both are idempotent backfill-the-window renders, so a 15-min dispatch is
// well inside GIBS' own ~40-min latency.
const CRON_TO_WORKFLOW = {
  // Soundings (skew-T sites + point tiles, hourly f00-f18 scrubber) ride the
  // HRRR slot's :35 tick only: HRRR f18 for the previous hour's cycle
  // publishes ~:15-:30, so :35 extracts each new complete run minutes after
  // it lands. Was GitHub-native-cron-only ('50 * * * *'), which is throttled
  // — the same staleness trap as GOES/GFS/GeoColor before it.
  "5,20,35,50 * * * *": [
    "render_hrrr.yml",
    { wf: "extract_soundings.yml", minutes: [35] },
  ],
  "10,25,40,55 * * * *": ["render_rrfs.yml"],
  "2,17,32,47 * * * *": [
    "render_mrms_qpe.yml",
    "render_goes.yml",
    "render_goes_geocolor.yml",
  ],
  // Wildfire rides the NAM slot — the least loaded of the five, and this
  // bake is short (three HTTP fetches + three small uploads, no gdal). WFIGS
  // refreshes every 5 min upstream, so 15-min dispatches keep the layer
  // within one refresh of the authoritative record. wildfire.yml also keeps
  // an hourly GitHub-native cron as a backstop for the 2026-07-13 failure
  // mode where this Worker's triggers silently stopped firing.
  "0,15,30,45 * * * *": ["render_nam.yml", "wildfire.yml"],
  // GFS rides the ECMWF slot: GitHub-native cron for render_gfs.yml was
  // firing with gaps up to 4 h, compounding the old single-target render
  // strategy into ~5-9 h of app-visible staleness. 30-min dispatches +
  // the sweep rewrite in render_gfs.sh put new runs on the CDN within
  // one tick of NOAA publishing.
  // ICON-D2 (Open-Meteo om-source pilot) rides the same slot: 3-hourly
  // cycles publishing ~46-80 min after init make 30-min dispatches ample.
  // render_om_models.yml is the batch-2 om matrix (AIGFS/GFS013/UKMO/
  // GDPS/HGEFS) — all 6-12 h cycle models, same cadence logic.
  "7,37 * * * *": [
    "render_ecmwf.yml",
    "render_gfs.yml",
    "render_aifs.yml",
    "render_gefs.yml",
    "render_icond2.yml",
    "render_om_models.yml",
    // Batch 3 (13-model matrix) fires once an hour, not twice: all its
    // models cycle every 3-12 h, and halving the dispatch rate halves
    // the queue pressure 13 parallel jobs put on the 20-runner cap.
    { wf: "render_om_models2.yml", minutes: [37] },
    // Batch 5 (12-model matrix: projected grids + CAMS) takes the :07
    // tick so the two hourly matrices alternate half-hours.
    { wf: "render_om_models3.yml", minutes: [7] },
  ],
};

// Fallback if Cloudflare ever hands us a cron string we didn't map (e.g. after
// editing wrangler.toml but not this file) — better to fire HRRR than nothing.
const DEFAULT_WORKFLOW = "render_hrrr.yml";

// Entries are plain workflow-name strings or { wf, minutes } objects.
const entryName = (e) => (typeof e === "string" ? e : e.wf);
const entryFiresAt = (e, minute) =>
  typeof e === "string" || e.minutes.includes(minute);

// Every workflow this Worker knows how to dispatch (for the manual endpoint).
const KNOWN_WORKFLOWS = new Set(
  Object.values(CRON_TO_WORKFLOW).flat().map(entryName).concat(DEFAULT_WORKFLOW),
);

async function dispatch(workflow, token) {
  const url = `https://api.github.com/repos/${OWNER}/${REPO}/actions/workflows/${workflow}/dispatches`;
  const resp = await fetch(url, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: "application/vnd.github+json",
      "X-GitHub-Api-Version": "2022-11-28",
      // GitHub rejects API requests without a User-Agent.
      "User-Agent": "stp-models-cron",
    },
    body: JSON.stringify({ ref: "main" }),
  });
  // workflow_dispatch returns 204 No Content on success.
  if (!resp.ok) {
    const body = await resp.text();
    throw new Error(`dispatch ${workflow} failed: ${resp.status} ${body}`);
  }
  console.log(`dispatched ${workflow}`);
}

export default {
  async scheduled(event, env, ctx) {
    if (!env.GH_DISPATCH_TOKEN) {
      throw new Error("GH_DISPATCH_TOKEN secret is not set");
    }
    // event.scheduledTime is the SCHEDULED epoch ms (not the actual firing
    // time), so the minute extracted here matches the cron expression even
    // when Cloudflare fires a beat late.
    const minute = new Date(event.scheduledTime).getUTCMinutes();
    const workflows = (CRON_TO_WORKFLOW[event.cron] || [DEFAULT_WORKFLOW])
      .filter((e) => entryFiresAt(e, minute))
      .map(entryName);
    ctx.waitUntil(
      Promise.all(workflows.map((wf) => dispatch(wf, env.GH_DISPATCH_TOKEN))),
    );
  },

  // Optional manual trigger for testing: `curl https://<worker-url>/?wf=render_hrrr.yml`
  // Remove this handler if you'd rather the Worker have no public surface.
  async fetch(request, env) {
    if (!env.GH_DISPATCH_TOKEN) {
      return new Response("GH_DISPATCH_TOKEN not set\n", { status: 500 });
    }
    const wf = new URL(request.url).searchParams.get("wf") || DEFAULT_WORKFLOW;
    if (!KNOWN_WORKFLOWS.has(wf)) {
      return new Response(`unknown workflow: ${wf}\n`, { status: 400 });
    }
    try {
      await dispatch(wf, env.GH_DISPATCH_TOKEN);
      return new Response(`dispatched ${wf}\n`);
    } catch (e) {
      return new Response(`${e}\n`, { status: 502 });
    }
  },
};
