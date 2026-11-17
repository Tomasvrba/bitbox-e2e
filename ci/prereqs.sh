#!/usr/bin/env bash
# Root prerequisites of setup.sh on an Ubuntu 24.04 runner; run with sudo. setup.sh's preflight is
# the check: it names anything this list lacks.
set -euo pipefail

export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q --no-install-recommends \
  git curl ca-certificates tar xz-utils unzip gcc g++ make pkg-config python3 coreutils binutils \
  util-linux iproute2 libssl-dev \
  xvfb libegl1 libgl1 libx11-xcb1 libxcursor1 libxi6 libxrender1 libxkbcommon-x11-0 libgl1-mesa-dri

# Ubuntu 23.10+ keeps unprivileged processes from creating user namespaces, which isolated.sh needs
# (base-side graphical runs of simulators without --port, the API suites, the Rabby flow). Without
# them those run one at a time. Not settable in a container: the sysctl is the host's.
sysctl -q -w kernel.apparmor_restrict_unprivileged_userns=0 2>/dev/null ||
  echo "prereqs.sh: user namespaces stay restricted; units and API suites on port 15423 run one at a time" >&2
