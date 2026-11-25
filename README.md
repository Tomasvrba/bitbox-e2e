# BitBox e2e

End-to-end regression and device-screen diff harness for BitBox repos. It runs a change on the BitBox02 simulator, compares it against the merge-base before the change, and writes an HTML + Markdown report showing regressions and device screen differences (as text and screenshots).

## What it tests

Each run tests a change in one repo (`--repo`) while holding the other repos fixed, including the downstream wallet **integrations** built on top of it:

| `--repo` | Repo | Flows (`--flows`) |
|---|---|---|
| `firmware` | BitBoxSwiss/bitbox02-firmware | `device`, `app`, `wc`, `rabby` |
| `api-rs` | BitBoxSwiss/bitbox-api-rs | `device` |
| `api-go` | BitBoxSwiss/bitbox02-api-go | `device`, `app`, `wc` |
| `api-ts` | BitBoxSwiss/bitbox-api-ts | `device`, `rabby` |
| `app` | BitBoxSwiss/bitbox-wallet-app | `app`, `wc` |
| `rabby` | RabbyHub/Rabby | `rabby` |

- `device`: scenario suite (`scenarios/*.json`, generated EVM matrix, firmware BTC test vectors) and the three host API simulator suites (`api-rs`, `api-go`, `api-ts`).
- `app`: BitBoxApp's Playwright tests against the simulator.
- `wc`: WalletConnect dapp signing through BitBoxApp with a local Etherscan proxy and Anvil test chains.
- `rabby`: Rabby extension signing through a fake BitBoxBridge against the graphical simulator (offline in `isolated.sh`, replaying recorded Rabby API fixtures).

`pr` compares `--head` against `merge-base(--base, --head)` so unrelated changes on the base branch never pollute the diff.

## Test a change on GitHub

### Trigger from a PR (checkboxes)

Drop [ci/pr-caller.yml](ci/pr-caller.yml) into `.github/workflows/e2e.yml` in any target repo (setting `repo:` to `firmware`, `app`, `api-rs`, `api-go`, `api-ts`, or `rabby`) and add this checklist to the repo's PR template:

```markdown
### E2E tests
- [x] `device` — device scenarios & API suites
- [ ] `app` — BitBoxApp Playwright tests
- [ ] `wc` — WalletConnect dapp flow
- [ ] `rabby` — Rabby extension flow
```

When a maintainer adds the `run-e2e` label, the caller invokes `.github/workflows/pr.yml` via `workflow_call` for only the checked flows (or all default flows if no e2e checklist is present), publishes `report.html` + referenced PNGs to `gh-pages`, posts/updates a sticky PR comment with inline screenshots and a clickable link to the hosted report, and removes the `run-e2e` label. Set an optional `E2E_PAGES_TOKEN` secret (with `contents: write` on `bitbox-e2e`) to host reports centrally on `bitbox-e2e`'s `gh-pages`; otherwise they publish to the calling repo's `gh-pages` (or fall back to the workflow artifact).

### Manual run

1. Open **Actions -> change -> Run workflow** (`.github/workflows/pr.yml`).
2. Set **Repo** and **Head** (plus **Fork holding head** as `owner/repo` for fork PRs).
3. Optional fields: **Base** (defaults to upstream default branch), **Pins** (`repo=ref` pairs), **Graphical** (scenario globs for screenshots), **Tests** (BitBoxApp Playwright files), **Flows** (subset of flows).
4. Each flow runs in its own job and uploads its directory; the `report` job combines them into the **e2e-report** artifact (`report.html`) and posts `summary.md` to the run summary.

Runs fail (red) only on regressions (tests/vectors that worked on base and fail on head) or harness errors; device screen changes are shown for human review.

## Local setup and usage

Requires x86_64 Linux with sibling repo checkouts next to `e2e/`:

```text
bitbox/
  e2e/                 this repo (Python package bitbox_e2e)
  bitbox02-firmware/
  bitbox-api-rs/
  bitbox02-api-go/
  bitbox-api-ts/
  bitbox-wallet-app/   for flows app and wc
  Rabby/               for flow rabby
```

One-time setup (installs toolchains into `~/.local/share/bitbox-e2e` and creates `.venv` via `uv sync --frozen` from `pyproject.toml` + `uv.lock`):

```sh
e2e/setup.sh
source e2e/env.sh   # run in each new shell
```

### CLI commands

Invoke via `uv run --project e2e --no-sync e2e <cmd>` or `./e2e/e2e.py <cmd>`:

| Command | Purpose |
|---|---|
| `e2e pr [--repo <repo>] --head <ref> --out <dir>` | Run base-vs-head comparison for a change |
| `e2e baseline <fw-ref>` | Pre-record a cached firmware baseline bundle |
| `e2e report <dir>` | Re-render per-flow reports and aggregate `<dir>/report.html` + `summary.md` |
| `e2e flows <repo>` | Print JSON list of flows for `<repo>` (used by CI matrix) |
| `e2e device --scenario <json> --sim-dir <dir> --out <dir>` | Run one scenario against one simulator build |
| `e2e record-rabby --firmware <ref> --out <dir>` | Re-record `rabby/fixtures/rabby-api.json.gz` |

Examples:

```sh
./e2e/e2e.py pr --head my-branch --out /tmp/e2e/fw
./e2e/e2e.py pr --head WORKTREE --out /tmp/e2e/fw
./e2e/e2e.py pr --repo api-rs --head my-branch --out /tmp/e2e/api-rs
./e2e/e2e.py pr --repo app --head my-branch --tests tests/send.test.ts --out /tmp/e2e/app
./e2e/e2e.py pr --head my-branch --flows wc rabby --out /tmp/e2e/fw-wallets
./e2e/e2e.py pr --repo rabby --base v0.94.15 --head my-branch --out /tmp/e2e/rabby
```

Key `pr` options: `--base <ref>`, `--pin <repo>=<ref>` (repeatable), `--flows <flow>...`, `--graphical <glob>...`, `--tests <file>...`, `-j <N>`. Caches live in `~/.cache/bitbox-e2e`.

### Output layout and exit codes

Every flow writes into a uniform subdirectory under `--out`:

```text
<out>/
  report.html         combined report across all flows (open in a browser)
  summary.md          concise Markdown summary for CI / PR comments
  report-files.txt    publish manifest (HTML + referenced PNGs)
  device/             findings.json, report.html, summary.md, pr.json, ...
  app/                findings.json, report.html, summary.md, base/, head/
  wc/                 findings.json, report.html, summary.md, base/, head/
  rabby/              findings.json, report.html, summary.md, base/, head/
```

Exit codes: `0` no regression, `1` regression found, `2` harness error or a flow could not run (`flow-error.json`).

## Reading the report

- **What broke**: tests or rejection vectors that passed on base and fail on head (causes exit code `1`).
- **Fixed by this PR**: tests that failed on base and pass on head.
- **What changed on the device**: text diffs and red-marked pixel diffs of changed screens, grouped by change kind.
- **What was tested / Run details**: summary per flow, already-failing items (collapsed), and revision table.

## Development and self-CI

This repository's self-CI (`.github/workflows/ci.yml`) runs on every push and pull request:

- `uv lock --check` and `uv sync --frozen`
- `uv run ruff check .`
- `uv run pytest`
- `shellcheck build-sim.sh setup.sh env.sh isolated.sh ci/*.sh`

To run checks locally inside `e2e/`:

```sh
uv sync --frozen
uv run ruff check .
uv run pytest
```
