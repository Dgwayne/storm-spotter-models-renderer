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
// GOES rides the :02/:17/:32/:47 slot — satellite frames land in IEM's
// archive on the quarter-hours, so this catches each one a few minutes
// after it publishes (GitHub-native cron was leaving the satellite loop
// 1-3 h stale overnight).
const CRON_TO_WORKFLOW = {
  "5,20,35,50 * * * *": ["render_hrrr.yml"],
  "10,25,40,55 * * * *": ["render_rrfs.yml"],
  "2,17,32,47 * * * *": ["render_mrms_qpe.yml", "render_goes.yml"],
  "0,15,30,45 * * * *": ["render_nam.yml"],
  "7,37 * * * *": ["render_ecmwf.yml"],
};

// Fallback if Cloudflare ever hands us a cron string we didn't map (e.g. after
// editing wrangler.toml but not this file) — better to fire HRRR than nothing.
const DEFAULT_WORKFLOW = "render_hrrr.yml";

// Every workflow this Worker knows how to dispatch (for the manual endpoint).
const KNOWN_WORKFLOWS = new Set(
  Object.values(CRON_TO_WORKFLOW).flat().concat(DEFAULT_WORKFLOW),
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
    const workflows = CRON_TO_WORKFLOW[event.cron] || [DEFAULT_WORKFLOW];
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
