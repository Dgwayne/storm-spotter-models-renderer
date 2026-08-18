# MRMS render box (Oracle Cloud Ampere A1, ARM64)

The MRMS observation catalog and the MultiSensor QPE accumulations render
here, not on GitHub Actions.

> ## ⚠ ACCOUNT STATUS: FREE TRIAL, NOT ALWAYS FREE
>
> This box was originally documented as "Always Free". It is not, and the
> difference has a deadline attached.
>
> `stp-mrms-render` was created **4 OCPU / 24 GB** on 2026-08-17 07:09:58Z on a
> **30-day Free Trial** ($300 credit, console shows "Days remaining 30 of 30").
> Oracle halved the Always Free Ampere A1 allowance from 4 OCPU / 24 GB to
> **2 OCPU / 12 GB on 2026-06-15**, without announcement. This box is exactly
> twice the Always Free ceiling, so it does not survive the trial as-is.
>
> Oracle's docs, verbatim: *"If you have more OCI Ampere A1 Compute instances
> provisioned than are available for an Always Free tenancy, all existing OCI
> Ampere A1 Compute instances are disabled and then deleted after 30 days,
> unless you upgrade to a paid account."* Note **all** instances, not just the
> excess.
>
> | date | event |
> |---|---|
> | 2026-09-16 | trial ends, box **disabled** |
> | ~2026-10-16 | box **deleted permanently** |
>
> **No bill is possible in the meantime.** A Free Trial tenancy cannot be
> charged; Oracle disables resources rather than invoicing. A payment
> obligation begins only on an explicit *Upgrade to Pay As You Go*.
>
> ## ✅ RESOLVED 2026-08-17 20:20Z: resized to 2 OCPU / 12 GB
>
> The box now sits exactly on the Always Free ceiling, so **nothing is
> disabled on 2026-09-16 and nothing bills**. The `fast` tier was slowed
> 2 min -> 4 min -> 3 min to fit. Resizing was an in-place shape edit plus a
> reboot and preserved the boot volume, VNIC and public IP; the timers are
> `enabled` so they came back on their own.
>
> Margin note: 2 OCPU x 744 h = 1,488 of 1,500 free OCPU-hours and 12 GB x
> 744 h = 8,928 of 9,000 free GB-hours. That is ~1% of headroom, so **this
> one box consumes essentially the whole A1 allowance** — a second A1
> instance, or any bump to either number, bills from hour one. Memory above
> 12 GB costs ~$1.12/GB/month; a third core would be ~$7.32/month.
>
> ⚠️ **12 GB is NOT generous, it is the binding constraint.** See the OOM
> section below: sizing this box from idle memory or the OCI mean-utilization
> graph said 12 GB was ample and both were wrong.
>
> **Do not size this box from load average or CPU-second arithmetic.** Both
> said 2 cores would fit and both were wrong. Simulate it exactly instead:
> ```
> sudo systemctl set-property 'system-stp\x2dmrms.slice' CPUQuota=200%   # test
> sudo systemctl set-property 'system-stp\x2dmrms.slice' CPUQuota=        # revert
> ```
> All four tiers already share that slice, so the cap is a true 2-core box.
> At the old 2-minute cadence the fast tier diverged under it: 74 -> 121 ->
> 138 -> 181 s against a 120 s budget.

**Cutover: done 2026-08-17 08:46Z.** Measured immediately after, against
NOAA's newest published file:

| product | before (median / p90) | after |
|---|---|---|
| et18 | 21.1 / 41.1 min | 4.5 min |
| vil | 21.1 / 41.1 | 2.5 |
| posh | 21.1 / 41.1 | 2.5 |
| shi | 15.0 / 35.1 | 2.5 |
| rala | 7.1 / 16.1 | 2.5 |

Seven of ten watched products sit at **0.0 min of lag** — the app is exactly
as fresh as NOAA, not approaching it. The "before" column is a real hour of
sampling, and note its p90: Actions was not merely slow, it was erratic, and
the tail is what put a rotation product outside the polygon it belonged to.

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
| `fast` | 3 min | 41 | 2.0 min — rotation, MESH, POSH, SHI, all 4 echo tops, VIL/VILD/VII, RALA, isothermal reflectivity, rate, ARI, FFG |
| `mid` | 10 min | 9 | 10 min (FLASH soil sat / streamflow), plus `rq15m` at 15 |
| `slow` | 20 min | 37 | exactly 30 or 60 min (measured, see below) — the swaths, 3-72h QPE accums, all multi-sensor |
| `qpe` | 15 min | 5 | MultiSensor Pass1 ~:16, Pass2 ~:58 after each valid hour |

The 37 slow ones cannot be improved: their source only publishes every
30-71 minutes. WeatherWise hits the same wall.

**MEASURED 2026-08-18** against `noaa-mrms-pds` (~19 h of published files per
product, script pattern in the `newest_key` helper of `render_mrms_obs.sh`).
The cadences are stricter than "30-71 min" suggests, and **median == min for
every single product**, i.e. perfectly regular with no fast bursts hiding in
an average:

| group | products | publish gap |
|---|---|---|
| swaths + tracks (`mesh*`, `rot*`, `rotml*`, `vilsw*`) | 14 | exactly **30.0 min** |
| QPE accums + hourly max (`rq*`, `ms*`, `crefmax1h`, `brefmax1h`) | 23 | exactly **60.0 min** |

**Zero products publish faster than the 20-minute tier cadence.** The
20-minute timer is already 1.5x more frequent than the fastest thing it
renders, so 25 min would still catch every publish within one tick if ticks
ever need trimming.

⚠️ That headroom made `OBS_JOBS=2` look free on this tier, and it was not.
Tried and reverted 2026-08-18: the tier tripled (112s -> 320s) rather than
doubled, because the 37 per-product S3 freshness checks lose parallelism too
and are fixed cost regardless of how many products render. The longer window
then overlapped the fast tier, costing one 290s tick, one 271s tick and two
lost 3-minute slots, and pushing et18 lag from 2.1 to 6.1 min. **On a 2-core
box the scarce resource is not memory or budget headroom, it is how long a
tier occupies the cores while another tier needs them.**

`qpe` is not a cadence tier — it runs `render_mrms_qpe.sh`, a different
script with the Pass1/Pass2 gauge-correction cycle. It lives here rather
than on Actions because it rebuilds `manifest.json` twice per tick and
prunes. Two processes doing that on separate schedules is not corrupting
(every rebuild derives from a fresh listing) but it lets a fast-tier
srcTimes patch land on a rebuild and briefly revert QPE availability. One
box, one writer, one manifest lock.

Measured runtimes, worst observed:

| tier | elapsed | cadence | headroom |
|---|---|---|---|
| fast | 57-85s | 120s | 29% at worst, under QPE contention |
| mid | 25s | 600s | 96% |
| slow | 81s | 1200s | 93% |
| qpe | 302s cold | 900s | 66% |

**After the 2026-08-17 resize to 2 OCPU / 12 GB** (measured under
`CPUQuota=200%`, four consecutive ticks, zero skips):

| tier | elapsed | cadence | headroom |
|---|---|---|---|
| fast | 87-153s | **180s** | **15%** at worst (see the timer unit for the 4-min vs 3-min trade) |
| mid | 36s | 600s | 94% |
| slow | 56s | 1200s | 95% |
| qpe | 43s | 900s | 95% |

Halving the cores cost the fast tier only ~40% more wall time, not 2x, because
the render already runs `OBS_JOBS=4` and is partly I/O bound on NOAA fetches.
The old 2-minute cadence failed not because a single tick was too slow but
because five of them per ten minutes, plus mid/slow/qpe, exceeded what two
cores can serialise. At three minutes that drops to 3.3 ticks and it fits,
though at 85% of budget rather than 59%. Four minutes is the conservative
setting if ticks start landing off the 3-minute boundary (see the timer unit
for why `status=skipped` is NOT the signal to grep for).

**The freshness cost is far smaller than the cadence change suggests.**
et18 live lag sampled every 45 s over 9 samples at the 3-minute cadence:
0.0, 2.0, 2.0, 2.0, 2.0, 2.0, 4.0, 4.0, 4.1, mean **2.46 min** (total age
mean 4.82 min). Polling interval is not the dominant term; NOAA's own ~2.3
min publish latency plus ~1.5 min of render time is the floor, which is why
no cadence makes these products look as fast as base reflectivity. Base
reflectivity is not in this catalog at all — it is fetched live from IEM and
never touches this box.

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

## Shadow mode (how this was validated, and how to re-enter it)

`OBS_PREFIX` in `/etc/stp-renderer/prefix.env` is the whole switch. It reads
`OBS` now. Set it to `OBS-shadow` and the box renders a complete parallel
copy instead, which is how any future change to this pipeline should be
proven before it touches production:

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

## THE SWITCH (executed 2026-08-17 08:46Z — kept as the procedure)

Server-side, so it needs no app release and takes effect on the app's next
60-second manifest poll. Same URLs, fresher data behind them.

One step below is easy to skip and matters: **cancel queued Actions runs**.
Four MRMS runs were sitting `queued`/`pending` at cutover, some for eight
minutes. Removing the workflows from the Worker stops new dispatches but
does nothing about work already in the queue, and a `workflow_dispatch` run
checks out `main` when it finally starts — so those would have woken up
after the switch and written to `v1/OBS/` behind the box.

1. **Stop GitHub rendering OBS.** Two edits, both required — a workflow
   change in this repo is always the `.yml` *and* `CRON_TO_WORKFLOW`:
   - `cron-trigger/src/worker.js`: drop `SEVERE_WORKFLOW` from all three
     cron slots, `wrangler deploy`
   - `.github/workflows/render_mrms_qpe.yml`: remove the
     "Render MRMS observation catalog" step
2. **Drain the queue — do not just watch it.**
   ```bash
   for wf in render_mrms_severe.yml render_mrms_qpe.yml; do
     for id in $(gh run list --workflow "$wf" --limit 15 \
         --json databaseId,status \
         --jq '.[]|select(.status=="queued" or .status=="pending"
                          or .status=="in_progress")|.databaseId'); do
       gh run cancel "$id"
     done
   done
   ```
   Then re-run the query until both report zero. Waiting alone can take
   25 minutes here — that queue depth is the reason for the whole
   migration — and every run still in it will check out `main` when it
   starts and write to `v1/OBS/` behind the box.
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
| GitHub OBS, before cutover | ~1,536 |
| shadow during validation (retain 4 ⇒ one page) | ~144 |
| **box on live now** (slow tier, retain 26 ⇒ 8 pages) | ~1,152 |
| **plus the qpe tier** (1 pre-list + up to 3 rebuild/prune) | ~384 |
| **total now** | **~1,536** |

Unchanged from before the migration, against a 2,500/day free allowance —
the work moved, the LIST cost did not. Verified in the journal: fast and
mid tiers performed **zero** bucket listings across every tick, because
local state answers idempotency.

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
