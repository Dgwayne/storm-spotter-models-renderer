#!/bin/bash
# Box-2 MRMS 3D volume tiles -> LIVE v1/VOL3D/ (the app's "3D" radar chip,
# MRMS mosaic source). One tick = scripts/mrms_volume_tiles.py --publish:
# list the newest complete MRMS 3D cycle, fetch its 33 reflectivity CAPPIs
# + RhoHV to 8 km + the two AzShear layers (~40 MB), cut 2x2 deg voxel
# tiles, upload, prune stamps past the retention window.
#
# Measured on this box 2026-09-08 (quiet night, 208 lit tiles): 44 s per
# tick, 3.1 GB peak RSS, 16 MB uploaded. MRMS publishes every ~2 min; the
# timer matches that. The script itself is idle (one listing, no download)
# when the stamp has not moved, so a faster timer costs nothing but the
# listing.
#
# This is the ONLY writer of v1/VOL3D/ (no Oracle twin: the Oracle box is
# 2 cores / 12 GB and this job wants ~1 core-minute and 3 GB per cycle).
# If this box is down the app shows "MRMS 3D feed unavailable" and the
# user can switch the chip's Source to the radar site, which needs no
# server at all.
set -uo pipefail
source ~/stp-prod/env.sh
cd ~/storm-spotter-models-renderer
export VOL3D_STATE_DIR=$HOME/stp-vol3d/state
export VOL3D_JOBS="${VOL3D_JOBS:-6}"
export VOL3D_RETAIN_MIN="${VOL3D_RETAIN_MIN:-120}"
mkdir -p ~/stp-vol3d/{state,logs,locks}
exec 9>"$HOME/stp-vol3d/locks/tick.lock"
flock -n 9 || exit 0     # a tick still running owns this window
LOG=~/stp-vol3d/logs/tick-$(date -u +%Y%m%dT%H%M%S).log
t0=$(date +%s)
python3 scripts/mrms_volume_tiles.py --publish > "$LOG" 2>&1; rc=$?
line=$(grep -E '^TICK ' "$LOG" | tail -1)
echo "$(date -u +%FT%TZ) rc=$rc ${line:-no-tick-line} wall=$(( $(date +%s) - t0 ))s" >> ~/stp-vol3d/logs/ticks.log
# Keep idle ticks out of the way: only failures and real renders keep their log.
if [ "$rc" = 0 ] && grep -q 'status=idle' "$LOG"; then rm -f "$LOG"; fi
ls -t ~/stp-vol3d/logs/tick-*.log 2>/dev/null | tail -n +80 | xargs -r rm -f
exit $rc
