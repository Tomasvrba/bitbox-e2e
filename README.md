# BitBox e2e

This tool tests one change in a BitBox repo from end to end. It runs the change on the BitBox02
simulator, compares the result with the code before the change, and writes a report.

The report shows:

- The device screens that the change makes different, as text and as screenshots.
- The tests that passed before the change and fail after it. These are **regressions**.

## What it can test

Each run tests a change in one of these repos. The other repos stay at a fixed version.

A run also checks the **integrations**: the wallets built on top of the changed repo. A firmware
change can break the BitBoxApp, or a dapp that signs through the app over WalletConnect, or the
Rabby browser extension. For example,
[bitbox-wallet-app#4579](https://github.com/BitBoxSwiss/bitbox-wallet-app/pull/4579) fixed
WalletConnect transactions on chains other than Ethereum: every send had failed there for six
weeks, large ones for six months. The WalletConnect flow catches this class of bug.

| `--repo` | Repo | What runs, before and after the change |
|---|---|---|
| `firmware` | BitBoxSwiss/bitbox02-firmware | All device scenarios (88) and the simulator tests of the three API libraries. Then the app's tests, WalletConnect and Rabby, each on the firmware before and after. |
| `api-rs` | BitBoxSwiss/bitbox-api-rs | The device scenarios and the API tests. The scenarios use api-rs, so screen changes show too. |
| `api-go` | BitBoxSwiss/bitbox02-api-go | The device scenarios and the API tests. Then the app's tests and WalletConnect, with the app built against api-go before and after. |
| `api-ts` | BitBoxSwiss/bitbox-api-ts | The device scenarios and the API tests. Then Rabby, built with api-ts before and after. |
| `app` | BitBoxSwiss/bitbox-wallet-app | The BitBoxApp's own Playwright tests, then WalletConnect. |
| `rabby` | RabbyHub/Rabby | The Rabby browser extension signs messages and transactions with the device. |

Each of these is a **flow**: `device` (the scenarios and API tests), `app`, `wc` (WalletConnect)
and `rabby`. Run fewer with `--flows`. In the integration flows, the app is at upstream `master`
and Rabby at release v0.94.15, unless the change is in them or `--pin` says otherwise.

The tool compares the head of the change with the **merge-base**: the commit where the change
started from the base branch. Thus, other changes on the base branch do not show in the report.

## Test a change on GitHub

Use this procedure to test a pull request (PR) without a local setup.

1. Open the **change** workflow: Actions > change > Run workflow.
2. Set **Repo** to the repo of the change.
3. Set **Head** to the branch of the PR.
4. If the PR is from a fork, set **Fork holding head** to the fork, for example `alice/bitbox02-firmware`.
5. Keep the other fields empty, or see [Workflow fields](#workflow-fields).
6. Click **Run workflow**.

A run takes 10 to 35 minutes. When the run is complete:

- The run page shows the result and one line for each finding. Each flow runs as its own job, so
  a red job names the flow that found a regression.
- The **e2e-report** artifact has the full report with the screenshots. Download it, unzip it, and
  open `report.html`.

The run fails (red) only if there is a regression. Changed screens do not make the run fail. A
reviewer must look at them.

### Workflow fields

| Field | Use |
|---|---|
| Repo | The repo of the change. |
| Head | The branch or commit of the change. |
| Fork holding head | The fork (`owner/repo`) if the branch is not in the upstream repo. |
| Base | The branch to compare with. Empty: the default branch of the upstream repo. |
| Pins | Other repos at a fixed version, for example `firmware=v9.24.0 api-rs=v0.12.0`. Empty: the latest version. |
| Graphical | Firmware or API change: more scenarios that must have screenshots, for example `btc-vector/*fee*`. Changed scenarios always get screenshots. |
| Tests | Flow `app`: the test files to run, for example `tests/watch-only.test.ts`. Empty: all tests. |
| Flows | Only these flows, for example `device wc`. Empty: all flows of the change. Each flow runs as its own job. |

## Test a change on your computer

### Set up (one time)

You need x86_64 Linux. Put the repos next to each other:

```
bitbox/
  e2e/                    this repo
  bitbox02-firmware/
  bitbox-api-rs/
  bitbox02-api-go/
  bitbox-api-ts/
  bitbox-wallet-app/      for the flows app and wc
  Rabby/                  for the flow rabby
```

Then do these steps:

1. Run `e2e/setup.sh`. It installs the tools into your home directory. It does not use root.
2. If `setup.sh` stops and shows an `apt-get install` command, run that command as root. Then run `setup.sh` again.
3. Run `source e2e/env.sh`. Do this in each new shell before you use the tool.

### Run

```sh
e2e/e2e.py pr --repo <repo> --head <branch> --out /tmp/e2e/<name>
```

Examples:

```sh
e2e/e2e.py pr --head my-branch --out /tmp/e2e/fw                       # firmware change
e2e/e2e.py pr --head WORKTREE --out /tmp/e2e/fw                        # firmware, uncommitted changes
e2e/e2e.py pr --repo api-rs --head my-branch --out /tmp/e2e/api-rs
e2e/e2e.py pr --repo app --head my-branch --tests tests/send.test.ts --out /tmp/e2e/app
e2e/e2e.py pr --head my-branch --flows wc rabby --out /tmp/e2e/fw-wallets   # firmware, integrations only
e2e/e2e.py pr --repo rabby --base v0.94.15 --head my-branch --out /tmp/e2e/rabby
```

Options:

- `--base <ref>`: the branch to compare with. Default: the default branch of the upstream remote,
  for example `bitbox/master`.
- `--pin <repo>=<ref>`: keep another repo at this version. You can use it more than one time.
  Default: the upstream `master` for the firmware and the app, and Rabby v0.94.15. For the API
  repos: your checkout's `HEAD` in the device flow, and the version the app or Rabby ships with in
  their flows.
- `--flows <flow>...`: run only these flows of the change. Default: all.
- `--graphical <glob>`: firmware or API change: also take screenshots of these scenarios.
- `--tests <file>...`: flow `app`, as in [Workflow fields](#workflow-fields).
- `--pr-info <file>`: the PR, from `gh pr view <number> -R <repo> --json url,number,title,body`. The
  report then shows the PR title and description.

The first run for a firmware version builds the simulators and records the "before" results. This
takes 10 to 20 minutes. Later runs use the cache in `~/.cache/bitbox-e2e`.

### Result

The tool writes these files to the `--out` directory:

- `report.html`: the report. Open it in a browser. Keep it in the directory: it shows the images
  from there.
- `summary.md`: the result and one line for each finding.
- `pr.json`: the data of the report (firmware and API changes).
- `app/`, `wc/`, `rabby/`: the integration flows, each with its own `report.html`. The top
  `report.html` shows their findings too.

The exit code tells the result:

| Exit code | Meaning |
|---|---|
| 0 | No regression. |
| 1 | Regression: something that worked before the change does not work after it. |
| 2 | The tool itself failed, or a flow could not run (the report says which). Read the log. |

## Read the report

The report starts with the result: what broke, and how many device screens changed. Then it has
these sections:

1. **What this PR should change**: the PR description. Compare the changes below with it.
2. **What broke**: each test that passed before the PR and fails with it, with its error message and
   screens. Each item makes the run fail.
3. **What changed on the device**: each screen that is different, before and after. The change is
   marked in red. A change does not make the run fail. Make sure that each change is what the PR
   intends.
4. **To do after merge**: for example, approve new screens with `e2e.py promote`.
5. **What was tested**: the scenarios and tests that ran, and changed code that no scenario runs.

Collapsed at the end: problems that were there before the PR, and the run details.

To mark the screen changes that the PR intends, add this block to the PR description. The report
then marks each change as *Expected* or *Not expected*.

```
<!-- e2e
expect: btc-vector/*fee*
-->
```

## More documentation

- [docs/reference.md](docs/reference.md): all commands and options, the scenarios, the WalletConnect
  and Rabby flows, the cache, and known problems.
- [DESIGN.md](DESIGN.md): how the parts work together.
- [docs/showcase.html](docs/showcase.html): slides for the team.
