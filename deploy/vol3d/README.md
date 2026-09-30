# vol3d: the MRMS 3D volume producer on two hosts

`scripts/mrms_volume_tiles.py` cuts the app's 3D radar volume tiles from
NOAA's MRMS 3D mosaic into `v1/VOL3D/`. Since 2026-09-30 it runs on two
hosts against that one prefix:

| host | role | timer | credentials env | why |
|---|---|---|---|---|
| OVH VPS-4 `vps-021a1204` (15.204.211.25) | **primary** | `*:0/2` | `~/stp-vol3d/env.sh` -> `~/.stp-b2.env` | 8 vCore / 22 GB, 4 ms to us-east-1, off the home ISP |
| Box 2 `stp-render2copy` (192.168.50.134) | **fallback** | `*:1/2` | `~/stp-prod/env.sh` | the box that ran it alone until 2026-09-30 |

Oracle was measured and rejected: 2 cores at load 4, its MRMS fast tier
already uses 104 s of a 180 s window; this job wants ~6 threads for ~45 s.

## How the two coordinate

No shared lock, no cross-box network. Each tick reads the published
pointer straight from the bucket (`rclone cat .../latest.json`, never the
20 s CDN copy) before it downloads anything:

1. the pointer already names this stamp or a newer one -> idle
   `published-elsewhere` (both roles), and the stamp is absorbed into local
   state so the following ticks are one-listing idles;
2. the **primary** otherwise publishes at once;
3. the **fallback** publishes at once when no other producer has shown
   up within `VOL3D_PRIMARY_ALIVE_S` (600 s) in either the pointer or the
   primary's heartbeat (`primary.json`, written by every primary tick that
   reaches the pointer phase): a lone fallback is exactly as fresh as a
   primary. The heartbeat is what lets a returning primary take the
   prefix back from a healthy fallback that keeps catching stamps first;
4. while the primary is alive, the fallback leaves a new stamp alone until
   NOAA's objects are `VOL3D_FALLBACK_GRACE_S` (300 s) old (S3
   Last-Modified), then publishes it as `primary-behind`.

`recent[]` in the pointer is the union of both producers' stamps (each tick
absorbs the pointer's list into its state), so volume loops keep every
frame across a failover and prune never deletes the other box's stamps
early. The pointer carries `producer` (hostname) and `role`, which is how
you read who is serving:

```bash
curl -s https://models.dgwaynes.com/v1/VOL3D/latest.json | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["stamp"], d["generatedAt"], d.get("producer"), d.get("role"))'
```

Failover timeline: primary dies -> the fallback publishes the first missed
stamp ~5 min after NOAA posted it, every stamp after that immediately.
Primary returns -> it publishes the next stamp without waiting; the
fallback sees a fresh foreign pointer and steps back. One stamp may be
published by both around each transition; the tiles are identical and the
second `latest.json` simply wins.

## Install / refresh on a host

```bash
# OVH (primary)
VOL3D_ENV=$HOME/stp-vol3d/env.sh VOL3D_NICE=10 VOL3D_ONCALENDAR='*:0/2' \
  ~/storm-spotter-models-renderer/deploy/vol3d/install.sh primary
# Box 2 (fallback)
~/storm-spotter-models-renderer/deploy/vol3d/install.sh fallback
```

The installer copies the two units into `~/.config/systemd/user/`, writes
`stp-vol3d.service.d/role.conf` (and `stp-vol3d.timer.d/cadence.conf` when a
cadence is given) and enables the timer. Deploying a script change is the
usual `git pull` in `~/storm-spotter-models-renderer` on each host; the
tick reads the working tree.

## Credentials on the OVH box

The box's `~/stp-prod/env.sh` is the **shadow** env for everything else
there (rclone aliased onto local disk, no B2 keys by design), so vol3d has
its own `~/stp-vol3d/env.sh`, which sources `~/.stp-b2.env` (mode 600) and
exports the rclone `R2_*` variables from it. The L2 ingest's key on that
box is scoped to `l2rt/` and gets `403 not entitled` on `v1/VOL3D/`, so it
cannot be reused. Until `~/.stp-b2.env` is filled in by hand the tick logs
`status=idle reason=credentials-unfilled` every 2 min and does nothing.
Prove the fill without printing values:

```bash
source ~/stp-vol3d/env.sh && rclone lsf --dirs-only r2:$R2_BUCKET/v1/VOL3D/ | tail -3
```

## Reading the logs

`~/stp-vol3d/logs/ticks.log` on each host, one line per tick. Healthy
steady state:

- primary: `status=ok ... role=primary` every ~2 min, `status=idle
  reason=unchanged` between MRMS cycles;
- fallback: `status=idle reason=published-elsewhere` or
  `reason=unchanged`; `reason=primary-grace` for a tick or two when it
  looked before the primary finished; `status=ok ... role=fallback` means
  the primary missed a cycle.

## Rollback

Box 2 alone, as before 2026-09-30: `systemctl --user disable --now
stp-vol3d.timer` on OVH. Box 2 in the fallback role publishes without
waiting as soon as the last pointer is its own or the OVH pointer is
10 min old, so nothing else needs to change; `install.sh primary` on box 2
removes even that first wait.
