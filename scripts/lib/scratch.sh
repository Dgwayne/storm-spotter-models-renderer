# Per-run scratch root for the render scripts. Source it, then call
# `stp_scratch_init <tag>` once near the top and add "$STP_SCRATCH" to the
# script's EXIT trap.
#
# Why: every mktemp -d / python tempfile in the run (including ones made in
# background subshells, which drop the parent's EXIT trap) lands under one
# per-run dir via TMPDIR. EXIT traps survive TERM but nothing survives
# SIGKILL (kernel OOM, systemd's final kill), and on box 2 /tmp is a
# RAM-backed tmpfs with a per-user quota: 2026-09-18..22 killed runs left
# 1175 tmp.* dirs (6.2 GB) that filled the quota and failed every
# stp-vol3d tick. So each init also sweeps siblings whose owning PID is
# gone, i.e. a hard-killed run is cleaned up by the next tick.
stp_scratch_init() {
  local tag="$1" base="${TMPDIR:-/tmp}" d
  for d in "$base"/stp-"$tag".*; do
    [ -d "$d" ] || continue
    kill -0 "${d##*.}" 2>/dev/null && continue   # owner still running
    rm -rf "$d"
  done
  STP_SCRATCH="$base/stp-$tag.$$"
  mkdir -p "$STP_SCRATCH"
  export TMPDIR="$STP_SCRATCH"
}
