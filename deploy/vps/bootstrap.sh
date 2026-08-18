#!/usr/bin/env bash
# One-shot setup for the MRMS render box (Oracle Cloud Always Free, Ampere
# A1 / Ubuntu ARM64). Idempotent: safe to re-run after a repo change or a
# reboot.
#
#   sudo bash deploy/vps/bootstrap.sh
#
# What it does NOT do: write credentials or start any timer. Credentials go
# in /etc/stp-renderer/renderer.env by hand (see renderer.env.example), and
# the timers are started deliberately once a shadow run has been eyeballed.
# See README.md in this directory for the full runbook.
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/Dgwayne/storm-spotter-models-renderer.git}"
REPO_DIR="${REPO_DIR:-/opt/stp-renderer}"
# The branch the box tracks. Stays on the VPS branch until the cutover has
# held; hardcoding main would silently roll the box back to the Actions-era
# script on the next bootstrap run.
REPO_REF="${REPO_REF:-main}"
ETC_DIR="/etc/stp-renderer"
STATE_DIR="/var/lib/stp-renderer"
# The account the timers run as. Oracle's Ubuntu image logs in as `ubuntu`;
# SUDO_USER keeps this correct if the image ever uses another name.
RUN_USER="${RUN_USER:-${SUDO_USER:-ubuntu}}"
YQ_VERSION="v4.53.3"
# Ubuntu 24.04 ships rclone 1.60.1 (2022), which sends `x-amz-acl: private`
# on every PUT. B2's S3 endpoint rejects that outright:
#   InvalidArgument: Unsupported value for canned acl 'private'
# so EVERY upload fails. --s3-acl="" does not suppress it on 1.60 either
# (tested on the box). CI never hit this because conda-forge ships current
# rclone. Pin a modern one rather than depend on the distro's.
RCLONE_VERSION="v1.75.0"

[ "$(id -u)" -eq 0 ] || { echo "run me with sudo" >&2; exit 1; }
id "$RUN_USER" >/dev/null 2>&1 || { echo "no such user: $RUN_USER" >&2; exit 1; }

echo "==> Packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
# gdal-bin  : gdal_translate/gdalwarp/gdaldem/gdal_calc.py (classic chain)
# python3-gdal: the osgeo bindings the single-pass renderer imports
# python3-numpy: gdal_calc.py needs it too, but name it so a slim image
#               can't leave the single pass without it
# rclone    : every upload, and the one recursive listing per sweep
# flock     : util-linux; serialises manifest writes across tiers
apt-get install -y -qq --no-install-recommends \
  gdal-bin python3-gdal python3-numpy python3-yaml \
  git curl ca-certificates util-linux unzip

echo "==> rclone ${RCLONE_VERSION} (arm64)"
# Deliberately NOT the distro package — see RCLONE_VERSION above. Same
# retry hardening as yq: a release-CDN outage took the renderer's Actions
# out for hours on 2026-08-12, and a box that cannot install rclone cannot
# upload anything.
RCLONE_HAVE=$(rclone --version 2>/dev/null | head -1 || true)
if ! grep -q "${RCLONE_VERSION}" <<<"$RCLONE_HAVE"; then
  curl -sSfL --retry 6 --retry-all-errors --retry-delay 5 \
    --connect-timeout 10 --max-time 180 \
    -o /tmp/rclone.zip \
    "https://github.com/rclone/rclone/releases/download/${RCLONE_VERSION}/rclone-${RCLONE_VERSION}-linux-arm64.zip"
  rm -rf /tmp/rclone-extract && mkdir -p /tmp/rclone-extract
  unzip -q -o /tmp/rclone.zip -d /tmp/rclone-extract
  install -m 0755 /tmp/rclone-extract/rclone-*/rclone /usr/local/bin/rclone
  rm -rf /tmp/rclone.zip /tmp/rclone-extract
fi
# /usr/local/bin precedes /usr/bin, so this shadows any distro rclone.
hash -r 2>/dev/null || true

echo "==> yq ${YQ_VERSION} (arm64)"
# Pinned and retried: the renderer's GitHub Actions were taken out for
# several hours on 2026-08-12 by a GitHub release-CDN outage, and a box
# that cannot install yq cannot render anything at all.
YQ_HAVE=$(yq --version 2>/dev/null || true)   # same SIGPIPE/pipefail trap as the GRIB check
if ! grep -q "${YQ_VERSION}" <<<"$YQ_HAVE"; then
  curl -sSfL --retry 6 --retry-all-errors --retry-delay 5 \
    --connect-timeout 10 --max-time 60 \
    -o /tmp/yq \
    "https://github.com/mikefarah/yq/releases/download/${YQ_VERSION}/yq_linux_arm64"
  install -m 0755 /tmp/yq /usr/local/bin/yq
  rm -f /tmp/yq
fi

echo "==> Repo at ${REPO_DIR} (${REPO_REF})"
if [ -d "${REPO_DIR}/.git" ]; then
  git -C "${REPO_DIR}" fetch --quiet origin
  git -C "${REPO_DIR}" reset --hard --quiet "origin/${REPO_REF}"
else
  git clone --quiet --branch "${REPO_REF}" "${REPO_URL}" "${REPO_DIR}"
fi
git -C "${REPO_DIR}" --no-pager log --oneline -1
chown -R "${RUN_USER}:${RUN_USER}" "${REPO_DIR}"

echo "==> Directories"
install -d -m 0755 -o "${RUN_USER}" -g "${RUN_USER}" \
  "${STATE_DIR}" "${STATE_DIR}/state" "${STATE_DIR}/cache" "${STATE_DIR}/locks"
install -d -m 0755 "${ETC_DIR}"

# THE SWITCH. Shadow by default: a fresh box can never write to the live
# prefix by accident, only by an edit someone had to mean.
if [ ! -f "${ETC_DIR}/prefix.env" ]; then
  cat > "${ETC_DIR}/prefix.env" <<'EOF'
# ── THE CUTOVER SWITCH ────────────────────────────────────────────────
# OBS-shadow : renders alongside production, app never sees it.
# OBS        : renders INTO production. Only set this once the GitHub side
#              has stopped rendering OBS and no run is still in flight.
# After editing:  sudo systemctl restart stp-mrms@fast stp-mrms@mid stp-mrms@slow
OBS_PREFIX=OBS-shadow
EOF
  chmod 0644 "${ETC_DIR}/prefix.env"
fi

# Per-tier settings. Which products ride which timer comes from
# products.yml (cadence_tier), so these files never change.
cat > "${ETC_DIR}/tier-fast.env" <<'EOF'
OBS_TIER=fast
# Patch the published manifest instead of rebuilding it: a rebuild lists
# the whole prefix, and doing that thirty times an hour is the one thing
# here that would cost real money on B2's free Class C allowance.
OBS_SKIP_PRUNE=1
EOF
cat > "${ETC_DIR}/tier-mid.env" <<'EOF'
OBS_TIER=mid
OBS_SKIP_PRUNE=1
EOF
cat > "${ETC_DIR}/tier-slow.env" <<'EOF'
OBS_TIER=slow
# The slow tier owns the authoritative rebuild + retention prune. Deletes
# are scoped to OBS_PREFIX, so while shadowing this cannot reach the live
# prefix even in principle.
#
# HALVED from the default 4 on 2026-08-18. mrms_render_one.py deliberately
# holds the whole GRIB->PNG chain in numpy rather than spilling intermediate
# GTiffs to disk (that is what makes 2 OCPU viable), which costs ~2.5 GB per
# job, so 4 jobs peaks ~5.2 GB. Two tiers overlapping at 4 jobs exceeded the
# 12 GB box and OOM-killed renders. This tier is the one that can absorb it:
# 112s mean / 310s worst against a 1200s budget, and all 37 of its products
# publish on a strict 30 or 60 min cadence (measured against noaa-mrms-pds
# 2026-08-18, median == min for every product), so the extra ~2 min is 3-6%
# of the publish interval. Do NOT do this to the fast tier: it runs 103s of
# a 180s budget and would go over.
OBS_JOBS=2
EOF
cat > "${ETC_DIR}/tier-qpe.env" <<'EOF'
# Not a cadence tier: this runs render_mrms_qpe.sh (the 5 MultiSensor
# accumulations with the Pass1/Pass2 cycle), which the observation catalog
# script does not cover. No OBS_TIER, and no OBS_SKIP_PRUNE — it owns its
# own prune and manifest rebuild, serialised against the other tiers by
# OBS_LOCK_FILE.
EOF
chmod 0644 "${ETC_DIR}"/tier-*.env

if [ ! -f "${ETC_DIR}/renderer.env" ]; then
  install -m 0600 /dev/null "${ETC_DIR}/renderer.env"
  cat "${REPO_DIR}/deploy/vps/renderer.env.example" > "${ETC_DIR}/renderer.env"
  echo "    !! ${ETC_DIR}/renderer.env is a TEMPLATE — put the real B2"
  echo "       credentials in it before starting any timer."
fi
chmod 0600 "${ETC_DIR}/renderer.env"

echo "==> swap (OOM cushion)"
# 12 GB with no swap OOM-killed renders on 2026-08-17/18: peak RSS is bursty
# and a kill loses the whole tick, while a little paging only makes it slow.
# Low swappiness keeps this an emergency cushion, not routine paging.
if ! swapon --show=NAME --noheadings 2>/dev/null | grep -q '^/swapfile$'; then
  fallocate -l 4G /swapfile
  chmod 600 /swapfile
  mkswap -q /swapfile
  swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  echo "    created 4G /swapfile"
else
  echo "    /swapfile already present"
fi
printf 'vm.swappiness=10
' > /etc/sysctl.d/99-stp-swappiness.conf
sysctl -q -w vm.swappiness=10

echo "==> systemd units"
install -m 0644 "${REPO_DIR}/deploy/vps/stp-mrms@.service" /etc/systemd/system/
for tier in fast mid slow qpe; do
  install -m 0644 "${REPO_DIR}/deploy/vps/stp-mrms@${tier}.timer" /etc/systemd/system/
done
# Substitute the run user into the template rather than hardcoding ubuntu.
sed -i "s/^User=.*/User=${RUN_USER}/;s/^Group=.*/Group=${RUN_USER}/" \
  /etc/systemd/system/stp-mrms@.service
systemctl daemon-reload

echo "==> Toolchain check"
gdalinfo --version
# Capture first, then match. `gdalinfo --formats | grep -qi grib` looks
# equivalent and is not: grep -q exits at the first match, gdalinfo takes
# SIGPIPE, and under `set -o pipefail` the pipeline reports THAT as the
# result. The check then fails on a box where the driver is present and
# working (verified 2026-08-17 — 149 drivers listed, GRIB among them).
GDAL_FORMATS=$(gdalinfo --formats 2>/dev/null || true)
if grep -qi 'grib' <<<"$GDAL_FORMATS"; then
  echo "GRIB driver: present"
else
  echo "GRIB driver MISSING — gdal cannot read MRMS files" >&2; exit 1
fi
python3 -c "from osgeo import gdal; import numpy; print('osgeo bindings:', gdal.__version__)"
python3 -c "import yaml; print('pyyaml: ok')"
rclone --version | head -1
yq --version
flock --version | head -1

# The C++ tools and the Python utility scripts are packaged separately on
# Debian-family distros, and gdal_calc.py/gdal_edit.py have moved between
# gdal-bin, python3-gdal and a gdal-python-tools package across GDAL
# releases. The single pass does not need them, but the classic chain is
# the fallback we keep reachable precisely so a suspect frame can be
# re-rendered the old way — a fallback that turns out not to be installed
# is worse than no fallback, because you find out while trying to use it.
missing=""
for t in gdal_translate gdalwarp gdaldem gdalinfo gdal_calc.py gdal_edit.py; do
  command -v "$t" >/dev/null 2>&1 || missing="${missing} ${t}"
done
if [ -n "${missing}" ]; then
  echo "    missing GDAL tools:${missing}" >&2
  echo "    trying gdal-python-tools (newer GDAL splits the scripts out)" >&2
  apt-get install -y -qq --no-install-recommends gdal-python-tools 2>/dev/null || true
  for t in ${missing}; do
    command -v "$t" >/dev/null 2>&1 || {
      echo "STILL MISSING: ${t} — the classic render chain cannot run" >&2
      echo "  (OBS_SINGLE_PASS=1 will still work; the fallback will not)" >&2
      exit 1; }
  done
fi
echo "GDAL tools: all present (single pass + classic fallback)"

CURRENT_PREFIX=$(grep '^OBS_PREFIX=' "${ETC_DIR}/prefix.env" | cut -d= -f2)
cat <<EOF

==> Bootstrap complete.

Next:
  1. put the B2 credentials in ${ETC_DIR}/renderer.env
  2. sudo -u ${RUN_USER} bash ${REPO_DIR}/deploy/vps/run-tier.sh fast --dry-run
  3. start the timers:
       sudo systemctl enable --now stp-mrms@fast.timer stp-mrms@mid.timer \
                                   stp-mrms@slow.timer stp-mrms@qpe.timer

Rendering to prefix: v1/${CURRENT_PREFIX}/   <-- shadow until you flip it
EOF
