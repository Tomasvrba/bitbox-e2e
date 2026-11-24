# shellcheck shell=bash
# Source this to get the native (no docker) toolchain used by the e2e scripts.
# Everything setup.sh installs lives under $BITBOX_E2E_HOME so nothing needs root. Where the host
# already has a suitable tool (e.g. the firmware dev container), that one is used in place.

export BITBOX_E2E_HOME="${BITBOX_E2E_HOME:-$HOME/.local/share/bitbox-e2e}"
export BITBOX_E2E_TOOLS="$BITBOX_E2E_HOME/tools"
export BITBOX_E2E_CACHE="${BITBOX_E2E_CACHE:-$HOME/.cache/bitbox-e2e}"
# zsh sets $0 to the sourced file; bash only sets BASH_SOURCE.
export BITBOX_WORKSPACE="${BITBOX_WORKSPACE:-$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)}"
export BITBOX_E2E_DIR="$BITBOX_WORKSPACE/e2e"

# rustup won't install toolchains or components unless CARGO_HOME is where rustup itself lives. The
# firmware container installs it to /opt/cargo and sets RUSTUP_HOME but not CARGO_HOME.
if [ -z "${CARGO_HOME:-}" ] && [ ! -x "$HOME/.cargo/bin/rustup" ] && _r="$(command -v rustup 2>/dev/null)" &&
  [ "$_r" != /usr/bin/rustup ] && [ -x "$(dirname "$_r")/cargo" ]; then
  CARGO_HOME="$(dirname "$(dirname "$_r")")"
  export CARGO_HOME
fi
unset _r

# tools/bin: single binaries (uv) and uv-installed tools (cmake). The protoc and llvm dirs
# only exist when setup.sh had to download them; a missing PATH entry is harmless.
export PATH="$BITBOX_E2E_TOOLS/bin:$BITBOX_E2E_TOOLS/llvm/bin:$BITBOX_E2E_TOOLS/protoc/bin:$HOME/.cargo/bin:$HOME/.local/bin:$PATH"

# bindgen-cli loads libclang at runtime via clang-sys. setup.sh's LLVM if it downloaded one, else a
# system LLVM 22 (apt.llvm.org's llvm-22 in the firmware dev container).
export LIBCLANG_PATH="$BITBOX_E2E_TOOLS/llvm/lib"
if [ ! -x "$BITBOX_E2E_TOOLS/llvm/bin/clang" ]; then
  for _c in llvm-config-22 llvm-config; do
    case "$("$_c" --version 2>/dev/null)" in 22.*) LIBCLANG_PATH="$("$_c" --libdir)"; break ;; esac
  done
  unset _c
fi

export PKG_CONFIG_PATH="$BITBOX_E2E_TOOLS/cmocka/lib/pkgconfig:$BITBOX_E2E_TOOLS/pkgconfig-stubs${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"
export LD_LIBRARY_PATH="$BITBOX_E2E_TOOLS/cmocka/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

# setup.sh installs Go only when the host's is missing or older than 1.21, which can't fetch the
# toolchain go.mod asks for. The firmware container exports GOROOT for its Go 1.19; override it.
if [ -x "$BITBOX_E2E_TOOLS/go/bin/go" ]; then
  export GOROOT="$BITBOX_E2E_TOOLS/go"
  export PATH="$GOROOT/bin:$PATH"
fi
# wallet-app go.mod pins a newer Go than most distros ship; let the go command fetch it.
export GOTOOLCHAIN="${GOTOOLCHAIN:-auto}"

# Node 26 (wallet-app engines: >=26.12.1 <27): setup.sh's copy, else mise's node@26, else PATH node
# if it is 26. BITBOX_NODE_BIN is that node's bin dir, unset when there is none: the flows that need
# node 26 (app tests, wc, rabby) must refuse to run then instead of using whatever node is on PATH.
unset BITBOX_NODE_BIN
if [ -x "$BITBOX_E2E_TOOLS/node/bin/node" ]; then
  BITBOX_NODE_BIN="$BITBOX_E2E_TOOLS/node/bin"
elif _d="$(mise where node@26 2>/dev/null)" && [ -x "$_d/bin/node" ]; then
  BITBOX_NODE_BIN="$_d/bin"
else
  case "$(node --version 2>/dev/null)" in v26.*) BITBOX_NODE_BIN="$(dirname "$(command -v node)")" ;; esac
fi
unset _d
if [ -n "${BITBOX_NODE_BIN:-}" ]; then
  export BITBOX_NODE_BIN
  export PATH="$BITBOX_NODE_BIN:$PATH"
fi
