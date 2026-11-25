#!/usr/bin/env bash
# One change, as CI runs it: resolve the refs, install the toolchain, `e2e pr`.
#
#   ci/pr.sh [--repo firmware|app|api-rs|api-go|api-ts|rabby] [--base <branch|commit>] --head <branch|commit>
#            [--head-repo <owner>/<repo>] [--pins '<repo>=<ref> ...'] [--graphical '<glob> ...']
#            [--tests '<file> ...'] [--flows '<flow> ...'] --out <dir>
#
# Expects the workspace CI checks out next to this repo's checkout, which must be named e2e:
# bitbox02-firmware (full history, submodules), bitbox-api-rs, bitbox02-api-go and bitbox-api-ts,
# plus bitbox-wallet-app for the flows app and wc and Rabby for the flow rabby (bitbox_e2e.config.FLOWS).
# --flows defaults to all of the change's flows; CI runs one per job, into the same --out/<flow>/ layout.
# --base defaults to the repo's default branch. --head-repo is the GitHub fork holding head;
# without it head is looked up in the checkout.
# Exit code is e2e pr's: 0 no regression, 1 regression, 2 the run itself failed.
set -euo pipefail
set -f  # the lists below are split on spaces; their globs are for e2e, not the shell

usage() { sed -n '2,14p' "$0" >&2; exit 2; }
repo=firmware base='' head='' head_repo='' pins='' graphical='' tests='' flows='' out=''
while [ $# -gt 0 ]; do
  case "$1" in
    --repo) repo="${2:-}" ;;
    --base) base="${2:-}" ;;
    --head) head="${2:-}" ;;
    --head-repo) head_repo="${2:-}" ;;
    --pins) pins="${2:-}" ;;
    --graphical) graphical="${2:-}" ;;
    --tests) tests="${2:-}" ;;
    --flows) flows="${2:-}" ;;
    --out) out="${2:-}" ;;
    *) usage ;;
  esac
  shift 2 || usage
done
if [ -z "$head" ] || [ -z "$out" ]; then
  usage
fi

case "$repo" in
  firmware) dir=bitbox02-firmware branch=master ;;
  app) dir=bitbox-wallet-app branch=master ;;
  api-rs) dir=bitbox-api-rs branch=master ;;
  api-go) dir=bitbox02-api-go branch=master ;;
  api-ts) dir=bitbox-api-ts branch=master ;;
  rabby) dir=Rabby branch=develop ;;
  *) echo "pr.sh: --repo '$repo' is not one of firmware app api-rs api-go api-ts rabby" >&2; exit 2 ;;
esac
base="${base:-$branch}"

# The inputs come from a workflow form or a PR: nothing that git or e2e could read as an option.
ref_re='[A-Za-z0-9][A-Za-z0-9._/-]*'
check() {  # <what> <value> <regex>
  [[ "$2" =~ ^$3$ ]] || { echo "pr.sh: $1 '$2' is not valid" >&2; exit 2; }
}
check --base "$base" "$ref_re"
check --head "$head" "$ref_re"
[ -z "$head_repo" ] || check --head-repo "$head_repo" '[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9._-]+'
extra=()
# shellcheck disable=SC2086  # intentional word splitting with set -f
for p in $pins; do check --pins "$p" "(firmware|app|api-rs|api-go|api-ts|rabby)=$ref_re"; extra+=(--pin "$p"); done
# shellcheck disable=SC2086
for g in $graphical; do check --graphical "$g" '[A-Za-z0-9*?][A-Za-z0-9*?/._-]*'; extra+=(--graphical "$g"); done
t=()
# shellcheck disable=SC2086
for f in $tests; do check --tests "$f" '[A-Za-z0-9][A-Za-z0-9/._-]*'; t+=("$f"); done
[ ${#t[@]} -eq 0 ] || extra+=(--tests "${t[@]}")
fl=()
# shellcheck disable=SC2086
for f in $flows; do check --flows "$f" '(device|app|wc|rabby)'; fl+=("$f"); done
[ ${#fl[@]} -eq 0 ] || extra+=(--flows "${fl[@]}")

# shellcheck disable=SC1091
source "$(dirname "${BASH_SOURCE[0]}")/../env.sh"
src="$BITBOX_WORKSPACE/$dir"
[ "$repo" != firmware ] || src="${BITBOX_FIRMWARE:-$src}"

# The name to pass on, so the report shows it: a branch of the checkout's origin first (after
# actions/checkout the only local branch is the default one, and a stale local branch must not win
# over the remote's), else the ref as given.
resolve() {
  if git -C "$src" rev-parse -q --verify "origin/$1^{commit}" >/dev/null; then echo "origin/$1"
  elif git -C "$src" rev-parse -q --verify "$1^{commit}" >/dev/null; then echo "$1"
  else echo "pr.sh: '$1' is neither a branch of origin nor a commit in $src" >&2; exit 2
  fi
}
base_ref="$(resolve "$base")"
if [ -n "$head_repo" ]; then
  git -C "$src" fetch -q --no-tags "https://github.com/$head_repo.git" "$head"
  head_ref="${head_repo%%/*}/$head"
  git -C "$src" update-ref "refs/remotes/$head_ref" FETCH_HEAD
else
  head_ref="$(resolve "$head")"
fi
echo "pr.sh: $repo base $base_ref, head $head_ref ($(git -C "$src" rev-parse "$head_ref"))" >&2

"$BITBOX_E2E_DIR/setup.sh"
# Again: env.sh only exports what exists, and node and go were not installed the first time.
# shellcheck disable=SC1091
source "$BITBOX_E2E_DIR/env.sh"

# Every flow but device drives a browser: all of them unless --flows says only device.
if [ "$flows" != device ] && { [ -n "$flows" ] || [ "$repo" != api-rs ]; }; then
  # Chromium's system libraries, by the Playwright version rabby/package-lock.json pins.
  (cd "$BITBOX_E2E_DIR/rabby" && npm ci --ignore-scripts --no-audit --no-fund >&2)
  sudo env "PATH=$PATH" "$BITBOX_E2E_DIR/rabby/node_modules/.bin/playwright" install-deps chromium >&2
fi

rc=0
uv run --project "$BITBOX_E2E_DIR" --no-sync e2e pr --repo "$repo" --base "$base_ref" --head "$head_ref" --out "$out" "${extra[@]}" || rc=$?

# The result and one line per finding on the run's page; the report itself is in the artifact.
# With one flow per job this is that flow's; the workflow's report job sums them up.
if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  if [ -f "$out/summary.md" ]; then
    cat "$out/summary.md" >> "$GITHUB_STEP_SUMMARY"
  else
    echo "e2e pr wrote no report (exit $rc); see the job log." >> "$GITHUB_STEP_SUMMARY"
  fi
fi
exit "$rc"
