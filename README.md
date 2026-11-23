# BitBox e2e

This tool tests one change in a BitBox repo from end to end. It runs the change on the BitBox02
simulator, compares the result with the code before the change, and writes a report.

The report shows:

- The device screens that the change makes different, as text and as screenshots.
- The tests that passed before the change and fail after it. These are **regressions**.

## What it can test

Each run tests a change in one of these repos. The other repos stay at a fixed version.

| `--repo` | Repo | What runs, before and after the change |
|---|---|---|
| `firmware` | BitBoxSwiss/bitbox02-firmware | All device scenarios (88), and the simulator tests of the three API libraries |
| `api-rs` | BitBoxSwiss/bitbox-api-rs | The same as `firmware`. The scenarios use api-rs, so screen changes show too. |
| `api-go` | BitBoxSwiss/bitbox02-api-go | The same as `firmware`. Only the api-go tests can change. |
| `api-ts` | BitBoxSwiss/bitbox-api-ts | The same as `firmware`. Only the api-ts tests can change. |
| `app` | BitBoxSwiss/bitbox-wallet-app | The BitBoxApp's own Playwright tests. Optional: the WalletConnect flow. |
| `rabby` | RabbyHub/Rabby | The Rabby browser extension signs messages and transactions with the device. |

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

A run takes 20 to 60 minutes. When the run is complete:

- The run page shows the top of the report: the verdict and the summary.
- The **e2e-report** artifact has the full report with the screenshots. Open `report.html`.

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
| Tests | App change: the test files to run, for example `tests/watch-only.test.ts`. Empty: all tests. |
| WalletConnect | App change: also run the WalletConnect flow. |

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
  bitbox-wallet-app/      only for app changes
  Rabby/                  only for Rabby changes
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
e2e/e2e.py pr --repo rabby --base v0.94.15 --head my-branch --out /tmp/e2e/rabby
```

Options:

- `--base <ref>`: the branch to compare with. Default: the default branch of the upstream remote,
  for example `bitbox/master`.
- `--pin <repo>=<ref>`: keep another repo at this version. You can use it more than one time.
  Default: the upstream `master` for the firmware, and your checkout's `HEAD` for the API repos.
- `--graphical <glob>`: firmware or API change: also take screenshots of these scenarios.
- `--tests <file>...`, `--wc`: app change, as in [Workflow fields](#workflow-fields).

The first run for a firmware version builds the simulators and records the "before" results. This
takes 10 to 20 minutes. Later runs use the cache in `~/.cache/bitbox-e2e`.

### Result

The tool writes these files to the `--out` directory:

- `report.html`: the report with all images in one file. Open it in a browser.
- `report.md`: the same report, with links to the images in the directory.
- `pr.json`: the data of the report (firmware and API changes).

The exit code tells the result:

| Exit code | Meaning |
|---|---|
| 0 | No regression. |
| 1 | Regression: something that worked before the change does not work after it. |
| 2 | The tool itself failed. Read the log. |

## Read the report

1. Read the **Verdict**. It lists each regression.
2. Read the table at the top. It shows the version of each repo, before and after.
3. If the note under the table says that another repo has a different version, then a difference can come from that repo.
4. Look at the changed screens. Make sure that each change is what the PR intends.

These items do not make the run fail, but they need a decision:

- **Changed units**: screens that are different after the change.
- **Expectation failures**: screens that are different from the approved screens. After the PR is
  merged, a maintainer approves the new screens with `e2e.py promote`.
- **Unreached changed functions**: changed firmware code that no scenario runs. Add a scenario.

## More documentation

- [docs/reference.md](docs/reference.md): all commands and options, the scenarios, the WalletConnect
  and Rabby flows, the cache, and known problems.
- [DESIGN.md](DESIGN.md): how the parts work together.
- [docs/showcase.html](docs/showcase.html): slides for the team.
