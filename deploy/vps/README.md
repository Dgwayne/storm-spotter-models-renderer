# MRMS render box (Oracle Always Free, ARM64)

Moves the MRMS observation catalog off GitHub Actions and onto a single
always-on box.

## Why this exists

The renderer creates 66-76 Actions runs an hour and completes 19-26. Runs
wait 10-25 minutes for a runner and only ~8 jobs execute account-wide at
once. Measured 2026-08-17 03:37Z on Echo Top 18 dBZ:

| | valid time | age |
|---|---|---|
| NOAA newest `et18` | 03:34:41Z | 2.8 min (the floor) |
| WeatherWise | 03:34:41Z | ~2 min |
| Ours | 03:02:40Z | 34.8 min |

Every one of those 32 minutes is queue time. It is not a renderer problem
and not an app problem, and no amount of workflow tuning fixes a starved
runner pool.

## The three tiers

Which products ride which timer comes from `cadence_tier` in
`config/products.yml`, measured against `noaa-mrms-pds` rather than assumed.
Rendering faster than the source publishes just burns CPU on identical
bytes; rendering slower throws away freshness that was there for the taking.

| tier | timer | products | source cadence |
|---|---|---|---|
| `fast` | 2 min | 41 | 2.0 min — rotation, MESH, POSH, SHI, all 4 echo tops, VIL/VILD/VII, RALA, isothermal reflectivity, rate, ARI, FFG |
| `mid` | 10 min | 9 | 10 min (FLASH soil sat / streamflow), plus `rq15m` at 15 |
| `slow` | 20 min | 37 | 30-71 min — the swaths, 3-72h QPE accums, all multi-sensor |

The 37 slow ones cannot be improved: their source only publishes every
30-71 minutes. WeatherWise hits the same wall.

## Install

```bash
sudo bash /opt/stp-renderer/deploy/vps/bootstrap.sh
```

Idempotent. Installs gdal/rclone/yq/git, clones the repo to
`/opt/stp-renderer`, writes the systemd units, and verifies the GRIB driver
is present. It does **not** write credentials and does **not** start a timer.

Then, by hand:

```bash
sudoedit /etc/stp-renderer/renderer.env
```

and the rclone remote, in the run user's home:

```bash
mkdir -p ~/.config/rclone
cat > ~/.config/rclone/rclone.conf <<'EOF'
[r2]
type = s3
provider = Other
region = us-west-004
no_check_bucket = true
access_key_id = <B2_KEY_ID>
secret_access_key = <B2_APP_KEY>
endpoint = <B2_S3_ENDPOINT>
EOF
chmod 0600 ~/.config/rclone/rclone.conf
```

Check it before starting anything:

```bash
bash /opt/stp-renderer/deploy/vps/run-tier.sh fast --dry-run
rclone lsd r2:$R2_BUCKET/v1/ | head
```

Start the timers:

```bash
sudo systemctl enable --now stp-mrms@fast.timer stp-mrms@mid.timer stp-mrms@slow.timer
```

## Shadow mode

`/etc/stp-renderer/prefix.env` ships as `OBS_PREFIX=OBS-shadow`. In that
state the box renders a complete parallel copy of the catalog to
`v1/OBS-shadow/` with its own `manifest.json`, and:

- the app keeps reading `v1/OBS/` and cannot tell the box exists;
- `prune_old_runs.py` addresses `v1/OBS-shadow/...` and has no path to
  production data even in principle;
- the GitHub pipeline stays exactly as it is, starved but working, as the
  rollback target.

Storage cost is about half a cent a month (OBS is ~2,385 frames at ~395 KB,
under 1 GB) and the shadow keeps only `OBS_RETAIN=4` hours of it.

Watch it:

```bash
journalctl -u 'stp-mrms@*' -g TICK -f
python3 /opt/stp-renderer/deploy/vps/compare_freshness.py
```

## THE SWITCH

Server-side, so it needs no app release and takes effect on the app's next
60-second manifest poll. Same URLs, fresher data behind them.

1. **Stop GitHub rendering OBS.** Two edits, both required — a workflow
   change in this repo is always the `.yml` *and* `CRON_TO_WORKFLOW`:
   - `cron-trigger/src/worker.js`: drop `SEVERE_WORKFLOW` from all three
     cron slots, `wrangler deploy`
   - `.github/workflows/render_mrms_qpe.yml`: remove the
     "Render MRMS observation catalog" step
2. **Confirm nothing is still in flight:**
   ```bash
   gh run list --repo Dgwayne/storm-spotter-models-renderer \
     --workflow render_mrms_severe.yml --limit 5
   gh run list --repo Dgwayne/storm-spotter-models-renderer \
     --workflow render_mrms_qpe.yml --limit 5
   ```
   Wait for `in_progress` to clear. A run queued on old code can finish
   after a patch and clobber it.
3. **Point the box at live:**
   ```bash
   sudo sed -i 's/^OBS_PREFIX=.*/OBS_PREFIX=OBS/' /etc/stp-renderer/prefix.env
   sudo systemctl restart stp-mrms@fast stp-mrms@mid stp-mrms@slow
   ```

`OBS_RETAIN` is ignored for the live prefix by construction
(`render_mrms_obs.sh`), so forgetting to unset the shadow's shallow
retention cannot prune live history down to 4 hours.

## ROLLBACK

The same three steps reversed. Rehearse this **before** the cutover.

```bash
# 1. box back to shadow — the app stops seeing it immediately
sudo sed -i 's/^OBS_PREFIX=.*/OBS_PREFIX=OBS-shadow/' /etc/stp-renderer/prefix.env
sudo systemctl restart stp-mrms@fast stp-mrms@mid stp-mrms@slow

# 2. GitHub resumes: revert the worker + workflow commit, redeploy
git revert --no-edit <cutover-sha> && git push
cd cron-trigger && npx wrangler deploy

# 3. force one immediate sweep rather than waiting for the next tick
gh workflow run render_mrms_qpe.yml --repo Dgwayne/storm-spotter-models-renderer
```

Recovery time is one manifest poll (60 s) for the app to stop seeing
whatever the box was publishing, plus one QPE sweep for Actions to
re-assert `v1/OBS/`. To stop the box entirely rather than shadow it:

```bash
sudo systemctl disable --now stp-mrms@fast.timer stp-mrms@mid.timer stp-mrms@slow.timer
```

## Operating notes

**Headroom.** Every tick logs one line:

```
TICK tier=fast status=ok prefix=OBS-shadow selected=41 rendered=41 elapsed=63s
```

`elapsed` against the tier's cadence is the headroom. `rendered=0` is the
healthy steady state between source publishes. `status=skipped` means the
previous tick was still running — normal in moderation, a problem if
frequent.

**Cost.** The fast and mid tiers must never list the bucket. They read the
manifest over the CDN (Cloudflare serves it; B2 never sees the read) and
write it back as a free Class A write. Only the slow tier lists, twice per
tick (rebuild + prune). B2's free Class C allowance is 2,500/day:

| | LIST calls/day |
|---|---|
| GitHub OBS today | ~1,536 |
| shadow (retain 4 ⇒ one page) | ~144 |
| box on live after cutover (retain 26 ⇒ 8 pages) | ~1,152 |

The naive version of the fast path — pre-list the bucket and fully rebuild
on every tick — needed ~6,144/day, which is ~$0.44/mo. That is why
`patch_obs_srctimes.py` exists.

**Freshness checks that actually work.** B2's origin sends no
`Last-Modified`; Cloudflare synthesizes one, so edge headers say nothing
about freshness. Go direct:

```bash
curl -sI -r 0-0 \
  "https://f004.backblazeb2.com/file/$R2_BUCKET/v1/OBS/et18/$(date -u +%Y%m%d%H)/F000.png" \
  | grep -i x-bz-upload-timestamp
```

And `models.dgwaynes.com` 403s urllib's default User-Agent — set one.
Fetching a bare CDN URL reports the zone rule's `Cache-Control` (4h), not
the origin's; cache-bust before comparing.

**Two GDAL paths.** `OBS_SINGLE_PASS=1` uses `scripts/mrms_render_one.py`
(one process); `0` uses the historical five-spawn chain. Both produce the
same PNG — `validate_single_pass.sh` is what proves it on this box. Keep
the switch reachable so a suspect frame can be re-rendered the old way
without a deploy.
