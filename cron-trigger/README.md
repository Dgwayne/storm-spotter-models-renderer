# stp-models-cron — reliable scheduler for the renderer

GitHub Actions' built-in `on: schedule:` is best-effort. For this repo's
sub-hourly HRRR cron (`5,20,35,50 * * * *`, i.e. every 15 min) GitHub was
firing only ~once every 90 min — so the HRRR model layer ran 2-3 runs behind.

This Worker fires on Cloudflare's **Cron Triggers** (which are reliable) and
calls the GitHub `workflow_dispatch` API to kick the render workflow on time.
The `on: schedule:` block in the workflows is left in place as a backstop.

## Cost

Free. Cron Triggers are included on the Workers free plan and ~96
invocations/day is negligible against the 100k req/day allowance. No R2 cost.

## One-time deploy

From this directory (`cron-trigger/`):

```sh
# 1. Authenticate wrangler with your Cloudflare account (opens a browser).
npx wrangler login

# 2. Create the GitHub token the Worker uses to dispatch the workflow:
#    github.com/settings/personal-access-tokens/new
#      - Resource owner: Dgwayne
#      - Repository access: Only select repositories -> storm-spotter-models-renderer
#      - Permissions -> Repository -> Actions: Read and write
#    Copy the token, then paste it when prompted by:
npx wrangler secret put GH_DISPATCH_TOKEN

# 3. Deploy.
npx wrangler deploy
```

## Verify

```sh
# Manually fire HRRR through the Worker (also confirms the token works):
curl "https://stp-models-cron.<your-subdomain>.workers.dev/?wf=render_hrrr.yml"

# Then watch the run actually start:
gh run list --workflow=render_hrrr.yml --limit 3
```

Cloudflare also shows the cron invocation log under
Workers & Pages → stp-models-cron → Logs / Cron Events.

## Adding GFS / GOES / lightning later

1. Add the cron expression to `crons = [...]` in `wrangler.toml`.
2. Add the same expression → workflow-file mapping in `CRON_TO_WORKFLOW`
   in `src/worker.js`.
3. `npx wrangler deploy`.
