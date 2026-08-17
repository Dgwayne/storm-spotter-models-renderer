#!/usr/bin/env bash
# Tier wrapper: what systemd actually executes, and what a human runs to
# try a tick by hand.
#
#   run-tier.sh <fast|mid|slow> [--dry-run]
#
# Three jobs the render script should not have to know about:
#
#  1. OVERRUN. If a tick is still running when its timer fires again, this
#     exits 0 immediately instead of stacking a second copy on top. A
#     2-minute timer over a tier that occasionally takes 3 minutes must
#     skip, not pile up — and a skipped tick is normal operation, not a
#     failure, so it must not turn the unit red.
#  2. MEASUREMENT. Emits one structured line per tick with the elapsed
#     time and how many products actually rendered. This is the evidence
#     for "a full cycle runs inside its tier's cadence with headroom" —
#     `journalctl -u stp-mrms@fast -g TICK` is the whole answer.
#  3. ENVIRONMENT. Loads the env files when run by hand, so an interactive
#     invocation behaves exactly like the timer's.
set -euo pipefail

TIER="${1:-}"
case "$TIER" in
  fast|mid|slow|qpe) ;;
  *) echo "usage: $0 <fast|mid|slow|qpe> [--dry-run]" >&2; exit 2 ;;
esac
# qpe is the MultiSensor Pass1/Pass2 accumulation script, not a cadence tier
# of the observation catalog. It rides this wrapper for the lock, the env
# loading and the TICK line; it owns its own prune and manifest rebuild.
#
# It has to live on the same box as the rest. It rebuilds
# v1/<prefix>/manifest.json twice per tick, and a second process doing that
# on its own schedule lets a fast-tick srcTimes patch land on top of a
# rebuild and briefly revert QPE availability. One box, one writer, one
# manifest lock.
if [ "$TIER" = qpe ]; then
  RENDER_SCRIPT="scripts/render_mrms_qpe.sh"
else
  RENDER_SCRIPT="scripts/render_mrms_obs.sh"
fi
DRY_RUN=""
[ "${2:-}" = "--dry-run" ] && DRY_RUN=1

REPO_DIR="${REPO_DIR:-/opt/stp-renderer}"
ETC_DIR="/etc/stp-renderer"
# Locks live under the state dir, not /run. /run/stp-renderer only exists
# because systemd's RuntimeDirectory creates it for the service, so a
# hand-run cannot make it — and after a reboot neither can the first tick
# if it beats the service. The state dir is owned by the run user, already
# in the unit's ReadWritePaths, and a stale lock file is harmless: flock is
# advisory and the kernel drops it when the fd closes.
LOCK_DIR="${LOCK_DIR:-/var/lib/stp-renderer/locks}"

# systemd supplies these via EnvironmentFile; a hand-run does not.
if [ -z "${R2_BUCKET:-}" ]; then
  for f in "${ETC_DIR}/renderer.env" "${ETC_DIR}/prefix.env" "${ETC_DIR}/tier-${TIER}.env"; do
    [ -r "$f" ] || { echo "missing or unreadable: $f" >&2; exit 1; }
    set -a; . "$f"; set +a
  done
fi

mkdir -p "$LOCK_DIR"
TICK_LOCK="${LOCK_DIR}/tier-${TIER}.lock"
export OBS_LOCK_FILE="${LOCK_DIR}/manifest.lock"
export OBS_STATE_DIR="${OBS_STATE_DIR:-/var/lib/stp-renderer/state}"
export OBS_JOBS="${OBS_JOBS:-4}"
export OBS_SINGLE_PASS="${OBS_SINGLE_PASS:-1}"
# Close the hourly-rollover hole: without this the app keeps showing the
# previous hour until the next full sweep, which at a 20-minute sweep would
# hand back most of what the 2-minute tier just bought.
export OBS_PATCH_CREATE_RUNS=1
# Shadow keeps few runs so its listing stays one page — see OBS_RETAIN in
# render_mrms_obs.sh, which ignores this entirely for the live prefix.
export OBS_RETAIN="${OBS_RETAIN:-4}"

if [ -n "$DRY_RUN" ]; then
  echo "tier=${TIER} prefix=${OBS_PREFIX:-OBS} jobs=${OBS_JOBS} single_pass=${OBS_SINGLE_PASS:-0}"
  echo "state=${OBS_STATE_DIR} retain=${OBS_RETAIN} skip_prune=${OBS_SKIP_PRUNE:-0}"
  echo "bucket=${R2_BUCKET:-<unset>} endpoint=${R2_ENDPOINT:-<unset>}"
  if [ "$TIER" = qpe ]; then
    yq -r '.products | to_entries | map(select(.value.mrms_product)) | length' \
      "${REPO_DIR}/config/products.yml" | xargs -I{} echo "QPE products: {}"
  else
    yq -r ".products | to_entries | map(select(.value.cadence_tier == \"${TIER}\")) | length" \
      "${REPO_DIR}/config/products.yml" | xargs -I{} echo "products in tier: {}"
  fi
  echo "script: ${RENDER_SCRIPT}"
  exit 0
fi

exec 8>"$TICK_LOCK"
if ! flock -n 8; then
  echo "TICK tier=${TIER} status=skipped reason=previous-tick-still-running"
  exit 0
fi

START=$(date +%s)
STATUS=ok
LOG=$(mktemp)
trap 'rm -f "$LOG"' EXIT

if bash "${REPO_DIR}/${RENDER_SCRIPT}" 2>&1 | tee "$LOG"; then
  :
else
  STATUS=failed
fi

ELAPSED=$(( $(date +%s) - START ))
RENDERED=$(grep -c '^  uploaded ' "$LOG" || true)
SELECTED=$(grep -oE '^==> [0-9]+ products selected' "$LOG" | grep -oE '[0-9]+' || echo '?')
# One greppable line per tick. Elapsed vs the tier's cadence is the headroom
# number; rendered=0 is the healthy steady state between source publishes.
echo "TICK tier=${TIER} status=${STATUS} prefix=${OBS_PREFIX:-OBS}" \
     "selected=${SELECTED} rendered=${RENDERED} elapsed=${ELAPSED}s"
[ "$STATUS" = ok ]
