#!/usr/bin/env bash
# Install the native toolchain for e2e runs into $BITBOX_E2E_HOME (default ~/.local/share/bitbox-e2e).
# No root needed. Idempotent: rerun after bumping a pin below or the firmware's rust-toolchain.toml.
#
# Tools the host already has at a suitable version (LLVM 22, protoc, cmocka, cbindgen, bindgen, as in
# the firmware dev container) are used in place; env.sh resolves the same locations.
# Prerequisites that need root are checked first and listed in one go, nothing is installed then.
set -euo pipefail

if [ "$(uname -sm)" != "Linux x86_64" ]; then
  echo "setup.sh: only x86_64 Linux is supported (this is $(uname -sm)): the pinned downloads below are x86_64 builds" >&2
  exit 1
fi

source "$(dirname "${BASH_SOURCE[0]}")/env.sh"
FW="${BITBOX_FIRMWARE:-$BITBOX_WORKSPACE/bitbox02-firmware}"
T="$BITBOX_E2E_TOOLS"

# Pins follow bitbox02-firmware/Dockerfile.
LLVM_VERSION=22.1.8
PROTOC_VERSION=21.2
CBINDGEN_VERSION=0.29.2
BINDGEN_VERSION=0.72.1
CMOCKA_VERSION=1.1.7
CMOCKA_SHA256=810570eb0b8d64804331f82b29ff47c790ce9cd6b163e98d47a4807047ecad82
# Only installed when the host has none (or, for go, one too old for GOTOOLCHAIN=auto).
UV_VERSION=0.8.15
CMAKE_VERSION=4.4.4
AST_GREP_VERSION=0.39.4
GO_VERSION=1.26.10
NODE_VERSION=26.13.0
FOUNDRY_VERSION=1.5.1

step() { echo "==> $*" >&2; }

# Preflight: everything that needs root, reported in one pass.
apt_missing=() other_missing=()
need() {  # <command> <Debian/Ubuntu package>
  command -v "$1" >/dev/null || apt_missing+=("$1:$2")
}
need git git
need curl curl
need tar tar
need xz xz-utils
need unzip unzip
need gcc gcc
need make make
need c++ g++               # bitbox-api-rs's tests compile libbitcoinconsensus
need pkg-config pkg-config
need python3 python3
need sha256sum coreutils
need stdbuf coreutils
need nice coreutils
need c++filt binutils      # coverage.py demangles Rust symbols
need unshare util-linux    # isolated.sh
need ip iproute2           # isolated.sh brings up loopback in the namespace
need Xvfb xvfb             # graphical simulator
# simulator-graphical dlopens these (winit X11 backend, glutin EGL/GLX); Mesa's swrast renders on Xvfb.
libs="$(PATH="$PATH:/sbin:/usr/sbin" ldconfig -p 2>/dev/null || true)"
need_lib() {  # <soname> <Debian/Ubuntu package>
  grep -qF "$(printf '\t')$1 (" <<<"$libs" || apt_missing+=("$1:$2")
}
need_lib libEGL.so.1 libegl1
need_lib libGL.so.1 libgl1
need_lib libX11-xcb.so.1 libx11-xcb1
need_lib libXcursor.so.1 libxcursor1
need_lib libXi.so.6 libxi6
need_lib libXrender.so.1 libxrender1
need_lib libxkbcommon-x11.so.0 libxkbcommon-x11-0
[ -e /usr/lib/x86_64-linux-gnu/dri/swrast_dri.so ] || [ -e /usr/lib64/dri/swrast_dri.so ] ||
  apt_missing+=("swrast_dri.so:libgl1-mesa-dri")
# bitbox-api-rs's simulator tests link the system OpenSSL (openssl-sys) and libstdc++ (bitcoinconsensus).
pkg-config --exists openssl 2>/dev/null || apt_missing+=("openssl.pc:libssl-dev")
# rustc links with cc, and a GCC only finds its own version's libstdc++: the firmware dev container
# has cc = gcc-10 next to g++-11. Linked to a temp file; as root, ld may replace /dev/null.
if command -v cc >/dev/null; then
  probe="$(mktemp)"
  echo 'int main(void) { return 0; }' | cc -x c - -lstdc++ -o "$probe" 2>/dev/null ||
    apt_missing+=("libstdc++ for cc:libstdc++-$(cc -dumpversion | cut -d. -f1)-dev")
  rm -f "$probe"
fi
for t in rustup cargo; do command -v "$t" >/dev/null || other_missing+=("$t"); done

if [ ${#apt_missing[@]} -gt 0 ] || [ ${#other_missing[@]} -gt 0 ]; then
  echo "setup.sh: missing prerequisites for build-sim.sh, the units and the API suites:" >&2
  for m in "${apt_missing[@]}"; do printf '  %-24s package %s\n' "${m%%:*}" "${m#*:}" >&2; done
  for m in "${other_missing[@]}"; do printf '  %-24s install rustup without root: https://rustup.rs\n' "$m" >&2; done
  if [ ${#apt_missing[@]} -gt 0 ]; then
    echo "as root (Debian/Ubuntu): apt-get install -y $(printf '%s\n' "${apt_missing[@]#*:}" | sort -u | paste -sd ' ')" >&2
  fi
  exit 1
fi

mkdir -p "$T/bin"

if ! command -v uv >/dev/null; then
  step "uv $UV_VERSION"
  curl -fsSL "https://github.com/astral-sh/uv/releases/download/$UV_VERSION/uv-x86_64-unknown-linux-gnu.tar.gz" \
    | tar -xz -C "$T/bin" --strip-components=1
fi

if ! command -v cmake >/dev/null; then
  step "cmake $CMAKE_VERSION"
  UV_TOOL_DIR="$T/uv-tools" UV_TOOL_BIN_DIR="$T/bin" uv tool install "cmake==$CMAKE_VERSION" >&2
fi

# coverage.py parses Rust fn items with it. Not `sg`: on Debian/Ubuntu that is newgrp, so only the
# ast-grep binary from the release zip goes onto PATH.
if ! command -v ast-grep >/dev/null; then
  step "ast-grep $AST_GREP_VERSION"
  tmp="$(mktemp -d)"
  curl -fsSL -o "$tmp/ast-grep.zip" "https://github.com/ast-grep/ast-grep/releases/download/$AST_GREP_VERSION/app-x86_64-unknown-linux-gnu.zip"
  unzip -qo "$tmp/ast-grep.zip" ast-grep -d "$T/bin"
  rm -rf "$tmp"
fi

# api-go (lane B) and the wallet-app flows. Go before 1.21 can't fetch the toolchain go.mod asks for.
# GOTOOLCHAIN=local: inside a module, `go version` would otherwise download that toolchain.
go_minor="$(GOTOOLCHAIN=local go version 2>/dev/null | sed -n 's/^go version go1\.\([0-9]*\).*/\1/p' || true)"
if [ "${go_minor:-0}" -lt 21 ]; then
  step "go $GO_VERSION (host go missing or older than 1.21)"
  rm -rf "$T/go" && mkdir -p "$T/go"
  curl -fsSL "https://go.dev/dl/go$GO_VERSION.linux-amd64.tar.gz" | tar -xz -C "$T/go" --strip-components=1
fi

# api-ts (lane B) needs node >= 20, the wallet-app flows node 26; env.sh found none of setup.sh's, mise's or PATH's.
if [ -z "${BITBOX_NODE_BIN:-}" ]; then
  step "node $NODE_VERSION (no node 26 from mise or PATH)"
  rm -rf "$T/node" && mkdir -p "$T/node"
  curl -fsSL "https://nodejs.org/dist/v$NODE_VERSION/node-v$NODE_VERSION-linux-x64.tar.xz" \
    | tar -xJ -C "$T/node" --strip-components=1
fi

# Rabby flow only; rabby/run.py expects it at this path.
if [ ! -x "$T/foundry/bin/anvil" ]; then
  step "anvil (foundry $FOUNDRY_VERSION)"
  mkdir -p "$T/foundry/bin"
  curl -fsSL "https://github.com/foundry-rs/foundry/releases/download/v$FOUNDRY_VERSION/foundry_v${FOUNDRY_VERSION}_linux_amd64.tar.gz" \
    | tar -xz -C "$T/foundry/bin" anvil
fi

# env.sh points LIBCLANG_PATH at our copy if there is one, else at a system LLVM 22.
llvm_ok=
if [ "$LIBCLANG_PATH" = "$T/llvm/lib" ]; then
  if [ "$("$T/llvm/bin/clang" --version 2>/dev/null | sed -n 's/.*clang version \([0-9.]*\).*/\1/p')" = "$LLVM_VERSION" ]; then
    llvm_ok=1
  fi
elif compgen -G "$LIBCLANG_PATH/libclang.so*" >/dev/null || compgen -G "$LIBCLANG_PATH/libclang-[0-9]*.so*" >/dev/null; then
  llvm_ok=1
  step "LLVM: using the system's libclang in $LIBCLANG_PATH"
fi
if [ -z "$llvm_ok" ]; then
  step "LLVM $LLVM_VERSION (clang + libclang for bindgen)"
  rm -rf "$T/llvm" "$T/llvm.tmp" && mkdir -p "$T/llvm.tmp"
  # The release tarball is ~14G unpacked. Extract only libclang with clang's resource dir (headers,
  # compiler-rt incl. the profile runtime for coverage builds) and the tools used for building and
  # formatting, while streaming, so the peak disk use is the final size (~1.1G).
  top="LLVM-$LLVM_VERSION-Linux-X64"
  curl -fsSL "https://github.com/llvm/llvm-project/releases/download/llvmorg-$LLVM_VERSION/$top.tar.xz" \
    | tar -xJ -C "$T/llvm.tmp" --strip-components=1 --wildcards \
      "$top/bin/"{clang,clang-"${LLVM_VERSION%%.*}",clang++,clang-cpp,clang-format,clang-tidy,git-clang-format,ld.lld,lld} \
      "$top/bin/"{llvm-ar,llvm-nm,llvm-objcopy,llvm-strip,llvm-config,llvm-cov,llvm-profdata,llvm-symbolizer} \
      "$top/lib/libclang.so*" "$top/lib/clang"
  mv "$T/llvm.tmp" "$T/llvm"
fi

# PATH has our copy first, then the host's (/opt/protoc in the firmware container).
if [ "$(protoc --version 2>/dev/null)" != "libprotoc 3.$PROTOC_VERSION" ]; then
  step "protoc $PROTOC_VERSION"
  rm -rf "$T/protoc" && mkdir -p "$T/protoc"
  curl -fsSL -o "$T/protoc.zip" "https://github.com/protocolbuffers/protobuf/releases/download/v$PROTOC_VERSION/protoc-$PROTOC_VERSION-linux-x86_64.zip"
  unzip -qo "$T/protoc.zip" -d "$T/protoc" && rm "$T/protoc.zip"
fi

if ! pkg-config --exists cmocka 2>/dev/null; then
  step "cmocka $CMOCKA_VERSION (required at cmake configure time)"
  tmp="$(mktemp -d)"
  curl -fsSL -o "$tmp/cmocka.tar.xz" "https://cmocka.org/files/1.1/cmocka-$CMOCKA_VERSION.tar.xz"
  echo "$CMOCKA_SHA256  $tmp/cmocka.tar.xz" | sha256sum -c - >&2
  tar -xJf "$tmp/cmocka.tar.xz" -C "$tmp"
  cmake -S "$tmp/cmocka-$CMOCKA_VERSION" -B "$tmp/build" -DCMAKE_INSTALL_PREFIX="$T/cmocka" \
    -DCMAKE_POLICY_VERSION_MINIMUM=3.5 -DWITH_EXAMPLES=OFF >/dev/null
  cmake --build "$tmp/build" -j"$(nproc)" >/dev/null && cmake --install "$tmp/build" >/dev/null
  rm -rf "$tmp"
fi

if ! pkg-config --exists hidapi-hidraw 2>/dev/null; then
  step "hidapi-hidraw pkg-config stub"
  mkdir -p "$T/pkgconfig-stubs"
  cat > "$T/pkgconfig-stubs/hidapi-hidraw.pc" <<'EOF'
# Configure-time stub: firmware CMake requires hidapi for the u2f unit tests only.
# The simulator targets never link it. Install libhidapi-dev to build u2f tests.
Name: hidapi-hidraw
Description: stub
Version: 0.0.0
Libs:
Cflags:
EOF
fi

pin_file="$FW/rust-toolchain.toml"
[ -f "$pin_file" ] || pin_file="$FW/src/rust/rust-toolchain.toml"  # refs before f74c90519
FW_RUST="$(sed -n 's/^channel = "\(.*\)"/\1/p' "$pin_file")"
step "rust $FW_RUST (firmware pin)"
rustup toolchain install "$FW_RUST" --profile minimal -c clippy,rust-src,rustfmt \
  -t thumbv7em-none-eabi,thumbv8m.main-none-eabihf >&2

# Resolved through PATH, so the host's copies count (/opt/cargo/bin in the firmware container).
cargo_tool() {  # <binary> <version-substring> <cargo install args...>
  local bin="$1" ver="$2"; shift 2
  if ! "$bin" --version 2>/dev/null | grep -q -- "$ver"; then
    step "$bin $ver"
    cargo +"$FW_RUST" install --locked "$@" >&2
  fi
}
cargo_tool cbindgen "$CBINDGEN_VERSION" cbindgen --version "$CBINDGEN_VERSION"
cargo_tool bindgen "$BINDGEN_VERSION" bindgen-cli --version "$BINDGEN_VERSION"
# Regenerates the Rust protobufs during the firmware build; it has no --version, so always refresh.
step "prost-build-proto"
cargo +"$FW_RUST" install --locked --path "$FW/tools/prost-build-proto" >&2

step "e2e driver"
cargo build --release -q --manifest-path "$BITBOX_E2E_DIR/driver/Cargo.toml"

# Not needed for the units: reported, setup still succeeds.
warn=()
if ! err="$("$BITBOX_E2E_DIR/isolated.sh" true 2>&1)"; then
  warn+=("units: isolated.sh can't run, so no parallel isolated runs. $err")
fi
# The flows download Playwright's Chromium themselves; its system libraries need root.
chrome="$(compgen -G "${PLAYWRIGHT_BROWSERS_PATH:-$HOME/.cache/ms-playwright}/chromium-*/chrome-linux*/chrome" | tail -1 || true)"
playwright="$(sed -n 's/.*"playwright": "\(.*\)".*/\1/p' "$BITBOX_E2E_DIR/rabby/package.json")"
deps="install them as root: npx -y playwright@$playwright install-deps chromium"
if [ -z "$chrome" ]; then
  warn+=("wallet-compare, wc, rabby: Playwright Chromium not downloaded yet (the flows do that), its system libraries are unchecked; $deps")
elif missing_libs="$(ldd "$chrome" | awk '/not found/ { print $1 }' | tr '\n' ' ')" && [ -n "$missing_libs" ]; then
  warn+=("wallet-compare, wc, rabby: Chromium can't start, missing ${missing_libs}; $deps")
fi
if [ ${#warn[@]} -gt 0 ]; then
  echo "setup.sh: optional, per flow:" >&2
  printf '  %s\n' "${warn[@]}" >&2
fi

step "done; next: source e2e/env.sh, then e2e/build-sim.sh <ref> or e2e/e2e.py --help"
