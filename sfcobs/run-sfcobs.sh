#!/bin/bash
# One Surface Obs bake tick (sfcobs_bake.py) on the always-on STP box.
#
# The box primary is the Astrol VPS (stp-sfcobs.timer, every 5 min, system
# unit with User=ubuntu). GitHub Actions (.github/workflows/sfcobs.yml) is
# the fallback only: the stp-models-cron Worker dispatches it when
# sfcobs/v1/manifest.json is more than 15 min old.
#
# Runs from its own sparse clone (~/stp-sfcobs/renderer, sfcobs/ only) so
# deploying this feed never moves the shared renderer checkout the vol3d
# producer runs from.
set -uo pipefail
mkdir -p ~/stp-sfcobs/{cache,logs,locks,out}
exec 9>"$HOME/stp-sfcobs/locks/tick.lock"
flock -n 9 || exit 0     # a slow tick still owns this window
tick_line() { echo "$(date -u +%FT%TZ) rc=$1 $2" >> ~/stp-sfcobs/logs/ticks.log; }

# The env file sets PATH (micromamba `renderer` env: python + netCDF4 +
# rclone), R2_BUCKET and the rclone "r2:" remote. It sources the box's
# shared ~/.stp-b2.env, so no credential lives in this repo or this dir.
ENV_FILE="${SFCOBS_ENV:-$HOME/stp-sfcobs/env.sh}"
if [ ! -r "$ENV_FILE" ]; then
  tick_line 0 "status=idle reason=env-missing file=$ENV_FILE"
  exit 0
fi
source "$ENV_FILE"
export SFCOBS_CACHE_DIR=$HOME/stp-sfcobs/cache
export SFCOBS_OUT_DIR=$HOME/stp-sfcobs/out
export SFCOBS_HOST="${SFCOBS_HOST:-$(hostname)}"
export PYTHONUNBUFFERED=1
# Fail-fast B2 uploads (rclone's default 5 min idle timeout outlives a stalled
# PUT; see render_mrms_obs.sh). Caller env wins.
export RCLONE_CONTIMEOUT="${RCLONE_CONTIMEOUT:-15s}"
export RCLONE_TIMEOUT="${RCLONE_TIMEOUT:-30s}"

T0=$(date +%s)
OUT=$(python -I "$(dirname "$0")/sfcobs_bake.py" 2>&1)
RC=$?
printf '%s\n' "$OUT" > ~/stp-sfcobs/logs/last.log
# A failed tick's traceback would be overwritten 5 min later; keep it.
if [ "$RC" -ne 0 ]; then
  cp ~/stp-sfcobs/logs/last.log ~/stp-sfcobs/logs/fail-$(date -u +%Y%m%dT%H%M%S).log
  ls -t ~/stp-sfcobs/logs/fail-*.log 2>/dev/null | tail -n +51 | xargs -r rm -f
fi
SUMMARY=$(printf '%s\n' "$OUT" | grep -E '^(stations:|stations.json |ERROR)' | tr '\n' ' ')
tick_line "$RC" "wall=$(( $(date +%s) - T0 ))s $SUMMARY"
# Keep the tick log bounded (~2 weeks at 288 ticks/day).
tail -n 4000 ~/stp-sfcobs/logs/ticks.log > ~/stp-sfcobs/logs/ticks.tmp && mv ~/stp-sfcobs/logs/ticks.tmp ~/stp-sfcobs/logs/ticks.log
exit "$RC"
