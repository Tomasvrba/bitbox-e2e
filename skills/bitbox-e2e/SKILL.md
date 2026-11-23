---
name: bitbox-e2e
description: End-to-end check of a firmware, host-API, BitBoxApp or Rabby change against the BitBox02 simulators, producing a base-vs-head diff report with device screenshots. Use when asked to e2e-test, verify on the simulator, or attach screenshot evidence to a PR.
---

Harness and usage: `e2e/README.md`, details in `e2e/docs/reference.md`; contract: `e2e/DESIGN.md`.
Read the README before the first run.

1. **Pick the repo.** `--repo` is where the change is: `firmware` (default), `api-rs`, `api-go`,
   `api-ts`, `app` or `rabby`. Base defaults to the upstream default branch (the remote pointing at
   the upstream repo; `git remote -v`); `pr` diffs against `merge-base(base, head)`. Head is the PR
   ref, or `WORKTREE` for uncommitted firmware. A PR that needs an unreleased change in another repo:
   `--pin <repo>=<ref>` (e.g. a firmware PR that needs an api-rs branch for its new call). Done when
   both refs resolve with `git rev-parse` in that repo's checkout.
2. **Run setup if needed.** `e2e/setup.sh` is idempotent and fast when installed.
3. **Run** `e2e/e2e.py pr [--repo <repo>] --head <ref> --out /tmp/e2e/<name>`. Firmware/API: add
   `--graphical <glob>` for units whose screenshots the reviewer needs (changed units get them
   anyway); the first run for a merge-base records its baseline, later runs reuse it. App: `--tests
   <file>...` to run fewer than all tests, `--wc` for the WalletConnect flow.
4. **Read `report.md`.** The *Verdict* section and the exit code (0 clean, 1 regression, 2
   harness error) say whether anything that worked on the merge-base broke.
   - Lane A, evidence: *Changed units* and *New units* are what the change looks like on the
     device (headless screen diff is authoritative; frames are the screenshots that differ).
     *Graphical-only differences* are layout changes or capture timing: rerun that unit once
     (`--graphical <id>`) to classify.
   - Lane B, regression: units failing on head that passed on base and the API suites with the
     firmware's vectors gate. Reported only: expectation failures (promoted after the firmware
     PR merges) and API suite failures with committed vectors (drift of that repo's copy).
   - *Unreached changed functions*: changed firmware code no unit executes, and changed files the
     coverage map cannot see (C, protos, token lists). If non-empty, run the draft procedure below
     before reporting.
   - A note under the header table that the harness or another revision under test (the
     `bitbox-api-rs` tree the driver links, the API repos) differs between base and head: a
     difference may come from that rather than the firmware; check before attributing it.
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
graphical: btc-vector/*fee*
pins: api-rs=<branch or sha>   # when the PR needs an unreleased API call
-->
```

They are the `--graphical` and `--pin` options (the workflow's Graphical and Pins fields). Planned:
CI is to parse the block; today pass them by hand.
