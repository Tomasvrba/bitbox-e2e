#!/usr/bin/env bash
# Build the headless and graphical simulators for a firmware ref, natively.
#
#   build-sim.sh <ref>        build a commit from a throwaway local clone, cached by commit sha
#   build-sim.sh WORKTREE     build the firmware checkout as-is (uncommitted changes included)
#
# Prints the directory holding `simulator` and `simulator-graphical` on the last stdout line.
# Build output goes to stderr and to <dir>/build.log.
set -euo pipefail

if [ "$(uname -sm)" != "Linux x86_64" ]; then
  echo "build-sim.sh: only x86_64 Linux is supported (this is $(uname -sm))" >&2
  exit 1
fi

# shellcheck disable=SC1091  # followed only with -x; env.sh is linted on its own
source "$(dirname "${BASH_SOURCE[0]}")/env.sh"
FW="${BITBOX_FIRMWARE:-$BITBOX_WORKSPACE/bitbox02-firmware}"
REF="${1:?usage: build-sim.sh <ref|WORKTREE>}"

# The Rust pin moved from src/rust/ to the repo root (f74c90519). Older refs kept it in src/rust/,
# where cargo runs for simulator-graphical (from the cmake build dir) did not see it, so export it.
rust_pin() {
  local f
  for f in "$1/rust-toolchain.toml" "$1/src/rust/rust-toolchain.toml"; do
    [ -f "$f" ] && { sed -n 's/^channel = "\(.*\)"/\1/p' "$f"; return; }
  done
  echo "no rust-toolchain.toml in $1" >&2
  return 1
}

build() {
  local src="$1" out="$2"
  mkdir -p "$out"
  RUSTUP_TOOLCHAIN="$(rust_pin "$src")"
  export RUSTUP_TOOLCHAIN
  rustup toolchain list | grep -q "^$RUSTUP_TOOLCHAIN" ||
    rustup toolchain install "$RUSTUP_TOOLCHAIN" --profile minimal -c rust-src >&2
  local bin=(simulator simulator-graphical)
  (
    cd "$src"
    ./scripts/bootstrap-cargo-config >&2
    cmake -S . -B build-e2e -DCOVERAGE=OFF -DSANITIZE_ADDRESS=OFF -DSANITIZE_UNDEFINED=OFF >&2
    make -C build-e2e -j"$(nproc)" "${bin[@]}" >&2
  ) > >(tee "$out/build.log" >&2) 2>&1
  for b in "${bin[@]}"; do cp "$src/build-e2e/bin/$b" "$out/"; done
  # The release name (bitbox02-multi-v9.28.0-simulator1.0.0-linux-amd64) is the simulator version
  # everywhere downstream; without it the build is unusable, and a version.txt left behind would
  # make the next run take it from the cache.
  local version
  if ! version="$("$out/simulator" --version 2>&1)" || [[ "$version" != bitbox02-*-simulator* ]]; then
    rm -f "$out/version.txt"
    echo "build-sim.sh: '$out/simulator --version' gave no release name (bitbox02-*-simulator*):" >&2
    echo "$version" >&2
    return 1
  fi
  echo "$version" > "$out/version.txt"
  # Host test harnesses (e.g. bitbox-api-ts) parse the firmware version out of the file name,
  # so also expose the binary under its release name.
  ln -sf simulator "$out/${version%%$'\n'*}"
}

if [ "$REF" = "WORKTREE" ]; then
  sha="$(git -C "$FW" rev-parse HEAD)"
  out="$BITBOX_E2E_CACHE/sims/worktree"
  build "$FW" "$out"
  dirty="$(git -C "$FW" status --short -- . ':!build*' | wc -l)"
  echo "WORKTREE (${sha:0:9} + $dirty uncommitted paths)" > "$out/source.txt"
  git -C "$FW" status --short -- . ':!build*' > "$out/dirty.txt"
  echo "$out"
  exit 0
fi

sha="$(git -C "$FW" rev-parse --verify "$REF^{commit}")"
out="$BITBOX_E2E_CACHE/sims/$sha"
if [ -x "$out/simulator" ] && [ -x "$out/simulator-graphical" ] && [ -s "$out/version.txt" ]; then
  echo "cached: $out" >&2
  echo "$out"
  exit 0
fi

src="$BITBOX_E2E_CACHE/src/bitbox02-firmware-$sha"
rm -rf "$src"
# --shared borrows objects from the local checkout: no network, and the user's repo is untouched.
git clone -q --shared --no-checkout "$FW" "$src"
git -C "$src" checkout -q --detach "$sha"
# Populate submodules from the local checkout's submodule repos; fall back to the network.
git -C "$src" ls-tree -r "$sha" | awk '$1 == "160000" { print $3, $4 }' | while read -r subsha path; do
  mkdir -p "$src/$path"
  if git -C "$FW/$path" cat-file -e "$subsha^{commit}" 2>/dev/null; then
    git -C "$FW/$path" archive "$subsha" | tar -x -C "$src/$path"
  else
    git -C "$src" submodule update --init --recursive -- "$path" >&2
  fi
done

build "$src" "$out"
echo "$REF ($sha)" > "$out/source.txt"
rm -rf "$src"
echo "$out"
