#!/bin/bash
# Install or refresh the vol3d user units on this host and write the
# per-host drop-in (role, credentials env, nice, cadence). Idempotent.
#
#   deploy/vol3d/install.sh primary|fallback
#
# Optional env:
#   VOL3D_ENV=<file>          credentials env the tick sources
#                             (default ~/stp-prod/env.sh; OVH: ~/stp-vol3d/env.sh)
#   VOL3D_NICE=<n>            nice level for the tick (default: the unit's 5;
#                             OVH uses 10 to sit level with the L2 ingest)
#   VOL3D_ONCALENDAR=<spec>   timer cadence (default: the unit's *:1/2;
#                             OVH uses *:0/2 to lead the fallback by a minute)
#
# Nothing here touches credentials: the tick idles with
# reason=credentials-unfilled until the env file's key pair is filled in.
set -euo pipefail
ROLE="${1:-}"
case "$ROLE" in
  primary|fallback) ;;
  *) echo "usage: $0 primary|fallback" >&2; exit 2 ;;
esac
HERE=$(cd "$(dirname "$0")" && pwd)
UD="$HOME/.config/systemd/user"
mkdir -p "$UD/stp-vol3d.service.d" "$UD/stp-vol3d.timer.d"
install -m 0644 "$HERE/stp-vol3d.service" "$HERE/stp-vol3d.timer" "$UD/"

{
  echo "# written by deploy/vol3d/install.sh on $(date -u +%FT%TZ) for $(hostname)"
  echo "# per-host settings; the unit file itself is the same on every host"
  echo "[Service]"
  echo "Environment=VOL3D_ROLE=$ROLE"
  if [ -n "${VOL3D_ENV:-}" ]; then echo "Environment=VOL3D_ENV=$VOL3D_ENV"; fi
  if [ -n "${VOL3D_NICE:-}" ]; then echo "Nice=$VOL3D_NICE"; fi
} > "$UD/stp-vol3d.service.d/role.conf"

if [ -n "${VOL3D_ONCALENDAR:-}" ]; then
  {
    echo "# written by deploy/vol3d/install.sh on $(date -u +%FT%TZ) for $(hostname)"
    echo "[Timer]"
    echo "OnCalendar="            # clear the unit's default before setting ours
    echo "OnCalendar=$VOL3D_ONCALENDAR"
  } > "$UD/stp-vol3d.timer.d/cadence.conf"
else
  rm -f "$UD/stp-vol3d.timer.d/cadence.conf"
fi

systemctl --user daemon-reload
systemctl --user enable --now stp-vol3d.timer
echo "== role.conf"; cat "$UD/stp-vol3d.service.d/role.conf"
if [ -f "$UD/stp-vol3d.timer.d/cadence.conf" ]; then echo "== cadence.conf"; cat "$UD/stp-vol3d.timer.d/cadence.conf"; fi
systemctl --user list-timers --no-pager stp-vol3d.timer
