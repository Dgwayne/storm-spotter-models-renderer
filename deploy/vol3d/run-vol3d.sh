#!/bin/bash
# MRMS 3D volume tiles -> LIVE v1/VOL3D/ (the app's "3D" radar chip, MRMS
# mosaic source). One tick = scripts/mrms_volume_tiles.py --publish: list
# the newest complete MRMS 3D cycle, fetch its 33 reflectivity CAPPIs +
# RhoHV to 8 km + the two AzShear layers (~45 MB), cut 2x2 deg voxel tiles,
# upload, prune stamps past the retention window.
#
# TWO HOSTS, ONE PREFIX (since 2026-09-30):
#   OVH VPS  vps-021a1204    VOL3D_ROLE=primary    (timer *:0/2)
#   Box 2    stp-render2copy VOL3D_ROLE=fallback   (timer *:1/2)
# They coordinate through the published pointer alone (see the script's
# docstring: a stamp already up is skipped by both; the fallback waits
# VOL3D_FALLBACK_GRACE_S while a live primary has the stamp, and publishes
# at once when the last pointer is its own or the primary has gone quiet).
# The role, the credentials env and the nice level come from the per-host
# systemd drop-in that install.sh writes, never from this file, so the repo
# copy is host-neutral. If both boxes are down the app shows "MRMS 3D feed
# unavailable" and the user can switch the chip's Source to the radar site,
# which needs no server at all.
#
# Measured on box 2 2026-09-08 (quiet night, 208 lit tiles): 44 s per tick,
# 3.1 GB peak RSS, 16 MB uploaded. MRMS publishes every ~2 min; the timers
# match that. The script is idle (one listing, no download) when the stamp
# has not moved, so a faster timer costs nothing but the listing.
set -uo pipefail
mkdir -p ~/stp-vol3d/{state,logs,locks}
exec 9>"$HOME/stp-vol3d/locks/tick.lock"
flock -n 9 || exit 0     # a tick still running owns this window
ROLE="${VOL3D_ROLE:-primary}"
tick_line() { echo "$(date -u +%FT%TZ) rc=$1 $2 wall=0s" >> ~/stp-vol3d/logs/ticks.log; }

# The production env: rclone R2_* credentials + the mamba PATH. Box 2 uses
# its ~/stp-prod/env.sh; the OVH box points VOL3D_ENV at ~/stp-vol3d/env.sh
# because ITS ~/stp-prod/env.sh is the shadow env (rclone aliased to local
# disk) for everything else there. An unfilled credentials file is an idle
# tick with a reason, not a red unit: fill the file and the next tick runs.
ENV_FILE="${VOL3D_ENV:-$HOME/stp-prod/env.sh}"
if [ ! -r "$ENV_FILE" ]; then
  tick_line 0 "TICK vol3d status=idle reason=env-missing file=$ENV_FILE role=$ROLE"
  exit 0
fi
source "$ENV_FILE"
case "${RCLONE_CONFIG_R2_ACCESS_KEY_ID:-}${RCLONE_CONFIG_R2_SECRET_ACCESS_KEY:-}" in
  ""|*FILL_ME*)
    tick_line 0 "TICK vol3d status=idle reason=credentials-unfilled file=$ENV_FILE role=$ROLE"
    exit 0;;
esac
cd ~/storm-spotter-models-renderer
export VOL3D_ROLE="$ROLE"
export VOL3D_STATE_DIR=$HOME/stp-vol3d/state
export VOL3D_JOBS="${VOL3D_JOBS:-6}"
export VOL3D_RETAIN_MIN="${VOL3D_RETAIN_MIN:-120}"
# The tick's own budget (the script aborts itself with a TICK line naming
# the phase, exit 3); the unit's TimeoutStartSec is the outer fuse.
export VOL3D_DEADLINE_S="${VOL3D_DEADLINE_S:-300}"
# systemd's kill (TimeoutStartSec, a stop) TERMs this shell along with
# python. Untrapped, bash dies with it and ticks.log gets NO line for the
# tick: 2026-09-09 eighteen killed ticks left a 2 h gap that read as a
# healthy log with one odd idle line. A handler (not '' — an IGNORED
# signal is inherited across exec and python would stop dying) keeps
# bash alive just long enough to write the accounting line below.
trap 'true' TERM INT
LOG=~/stp-vol3d/logs/tick-$(date -u +%Y%m%dT%H%M%S).log
t0=$(date +%s)
python3 scripts/mrms_volume_tiles.py --publish > "$LOG" 2>&1; rc=$?
line=$(grep -E '^TICK ' "$LOG" | tail -1)
if [ -z "$line" ]; then
  # Killed (rc 143 = TERM, 137 = KILL) or crashed before its TICK line:
  # carry the last phase marker so the gap is diagnosable from ticks.log.
  last=$(grep -E '^ +\[\+[0-9]+s\] ' "$LOG" | tail -1 | sed -E 's/^ +//')
  line="TICK vol3d status=killed role=$ROLE last=${last:-none}"
fi
echo "$(date -u +%FT%TZ) rc=$rc $line wall=$(( $(date +%s) - t0 ))s" >> ~/stp-vol3d/logs/ticks.log
# Keep idle ticks out of the way: only failures and real renders keep their log.
if [ "$rc" = 0 ] && grep -q 'status=idle' "$LOG"; then rm -f "$LOG"; fi
ls -t ~/stp-vol3d/logs/tick-*.log 2>/dev/null | tail -n +80 | xargs -r rm -f
exit $rc
