#!/usr/bin/env bash
# One firmware change, as CI runs it: resolve the two refs, install the toolchain, `e2e.py pr`.
#
#   ci/pr.sh --base <branch|commit> --head <branch|commit> [--head-repo <owner>/<repo>] --out <dir>
#
# Expects the workspace CI checks out: bitbox02-firmware (full history, submodules), bitbox-api-rs,
# bitbox02-api-go and bitbox-api-ts next to this repo's checkout, which must be named e2e.
# --head-repo is the GitHub fork holding head; without it head is looked up in the firmware checkout.
# Exit code is e2e.py pr's: 0 no regression, 1 regression, 2 the run itself failed.
set -euo pipefail

usage() { sed -n '2,9p' "$0" >&2; exit 2; }
base= head= head_repo= out=
while [ $# -gt 0 ]; do
  case "$1" in
    --base) base="${2:-}" ;;
    --head) head="${2:-}" ;;
    --head-repo) head_repo="${2:-}" ;;
    --out) out="${2:-}" ;;
    *) usage ;;
  esac
  shift 2 || usage
done
[ -n "$base" ] && [ -n "$head" ] && [ -n "$out" ] || usage

# The refs come from a workflow form or a PR: nothing that git could read as an option or a refspec.
ref_re='^[A-Za-z0-9][A-Za-z0-9._/-]*$'
[[ "$base" =~ $ref_re ]] || { echo "pr.sh: --base '$base' is not a branch name or commit" >&2; exit 2; }
[[ "$head" =~ $ref_re ]] || { echo "pr.sh: --head '$head' is not a branch name or commit" >&2; exit 2; }
if [ -n "$head_repo" ] && ! [[ "$head_repo" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9._-]+$ ]]; then
  echo "pr.sh: --head-repo '$head_repo' is not <owner>/<repo>" >&2
  exit 2
fi

# shellcheck disable=SC1091
source "$(dirname "${BASH_SOURCE[0]}")/../env.sh"
FW="${BITBOX_FIRMWARE:-$BITBOX_WORKSPACE/bitbox02-firmware}"

# A branch of the checkout's origin first: after actions/checkout the only local branch is the
# default one, and a stale local branch must not win over the remote's.
resolve() {
  git -C "$FW" rev-parse -q --verify "origin/$1^{commit}" ||
    git -C "$FW" rev-parse -q --verify "$1^{commit}" ||
    { echo "pr.sh: '$1' is neither a branch of origin nor a commit in $FW" >&2; exit 2; }
}
base_sha="$(resolve "$base")"
if [ -n "$head_repo" ]; then
  git -C "$FW" fetch -q --no-tags "https://github.com/$head_repo.git" "$head"
  head_sha="$(git -C "$FW" rev-parse FETCH_HEAD)"
else
  head_sha="$(resolve "$head")"
fi
echo "pr.sh: base $base ($base_sha), head ${head_repo:+$head_repo }$head ($head_sha)" >&2

"$BITBOX_E2E_DIR/setup.sh"
# Again: env.sh only exports what exists, and node and go were not installed the first time.
# shellcheck disable=SC1091
source "$BITBOX_E2E_DIR/env.sh"

rc=0
"$BITBOX_E2E_DIR/e2e.py" pr --base "$base_sha" --head "$head_sha" --out "$out" || rc=$?

# The top of the report (revisions, verdict, summary) on the run's page; the images are in the artifact.
if [ -n "${GITHUB_STEP_SUMMARY:-}" ]; then
  if [ -f "$out/report.md" ]; then
    sed '/^### /,$d' "$out/report.md" >> "$GITHUB_STEP_SUMMARY"
  else
    echo "e2e.py pr wrote no report (exit $rc); see the job log." >> "$GITHUB_STEP_SUMMARY"
  fi
fi
exit "$rc"
