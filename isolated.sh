#!/usr/bin/env bash
# Run a command in its own network namespace (unprivileged user ns, loopback only), so several
# simulators can listen on the fixed port 15423 at once. Xvfb's abstract socket is per netns too.
# Build outside: cargo/uv can't reach the network in here.
#
#   isolated.sh e2e.py device --scenario ... --out ...
exec unshare --user --map-root-user --net sh -c 'ip link set lo up && exec "$@"' sh "$@"
