# Reference

Details behind [the README](../README.md). [DESIGN.md](../DESIGN.md) is the contract between the parts.

## Commands

| Command | What it does |
|---|---|
| `e2e.py pr` | One change, base vs head ([below](#e2epy-pr)) |
| `e2e.py baseline <fw-ref> [--pin ...] [--graphical ...]` | Record the "before" bundle of a firmware commit |
| `e2e.py report <pr-out> [--pr-info <file>]` | Render the report of a run again ([below](#the-report)) |
| `e2e.py promote <pr-out> <unit>...` | Approved head screens become a scenario's expectation |
| `e2e.py device --scenario <json> --sim-dir <dir> --out <dir>` | One scenario on one simulator |
| `e2e.py wc --base --head --out [--firmware]` | WalletConnect flow at two app refs |
| `e2e.py rabby --base --head --out [--rabby-ref]` | Rabby flow at two **firmware** refs, one Rabby build |
| `build-sim.sh <ref\|WORKTREE>` | Build both simulators, print their directory |
| `coverage.py map\|select` | Coverage map of the units ([below](#coverage)) |
| `flake.py` | Repeat graphical runs, report frame identity ([below](#parallel-runs-and-flakiness)) |
| `ci/pr.sh`, `ci/local.sh` | The CI job, and the same job in a local container |

## Setup details

`setup.sh` first checks what needs root. If anything is missing, it lists all of it with the
`apt-get install` line and stops: git, curl, tar, xz, unzip, gcc, g++, make, pkg-config, python3,
coreutils, binutils (`c++filt`), util-linux (`unshare`), iproute2 (`ip`), libssl-dev, a libstdc++
that `cc` links (api-rs suite), Xvfb and the graphical simulator's libraries (`libegl1 libgl1
libx11-xcb1 libxcursor1 libxi6 libxrender1 libxkbcommon-x11-0 libgl1-mesa-dri`). rustup must be
installed too (no root needed).

Everything else goes into `~/.local/share/bitbox-e2e/tools` (`$BITBOX_E2E_HOME/tools`, ~1.5G fresh),
unless the host already has it. LLVM 22 with libclang (`llvm-config-22`), protoc 21.2, cmocka,
cbindgen 0.29.2 and bindgen 0.72.1 on `PATH` are used in place, as in the firmware dev container
(`shiftcrypto/firmware_v2`). uv, cmake, ast-grep (invoked as `ast-grep`; `sg` is `newgrp` on
Debian/Ubuntu), Go (when the host's is older than 1.21), node 26 (when neither mise's `node@26` nor
the `PATH` node is 26) and anvil are downloaded at pinned versions. Builds, clones and baselines go
to `~/.cache/bitbox-e2e` (`$BITBOX_E2E_CACHE`).

Pins: every `setup.sh` download is checked against the sha256 next to its version. To bump one,
set the version, download that exact file, compare its `sha256sum` with the publisher's (GitHub
release asset digest, go.dev/dl, nodejs `SHASUMS256.txt`, PyPI for the cmake wheel) and pin it.
Python deps are locked per script in `<script>.py.lock` (uv >= 0.5.17). After changing a script's
dependencies, or to upgrade, run `uv lock --script <script>.py [--upgrade]` and commit the lock;
`uv lock --script <script>.py --check` fails on a stale one.

Reported at the end without failing:

- `isolated.sh` needs unprivileged user namespaces. Ubuntu 23.10+ restricts them via AppArmor
  (`ci/prereqs.sh` lifts that), Docker's default seccomp profile blocks them. `isolated.sh true`
  fails with the reason.
- The browser flows need Playwright Chromium's system libraries (as root:
  `rabby/node_modules/.bin/playwright install-deps chromium`, after `npm ci` in `rabby/`).

The app tests also need Docker (regtest bitcoind and electrs containers) and the Python packages
`ecdsa` and `bech32` (the aopp test server). `wallet_app.py` runs them with `uv run --with`, at the
versions in `TEST_PYTHON_DEPS`.

## `e2e.py pr`

```sh
e2e/e2e.py pr [--repo firmware|app|api-rs|api-go|api-ts|rabby] [--base <ref>] --head <ref|WORKTREE> \
  --out <dir> [--pin <repo>=<ref>...] [--flows <flow>...] [--graphical <glob>...] [--tests <file>...] \
  [--pr-info <file>] [-j N]
```

Refs are resolved in the sibling checkout of `--repo`. `--base` defaults to the default branch of
the remote that points at the upstream repo (`git remote -v`; `origin` in CI). Use the upstream
remote for bases: a personal fork's `origin` is often stale. `WORKTREE` (firmware only) builds the
firmware checkout as it is, uncommitted changes included. `-j` defaults to half the CPUs.

`--pin` keeps another repo at a ref on both sides. Pinnable: `firmware` and `app` (default upstream
`master`), `rabby` (default v0.94.15, the release the backend fixture was recorded with), `api-rs`,
`api-go`, `api-ts`. The API repos default to the sibling checkout's `HEAD` (in CI the upstream
default branch) in the device flow, and to what the app or Rabby ships with in theirs. The repo
under test cannot be pinned. A pinned `api-rs` is also what the driver links (otherwise the driver
links the sibling `bitbox-api-rs` working tree).

### Flows

A change runs the flows of its repo (`e2e.FLOWS`), or those of `--flows`:

| Repo | Flows |
|---|---|
| firmware | device, app, wc, rabby |
| api-go | device, app, wc |
| api-ts | device, rabby |
| api-rs | device |
| app | app, wc |
| rabby | rabby |

`device` is [firmware and API changes](#firmware-and-api-changes). The others are integrations: the
[app's tests](#app-flow), the [WalletConnect flow](#walletconnect-flow) and the
[Rabby flow](#rabby-flow), with the changed repo at the merge-base and at head and the rest the same
on both sides (`sides.json` in the flow's directory says which is which):

- firmware: each side gets the simulator of its firmware commit.
- api-go: the app is built against the side's api-go (`go mod edit -replace`, then `go mod vendor`
  in the app clone; the first run downloads the app's Go modules).
- api-ts: Rabby is built with the side's api-ts (`npm pack` of it, as the `@bitboxswiss/bitbox-api`
  dependency; `yarn install` may fetch dependencies that a newer api-ts needs).

The repo's first flow writes the run's report into `--out`, the others into `--out/<flow>`. The
run's page shows every flow's findings, each prefixed with the flow's name; a flow that could not
run is named at the top (`flow-error.json`), the other flows still run, and the exit code is 2. CI
runs one flow per job (`ci/pr.sh --flows <flow>`) and its report job puts the directories together.

### Firmware and API changes

Steps:

1. Baseline bundle for the base side: reused from the cache, recorded if missing (see
   [baseline](#baseline-promote)). For a firmware change the base side is `merge-base(base, head)`
   with the pins. For an API change it is the pinned firmware with the API repo at the merge-base.
2. Build head. Every unit runs headless on it (88 units: ~40s at `-j 4`).
3. **Lane A, evidence**: each unit's head screens are diffed against the bundle. Units whose
   screens changed (one per distinct change), new units and the `--graphical` globs also run on
   the graphical simulator, on both sides.
4. **Lane B, regression**: every unit with expectations is checked on head (pass/fail), and the
   api-go/api-rs/api-ts simulator suites run against head, once with each repo's committed vector
   copy (drift) and once with the firmware's vectors (regression).

The verdict (exit 1) lists:

- Units that completed on base and fail on head.
- BTC vectors the device must reject (outcome `invalid_input` or `unsupported`) that were rejected
  on base and complete on head. A rejected vector "fails" by design and is not reported.
- API suite tests that passed on base and fail on head, and suites that error on head (build
  failure, crash, no tests ran). For a firmware change only the run with the firmware's vectors
  counts: the API repos' committed copies assert the screens of the firmware they were synced from,
  so they fail on any PR that changes one. For an API change both runs count.

Reported, not gating: changed screens, expectation mismatches (expectations live here and are
promoted after the PR merges) and, for a firmware change, API tests failing with the committed
vector copies.

### App flow

The app's own Playwright tests (`frontends/web/tests`, all unless `--tests`) run from clean clones
of the app on each side, on natively built simulators. The app's backend is built first: if it does
not build (e.g. against an api-go change), the report says so with the compiler's output. Chromium only, no retries, screenshot/video/trace always on
(`wallet/playwright.evidence.config.ts`). The report shows each test that passed on base and fails
on head with its error, its device screens and its last screenshot on both sides; device screen
changes (from each test's `simulator.log`) of the other tests; and tests failing on both sides.
Device screens are compared with the values that are random per test run masked (wallet addresses,
request ids, the date). Screenshots are not compared pixel by pixel: charts animate and test
wallets are random, so they differ on every run. Exit 1 when a test that passed on base does not
pass on head.

### Rabby changes

The [Rabby flow](#rabby-flow) runs with Rabby built at the merge-base and at head. Use
`--base v0.94.15` (or a later tag) for a branch made from a release: the committed backend fixture
matches that flow.

### Units

A unit is one scenario run (`units.py <firmware-dir>` lists them):

- `scenario/<name>`: `scenarios/*.json`, `{ name, description, steps: [ { op, name, ... } ], expect? }`.
  Ops and their fields are the `Step` enum in `driver/src/main.rs` (`eth-address`, `eth-sign`,
  `eth-sign-1559`, `eth-sign-message`, `eth-sign-typed-data`, `btc-sign-psbt`). Byte fields are hex.
  `eth-sign-typed-data` takes the EIP-712 JSON object (as passed to `eth_signTypedData_v4`) in
  `typed_data`. Add an op there when a scenario needs another API call.
- `btc-vector/<id>`: one per vector in the firmware's
  `src/rust/bitbox-test-vectors/testdata/btc-transaction-test-vectors.json`, signed with
  `btc-sign-psbt`. Their expectations are version-ranged and owned by the firmware repo. Refs
  before that file (firmware 3bdaf583b) have no btc-vector units and no firmware-vector API run.
- `eth-matrix/<chain id>-<case>`: generated by `eth_matrix.py`, the same requests on chains 1, 10,
  56, 137, 8453, 42161, 11155111 and the unknown chain 123456. Cases: `address`, `legacy` and
  `eip1559` native transfers, `erc20-known` (USDC, chain 1 only), `erc20-unknown`, `personal-sign`,
  `typed-data` (USDC permit). 49 units. Expectations live in
  `scenarios/eth-matrix/<chain id>-<case>.json`, written by `promote`.

### The report

Every flow writes the same files to `--out`:

- `report.html`: the report. It links its images (relative paths), so open it inside the directory.
- `summary.md`: the result and one line per finding, for the CI run page and the PR comment.
- `report-files.txt`: `report.html` and every image it shows, for a step that publishes the report.
- `pr.json` (firmware and API changes), `*/tests.json` (app), `*/frames.json` (Rabby): the data.

The page, top to bottom:

1. The PR title and link (with `--pr-info`), base and head.
2. The result: what broke, how many device screens changed, how many problems were there before.
3. **What this PR should change**: the PR description, and the screen changes it expects.
4. **What broke**: each test or scenario that passed on base and fails on head, with the failure
   message (for the API suites, extracted from their log) and the screens of the failure.
5. **What changed on the device**: one card per change, with the screens as text, before and after,
   and the frames that differ with the change marked. Changes of the same kind (the same title,
   e.g. another fee per chain) are one card; the others are under it.
6. **To do after merge**, **What was tested**, then collapsed: problems on both sides, pixel-only
   differences (same screen text; usually capture timing), run details (versions, run mode, timings).

`--pr-info <file>` is the PR as JSON: `gh pr view <n> -R <repo> --json url,number,title,body` with a
`repo` key added. An optional block in the PR description lists the expected screen changes as unit
id globs (app: test titles; Rabby: step names):

```
<!-- e2e
expect: btc-vector/*fee*
-->
```

A change that matches is marked *Expected*, any other *Not expected*. Without the block every change
is *To review*. `e2e.py report <dir> [--pr-info <file>]` renders the page again, e.g. after adding the PR.

Device setup (pairing code, unlock, seeding) differs every run and is kept out of the diffs.
Signatures differ between runs because anti-klepto randomizes the host nonce.

## `baseline`, `promote`

```sh
e2e/e2e.py baseline <firmware-ref> [--pin api-rs=<ref>...] [--graphical <glob>...] [--force]
e2e/e2e.py promote /tmp/e2e/my-branch scenario/eth-erc20-unknown ...
```

`baseline` records `$BITBOX_E2E_CACHE/baselines/<sha>/` (with pins: `<sha>-<hash of the pins>/`):
every unit headless, lane B results, and graphical runs for the given globs (`pr` adds more later).
It fails (exit 2) instead of recording a bundle where no unit completed.

`promote`, after a maintainer approved the head screens in a report, writes them into the scenario
file's `expect` for the head simulator version (scenario units only). `pr.json` stores scenario
paths relative to the harness checkout; `promote` only writes under its `scenarios/`.

## Coverage

```sh
e2e/build-sim.sh --coverage <ref>               # instrumented headless sim, sims/<sha>-cov/
e2e/coverage.py map <ref> [-j N]                # every unit on it -> baselines/<sha>/coverage/map.json
e2e/coverage.py select --base <sha> --head <ref|WORKTREE> --summary
```

`select` maps the diff's changed Rust lines (only code linked into the simulator, no test code) to
the units that execute them. It lists changed functions that no unit executes, and changed files the
map has no lines for (C is not instrumented; protos, token lists, build files). `pr` uses it for a
firmware change when the merge-base bundle has a map, for the unreached list only; it never limits
which units run.

## Single scenario

```sh
e2e/e2e.py device --scenario <json> --sim-dir $(e2e/build-sim.sh <ref>) --out <dir> [--no-graphical|--no-headless]
```

For scenarios that are not units yet, or one generated unit:
`units.py <firmware-dir> --scenario eth-matrix/8453-erc20-unknown > /tmp/s.json`.

## Parallel runs and flakiness

Each headless run starts its simulator on a free port (`--port`). Graphical runs do the same when
the ref's `simulator-graphical` has `--port` (detected per binary from `--help`; firmware before
a540c993b has none). Without it, it always binds 15423. Each run then goes into its own network
namespace (`isolated.sh <cmd>`, unprivileged, loopback only, private `/tmp/.X11-unix`), or, where
namespaces are unavailable, runs one at a time. The API suites connect to 15423 too: in namespaces
all at once, else one at a time. The report's *Run mode* line says what ran how.

There is no network inside a namespace: build outside and set `E2E_DRIVER=<path>` to a prebuilt
driver, which also skips the cargo build:

```sh
E2E_DRIVER=/tmp/driver e2e/isolated.sh e2e/e2e.py device --scenario ... --out ...
```

`flake.py` repeats graphical runs (default 10, half the CPUs at a time) and reports, per scenario
or unit, how many produced the identical frame sequence, failures and run times:

```sh
E2E_DRIVER=/tmp/driver e2e/flake.py --sim-dir <dir> --out /tmp/e2e/flake scenarios/*.json --unit 'btc-vector/op-return'
```

## WalletConnect flow

```sh
e2e/e2e.py wc --base <app-ref> --head <app-ref> --out /tmp/e2e/wc [--firmware <fw-ref|WORKTREE>]
e2e/wc/flow.py run --ref <app-ref> --out <dir>           # one side; flow.py same <dir>... compares runs
```

`wc/dapp.mjs` (`@walletconnect/sign-client`) pairs with the app over the public relay and sends
`personal_sign`, `eth_signTypedData_v4` (Sepolia, mainnet), `eth_signTransaction` (Sepolia) and
`eth_sendTransaction` (Sepolia, Sepolia with 4.5 kB of calldata, Arbitrum, Polygon). It verifies the
signer, and for each sent transaction that it is on the chain and from the session account.
`wallet/tests/walletconnect.test.ts`, copied into the app clone, approves the session and each
request in the app; `wc/device.py` runs the graphical simulator (preseeded) and confirms everything
on the device.

The app reaches Ethereum through local chains (`wallet/chains.py`): one anvil per chain (1,
11155111, 42161, 137, 10, 8453, 56) and a stand-in for the app's Etherscan proxy
(`wallet/etherscan_fake.py`, a mitmproxy addon), behind a SOCKS5 proxy that the app's own proxy
setting points at (`config.json`, `backend.proxy`); `SSL_CERT_FILE` makes the app trust it. Only
the Etherscan host is intercepted. The stand-in funds every address the app asks about, answers
`module=proxy` calls from the chain, keeps a `txlist` of what was sent through it, and returns a
fixed gas oracle. It enforces the real proxy's request rules (probed with read-only calls,
2026-11-23):

- The chain must be in the URL query (`chainId`). A POST with the chain only in the body works for
  chain 1 and fails for any other chain ("Missing or unsupported chainid parameter").
- A GET whose path and query exceed 8147 characters gets 414.

Those are the rules bitbox-wallet-app#4579 was about: from 1d6ee7f9c every broadcast was a POST
with the chain only in the body, so sends on Sepolia and the L2s failed. Each side logs the app's
requests in `<side>/chains/etherscan.jsonl`; the report names the ones that fail only on head.

The report (the same page as for [`pr`](#the-report)) lists each request whose result got worse
from base to head under *What broke* (an error, a signature from another account, a sent
transaction that is not on the chain, or the test failing), with the app's proxy requests on that
chain that fail only on head. Requests that got better are under *Fixed by this PR*. Exits 1 when
anything is under *What broke*.

- Runs in the host network namespace (relay), so ports 8080/8082/15423, the anvil ports
  8645-8651 and the SOCKS port 8660 must be free. Not runnable offline or where the relay is blocked.
- App screenshots hide the banners (testnet, update, attestation, microSD).
- No BTC: the app only handles `eip155` over WalletConnect.

## Rabby flow

```sh
e2e/e2e.py rabby --base <fw-ref> --head <fw-ref|WORKTREE> --out /tmp/e2e/rabby [--rabby-ref v0.94.15]
e2e/rabby/rabby_e2e.py repeat --firmware <fw-ref> -n 3 --out <dir>   # same flow N times, device frame identity
e2e/rabby/rabby_e2e.py record --firmware <fw-ref> --out <dir>        # re-record rabby/fixtures/rabby-api.json.gz (network)
e2e/rabby/rabby_e2e.py report <dir>
```

Rabby is built unpacked from a `--shared` clone of `$BITBOX_RABBY` (default `../Rabby`), cached in
`$BITBOX_E2E_CACHE/src/rabby-<ref>` (~10 min the first time). It runs in Playwright Chromium and
talks to the graphical simulator through `bridge/sim_bridge.py`, a fake BitBoxBridge
(`127.0.0.1:8178`, U2F-WS frames <-> U2F-HID packets on the simulator's TCP port). `rabby/flow.ts`
onboards (password, Import -> Hardware Wallet -> BitBox02, unlock and pairing on the device, first
address), sets each chain's custom RPC to a local anvil, connects the test dapp (`rabby/dapp/`) and
sends: `personal_sign`, `eth_signTypedData_v4`, ETH and ERC-20 (USDC) transfers on Ethereum,
`wallet_switchEthereumChain` to Arbitrum with an ETH and an ERC-20 transfer, and to BNB Chain with a
legacy (type 0) transfer. `rabby/run.py` drives the device with the policy every graphical run uses
(`gsim.Recorder`). Per step the report shows the Rabby screenshots next to the device frames, base
vs head. Exits 1 when a step that passed on base fails on head, including Rabby importing another
account than the funded one.

- Runs offline inside `isolated.sh` (fixed ports), so it needs network namespaces; ~2.5 min per flow.
- Rabby's backend (api.rabby.io, static.debank.com) is replayed by `rabby/api_proxy.py` (mitmproxy
  addon) from the committed fixture, so Rabby's screens and the fees on the device are reproducible.
  The simulation results in it are for real mainnet/Arbitrum state of the test account (no balance),
  hence "Simulation Failed" on the ERC-20 approvals. Re-record after changing the flow: unmatched
  requests are reset.
- Chains are anvils (chain ids 1, 42161, 56, base fee 0) with the account funded and
  `rabby/fixtures/TestToken.sol` at the real USDC addresses.
- The dapp does not check who signed a `personal_sign`: a Rabby bug that only breaks message
  signing passes the flow.
- The bridge also works on its own, e.g. for a Rabby dev build against a simulator:
  `bridge/sim_bridge.py [--sim 127.0.0.1:15423]`; smoke test (headless simulator, auto-confirms):
  `node bridge/check_bridge.mjs <node_modules/@bitboxswiss/bitbox-api/dist/index.js>`.

## Building simulators

```sh
e2e/build-sim.sh <ref|WORKTREE>   # prints the output dir
SIMULATOR=<dir>/bitbox02-multi-v<ver>-simulator1.0.0-linux-amd64 go test -run TestSimulator ./api/firmware/   # bitbox02-api-go
SIMULATOR=<same> cargo test --features=simulator,tokio -- --test-threads 1                              # bitbox-api-rs
SIMULATOR=<same> npm run test:sim                                                                      # bitbox-api-ts
```

Refs are built from `git clone --shared` copies, so the checkout you work in is never touched. Results
are cached by commit sha.

## Graphical simulator

- It reacts to the cursor position only (XTest motion on Xvfb, no window manager). Geometry lives in
  `gsim.py` and must track `test/simulator-graphical/src/main.rs`. The last LCD row (scroll hint)
  animates and is excluded from screen identity.
- Startup: the harness taps "Tap this side" at the bottom until the simulator keeps a TCP client
  (it accepts and drops connections until its startup task ends), then starts the driver. On a
  timeout it saves `startup-failure.png` next to `simulator.log`.
- Auto-advance on a screen that stayed the same for 0.5s: tap top-right, then hold both sliders
  (pinch) until the screen settles. It never taps top-left (reject). A screen that needs other
  input (e.g. word entry) stalls until the 180s timeout.
- The pinch release is also a tap at 255 on both sliders, delivered to the screen that replaced a
  hold-to-confirm.
- The idle screen (logo) between two requests of a step is dropped from `frames.json`: whether it
  stays up for the settle time depends on host timing. The final frame is kept.
- The simulator busy-loops (~1 core per run). Under heavy load a late host poll lets the firmware
  time out the request ("error querying device info"); keep concurrent graphical runs at about half
  the cores and use `nice`.

## CI

`.github/workflows/pr.yml` (*change*, manual trigger) has the fields in the README. Its jobs run on
`ubuntu-24.04` with no secrets and a read-only token:

- `plan`: the flows (`e2e.py flows <repo>`, or the Flows field).
- `flow`, one job per flow: checks out the BitBoxSwiss firmware and the three API repos next to
  this one (plus the app for the flows app and wc, Rabby for rabby), and runs

  ```sh
  sudo e2e/ci/prereqs.sh                       # apt packages, user namespaces
  e2e/ci/pr.sh --repo <repo> --head <branch|commit> [--head-repo <owner>/<repo>] [--pins '...'] --flows <flow> ... --out <dir>
  ```

  It fails (red) when its flow found a regression; its directory is the `e2e-flow-<flow>` artifact.
- `report`: puts the flows' directories together as `e2e.py pr` lays them out, renders the run's
  page (`e2e.py report`) and uploads it as the `e2e-report` artifact; the run's summary is its top.

`pr.sh` checks the inputs, resolves the refs (fetching head from the fork), runs `setup.sh` and
`e2e.py pr`, and exits with its code. Only the tools directory is cached (`actions/cache`, keyed by
`setup.sh`).

The same job for a firmware change on this machine, in a bare Ubuntu 24.04 container with 4 CPUs
and 16G, cloned from the sibling checkouts so head can be an unpushed branch:

```sh
e2e/ci/local.sh --base master --head my-branch --out /tmp/e2e/ci [--flows 'device app wc']
```

Docker allows no user namespaces, so there the fixed-port runs go one at a time. `$E2E_CI_HOME`
(default `~/.cache/bitbox-e2e-ci`, ~9G after a run) is the container's home, kept between runs.

## Harness tests

```sh
cd e2e && uv run --no-project --with pytest --with pillow --with python-xlib pytest tests/   # no simulator needed
```

## Known problems

- App screenshots can differ by hover state alone (the last clicked sidebar item).
- The WalletConnect fee screens differ between any two runs (live gas).
