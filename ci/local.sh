#!/usr/bin/env bash
# Run ci/pr.sh the way the workflow does, on this machine: a bare Ubuntu 24.04 container with 4 CPUs
# and 16G (a GitHub-hosted runner), an unprivileged user with sudo, and a workspace of fresh clones.
#
#   ci/local.sh --base <branch|commit> --head <branch|commit> --out <dir> [--flows '<flow> ...']
#
# A firmware change; --flows as for pr.sh, default device. The rabby flow cannot run here (it needs
# network namespaces, see below).
#
# The clones come from the checkouts next to this repo instead of GitHub, so head can be a local
# branch that is not pushed. As in CI, `origin` is BitBoxSwiss: origin/<branch> is what each checkout
# last fetched from its BitBoxSwiss remote, and the API repos are at that remote's default branch.
# This repo is copied as it is, uncommitted changes included.
#
# Different from a hosted runner: no user namespaces (Docker's seccomp profile), so what needs port
# 15423 runs one at a time; the runner's preinstalled tools are missing, so setup.sh installs more.
# $E2E_CI_HOME (default ~/.cache/bitbox-e2e-ci) is the container's home and survives between runs,
# like the workflow's tools cache; delete it for a cold run.
set -euo pipefail

base='' head='' out=''
flows=device
while [ $# -gt 0 ]; do
  case "$1" in
    --flows) flows="${2:-}" ;;
    --base) base="${2:-}" ;;
    --head) head="${2:-}" ;;
    --out) out="${2:-}" ;;
    *) sed -n '2,8p' "$0" >&2; exit 2 ;;
  esac
  shift 2 || { sed -n '2,8p' "$0" >&2; exit 2; }
done
if [ -z "$base" ] || [ -z "$head" ] || [ -z "$out" ]; then
  sed -n '2,8p' "$0" >&2
  exit 2
fi
case " $flows " in *" rabby "*) echo "local.sh: the rabby flow needs network namespaces, which the container lacks" >&2; exit 2 ;; esac

src="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
home="${E2E_CI_HOME:-$HOME/.cache/bitbox-e2e-ci}"
mkdir -p "$home" "$out"

# rustup is the one thing the hosted runner has that prereqs.sh does not install.
docker build -q -t bitbox-e2e-ci --build-arg "uid=$(id -u)" - >&2 <<'EOF'
FROM ubuntu:24.04
ARG uid
RUN apt-get update -q && apt-get install -y -q --no-install-recommends sudo ca-certificates git rustup \
 && rm -rf /var/lib/apt/lists/* \
 && (userdel -r ubuntu 2>/dev/null || true) \
 && useradd -m -u "$uid" runner \
 && echo 'runner ALL=(ALL) NOPASSWD:ALL' > /etc/sudoers.d/runner
USER runner
EOF

docker run --rm -i --cpuset-cpus 0-3 --memory 16g \
  -v "$src:/src:ro" -v "$home:/home/runner" -v "$(cd "$out" && pwd):/out" \
  bitbox-e2e-ci bash -s -- "$base" "$head" "$flows" <<'EOF'
set -euo pipefail
base="$1" head="$2" flows="$3"
ws="$HOME/work"
rm -rf "$ws"
mkdir -p "$ws"
git config --global --replace-all safe.directory '*'

# What actions/checkout leaves: origin is BitBoxSwiss, HEAD its default branch. Local branches of
# the source checkout come along so an unpushed head resolves.
clone() {  # <dir> <BitBoxSwiss repo>
  local from="/src/$1" to="$ws/$1" up
  up="$(git -C "$from" remote -v | awk -v r="github.com[:/]BitBoxSwiss/$2(\\.git)?$" '$2 ~ r { print $1; exit }')"
  [ -n "$up" ] || { echo "local.sh: $from has no remote pointing at BitBoxSwiss/$2" >&2; exit 2; }
  git init -q --initial-branch=unused "$to"
  git -C "$to" remote add origin "https://github.com/BitBoxSwiss/$2.git"
  git -C "$to" fetch -q "$from" '+refs/heads/*:refs/heads/*' "+refs/remotes/$up/*:refs/remotes/origin/*"
  for b in master main; do
    git -C "$to" rev-parse -q --verify "origin/$b" >/dev/null && { git -C "$to" checkout -q --detach "origin/$b"; return; }
  done
  echo "local.sh: $from has neither $up/master nor $up/main" >&2
  exit 2
}
clone bitbox02-firmware bitbox02-firmware
git -C "$ws/bitbox02-firmware" submodule update -q --init --recursive
clone bitbox-api-rs bitbox-api-rs
clone bitbox02-api-go bitbox02-api-go
clone bitbox-api-ts bitbox-api-ts
case " $flows " in *" app "*|*" wc "*) clone bitbox-wallet-app bitbox-wallet-app ;; esac
tar -C /src --exclude=e2e/driver/target --exclude=e2e/.venv --exclude=.pytest_cache --exclude=.ruff_cache --exclude=node_modules --exclude=__pycache__ -cf - e2e | tar -C "$ws" -xf -

sudo "$ws/e2e/ci/prereqs.sh"
rc=0
"$ws/e2e/ci/pr.sh" --base "$base" --head "$head" --flows "$flows" --out "$ws/report" || rc=$?
cp -r "$ws/report/." /out/ 2>/dev/null || true
echo "local.sh: pr.sh exit $rc; $(du -sh "$HOME" 2>/dev/null | cut -f1) under the runner's home" >&2
exit "$rc"
EOF
