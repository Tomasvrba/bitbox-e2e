---
name: bitbox-e2e
description: End-to-end check of a firmware, host-API or BitBoxApp change against the BitBox02 simulators, producing a base-vs-head diff report with device screenshots. Use when asked to e2e-test, verify on the simulator, or attach screenshot evidence to a PR.
---

Harness and usage: `e2e/README.md`; contract: `e2e/DESIGN.md`. Read the README before the first run.

1. **Pick the mode.** Firmware or API behaviour on the device → `e2e/e2e.py pr` (default).
   App UI or backend → `e2e/e2e.py wallet-compare` with the Playwright tests that cover it.
   Base is the upstream branch (`bitbox/master`); `pr` diffs against `merge-base(base, head)`.
   Head is the PR ref, or `WORKTREE` for uncommitted firmware. Done when both refs resolve with
   `git rev-parse`.
2. **Run setup if needed.** `e2e/setup.sh` is idempotent and fast when installed.
3. **Run** `e2e/e2e.py pr --base bitbox/master --head <ref> --out /tmp/e2e/<name>`, adding
   `--graphical <glob>` for units whose screenshots the reviewer needs (changed units get them
   anyway). The first run for a merge-base records its baseline (`e2e.py baseline <sha>`, every
   unit headless plus the API suites); later runs reuse it. New ref builds take ~4 min.
4. **Read `report.md`.**
   - Lane A, evidence: *Changed units* and *New units* are what the change looks like on the
     device (headless screen diff is authoritative; frames are the screenshots that differ).
     *Graphical-only differences* are layout changes or capture timing: rerun that unit once
     (`--graphical <id>`) to classify.
   - Lane B, regression: expectation failures, units failing on head that passed on base, and the
     API suites (committed vectors = drift of that repo's copy; firmware vectors = regression).
   - *Unreached changed functions*: changed firmware code no unit executes. If non-empty, run the
     draft procedure below before reporting.
   Done when every differing screen and every failure is explained as the intended change, a
   regression, or noise (capture timing, hover state).
5. **Report**: the screen-level change in one or two sentences, lane B pass/fail, each
   unexplained difference, unreached functions left, and the path to `report.html`.
6. **Promote** only after a maintainer approved the head screens:
   `e2e/e2e.py promote /tmp/e2e/<name> scenario/<name> ...` writes them as the scenario's
   expectation for the head simulator version. Vector expectations are edited in the firmware
   repo's test vectors instead.

## Draft: evidence for unreached code

Input: the PR diff and `e2e/coverage.py select --base <merge-base> --head <ref> --summary`
(needs `coverage.py map <merge-base>`; `pr` lists the same functions).

1. For each unreached function, read it and its callers up to the API handler; find the request
   and input values that make it run (e.g. a token, chain, script type, fee level).
2. Write `e2e/scenarios/<name>.json` with the steps that send that request. If no driver op makes
   the call, add one to the `Step` enum in `e2e/driver/src/main.rs`; if `bitbox-api-rs` lacks the
   call, it needs an API branch (see the PR block below).
3. Run it on head, headless and graphical:
   `e2e/e2e.py device --scenario <json> --sim-dir $(e2e/build-sim.sh <ref>) --out /tmp/e2e/<name>`.
   Confirm the function ran: rerun `coverage.py select` after `coverage.py map <head>`, or check
   that the screens the function produces appear in `headless/screens.json`.
4. Rerun `pr`; the scenario is a new unit, reported with its screens.
5. Propose the scenario (and driver op) in the PR with that evidence: the new unit's screens, its
   frames, and which unreached functions it now covers. Done when every unreached function is
   covered or explained (not reachable from the simulator, C code, test-only).

## PR-body block

Authors may add to the PR description:

```
<!-- e2e
units: btc-vector/*fee*, scenario/eth-erc20-*
graphical: btc-vector/*fee*
api-rs: <branch or sha>   # when the PR needs an unreleased API call
-->
```

`units` adds unit id globs to the coverage selection, `graphical` is `--graphical`, `api-rs`
builds the driver against that `bitbox-api-rs` revision. Planned: CI is to parse it; `e2e.py`
does not yet. By hand: pass the globs as `--graphical` (which also adds them to the run), and for
`api-rs` check out that revision in the sibling `bitbox-api-rs` and rebuild the driver.
