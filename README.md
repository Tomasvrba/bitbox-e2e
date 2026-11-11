# BitBox simulator e2e

Per firmware change: run the units the change reaches on the head simulator, diff them against
the merge-base, and check everything that has expectations. Evidence is the exact device screen
text (headless C simulator transcript), LCD screenshots (graphical simulator on Xvfb) and the
host API simulator suites. Everything builds natively, without docker and without root. The
contract between the pieces is `DESIGN.md`.

## Setup (once per machine)

```sh
e2e/setup.sh          # LLVM 22, protoc 21.2, cmocka, cbindgen, bindgen, rust pin, node 26, driver
source e2e/env.sh     # PATH/LIBCLANG_PATH/PKG_CONFIG_PATH for manual builds
```

Host prerequisites: git, curl, gcc, make, pkg-config, python3, rustup, go, uv, Xvfb, Mesa GL
(`libgl1-mesa-dri`), `unshare` with unprivileged user namespaces. Tools land in
`~/.local/share/bitbox-e2e` (~2G), builds, clones and baselines in `~/.cache/bitbox-e2e`
(`$BITBOX_E2E_CACHE`).

## Firmware PR: `e2e.py pr`

```sh
e2e/e2e.py pr --base bitbox/master --head my-branch --out /tmp/e2e/my-branch \
  [--graphical 'btc-vector/*fee*' ...] [-j 4]
```

`--head WORKTREE` builds the firmware checkout as-is, uncommitted changes included. Steps:

1. Baseline bundle for `merge-base(base, head)`: reused from the cache, recorded if missing (see
   `baseline`).
2. Build head, select units: with a coverage map for the merge-base, the units that execute the
   changed Rust lines (`coverage.py select`), else all units. `--graphical` globs add units.
3. **Lane A, evidence**: selected units run headless on head and are diffed against the bundle.
   Units whose screens changed (one per distinct change), new units and the `--graphical` globs
   also run on the graphical simulator, on both sides.
4. **Lane B, regression**: units with expectations are checked on head (pass/fail), and the
   api-go/api-rs/api-ts simulator suites run against head, once with each repo's committed vector
   copy (drift) and once with the firmware's vectors (regression).

Output: `report.md` (relative images, for CI artifacts), `report.html` (self-contained), `pr.json`.

### Units

A unit is one scenario run (`units.py <firmware-dir>` lists them):

- `scenario/<name>`: `scenarios/*.json`, `{ name, description, steps: [ { op, name, ... } ], expect? }`.
  Ops and their fields are the `Step` enum in `driver/src/main.rs` (`eth-address`, `eth-sign`,
  `eth-sign-1559`, `eth-sign-message`, `btc-sign-psbt`). Byte fields are hex. Add an op there when
  a scenario needs another API call; the driver links the sibling `../bitbox-api-rs` checkout.
- `btc-vector/<id>`: one per vector in the firmware's
  `src/rust/bitbox-test-vectors/testdata/btc-transaction-test-vectors.json`, signed with
  `btc-sign-psbt`. Their expectations are version-ranged and owned by the firmware repo.

### Reading the report

- Summary: units run and why (coverage selection or all), changed/new/failed counts, expectation
  pass/fail, **changed functions no unit reached** (the change has no evidence; add a scenario),
  phase timings.
- API simulator suites: per suite, committed vs firmware vectors, head and base.
- Expectation failures: diff of expected vs head screens.
- Changed units: headless screen diff (authoritative: full addresses, no scrolling) and the
  graphical frames that differ, aligned by content hash. Units with the identical change are folded
  into the first one.
- Graphical-only differences: same text, different frames. Usually layout/font changes; capture
  timing is also possible, rerun the unit to tell.
- The base harness revision is shown; a baseline recorded by another harness revision can produce
  harness diffs.

Device setup (pairing code, unlock, seeding) differs every run and is kept out of the diffs.
Signatures differ between runs because anti-klepto randomizes the host nonce.

### `baseline`, `promote`

```sh
e2e/e2e.py baseline <firmware-ref> [--graphical <glob>...] [--force]
e2e/e2e.py promote /tmp/e2e/my-branch scenario/eth-erc20-unknown ...
```

`baseline` records `$BITBOX_E2E_CACHE/baselines/<sha>/`: every unit headless, lane B results, and
graphical runs for the given globs (added incrementally later by `pr`). CI publishes it as a
tarball. `promote`, after a maintainer approved the head screens in a report, writes them into
the scenario file's `expect` for the head simulator version (scenario units only).

### Coverage

```sh
e2e/build-sim.sh --coverage <ref>               # instrumented headless sim, sims/<sha>-cov/
e2e/coverage.py map <ref> [-j N]                # every unit on it -> baselines/<sha>/coverage/map.json
e2e/coverage.py select --base <sha> --head <ref|WORKTREE> --summary
```

`select` maps the diff's changed Rust lines (only code linked into the simulator, no test code)
to the units executing them, and lists changed functions that no unit executes. C is not
instrumented. `pr` uses it automatically when the merge-base bundle has a map.

## Single scenario: base vs head

```sh
e2e/e2e.py compare --scenario e2e/scenarios/eth-erc20-unknown.json \
  --base firmware-ref-before --head firmware-ref-after --out /tmp/e2e/erc20
e2e/e2e.py device --scenario <json> --sim-dir <dir> --out <dir> [--no-graphical|--no-headless]
```

For ad-hoc scenarios that are not units yet (e.g. while drafting one).

## Parallel runs, `isolated.sh`, `E2E_DRIVER`

Both simulators bind 127.0.0.1:15423. `isolated.sh <cmd>` runs a command in a private network
namespace (unprivileged, loopback only), so concurrent runs don't collide; give each its own
`E2E_DISPLAY` (Xvfb display). `pr`/`baseline` do this per unit (`-j`, displays from
`$E2E_DISPLAY` upward). There is no network inside: build outside and set
`E2E_DRIVER=<path>` to a prebuilt driver, which also skips the cargo build.

```sh
E2E_DRIVER=/tmp/driver E2E_DISPLAY=:91 e2e/isolated.sh e2e/e2e.py device --scenario ... --out ...
```

`flake.py` repeats graphical runs (default 10, 3 at a time) and reports, per scenario or unit, how
many produced the identical frame sequence, failures, and run times:

```sh
E2E_DRIVER=/tmp/driver e2e/flake.py --sim-dir <dir> --out /tmp/e2e/flake scenarios/*.json --unit 'btc-vector/op-return'
```

## BitBoxApp: base vs head

```sh
e2e/e2e.py wallet-compare --base bitbox/master --head my-branch --firmware <fw-ref|WORKTREE> \
  --out /tmp/e2e/wallet tests/watch-only.test.ts
```

Runs the app's own Playwright tests (`frontends/web/tests`) from clean clones of both refs, with
one natively built simulator, Chromium only, no retries, and screenshot/video/trace always on
(`wallet/playwright.evidence.config.ts`). The report lists per-test status, errors, device screen
diffs from each test's `simulator.log`, and final screenshots that differ.

Use the upstream remote for bases: `bitbox/master` in the BitBoxSwiss repos (`origin` is a personal
fork, stale in wallet-app), `rabby/develop` in Rabby.

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

## Gotchas

- App screenshots can differ by hover state alone (the last clicked sidebar item).

## Team showcase

`docs/showcase.html` (self-contained slides) and `docs/showcase.pdf`, built from `docs/showcase.src.html`
and real screenshots in `docs/assets/` by `docs/build.py --pdf`.
