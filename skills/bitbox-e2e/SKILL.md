---
name: bitbox-e2e
description: End-to-end check of a firmware, host-API or BitBoxApp change against the BitBox02 simulators, producing a base-vs-head diff report with device screenshots. Use when asked to e2e-test, verify on the simulator, or attach screenshot evidence to a PR.
---

Harness and usage: `e2e/README.md`. Read it before the first run.

1. **Pick the mode.** Firmware or API behaviour on the device → `e2e/e2e.py compare`. App UI or
   backend → `e2e/e2e.py wallet-compare` with the Playwright tests that cover it. Base is the PR's
   merge-base (`git merge-base HEAD bitbox/master` in wallet-app; the firmware branch's base);
   head is the PR ref, or `WORKTREE` for uncommitted firmware. Done when both refs resolve with
   `git rev-parse`.
2. **Get a scenario that reaches the changed code.** Reuse `e2e/scenarios/*.json` or write one;
   a scenario only counts if the changed firmware path runs (check the handler the diff touches).
   Add a driver op in `e2e/driver/src/main.rs` if the API call is missing. For a pure app change,
   name the existing test files that exercise it.
3. **Run setup if needed.** `e2e/setup.sh` is idempotent and fast when installed.
4. **Run the comparison** with `--out` under `/tmp/e2e/<name>`. Builds take ~4 min per new ref.
5. **Read the evidence.** Open `report.md`, the diff images and, for every failure, the side's
   logs (`headless/combined.log`, `graphical/simulator.log`, `test-results/*/error-context.md`).
   Done when every differing screen and every failure is explained as either the intended change,
   a regression, or noise (hover state, a known flake: rerun once to classify).
6. **Report**: the screen-level diff in one or two sentences, pass/fail per side, each
   unexplained difference, and the path to `report.html`.
