# shellcheck shell=bash
# Source this to get the native (no docker) toolchain used by the e2e scripts.
# Everything lives under $BITBOX_E2E_HOME so nothing needs root.

export BITBOX_E2E_HOME="${BITBOX_E2E_HOME:-$HOME/.local/share/bitbox-e2e}"
export BITBOX_E2E_TOOLS="$BITBOX_E2E_HOME/tools"
export BITBOX_E2E_CACHE="${BITBOX_E2E_CACHE:-$HOME/.cache/bitbox-e2e}"
# zsh sets $0 to the sourced file; bash only sets BASH_SOURCE.
export BITBOX_WORKSPACE="${BITBOX_WORKSPACE:-$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")/.." && pwd)}"
export BITBOX_E2E_DIR="$BITBOX_WORKSPACE/e2e"

export PATH="$BITBOX_E2E_TOOLS/llvm/bin:$BITBOX_E2E_TOOLS/protoc/bin:$HOME/.cargo/bin:$HOME/.local/bin:$PATH"
# bindgen-cli loads libclang at runtime via clang-sys.
export LIBCLANG_PATH="$BITBOX_E2E_TOOLS/llvm/lib"
export PKG_CONFIG_PATH="$BITBOX_E2E_TOOLS/cmocka/lib/pkgconfig:$BITBOX_E2E_TOOLS/pkgconfig-stubs${PKG_CONFIG_PATH:+:$PKG_CONFIG_PATH}"
export LD_LIBRARY_PATH="$BITBOX_E2E_TOOLS/cmocka/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"

# wallet-app go.mod pins a newer Go than most distros ship; let the go command fetch it.
export GOTOOLCHAIN="${GOTOOLCHAIN:-auto}"

# wallet-app frontend requires node >=26.12.1. mise is optional; fall back to PATH node.
if command -v mise >/dev/null 2>&1; then
  BITBOX_NODE_BIN="$(mise where node@26 2>/dev/null)/bin"
  [ -d "$BITBOX_NODE_BIN" ] && export PATH="$BITBOX_NODE_BIN:$PATH"
fi

export E2E_DISPLAY="${E2E_DISPLAY:-:77}"
