#!/usr/bin/env bash
# Run a command in its own network namespace (unprivileged user ns, loopback only), so several
# simulators can listen on the fixed port 15423 at once. /tmp/.X11-unix is private too: in a
# namespace Xvfb writes no lock file and always takes :0, and python-xlib prefers the socket file
# there over the per-netns abstract socket, so runs would drive each other's displays.
# Build outside: cargo/uv can't reach the network in here.
#
#   isolated.sh e2e device --scenario ... --out ...
#   isolated.sh true      exits non-zero, with the reason on stderr, where namespaces are unavailable
set -euo pipefail

# Probe first: when the namespace can't be created, unshare's own error doesn't say why.
if ! err="$(unshare --user --map-root-user --net --mount true 2>&1)"; then
  echo "isolated.sh: can't create an unprivileged user+network namespace (unshare: ${err:-failed})." >&2
  echo "  Ubuntu 23.10+ restricts them via AppArmor (kernel.apparmor_restrict_unprivileged_userns=1);" >&2
  echo "  Docker's default seccomp profile blocks them in containers; some kernels disable them" >&2
  echo "  (kernel.unprivileged_userns_clone=0, user.max_user_namespaces=0)." >&2
  exit 1
fi
command -v ip >/dev/null || { echo "isolated.sh: 'ip' (iproute2) is missing, it brings up loopback in the namespace" >&2; exit 1; }

mkdir -p /tmp/.X11-unix
exec unshare --user --map-root-user --net --mount sh -c \
  'mount -t tmpfs -o mode=1777 tmpfs /tmp/.X11-unix && ip link set lo up && exec "$@"' sh "$@"
