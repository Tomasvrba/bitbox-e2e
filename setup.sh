#!/usr/bin/env bash
# Install the native toolchain for e2e runs into $BITBOX_E2E_HOME (default ~/.local/share/bitbox-e2e).
# No root needed. Idempotent: rerun after bumping a pin below or the firmware's rust-toolchain.toml.
#
# Expects on the host: git, curl, tar, unzip, gcc, make, pkg-config, python3, cargo/rustup, go, uv,
# node (or mise), Xvfb and Mesa (libgl1-mesa-dri) for the graphical simulator.
set -euo pipefail

source "$(dirname "${BASH_SOURCE[0]}")/env.sh"
FW="${BITBOX_FIRMWARE:-$BITBOX_WORKSPACE/bitbox02-firmware}"
T="$BITBOX_E2E_TOOLS"
mkdir -p "$T"

# Pins follow bitbox02-firmware/Dockerfile.
LLVM_VERSION=22.1.8
PROTOC_VERSION=21.2
CBINDGEN_VERSION=0.29.2
BINDGEN_VERSION=0.72.1
CMOCKA_VERSION=1.1.7
CMOCKA_SHA256=810570eb0b8d64804331f82b29ff47c790ce9cd6b163e98d47a4807047ecad82

need() { command -v "$1" >/dev/null || { echo "missing host tool: $1" >&2; exit 1; }; }
for t in git curl tar unzip gcc make pkg-config python3 rustup cargo go uv Xvfb; do need "$t"; done

step() { echo "==> $*" >&2; }

if [ "$("$T/llvm/bin/clang" --version 2>/dev/null | sed -n 's/.*clang version \([0-9.]*\).*/\1/p')" != "$LLVM_VERSION" ]; then
  step "LLVM $LLVM_VERSION (clang + libclang for bindgen)"
  rm -rf "$T/llvm" "$T/llvm.tmp" && mkdir -p "$T/llvm.tmp"
  curl -fsSL "https://github.com/llvm/llvm-project/releases/download/llvmorg-$LLVM_VERSION/LLVM-$LLVM_VERSION-Linux-X64.tar.xz" \
    | tar -xJ -C "$T/llvm.tmp" --strip-components=1
  # The release tarball is ~14G unpacked; keep what the firmware build and formatting use.
  find "$T/llvm.tmp/lib" -maxdepth 1 -name '*.a' -delete
  keep=" clang clang-${LLVM_VERSION%%.*} clang++ clang-cpp clang-format clang-tidy git-clang-format ld.lld lld
         llvm-ar llvm-nm llvm-objcopy llvm-strip llvm-config llvm-cov llvm-profdata llvm-symbolizer "
  for f in "$T/llvm.tmp/bin/"*; do
    [[ "$keep" == *" $(basename "$f") "* ]] || rm -f "$f"
  done
  mv "$T/llvm.tmp" "$T/llvm"
fi

if [ "$("$T/protoc/bin/protoc" --version 2>/dev/null)" != "libprotoc 3.$PROTOC_VERSION" ]; then
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

command -v cmake >/dev/null || { step "cmake"; uv tool install cmake; }

pin_file="$FW/rust-toolchain.toml"
[ -f "$pin_file" ] || pin_file="$FW/src/rust/rust-toolchain.toml"  # refs before f74c90519
FW_RUST="$(sed -n 's/^channel = "\(.*\)"/\1/p' "$pin_file")"
step "rust $FW_RUST (firmware pin)"
rustup toolchain install "$FW_RUST" --profile minimal -c clippy,rust-src,rustfmt \
  -t thumbv7em-none-eabi,thumbv8m.main-none-eabihf >&2

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

if command -v mise >/dev/null && ! mise where node@26 >/dev/null 2>&1; then
  step "node 26 (wallet-app engines)"
  mise install node@26 >&2
fi

step "e2e driver"
cargo build --release -q --manifest-path "$BITBOX_E2E_DIR/driver/Cargo.toml"

step "done; next: e2e/build-sim.sh <ref> or e2e/e2e.py --help"
