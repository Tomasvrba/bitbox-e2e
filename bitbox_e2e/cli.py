"""BitBox simulator e2e CLI: run scenarios and integration flows against base/head and report diffs.

  e2e pr [--repo firmware|app|api-rs|api-go|api-ts|rabby] [--base <ref>] --head <ref> --out <dir>
         [--pin <repo>=<ref>...] [--flows <flow>...] [--graphical <glob>...] [--tests <file>...] [-j N]
  e2e baseline <ref> [--graphical <glob>...] [--pin <repo>=<ref>...] [--force] [-j N]
  e2e report <pr-out-dir>
  e2e flows <repo>
  e2e device --scenario scenarios/x.json --sim-dir <dir> --out <dir>
  e2e record-rabby --firmware <ref> --out <dir> [--rabby-ref v0.94.15]
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

from bitbox_e2e import config, report, runs
from bitbox_e2e.config import API_REPOS, FLOW_NAMES, FLOW_ORDER, FLOW_REPOS, FLOWS, PIN_REPOS, REPOS, log


def parse_pins(pins: list[str] | None, changed: str | None = None) -> dict[str, str]:
    """--pin name=ref ... -> {name: sha}."""
    out: dict[str, str] = {}
    for p in pins or ():
        name, sep, ref = p.partition("=")
        if not sep or name not in PIN_REPOS or not ref:
            sys.exit(f"--pin {p}: expected <{'|'.join(PIN_REPOS)}>=<ref>")
        if name == changed:
            sys.exit(f"--pin {p}: {name} is the repo under test, its refs are --base and --head")
        out[name] = config.resolve(ref, name)
    return out


def api_pins(pins: dict[str, str]) -> dict[str, str]:
    return {k: v for k, v in pins.items() if k in API_REPOS}


def flow_default(repo: str) -> str | None:
    if repo in ("firmware", "app"):
        return config.resolve(config.default_ref(repo), repo)
    if repo == "rabby":
        from bitbox_e2e.flows import rabby

        return rabby.DEFAULT_REF
    return None


def flow_sides(flow: str, repo: str, base: str, head: str, pins: dict[str, str]) -> dict:
    sides: dict = {"changed": repo, "base": {}, "head": {}}
    for r in FLOW_REPOS[flow]:
        b, h = (base, head) if r == repo else (pins.get(r) or flow_default(r),) * 2
        sides["base"][r], sides["head"][r] = b, h
    return sides


def run_flow(flow: str, args, base: str, pins: dict[str, str], out_dir: Path) -> int:
    """One flow of the change; 0 nothing broke, 1 something broke."""
    if flow == "device":
        from bitbox_e2e.flows import device

        return device.run(args.repo, args.base, args.head, pins, args.graphical, args.jobs, out_dir)
    sides = flow_sides(flow, args.repo, base, args.head, pins)
    if flow == "app":
        from bitbox_e2e.flows import wallet_app

        return wallet_app.compare(sides, args.tests, out_dir)
    if flow == "wc":
        from bitbox_e2e.flows import wc

        return wc.compare(sides, out_dir)
    from bitbox_e2e.flows import rabby

    return rabby.compare(sides, out_dir)


def cmd_baseline(args) -> int:
    from bitbox_e2e.flows import device

    runs.ensure_driver()
    timer = runs.Timer()
    sha = config.resolve(args.ref)
    root, m, unit_list = runs.ensure_baseline(
        sha, args.jobs, timer, force=args.force, revs=api_pins(parse_pins(args.pin, "firmware"))
    )
    runs.baseline_graphical(root, m, sha, unit_list, device.matching(unit_list, args.graphical), args.jobs)
    timer.done("baseline graphical")
    ok = sum(bool(u["headless_ok"]) for u in m["units"].values())
    log(f"baseline {sha[:9]}: {ok}/{len(m['units'])} units ok, timings {timer.phases}")
    print(root)
    return 0


def cmd_pr(args) -> int:
    """One change, base vs head, in the repo given by --repo; the other repos at their pins. Runs
    the repo's FLOWS (or --flows), each into out/<flow>, even when one fails."""
    args.base = args.base or config.default_ref(args.repo)
    pins = parse_pins(args.pin, args.repo)
    flows = args.flows or FLOWS[args.repo]
    if bad := [x for x in flows if x not in FLOWS[args.repo]]:
        sys.exit(f"--flows: a {args.repo} change runs {', '.join(FLOWS[args.repo])}, not {', '.join(bad)}")
    out = Path(args.out)
    for d in (out, *(out / x for x in FLOW_ORDER)):
        # Results of an earlier run must not show up as this one's.
        for name in ("flow-error.json", "findings.json", "report-files.txt"):
            (d / name).unlink(missing_ok=True)
    head = config.resolve(args.head, args.repo)
    base = config.git("merge-base", config.resolve(args.base, args.repo), head, cwd=REPOS[args.repo].dir)
    rc = 0
    for flow in sorted(flows, key=FLOWS[args.repo].index):
        d = out / flow
        log(f"flow {flow}: {d}")
        try:
            rc = max(rc, run_flow(flow, args, base, pins, d))
        except SystemExit as e:
            msg = str(e) or f"exited with {e.code}"
            log(f"flow {flow} failed: {msg}")
            report.write_flow_error(d, flow, msg)
            rc = 2
        except Exception:  # noqa: BLE001 - the other flows still run; the report says this one could not
            traceback.print_exc()
            report.write_flow_error(d, flow, traceback.format_exc())
            rc = 2
    page = report.render_run(out)
    log(f"report: {page}")
    print(page)
    return rc


def render_report(out: Path) -> Path:
    """Re-render every present out/<flow> that does not have flow-error.json, then aggregate."""
    out = Path(out)
    for flow in FLOW_ORDER:
        d = out / flow
        if (d / "flow-error.json").exists():
            continue
        if flow == "device" and (d / "pr.json").exists():
            from bitbox_e2e.flows import device

            device.render(d)
        elif flow == "app" and (d / "base").is_dir():
            from bitbox_e2e.flows import wallet_app

            wallet_app.render(d)
        elif flow == "wc" and (d / "base").is_dir():
            from bitbox_e2e.flows import wc

            wc.render(d)
        elif flow == "rabby" and (d / "base").is_dir():
            from bitbox_e2e.flows import rabby

            rabby.render(d)
    has_any = any(
        (out / flow / "findings.json").exists() or (out / flow / "flow-error.json").exists()
        for flow in FLOW_ORDER
    )
    if not has_any:
        sys.exit(f"{out}: not the --out directory of e2e pr")
    page = report.render_run(out)
    log(f"report: {page}")
    print(page)
    return page


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("device", help="run one scenario against one simulator")
    d.add_argument("--scenario", required=True)
    d.add_argument("--sim-dir", required=True)
    d.add_argument("--out", required=True)
    d.add_argument("--label")
    d.add_argument("--no-graphical", action="store_true")
    d.add_argument("--no-headless", action="store_true")

    for name in ("baseline", "pr"):
        b = sub.add_parser(name)
        if name == "baseline":
            b.add_argument("ref", help="firmware commit")
            b.add_argument("--force", action="store_true", help="re-record an existing bundle")
        else:
            b.add_argument("--repo", choices=REPOS, default="firmware", help="the repo the change is in")
            b.add_argument(
                "--base",
                help="ref in that repo; the run compares head against its merge-base "
                "with head (default: the upstream default branch)",
            )
            b.add_argument("--head", required=True, help="ref in that repo, or WORKTREE (firmware only)")
            b.add_argument("--out", required=True)
            b.add_argument(
                "--tests",
                nargs="+",
                default=[],
                metavar="FILE",
                help="flow app: Playwright test files to run, relative to frontends/web (default: all)",
            )
            b.add_argument(
                "--flows",
                nargs="+",
                choices=FLOW_NAMES,
                metavar="FLOW",
                help="run only these of the change's flows ("
                + "; ".join(f"{r}: {' '.join(f)}" for r, f in FLOWS.items())
                + "; default: all)",
            )
        b.add_argument(
            "--pin",
            action="append",
            default=[],
            metavar="REPO=REF",
            help=f"run another repo at this ref on both sides ({', '.join(PIN_REPOS)}); default: "
            "upstream master for the firmware and the app, Rabby v0.94.15, for the API repos the "
            "sibling checkout's HEAD in the device flow and the version the app or Rabby ships with in theirs",
        )
        b.add_argument(
            "--graphical",
            nargs="+",
            action="extend",
            default=[],
            metavar="GLOB",
            help="unit id globs to also run on the graphical simulator",
        )
        b.add_argument(
            "-j",
            "--jobs",
            type=int,
            default=config.JOBS,
            help=f"units in parallel (default: half the CPUs, {config.JOBS})",
        )

    rp = sub.add_parser("report", help="render report.html and summary.md again from a pr --out directory")
    rp.add_argument("dir")

    fl = sub.add_parser("flows", help="the flows a change in repo runs, as a JSON list (for the CI job matrix)")
    fl.add_argument("repo", choices=FLOWS)

    rr = sub.add_parser("record-rabby", help="re-record Rabby API fixture (rabby/fixtures/rabby-api.json.gz)")
    rr.add_argument("--firmware", required=True, help="firmware ref or WORKTREE")
    rr.add_argument("--out", required=True)
    rr.add_argument("--rabby-ref", default="v0.94.15", help="Rabby tag/commit, built unpacked from a clone")

    return p


def dispatch(args) -> int:
    if args.cmd == "device":
        runs.ensure_driver()
        meta = runs.cmd_device(args)
        return 1 if False in (meta["headless_ok"], meta["graphical_ok"]) else 0
    if args.cmd == "baseline":
        return cmd_baseline(args)
    if args.cmd == "pr":
        return cmd_pr(args)
    if args.cmd == "flows":
        print(json.dumps(list(FLOWS[args.repo])))
        return 0
    if args.cmd == "report":
        render_report(Path(args.dir))
        return 0
    if args.cmd == "record-rabby":
        from bitbox_e2e.flows import rabby

        rabby.record(args.firmware, args.rabby_ref, Path(args.out))
        return 0
    return 2


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    # Exit codes: 0 clean, 1 regression (per command), 2 the harness itself failed.
    try:
        rc = dispatch(args)
    except SystemExit as e:
        if isinstance(e.code, str):  # sys.exit("message"): an error the harness diagnosed
            log(e.code)
            sys.exit(2)
        raise
    except Exception:
        traceback.print_exc()
        sys.exit(2)
    sys.exit(rc or 0)


if __name__ == "__main__":
    main()
