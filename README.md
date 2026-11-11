# BitBox simulator e2e

Run a scenario against two refs and get a minimal diff report with evidence:
the exact device screen text (headless simulator transcript), device LCD screenshots (graphical
simulator on Xvfb), and BitBoxApp Playwright results/screenshots. Everything builds natively,
without docker and without root.

## Setup (once per machine)

```sh
e2e/setup.sh          # LLVM 22, protoc 21.2, cmocka, cbindgen, bindgen, rust pin, node 26, driver
source e2e/env.sh     # PATH/LIBCLANG_PATH/PKG_CONFIG_PATH for manual builds
```

Host prerequisites: git, curl, gcc, make, pkg-config, python3, rustup, go, uv, Xvfb, Mesa GL
(`libgl1-mesa-dri`). Tools land in `~/.local/share/bitbox-e2e` (~2G), builds and clones in
`~/.cache/bitbox-e2e`.

## Firmware / API scenario: base vs head

```sh
e2e/e2e.py compare --scenario e2e/scenarios/eth-erc20-unknown.json \
  --base firmware-ref-before --head firmware-ref-after --out /tmp/e2e/erc20
```

`--head WORKTREE` builds the firmware checkout as-is, uncommitted changes included.
Output: `report.md` (relative images, for CI artifacts) and `report.html` (self-contained).

Per side the scenario runs twice:

| view | simulator | how confirmations happen | evidence |
|---|---|---|---|
| `headless/` | `test/simulator` (C) | auto-accepted | `screens.json`: every `CONFIRM/STATUS/ENTER` screen as text |
| `graphical/` | `test/simulator-graphical` | XTest pointer gestures | one PNG per distinct LCD frame |

The text transcript is authoritative (full addresses, no scrolling). Screenshots are aligned by
content hash, so the report only lists frames that were added, removed or changed. Device setup
(pairing code, seeding, the `Is now?` clock) differs every run and is kept out of the diffs.
Signatures differ between runs because anti-klepto randomizes the host nonce.

### Scenarios

`scenarios/*.json`: `{ name, description, steps: [ { op, name, ... } ] }`. Ops and their fields
are the `Step` enum in `driver/src/main.rs` (`eth-address`, `eth-sign`, `eth-sign-1559`,
`eth-sign-message`). Byte fields are hex. Add an op there when a scenario needs another API call.
The driver links the sibling `../bitbox-api-rs` checkout.

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

## Gotchas

- Both simulators listen on TCP 15423 (the graphical one cannot change it); runs are sequential.
- The graphical simulator reacts to cursor position only. Geometry lives in `gsim.py` and must
  track `test/simulator-graphical/src/main.rs`. Its last LCD row (scroll hint) animates and is
  excluded from screen identity.
- The auto-advance policy taps top-right, then holds both sliders; it never taps top-left (reject).
  A screen needing other input (e.g. word entry) stalls until the 180s timeout.
- App screenshots can differ by hover state alone (the last clicked sidebar item).
