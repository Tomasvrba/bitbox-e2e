---
name: bitbox-e2e
description: End-to-end check of a firmware, host-API, BitBoxApp or Rabby change against the BitBox02 simulators, producing a base-vs-head diff report with device screenshots. Use when asked to e2e-test, verify on the simulator, or attach screenshot evidence to a PR.
---

Reference: `e2e/README.md`. Read it before the first run.

1. **Pick repo and refs.** `--repo` is `firmware` (default), `api-rs`, `api-go`, `api-ts`, `app`, or `rabby`. `--base` defaults to the upstream default branch (`merge-base(base, head)` is compared). `--head` is the PR ref, or `WORKTREE` for uncommitted firmware. Use `--pin <repo>=<ref>` when another repo needs a matching branch.
2. **Ensure environment.** Run `e2e/setup.sh` if needed (idempotent; installs tools and runs `uv sync --frozen`), then `source e2e/env.sh`.
3. **Run `pr`.**
   ```sh
   uv run --project e2e --no-sync e2e pr [--repo <repo>] --head <ref> --out /tmp/e2e/<name>
   ```
   (Or `e2e/e2e.py pr ...`.) Pass `--flows <flow>...` (`device`, `app`, `wc`, `rabby`) to scope flows, `--graphical <glob>...` for extra device screenshots (changed units get them automatically), and `--tests <file>...` to scope BitBoxApp Playwright tests.
4. **Inspect the uniform output directory.**
   - `<out>/summary.md` and `<out>/report.html`: combined results across all flows.
   - `<out>/<flow>/`: per-flow `report.html`, `summary.md`, `findings.json` (or `flow-error.json` if a flow failed to run).
   - Exit codes: `0` clean, `1` regression, `2` harness/flow error.
5. **Triage findings.**
   - *What broke* gates: tests failing on head that passed on base, BTC rejection vectors that now complete, and host API suite regressions.
   - *What changed on the device*: screen text diffs are authoritative; frames show visual pixel diffs. *Pixel-only differences* are usually layout tweaks or capture timing.
   - Check top-level cautions for harness or sibling-repo revision mismatches before attributing diffs.
6. **Other commands & new scenarios.**
   - `e2e baseline <fw-ref>`: warm a firmware baseline cache.
   - `e2e report <dir>`: re-render per-flow reports and aggregate `<dir>/report.html`.
   - `e2e device --scenario scenarios/<name>.json --sim-dir $(e2e/build-sim.sh <ref>) --out /tmp/e2e/<name>`: run a single scenario (add new scenarios under `e2e/scenarios/`).
   - `e2e record-rabby --firmware <ref> --out /tmp/e2e/rec`: refresh Rabby API fixtures.
   - Harness self-checks: `cd e2e && uv run ruff check . && uv run pytest`.
7. **Report back**: summarize regressions, screen-level changes, any unexplained differences, and the path to `<out>/report.html`.
