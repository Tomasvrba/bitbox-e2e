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
   anyway); the first run for a merge-base records its baseline, later runs reuse it. Every change
   also runs the integrations on top of its repo (app tests, WalletConnect, Rabby; README "What it
   can test"), each in `--out/<flow>`: `--flows <flow>...` to run fewer, `--tests <file>...` to run
   fewer app tests.
4. **Read `summary.md`, then `report.html`.** The exit code (0 clean, 1 regression, 2 harness
   error or a flow that could not run) and *What broke* say whether anything that worked on the
   merge-base broke; findings of the integrations carry their flow's name.
   - *What changed on the device*: what the change looks like on the device (screens as text are
     authoritative; frames are the screenshots that differ). *Pixel-only differences* are layout
     changes or capture timing: rerun that unit once (`--graphical <id>`) to classify.
   - *What broke* gates: units failing on head that passed on base, vectors that must be rejected
     and now complete, and the API suites with the firmware's vectors (for an API change also with
     the committed vectors). Reported only: expectation failures (*To do after merge*) and API
     failures with committed vectors on a firmware change (drift of that repo's copy).
   - *Not tested by any scenario*: changed firmware code no unit executes, and changed files the
     coverage map cannot see (C, protos, token lists). If non-empty, run the draft procedure below
     before reporting.
   - A caution at the top that the harness or another revision under test (the `bitbox-api-rs`
     tree the driver links, the API repos) differs between base and head: a difference may come
     from that rather than the change; check before attributing it.
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
