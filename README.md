# BitBox simulator e2e

Per firmware change: run every unit on the head simulator, diff them against the merge-base, and
check everything that has expectations. Evidence is the exact device screen
text (headless C simulator transcript), LCD screenshots (graphical simulator on Xvfb) and the
host API simulator suites. Everything builds natively, without docker and without root. The
contract between the pieces is `DESIGN.md`.

## Setup (once per machine)

```sh
e2e/setup.sh          # LLVM 22, protoc 21.2, cmocka, cbindgen, bindgen, rust pin, driver; if missing: uv, cmake, ast-grep, go, node 26, anvil
source e2e/env.sh     # PATH, LIBCLANG_PATH, PKG_CONFIG_PATH, BITBOX_NODE_BIN; source it before e2e.py and the flows
```

x86_64 Linux only. `setup.sh` first checks what needs root and, if anything is missing, lists all
of it with the `apt-get install` line and stops: git, curl, tar, xz, unzip, gcc, make, pkg-config,
python3, coreutils, binutils (`c++filt`), util-linux (`unshare`), iproute2 (`ip`), g++, libssl-dev
and a libstdc++ that `cc` links (api-rs suite), Xvfb and the graphical simulator's libraries
(`libegl1 libgl1 libx11-xcb1 libxcursor1 libxi6 libxrender1 libxkbcommon-x11-0 libgl1-mesa-dri`).
rustup has to be there too (no root needed).

The rest goes into `~/.local/share/bitbox-e2e/tools` (`$BITBOX_E2E_HOME/tools`, ~1.5G fresh)
unless the host already has it. LLVM 22 with libclang (`llvm-config-22`, e.g. apt.llvm.org's),
protoc 21.2, cmocka, cbindgen 0.29.2 and bindgen 0.72.1 on `PATH` are used in place, as in the
firmware dev container (`shiftcrypto/firmware_v2`). uv, cmake, ast-grep (invoked as `ast-grep`;
`sg` is `newgrp` on Debian/Ubuntu), Go (when the host's is older than 1.21), node 26 (when neither
mise's `node@26` nor the `PATH` node is 26) and anvil (Rabby flow) are downloaded at pinned
versions. Builds, clones and baselines go to `~/.cache/bitbox-e2e` (`$BITBOX_E2E_CACHE`).

Pins: every `setup.sh` download is checked against the sha256 next to its version before it is
unpacked. To bump one, set the version, download that exact file, compare its `sha256sum` with the
publisher's (GitHub release asset digest, go.dev/dl, nodejs `SHASUMS256.txt`, PyPI for the cmake
wheel) and pin it. Python deps are locked per script in `<script>.py.lock` (uv >= 0.5.17, else
`setup.sh` installs its pinned uv; mitmproxy runs from `rabby/mitmdump.py`). After changing a
script's dependencies or to upgrade, run `uv lock --script <script>.py [--upgrade]` and commit the
lock; `uv lock --script <script>.py --check` fails on a stale one (`uv run` rewrites stale locks).

Reported at the end without failing: `isolated.sh` needs unprivileged user namespaces (Ubuntu
23.10+ restricts them via AppArmor, Docker's default seccomp profile blocks them; `isolated.sh
true` fails with the reason), and the browser flows need Playwright Chromium's system libraries
(as root: `npx -y playwright@<version> install-deps chromium`).

## Firmware PR: `e2e.py pr`

```sh
e2e/e2e.py pr --base <upstream>/master --head my-branch --out /tmp/e2e/my-branch \
  [--graphical 'btc-vector/*fee*' ...] [-j N]
```

`<upstream>` is whichever remote of your firmware checkout points at BitBoxSwiss/bitbox02-firmware
(`git remote -v`; `origin` in CI). `-j` defaults to half the CPUs (2 on a 4-vCPU CI runner).

`--head WORKTREE` builds the firmware checkout as-is, uncommitted changes included. Steps:

1. Baseline bundle for `merge-base(base, head)`: reused from the cache, recorded if missing (see
   `baseline`).
2. Build head. Every unit runs headless on it (88 units: ~40s at `-j 4`). With a reused bundle
   the merge-base simulator is only built if base-side graphical runs are missing (step 3).
3. **Lane A, evidence**: each unit's head screens are diffed against the bundle. Units whose
   screens changed (one per distinct change), new units and the `--graphical` globs also run on
   the graphical simulator, on both sides.
4. **Lane B, regression**: every unit with expectations is checked on head (pass/fail), and the
   api-go/api-rs/api-ts simulator suites run against head, once with each repo's committed vector
   copy (drift) and once with the firmware's vectors (regression).

Output: `report.md` (relative images, for CI artifacts), `report.html` (self-contained), `pr.json`.

Exit code (the gate): 0 no regression, 1 regression, 2 the harness itself failed. A regression is
a unit that completed on the merge-base and fails on head, or, in the API suites' run with the
firmware's vectors, a test that passed on the merge-base and fails on head or a suite that errors
on head (build failure, crash, no tests ran). The reasons are `verdict` in `pr.json` and the top
of the report. Reported, not gating: changed screens, expectation mismatches (expectations live
here and are promoted after the firmware PR merges) and API tests failing with the API repos'
committed vector copies (those assert the old screens until the copies are synced).

### Units

A unit is one scenario run (`units.py <firmware-dir>` lists them):

- `scenario/<name>`: `scenarios/*.json`, `{ name, description, steps: [ { op, name, ... } ], expect? }`.
  Ops and their fields are the `Step` enum in `driver/src/main.rs` (`eth-address`, `eth-sign`,
  `eth-sign-1559`, `eth-sign-message`, `eth-sign-typed-data`, `btc-sign-psbt`). Byte fields are hex.
  `eth-sign-typed-data` takes the EIP-712 JSON object (as passed to `eth_signTypedData_v4`) in `typed_data`. Add an op there when
  a scenario needs another API call; the driver links the sibling `../bitbox-api-rs` checkout.
- `btc-vector/<id>`: one per vector in the firmware's
  `src/rust/bitbox-test-vectors/testdata/btc-transaction-test-vectors.json`, signed with
  `btc-sign-psbt`. Their expectations are version-ranged and owned by the firmware repo. Refs
  before that file (firmware 3bdaf583b) have no btc-vector units and no firmware-vector API run;
  the report says so.
- `eth-matrix/<chain id>-<case>`: generated by `eth_matrix.py`, the same requests on chains 1, 10,
  56, 137, 8453, 42161, 11155111 and the unknown chain 123456. Cases: `address` (display),
  `legacy` and `eip1559` native transfers, `erc20-known` (USDC; the firmware registry is
  mainnet-only, so chain 1 only), `erc20-unknown` (each chain's USDC, unknown to the firmware off
  mainnet), `personal-sign`, `typed-data` (USDC permit). 49 units, ~20s headless at `-j 4`.
  Expectations live in `scenarios/eth-matrix/<chain id>-<case>.json`, written by `promote`.

### Reading the report

- Verdict: the gate's reasons, and the non-gating counts (changed units, expectation mismatches).
- Summary: units run, changed/new/failed counts, expectation
  pass/fail, **changed functions no unit reached** (the change has no evidence; add a scenario)
  and changed files the coverage map cannot see (C, protos, token lists, build files), how units
  and API suites ran (run mode, see below), phase timings.
- API simulator suites: per suite, committed vs firmware vectors, head and base.
- Expectation failures: diff of expected vs head screens.
- Changed units: headless screen diff (authoritative: full addresses, no scrolling) and the
  graphical frames that differ, aligned by content hash. Units with the identical change are folded
  into the first one.
- Graphical-only differences: same text, different frames. Usually layout/font changes; capture
  timing is also possible, rerun the unit to tell.
- The base harness revision is shown; a baseline recorded by another harness revision can produce
  harness diffs. So are the other revisions under test: the sibling `bitbox-api-rs` tree the driver
  links (`+dirty` with uncommitted changes) and the API repos' revisions the suites ran. A
  difference between base and head is flagged under the table; it does not gate.

Device setup (pairing code, unlock, seeding) differs every run and is kept out of the diffs.
Signatures differ between runs because anti-klepto randomizes the host nonce.

### `baseline`, `promote`

```sh
e2e/e2e.py baseline <firmware-ref> [--graphical <glob>...] [--force]
e2e/e2e.py promote /tmp/e2e/my-branch scenario/eth-erc20-unknown ...
```

`baseline` records `$BITBOX_E2E_CACHE/baselines/<sha>/`: every unit headless, lane B results, and
graphical runs for the given globs (added incrementally later by `pr`). CI publishes it as a
tarball. It fails (exit 2) instead of recording a bundle where no unit completed.
`promote`, after a maintainer approved the head screens in a report, writes them into the
scenario file's `expect` for the head simulator version (scenario units only). `pr.json` stores
scenario paths relative to the harness checkout; `promote` only writes under its `scenarios/`.

### Coverage

```sh
e2e/build-sim.sh --coverage <ref>               # instrumented headless sim, sims/<sha>-cov/
e2e/coverage.py map <ref> [-j N]                # every unit on it -> baselines/<sha>/coverage/map.json
e2e/coverage.py select --base <sha> --head <ref|WORKTREE> --summary
```

`select` maps the diff's changed Rust lines (only code linked into the simulator, no test code)
to the units executing them, lists changed functions that no unit executes, and lists the changed
files the map has no lines for (C is not instrumented; protos, token lists, build files). `pr`
uses it, when the merge-base bundle has a map, for the unreached list only;
it never limits which units run.

## Single scenario

```sh
e2e/e2e.py device --scenario <json> --sim-dir $(e2e/build-sim.sh <ref>) --out <dir> [--no-graphical|--no-headless]
```

For ad-hoc scenarios that are not units yet (e.g. while drafting one), or a generated unit:
`units.py <firmware-dir> --scenario eth-matrix/8453-erc20-unknown > /tmp/s.json`.

## Parallel runs, `isolated.sh`, `E2E_DRIVER`

Each headless run starts its simulator on a free port (`--port`), so units run side by side
without anything else. Graphical runs do the same when the ref's `simulator-graphical` has
`--port` (detected per binary from `--help`; firmware before a540c993b has none). Without it, it
always binds 15423: each run then goes into its own network namespace (`isolated.sh <cmd>`,
unprivileged, loopback only), or, where namespaces are unavailable (most CI containers;
`isolated.sh true` says why), runs one at a time. The API suites connect to 15423 too: in
namespaces all at once, else one at a time. The report's *Run mode* line says what ran how.
Every Xvfb picks a free display itself (`-displayfd`).

There is no network inside a namespace: build outside and set `E2E_DRIVER=<path>` to a prebuilt
driver, which also skips the cargo build.

```sh
E2E_DRIVER=/tmp/driver e2e/isolated.sh e2e/e2e.py device --scenario ... --out ...
```

`flake.py` repeats graphical runs (default 10, half the CPUs at a time) and reports, per scenario
or unit, how many produced the identical frame sequence, failures, and run times:

```sh
E2E_DRIVER=/tmp/driver e2e/flake.py --sim-dir <dir> --out /tmp/e2e/flake scenarios/*.json --unit 'btc-vector/op-return'
```

## BitBoxApp: base vs head

```sh
e2e/e2e.py wallet-compare --base <upstream>/master --head my-branch --firmware <fw-ref|WORKTREE> \
  --out /tmp/e2e/wallet tests/watch-only.test.ts
```

Runs the app's own Playwright tests (`frontends/web/tests`) from clean clones of both refs, with
one natively built simulator, Chromium only, no retries, and screenshot/video/trace always on
(`wallet/playwright.evidence.config.ts`). The report lists per-test status, errors, device screen
diffs from each test's `simulator.log`, and final screenshots that differ. Exits 1 when a test that
passed on base does not pass on head.

Use the upstream remote for bases: whichever remote points at the BitBoxSwiss repo (`git remote -v`;
a personal fork's `origin` can be stale), `develop` of the one pointing at RabbyHub/Rabby in Rabby.

wallet-compare, `wc` and `rabby` need node 26 (the app's engines): they stop right away unless
`env.sh` found one (`BITBOX_NODE_BIN`; `setup.sh` installs it), and put it first on `PATH` for
npm, npx and node. The api-ts suite uses it too.

## WalletConnect: dapp -> app -> device

```sh
e2e/e2e.py wc --base <app-ref> --head <app-ref> --out /tmp/e2e/wc [--firmware <fw-ref|WORKTREE>]
e2e/wc/flow.py run --ref <app-ref> --out <dir>           # one side; flow.py same <dir>... compares runs
```

`--firmware` defaults to `master` of the firmware remote pointing at BitBoxSwiss/bitbox02-firmware
(else `origin/master`). `wc/dapp.mjs` (`@walletconnect/sign-client`) pairs with the app over the public relay and sends
`personal_sign`, `eth_signTypedData_v4` (Sepolia, mainnet), `eth_signTransaction` (Sepolia) and
`eth_sendTransaction` (Arbitrum, Polygon), and verifies the signer. `wallet/tests/walletconnect.test.ts`,
copied into the app clone, approves the session and each request in the app; `wc/device.py` runs
the graphical simulator (preseeded) and confirms everything on device. Per step the report shows
the app screenshots (request, result) next to the device frames, base vs head, changed ones flagged.
Exits 1 when either side's flow failed (Playwright test failed, or a signature recovered to another
account).

- Runs in the host network namespace (relay, Etherscan proxy), not in `isolated.sh`, so ports
  8080/8082/15423 must be free. Not runnable offline or where the relay is blocked.
- Transaction steps get gas from the live Etherscan proxy: their Total/Fee device screens differ
  between any two runs. Signing steps and app screenshots are reproducible.
- App screenshots hide the banners (testnet, update, attestation, microSD).
- No BTC: the app only handles `eip155` over WalletConnect.

## Rabby extension: dapp -> Rabby -> device

```sh
e2e/e2e.py rabby --base <fw-ref> --head <fw-ref|WORKTREE> --out /tmp/e2e/rabby [--rabby-ref v0.94.15]
e2e/rabby/rabby_e2e.py repeat --firmware <fw-ref> -n 3 --out <dir>   # same flow N times, device frame identity
e2e/rabby/rabby_e2e.py record --firmware <fw-ref> --out <dir>        # re-record rabby/fixtures/rabby-api.json.gz (network)
e2e/rabby/rabby_e2e.py report <dir>
```

The unmodified Rabby extension (built unpacked from a `--shared` clone of `$BITBOX_RABBY`, default
`../Rabby`, cached in `$BITBOX_E2E_CACHE/src/rabby-<ref>`, ~10 min the first time) runs in
Playwright Chromium and talks to the graphical simulator through `bridge/sim_bridge.py`, a fake
BitBoxBridge (`127.0.0.1:8178`, U2F-WS frames <-> U2F-HID packets on the simulator's TCP port).
`rabby/flow.ts` onboards (password, Import -> Hardware Wallet -> BitBox02, unlock + pairing on
device, first address), sets each chain's custom RPC to a local anvil, connects the test dapp
(`rabby/dapp/`) and sends: `personal_sign`, `eth_signTypedData_v4`, ETH and ERC-20 (USDC) transfers
on Ethereum, `wallet_switchEthereumChain` to Arbitrum with an ETH and an ERC-20 transfer (unknown
token on device), and to BNB Chain with a legacy (type 0) transfer. `rabby/run.py` drives the device
with the policy every graphical run uses (`gsim.Recorder`). Per step the report shows the Rabby approval and dapp result
screenshots next to the device frames, base vs head, changed frames flagged with a pixel diff.
Exits 1 when either side's flow failed, including Rabby importing an account other than the funded
one.

- Runs offline inside `isolated.sh` (fixed ports), so it needs network namespaces and stops right
  away without them; one flow takes ~2.5 min.
- Rabby's backend (api.rabby.io, static.debank.com: chain list, gas market, simulation, security
  checks) is replayed by `rabby/api_proxy.py` (mitmproxy addon) from the committed fixture, so
  Rabby's screens and the fees on the device are reproducible. The simulation results in it are
  for real mainnet/Arbitrum state of the test account (no balance), hence "Simulation Failed" on
  the ERC-20 approvals. Re-record after changing the flow: unmatched requests are reset.
- Chains are anvils (chain ids 1, 42161, 56, base fee 0) with the account funded and
  `rabby/fixtures/TestToken.sol` at the real USDC addresses.
- Extra tools, user-local: anvil in `$BITBOX_E2E_TOOLS/foundry/bin` (installed by `setup.sh`),
  mitmproxy via `uv` (its CA lands in
  `~/.mitmproxy`, Chromium runs with `--ignore-certificate-errors`), Playwright in `rabby/`
  (`npm ci`, done by the command).
- Rabby reconnects to the bridge for every keyring operation; the bridge lets the new socket take
  the device over, like the real one.
- The bridge also works on its own, e.g. for a Rabby dev build against a simulator:
  `bridge/sim_bridge.py [--sim 127.0.0.1:15423]`; smoke test (headless simulator, auto-confirms):
  `node bridge/check_bridge.mjs <node_modules/@bitboxswiss/bitbox-api/dist/index.js>`.

## Building simulators only

```sh
e2e/build-sim.sh <ref|WORKTREE>   # prints the output dir
SIMULATOR=<dir>/bitbox02-multi-v<ver>-simulator1.0.0-linux-amd64 go test -run TestSimulator ./api/firmware/   # bitbox02-api-go
SIMULATOR=<same> cargo test --features=simulator,tokio -- --test-threads 1                              # bitbox-api-rs
SIMULATOR=<same> npm run test:sim                                                                      # bitbox-api-ts
```

Refs are built from `git clone --shared` copies, so the checkout you work in is never touched and
its untracked files cannot leak into the build. Results are cached by commit sha.

## Graphical simulator

- It reacts to cursor position only (XTest motion on Xvfb, no window manager). Geometry lives in
  `gsim.py` and must track `test/simulator-graphical/src/main.rs`. The last LCD row (scroll hint)
  animates and is excluded from screen identity.
- Startup: the harness taps "Tap this side" at the bottom until the simulator keeps a TCP client
  (it accepts and drops connections until its startup task ends), then starts the driver.
- Auto-advance on a screen that stayed the same for 0.5s: tap top-right, then hold both sliders
  (pinch) until the screen settles. It never taps top-left (reject). A screen needing other input
  (e.g. word entry) stalls until the 180s timeout.
- The pinch release is also a tap at 255 on both sliders, delivered to the screen that replaced a
  hold-to-confirm.
- The idle screen (logo) between two requests of a step is dropped from `frames.json`: whether
  it stays up for the settle time depends on host timing. The final frame is kept.
- The simulator busy-loops (~1 core per run). Under heavy load a late host poll lets the firmware
  time out the request ("error querying device info"); keep concurrent graphical runs at about
  half the cores and use `nice`.

## CI

`.github/workflows/pr.yml` (*firmware change*, manual trigger): base and head of a firmware change,
optionally the fork holding head. It runs on `ubuntu-24.04` with no secrets and a read-only token,
checks out BitBoxSwiss firmware and the three API repos next to this one, and runs

```sh
sudo e2e/ci/prereqs.sh                                             # apt packages, user namespaces
e2e/ci/pr.sh --base master --head <branch|commit> [--head-repo <owner>/<repo>] --out <dir>
```

`pr.sh` resolves the refs, runs `setup.sh` and `e2e.py pr`, and exits with its code. The report
directory is the `e2e-report` artifact; its top (revisions, verdict, summary) is the run's summary.
Only the tools directory is cached (`actions/cache`, keyed by `setup.sh`).

The same job on this machine, in a bare Ubuntu 24.04 container with 4 CPUs and 16G, cloned from the
sibling checkouts so head can be an unpushed branch:

```sh
e2e/ci/local.sh --base master --head my-branch --out /tmp/e2e/ci
```

Docker allows no user namespaces, so there the fixed-port runs go one at a time. With tools installed
and both simulators to build it takes ~35 min (v9.28.0 against a one-commit branch); `$E2E_CI_HOME`
(default `~/.cache/bitbox-e2e-ci`, ~9G after a run) is the container's home, kept between runs.

## Harness tests

```sh
cd e2e && uv run --no-project --with pytest --with pillow --with python-xlib pytest tests/   # no simulator needed
```

## Gotchas

- App screenshots can differ by hover state alone (the last clicked sidebar item).

## Team showcase

`docs/showcase.html` (self-contained slides) and `docs/showcase.pdf`, built from `docs/showcase.src.html`
and real screenshots in `docs/assets/` by `docs/build.py --pdf`.
