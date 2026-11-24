#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pillow>=10", "python-xlib>=0.33"]
# ///
"""BitBox simulator e2e: run scenarios against base/head firmware and report the device diff.

  e2e.py baseline <ref> [--graphical <glob>...] [-j N]       record the bundle for a firmware commit
  e2e.py pr [--repo firmware|app|api-rs|api-go|api-ts|rabby] [--base <ref>] --head <ref> --out <dir>
            [--pin <repo>=<ref>...] [--flows <flow>...] [--graphical <glob>...] [--tests <file>...]
            [--pr-info <file>] [-j N]                        one change: its own tests and the integrations
  e2e.py report <pr-out-dir> [--pr-info <file>]               render the report again
  e2e.py flows <repo>                                          the flows a change in repo runs (JSON)
  e2e.py promote <pr-out-dir> <unit-id>...                    head screens become expectations
  e2e.py device  --scenario scenarios/x.json --sim-dir <dir> --out <dir>   one scenario, one simulator
  e2e.py wc --base <app-ref> --head <app-ref> --out <dir> [--firmware <ref>]   WalletConnect flow (wc/)
  e2e.py rabby --base <fw-ref> --head <fw-ref> --out <dir> [--rabby-ref v0.94.15]  Rabby extension flow (rabby/)

Simulator runs are in runs.py, report building blocks in report.py, app changes in wallet_app.py.
"""

import argparse
import difflib
import fnmatch
import json
import re
import shutil
import sys
import time
import traceback
from pathlib import Path

E2E_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(E2E_DIR))

import apis  # noqa: E402
import bundle  # noqa: E402
import expect  # noqa: E402
from bundle import build_sim, log, read_text, sim_version  # noqa: E402
import report  # noqa: E402
from report import SETUP_STEPS, frame_rows, plural, results, screen_lines  # noqa: E402
import wallet_app  # noqa: E402
from runs import cmd_device, driver_for, ensure_driver  # noqa: E402


# --- baseline / pr --------------------------------------------------------------------------------

def matching(units, globs):
    return {u["id"] for u in units if any(fnmatch.fnmatchcase(u["id"], g) for g in globs or ())}


def load_screens(unit_dir):
    p = unit_dir / "headless" / "screens.json"
    return json.loads(p.read_text()) if p.exists() else []


def frame_hashes(unit_dir):
    p = unit_dir / "graphical" / "frames.json"
    return [f["hash"] for f in json.loads(p.read_text()) if f["step"] not in SETUP_STEPS] if p.exists() else None


class Timer:
    def __init__(self):
        self.t, self.phases = time.time(), {}

    def done(self, name):
        now = time.time()
        self.phases[name] = round(now - self.t)
        self.t = now
        log(f"phase {name}: {self.phases[name]}s")


def ensure_baseline(sha, jobs, timer=None, force=False, revs=None):
    """Bundle for a firmware commit: created on first use (every unit headless + lane B), reused
    after. Returns (root, manifest, units). The simulator is built only to record: a bundle restored
    from a CI cache must not cost a simulator build. revs pins the API repos ({"api-rs": sha}): the
    suites run at those commits, and the driver links api-rs at its pin."""
    revs = revs or {}
    root = bundle.bundle_dir(sha, revs)
    fw_dir = bundle.firmware_dir(sha)
    units = bundle.load_units(fw_dir)
    m = None if force else bundle.read_manifest(root)
    if m is not None:
        log(f"reusing baseline {root}")
        return root, m, units
    sim_dir = build_sim(sha)
    # coverage/ is written by coverage.py map and costs far more to rebuild; keep it.
    (root / "manifest.json").unlink(missing_ok=True)
    for d in ("units", "apis"):
        shutil.rmtree(root / d, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    m = {"sha": sha, "simulator_version": sim_version(sim_dir), "harness_sha": bundle.harness_sha(),
         "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "pins": revs, "units": {}}
    ok = bundle.run_units(sim_dir, units, root, "headless", jobs, driver_for(revs.get("api-rs")))
    if not any(ok.values()):
        # Every head unit would compare against "failed on base", so the gate could never fire.
        raise RuntimeError(f"no unit completed on {sha[:9]}; not recording the baseline "
                           f"(logs: {root}/units/*/headless.log)")
    m["units"] = {i: {"headless_ok": v, "graphical_ok": None} for i, v in ok.items()}
    timer and timer.done("baseline headless")
    m["revisions"] = revisions(apis.run(sim_dir, fw_dir / bundle.FW_VECTORS, root / "apis", revs=revs), revs)
    timer and timer.done("baseline apis")
    # The manifest marks the bundle complete, so a recording that died halfway is redone next time.
    bundle.write_manifest(root, m)
    return root, m, units


def baseline_graphical(root, m, sha, units, ids, jobs):
    """Adds graphical runs to the bundle for units that do not have one yet. Builds the simulator
    only then. Returns how they ran (bundle.graphical_mode), None if nothing was missing."""
    todo = [u for u in units if u["id"] in ids and u["id"] in m["units"] and m["units"][u["id"]]["graphical_ok"] is None]
    if not todo:
        return None
    sim_dir = build_sim(sha)
    driver = driver_for((m.get("pins") or {}).get("api-rs"))
    for i, v in bundle.run_units(sim_dir, todo, root, "graphical", jobs, driver).items():
        m["units"][i]["graphical_ok"] = v
    bundle.write_manifest(root, m)
    return bundle.graphical_mode(sim_dir)


def revisions(api_results, pins=None):
    """Code under test besides the firmware: the bitbox-api-rs the driver links (its pin, else the
    sibling tree of the driver/Cargo.toml path dependency; with E2E_DRIVER whatever that binary was
    built from) and the API repo revisions the suites ran. Base and head can differ, e.g. a bundle
    recorded weeks ago."""
    revs = {"driver bitbox-api-rs": (pins or {}).get("api-rs") or bundle.checkout_rev(bundle.REPOS["api-rs"].dir)}
    for suite, modes in api_results["suites"].items():
        revs[suite] = next((r["repo_sha"] for r in modes.values() if r.get("repo_sha")), None)
    return revs


def revision_diffs(base, head, changed=None):
    """Keys of revisions() known on both sides that differ, other than the changed repo's own."""
    own = {changed, f"driver bitbox-{changed}"} if changed else set()
    return [k for k, v in head.items() if v and base.get(k) and base[k] != v and k not in own]


def cmd_baseline(args):
    ensure_driver()
    timer = Timer()
    sha = bundle.resolve(args.ref)
    root, m, units = ensure_baseline(sha, args.jobs, timer, force=args.force, revs=api_pins(parse_pins(args.pin, "firmware")))
    baseline_graphical(root, m, sha, units, matching(units, args.graphical), args.jobs)
    timer.done("baseline graphical")
    ok = sum(bool(u["headless_ok"]) for u in m["units"].values())
    log(f"baseline {sha[:9]}: {ok}/{len(m['units'])} units ok, timings {timer.phases}")
    print(root)


def select_units(base_sha, head):
    """coverage.select for the diff, or None without a map for the merge-base."""
    # Imported here: coverage imports this module.
    import coverage
    if not (bundle.bundle_dir(base_sha) / "coverage" / "map.json").exists():
        return None
    return coverage.select(base_sha, head)


def verdict(units, base_apis, head_apis, modes=("firmware",)):
    """Why the PR fails the gate, empty if it passes. Takes pr.json's units, base_apis and apis.
    Only regressions against the merge-base count: changed screens and expectation mismatches are
    evidence for the reviewer, expectations get promoted after the firmware PR merges.

    modes: the API vector modes that gate. For a firmware change only the run with head's own
    vectors: the API repos' committed copies assert the screens of the firmware they were synced
    from, so they fail on any PR that changes one (drift). For an API change both: the firmware is
    the same on both sides, so any newly failing test is the change's."""
    reasons = [f"{uid}: completed on base, fails on head" + (f" ({u['head_error']})" if u.get("head_error") else "")
               for uid, u in sorted(units.items()) if u["base_ok"] and not u["head_ok"]]
    # A vector the device must reject (outcome invalid_input, unsupported) "fails" by design; one
    # that was rejected on base and completes on head is the regression.
    reasons += [f"{uid}: must be rejected ({u['outcome']}), was rejected on base, completes on head"
                for uid, u in sorted(units.items()) if rejects(u) and u["base_ok"] is False and u["head_ok"]]
    for mode in modes:
        for suite, by_mode in head_apis["suites"].items():
            if err := by_mode.get(mode, {}).get("error"):
                reasons.append(f"{suite} ({mode} vectors): suite error on head: {err}")
        reasons += [f"{suite} ({mode} vectors): newly failing on head: " + ", ".join(tests)
                    for suite, tests in api_newly_failing(base_apis, head_apis, mode).items()]
    return reasons


def rejects(u):
    return u.get("outcome") not in (None, "success")


def api_newly_failing(base_apis, head_apis, mode):
    """{suite: tests} that passed on base and fail on head, for one vector mode."""
    base_suites = (base_apis or {}).get("suites", {})
    bv, hv = (base_apis or {}).get("simulator"), head_apis.get("simulator")
    out = {}
    for suite, modes in head_apis["suites"].items():
        r, b = modes.get(mode) or {}, base_suites.get(suite, {}).get(mode)
        if b is None:
            continue  # not run on base (new suite, or no firmware vectors at the merge-base)
        was = {api_test_key(t, bv) for t in b["passing"]}
        newly = [t for t in r.get("failing", []) if api_test_key(t, hv) in was]
        if newly:
            out[suite] = newly
    return out


def api_test_key(name, simulator):
    """API test name without the simulator version: api-go and api-ts put it into test names, so a
    PR that bumps the version would otherwise rename every test and hide its regressions."""
    m = re.search(r"\d+\.\d+\.\d+", simulator or "")
    return name.replace(simulator, "<simulator>").replace(m[0], "<version>") if m else name


def unit_error(unit_dir):
    """Short reason a headless run failed: the runner's (timeout, crash) or the first failing step."""
    meta = unit_dir / "meta.json"
    err = json.loads(meta.read_text()).get("headless_error") if meta.exists() else None
    return err or next((r.splitlines()[0][:160] for r in results(unit_dir).values() if r != "ok"), None)


def unit_path(u):
    """Scenario file of a promotable unit, relative to the harness checkout (pr.json travels from CI)."""
    return str(Path(u["path"]).resolve().relative_to(E2E_DIR)) if u.get("path") else None


def promote_path(p):
    """Local file for a pr.json unit path; only files under this checkout's scenarios/ are written."""
    scenarios = (E2E_DIR / "scenarios").resolve()
    full = (E2E_DIR / p).resolve()
    if not full.is_relative_to(scenarios) or full.suffix != ".json":
        raise ValueError(f"{p}: not a scenario file under {scenarios}")
    return full


# Repos that --pin can fix for both sides: any but the one under test.
PIN_REPOS = ("firmware", "app", "api-rs", "api-go", "api-ts", "rabby")
API_REPOS = ("api-rs", "api-go", "api-ts")


def parse_pins(pins, changed=None):
    """--pin name=ref ... -> {name: sha}."""
    out = {}
    for p in pins or ():
        name, sep, ref = p.partition("=")
        if not sep or name not in PIN_REPOS or not ref:
            sys.exit(f"--pin {p}: expected <{'|'.join(PIN_REPOS)}>=<ref>")
        if name == changed:
            sys.exit(f"--pin {p}: {name} is the repo under test, its refs are --base and --head")
        out[name] = bundle.resolve(ref, name)
    return out


def api_pins(pins):
    return {k: v for k, v in pins.items() if k in API_REPOS}


# What a change in each repo runs. "device": every unit and the API suites (cmd_device_change). The
# others are the integrations built on top of the BitBox repos: the app's own tests, a dapp over
# WalletConnect through the app, and the Rabby extension. They catch a change that breaks a
# wallet integration, like bitbox-wallet-app#4579. The first flow writes the run's report into
# --out, the others into --out/<flow>, and the run's page shows them all.
FLOWS = {
    "firmware": ("device", "app", "wc", "rabby"),
    "api-rs": ("device",),
    "api-go": ("device", "app", "wc"),
    "api-ts": ("device", "rabby"),
    "app": ("app", "wc"),
    "rabby": ("rabby",),
}
# The repos each integration flow builds on. The changed one is at base and head, the others at
# --pin or these defaults on both sides; None is the version the app or Rabby ships with.
FLOW_REPOS = {"app": ("firmware", "app", "api-go"), "wc": ("firmware", "app", "api-go"),
              "rabby": ("firmware", "rabby", "api-ts")}


def flow_default(repo):
    if repo in ("firmware", "app"):
        return bundle.resolve(bundle.default_ref(repo), repo)
    if repo == "rabby":
        from rabby import rabby_e2e
        return rabby_e2e.DEFAULT_REF
    return None


def flow_sides(flow, repo, base, head, pins):
    sides = {"changed": repo, "base": {}, "head": {}}
    for r in FLOW_REPOS[flow]:
        b, h = (base, head) if r == repo else (pins.get(r) or flow_default(r),) * 2
        sides["base"][r], sides["head"][r] = b, h
    return sides


def flow_dir(out, repo, flow):
    return out if flow == FLOWS[repo][0] else out / flow


def run_flow(flow, args, base, pins, out):
    """One flow of the change; 0 nothing broke, 1 something broke."""
    if flow == "device":
        return cmd_device_change(args, pins)
    sides = flow_sides(flow, args.repo, base, args.head, pins)
    if flow == "app":
        return wallet_app.compare(sides, args.tests, out)
    if flow == "wc":
        from wc import flow as wc_flow
        return wc_flow.compare(sides, out)
    from rabby import rabby_e2e
    return rabby_e2e.compare(sides, out)


def cmd_pr(args):
    """One change, base vs head, in the repo given by --repo; the other repos at their pins. Runs
    the repo's FLOWS (or --flows), each to the end even when one fails."""
    args.base = args.base or bundle.default_ref(args.repo)
    pins = parse_pins(args.pin, args.repo)
    flows = args.flows or FLOWS[args.repo]
    if bad := [x for x in flows if x not in FLOWS[args.repo]]:
        sys.exit(f"--flows: a {args.repo} change runs {', '.join(FLOWS[args.repo])}, not {', '.join(bad)}")
    # Read first: the flows start by emptying --out, which may hold the file.
    pr_info = Path(args.pr_info).read_text() if args.pr_info else None
    out = Path(args.out)
    for d in (out, *(out / x for x in report.FLOW_DIRS)):
        # Results of an earlier run must not show up as this one's.
        for name in ("flow-error.json", "findings.json"):
            (d / name).unlink(missing_ok=True)
    head = bundle.resolve(args.head, args.repo)
    base = bundle.git("merge-base", bundle.resolve(args.base, args.repo), head, cwd=bundle.REPOS[args.repo].dir)
    rc = 0
    for flow in sorted(flows, key=FLOWS[args.repo].index):
        d = flow_dir(out, args.repo, flow)
        log(f"flow {flow}: {d}")
        try:
            rc = max(rc, run_flow(flow, args, base, pins, d))
        except Exception:  # noqa: BLE001 - the other flows still run; the report says this one could not
            traceback.print_exc()
            report.write_flow_error(d, flow, traceback.format_exc())
            rc = 2
    if pr_info:
        (out / "pr-info.json").write_text(pr_info)
    render_report(out)
    return rc


def render_report(out):
    """Renders report.html and summary.md again from a run's directory (e.g. with pr-info.json
    added): the integration flows' own pages first, then the run's page, which shows them all."""
    from rabby import rabby_e2e
    from wc import flow as wc_flow
    renderers = {"app": wallet_app.wallet_report, "wc": wc_flow.report, "rabby": rabby_e2e.report}
    for flow in report.FLOW_DIRS:
        if (out / flow / "base").is_dir() and not (out / flow / "flow-error.json").exists():
            renderers[flow](out / flow)
    if (out / "flow-error.json").exists():
        e = json.loads((out / "flow-error.json").read_text())
        pr_info = report.load_pr_info(out)
        return report.render(out, {"title": (pr_info or {}).get("title") or f"{report.FLOW_NAMES[e['flow']]} could not run",
                                   "pr": pr_info, "intent": report.intent(pr_info)})
    if (out / "pr.json").exists():
        return pr_report(out)
    if (out / "base" / "tests.json").exists():
        return wallet_app.wallet_report(out)
    if (out / "base" / "frames.json").exists() or (out / "head" / "frames.json").exists():
        return rabby_e2e.report(out)
    sides = next((s for x in report.FLOW_DIRS if (s := report.load_sides(out / x))), None)
    if sides:  # only integration flows ran (--flows)
        pr_info, c = report.load_pr_info(out), sides["changed"]
        return report.render(out, {
            "title": (pr_info or {}).get("title") or f"{report.REPO_NAMES[c]} {report.short_ref(sides['head'][c])}",
            "repo": bundle.REPOS[c].slug, "sides": report.sides_header(sides), "pr": pr_info,
            "intent": report.intent(pr_info)})
    sys.exit(f"{out}: not the --out directory of e2e.py pr")


def cmd_device_change(args, pins):
    """A firmware or API change: every unit and the API suites, head against the merge-base."""
    timer = Timer()
    out = Path(args.out)
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    repo = args.repo
    head_sha = bundle.resolve(args.head, repo)
    base_sha = bundle.git("merge-base", bundle.resolve(args.base, repo), head_sha, cwd=bundle.REPOS[repo].dir)
    revs = api_pins(pins)
    if repo == "firmware":
        fw_base, fw_head, base_revs, head_revs = base_sha, args.head, revs, revs
    else:
        fw_base = fw_head = pins.get("firmware") or bundle.resolve(bundle.default_firmware_ref())
        base_revs, head_revs = revs | {repo: base_sha}, revs | {repo: head_sha}
    root, m, base_units = ensure_baseline(fw_base, args.jobs, timer, revs=base_revs)
    timer.done("baseline")

    head_sim = build_sim(fw_head)
    head_fw = bundle.firmware_dir(fw_head)
    head_units = bundle.load_units(head_fw)
    by_id = {u["id"]: u for u in head_units}
    head_driver = driver_for(head_revs.get("api-rs"))
    timer.done("head build")

    # Every unit runs headless: lane B checks all of them, and coverage cannot see C, protos or
    # token lists. Coverage only reports changed code no unit reaches. It does not pick graphical
    # runs either: a wide diff selects every unit (88 of 88 for one release), ~25 min of frames for
    # screens whose text did not change.
    cov = select_units(base_sha, args.head) if repo == "firmware" else None
    picked = matching(head_units, args.graphical)
    head_ok = bundle.run_units(head_sim, head_units, out / "head", "headless", args.jobs, head_driver)
    timer.done("head headless")

    def unit_dirs(uid):
        s = bundle.slug(uid)
        return root / "units" / s, out / "head" / "units" / s

    # One firmware change usually shows up identically in many units (a reworded warning hits
    # every vector that triggers it); evidence for one of them is evidence for all.
    groups = {}
    for u in head_units:
        b, h = unit_dirs(u["id"])
        lb, lh = screen_lines(load_screens(b)), screen_lines(load_screens(h))
        ok_b = m["units"].get(u["id"], {}).get("headless_ok")
        if u["id"] in m["units"] and (lb != lh or ok_b != head_ok[u["id"]]):
            key = (ok_b, head_ok[u["id"]], *(line for line in difflib.unified_diff(lb, lh, lineterm="", n=0)
                                              if line[:1] in "+-" and line[:3] not in ("+++", "---")))
            groups.setdefault(key, []).append(u["id"])
    same_as = {i: ids[0] for ids in groups.values() for i in ids[1:]}
    changed = {i for ids in groups.values() for i in ids}
    new = set(by_id) - set(m["units"])
    graphical = (changed - set(same_as)) | new | picked
    base_mode = baseline_graphical(root, m, fw_base, base_units, graphical, args.jobs)
    bundle.run_units(head_sim, [by_id[i] for i in sorted(graphical)], out / "head", "graphical", args.jobs, head_driver)
    # Frames can differ while the text does not (layout, fonts), but also by capture timing (a toast
    # caught on one side only), so these are reported apart from text changes.
    frames_only = set()
    for uid in graphical - new - changed:
        b, h = unit_dirs(uid)
        if frame_hashes(b) is not None and frame_hashes(h) is not None and frame_hashes(b) != frame_hashes(h):
            frames_only.add(uid)
    # Base evidence next to the head's, so the report's relative image links work from a CI artifact.
    for uid in changed | graphical:
        b, _ = unit_dirs(uid)
        if b.exists():
            shutil.copytree(b, out / "base" / "units" / b.name, dirs_exist_ok=True)
    timer.done("graphical")

    head_version = sim_version(head_sim)
    expectations = {u["id"]: expect.check(u, load_screens(unit_dirs(u["id"])[1]), head_version)
                    for u in head_units if u.get("expect")}
    api_results = apis.run(head_sim, head_fw / bundle.FW_VECTORS, out / "apis", revs=head_revs)
    timer.done("lane B")

    base_apis = json.loads((root / "apis" / "results.json").read_text()) if (root / "apis" / "results.json").exists() else None
    pr = {
        "repo": repo,
        "base": {"ref": args.base, "sha": base_sha, "firmware": fw_base, "bundle": str(root),
                 "simulator_version": m["simulator_version"],
                 "harness_sha": m.get("harness_sha"),
                 # Bundles recorded before revisions were: the API repos from their results, driver unknown.
                 "revisions": m["revisions"],
                 "vectors": bundle.has_vectors(bundle.firmware_dir(fw_base))},
        "head": {"ref": args.head, "sha": head_sha, "firmware": bundle.resolve(fw_head), "simulator_version": head_version,
                 "source": read_text(head_sim / "source.txt"), "harness_sha": bundle.harness_sha(),
                 "revisions": revisions(api_results, head_revs), "vectors": bundle.has_vectors(head_fw)},
        "run_mode": {
            "namespaces": bundle.namespaces(),
            "graphical_head": bundle.graphical_mode(head_sim) if graphical else None,
            "graphical_base": base_mode,
            "api_suites": "parallel, one network namespace each" if bundle.namespaces()
            else f"one at a time on {bundle.SIM_PORT} (no network namespaces here)",
        },
        "graphical_globs": args.graphical,
        "unreached": [f"{u['file']}:{u['lines'][0]} {u['function']}" for u in cov["unreached"]] if cov else None,
        "unmapped": cov["unmapped"] if cov else None,
        "units": {u["id"]: {
            "slug": bundle.slug(u["id"]), "kind": u["kind"], "path": unit_path(u),
            "status": "new" if u["id"] in new else "changed" if u["id"] in changed
            else "frames-only" if u["id"] in frames_only else "unchanged",
            "base_ok": m["units"].get(u["id"], {}).get("headless_ok"), "head_ok": head_ok[u["id"]],
            "head_error": None if head_ok[u["id"]] else unit_error(unit_dirs(u["id"])[1]),
            "graphical": u["id"] in graphical, "same_as": same_as.get(u["id"]), "expect": expectations.get(u["id"]),
            "outcome": expect.outcome(u, head_version),
        } for u in head_units},
        "removed": sorted(set(m["units"]) - set(by_id)),
        "base_apis": base_apis,
        "apis": api_results,
        "timings": timer.phases,
    }
    pr["verdict"] = verdict(pr["units"], pr["base_apis"], pr["apis"],
                            ("firmware",) if repo == "firmware" else ("committed", "firmware"))
    (out / "pr.json").write_text(json.dumps(pr, indent=2))
    pr_report(out)
    timer.done("report")
    pr["timings"] = timer.phases
    (out / "pr.json").write_text(json.dumps(pr, indent=2))
    log(f"timings: {timer.phases}")
    for r in pr["verdict"]:
        log(f"regression: {r}")
    log("verdict: " + (f"{len(pr['verdict'])} regression(s)" if pr["verdict"] else "no regressions"))
    return 1 if pr["verdict"] else 0


def run_mode_note(rm):
    if not rm:
        return "-"
    g = {k: bundle.GRAPHICAL_MODES.get(rm[k], "none run") for k in ("graphical_head", "graphical_base")}
    return (f"network namespaces {'available' if rm['namespaces'] else 'unavailable'}; headless units in parallel, "
            f"own port each; graphical on head: {g['graphical_head']}, on base: {g['graphical_base']}; "
            f"API suites: {rm['api_suites']}")


def short_sha(s):
    return "-" if not s else s[:9] + ("+dirty" if s.endswith("+dirty") else "")


def ref_label(ref, sha):
    """`my-branch` (`859df2be9`), or the commit alone when the ref is one."""
    name = re.sub(r"^origin/", "", ref or "")
    return f"`{sha[:9]}`" if not name or sha.startswith(name) else f"`{name}` (`{sha[:9]}`)"


VECTOR_MODES = {"firmware": "the firmware's test vectors", "committed": "the test vectors committed in {suite}"}


def api_failure(out, suite, mode, test, r, simulator, units):
    """A newly failing API test as a report item: the test's own message, and the changed vector unit
    it checks, if any."""
    name = test.replace(f"/{simulator}", "") if simulator else test
    item = {"title": f"{suite} simulator test `{name}`",
            "detail": f"Run with {VECTOR_MODES[mode].format(suite=suite)}."}
    log_path = out / "apis" / (r.get("log") or "")
    if r.get("log") and log_path.is_file():
        item["excerpt"] = apis.failure_excerpt(suite, log_path.read_text(errors="replace"), test)
    text = name + "\n" + (item.get("excerpt") or "")
    hit = next((i for i, u in units.items() if u["kind"] == "btc-vector" and u["status"] == "changed"
                and re.search(rf"\b{re.escape(i.split('/', 1)[1])}\b", text)), None)
    if hit:
        item["detail"] += (f" The test compares the device screens with the expected screens of vector "
                           f"`{hit.split('/', 1)[1]}`, and this PR changes them (see `{hit}` below). If the "
                           "change is intended, update the vector's expected screens in the firmware's "
                           "btc-transaction-test-vectors.json in this PR.")
    return item


def pr_findings(out):
    """pr.json and the evidence next to it as report.render's findings."""
    out = Path(out)
    pr = json.loads((out / "pr.json").read_text())
    pr_info = report.load_pr_info(out)
    globs = report.intent(pr_info).get("expect")
    units, repo, base, head = pr["units"], pr["repo"], pr["base"], pr["head"]
    modes = ("firmware",) if repo == "firmware" else ("committed", "firmware")
    head_sim, base_sim = pr["apis"].get("simulator"), (pr["base_apis"] or {}).get("simulator")
    f = {"repo": bundle.REPOS[repo].slug, "pr": pr_info, "intent": report.intent(pr_info),
         "title": (pr_info or {}).get("title") or f"{re.sub(r'^origin/', '', head['ref'])}",
         "sides": {"base": ref_label(base["ref"], base["sha"]) + ", where the PR branched off",
                   "head": ref_label(head["ref"], head["sha"])},
         "broke": [], "changes": [], "minor": [], "known": [], "followups": [], "cautions": [], "untested": []}

    for uid, u in sorted(units.items()):
        if u["base_ok"] and not u["head_ok"]:
            f["broke"].append({"title": f"Scenario `{uid}` no longer completes",
                               "detail": "It completed before this PR.", "excerpt": u.get("head_error")})
        elif rejects(u) and u["base_ok"] is False and u["head_ok"]:
            f["broke"].append({"title": f"Vector `{uid}` is no longer rejected",
                               "detail": f"The device must reject it ({u['outcome'].replace('_', ' ')}). It did "
                                         "before this PR; with it, the device completes it."})
        elif rejects(u) and not u["head_ok"]:
            pass  # rejected as it must be
        elif u["base_ok"] is False and not u["head_ok"]:
            f["known"].append(f"Scenario `{uid}` fails before and after this PR: {u.get('head_error') or 'see its log'}")
    base_suites = (pr["base_apis"] or {}).get("suites", {})
    for suite, by_mode in pr["apis"]["suites"].items():
        for mode, r in by_mode.items():
            gating = mode in modes
            if r.get("error"):
                (f["broke"] if gating else f["known"]).append(
                    {"title": f"{suite} simulator tests did not run ({VECTOR_MODES[mode].format(suite=suite)})",
                     "detail": f"Error: {r['error']}. Log: apis/{r.get('log')}"} if gating
                    else f"{suite} simulator tests did not run with {VECTOR_MODES[mode].format(suite=suite)}: {r['error']}")
            newly = api_newly_failing(pr["base_apis"], pr["apis"], mode).get(suite, [])
            if gating:
                f["broke"] += [api_failure(out, suite, mode, t, r, head_sim, units) for t in newly]
            elif newly:
                f["followups"].append(f"Update the copy of the firmware's test vectors in {suite}: "
                                      f"{plural(len(newly), 'test')} now {'fails' if len(newly) == 1 else 'fail'} with it.")
            b = base_suites.get(suite, {}).get(mode) or {}
            failed_before = {api_test_key(t, base_sim) for t in b.get("failing", [])}
            old = [t for t in r.get("failing", []) if api_test_key(t, head_sim) in failed_before]
            if old:
                f["known"].append(f"{suite} with {VECTOR_MODES[mode].format(suite=suite)}: "
                                  f"{plural(len(old), 'test')} {'fails' if len(old) == 1 else 'fail'} before and after this PR"
                                  + (" (its copy of the vectors is out of date)" if mode == "committed" else ""))

    def unit_dirs(u):
        return Path("base/units") / u["slug"], Path("head/units") / u["slug"]

    stale, stale_changed = [], []
    for uid, u in units.items():
        b, h = unit_dirs(u)
        exp_fail = (u.get("expect") or {}).get("status") == "fail"
        if u["status"] in ("changed", "new") and not u.get("same_as"):
            ids = [uid] + [j for j, v in units.items() if v.get("same_as") == uid]
            pairs = report.screen_changes(load_screens(out / b) if u["status"] == "changed" else [],
                                          load_screens(out / h))
            c = {"title": f"New scenario `{uid}`" if u["status"] == "new" else report.change_title(pairs),
                 "where": ids, "expected": report.is_expected(ids, globs), "screens": pairs, "notes": []}
            if u["graphical"]:
                c["compare"] = graphical_compare(out, b, h, u["slug"])
            if u["base_ok"] != u["head_ok"]:
                c["notes"].append(f"Run result: {'completed' if u['base_ok'] else 'failed'} before, "
                                  f"{'completed' if u['head_ok'] else 'failed'} after.")
            if exp_fail and u["kind"] == "btc-vector":
                c["notes"].append("The firmware's test vectors still expect the old screens for this vector.")
            elif exp_fail:
                stale_changed += [i for i in ids if (units[i].get("expect") or {}).get("status") == "fail"]
            f["changes"].append(c)
        elif u["status"] == "frames-only":
            f["minor"].append({"title": f"`{uid}`", "compare": graphical_compare(out, b, h, u["slug"])})
        elif u["status"] == "unchanged" and exp_fail:
            stale.append(uid)
    if stale_changed:
        f["followups"].append(f"If the screen changes of these {plural(len(stale_changed), 'scenario')} are "
                              "intended, approve them as their new expected screens after the merge: "
                              f"`e2e.py promote <report dir> {' '.join(sorted(stale_changed))}`")
    if stale:
        f["known"].append(f"{plural(len(stale), 'scenario')} differ from their approved screens before and after "
                          "this PR: " + ", ".join(f"`{i}`" for i in sorted(stale)))
    for uid in pr["removed"]:
        f["changes"].append({"title": f"Scenario `{uid}` was removed", "where": [uid],
                             "expected": report.is_expected([uid], globs)})

    if base["harness_sha"] != head["harness_sha"]:
        f["cautions"].append("Before and after ran with different versions of this test tool; some differences "
                             "can come from the tool.")
    base_revs, head_revs = base.get("revisions") or {}, head.get("revisions") or {}
    if diffs := revision_diffs(base_revs, head_revs, None if repo == "firmware" else repo):
        f["cautions"].append(f"Not only {repo} differs: {', '.join(diffs)} ran at another version before than "
                             "after. Their changes can show up here too.")
    for side in ("base", "head"):
        if pr[side].get("vectors") is False:
            f["cautions"].append(f"The firmware {'before' if side == 'base' else 'after'} this PR has no BTC test "
                                 "vectors file: no btc-vector scenarios and no API run with the firmware's vectors.")

    kinds = {}
    for uid in units:
        k = {"btc-vector": "BTC test vector", "eth-matrix": "Ethereum chain case"}.get(uid.split("/")[0], "scenario")
        kinds[k] = kinds.get(k, 0) + 1
    n_graph = sum(u["graphical"] for u in units.values())
    fw = "" if repo == "firmware" else f", firmware `{base['firmware'][:9]}`"
    f["tested"] = [
        f"{plural(len(units), 'device scenario')} ({', '.join(plural(n, k) for k, n in kinds.items())}), "
        f"each run on the simulator before and after this PR{fw}. Their screens are compared as text.",
        (f"{n_graph} of them also as screenshots: the changed ones"
         + (f" and {', '.join(f'`{g}`' for g in pr['graphical_globs'])}" if pr.get("graphical_globs") else "") + ".")
        if n_graph else "No screenshots: no screen changed.",
    ]
    for suite, by_mode in pr["apis"]["suites"].items():
        parts = []
        for mode, r in by_mode.items():
            b = base_suites.get(suite, {}).get(mode)
            total = r["passed"] + r["failed"]
            before = f" (before: {b['passed']} of {b['passed'] + b['failed']})" if b else ""
            parts.append(f"{r['passed']} of {total} pass with {VECTOR_MODES[mode].format(suite=suite)}{before}")
        f["tested"].append(f"The {suite} library's simulator tests: " + "; ".join(parts) + ".")
    if repo == "firmware":
        if pr["unreached"] is None:
            f["tested"].append("Not known which changed code no scenario runs: there is no coverage map for this base.")
        else:
            f["untested"] += [f"`{x}`" for x in pr["unreached"]]
            f["untested"] += [f"`{x}` (C or other code that coverage cannot see)" for x in pr.get("unmapped") or []]

    f["details"] = [(repo, short_sha(base["sha"]), short_sha(head["sha"]))]
    if repo != "firmware":
        f["details"].append(("firmware", short_sha(base["firmware"]), short_sha(head["firmware"])))
    f["details"] += [("simulator", base["simulator_version"], head["simulator_version"]),
                     ("test tool", short_sha(base["harness_sha"]), short_sha(head["harness_sha"]))]
    f["details"] += [(k, short_sha(base_revs.get(k)), short_sha(v)) for k, v in head_revs.items() if k != repo]
    f["notes"] = ["Run mode: " + run_mode_note(pr.get("run_mode")),
                  "Timings: " + ", ".join(f"{k} {v}s" for k, v in pr["timings"].items()),
                  "Data: pr.json; logs: head/units/*/headless, apis/*.log."]
    return f


def graphical_compare(out, b, h, slug):
    """Compare rows for the frames of a unit that differ between base and head."""
    def frames(d):
        p = out / d / "graphical" / "frames.json"
        return [x for x in json.loads(p.read_text()) if x["step"] not in SETUP_STEPS] if p.exists() else []
    rows = frame_rows(frames(b), frames(h), out / b / "graphical", out / h / "graphical", out / "diff" / slug)
    return report.frame_compare(rows, out, f"{b}/graphical", f"{h}/graphical")


def pr_report(out):
    page = report.render(out, pr_findings(out))
    log(f"report: {page}")
    print(page)


def cmd_promote(args):
    out = Path(args.dir)
    pr = json.loads((out / "pr.json").read_text())
    for uid in args.units:
        u = pr["units"].get(uid)
        if u is None:
            sys.exit(f"{uid}: not in {out / 'pr.json'}")
        if not u.get("path"):
            sys.exit(f"{uid}: only scenario units can be promoted; vector expectations live in the firmware repo")
        try:
            path = promote_path(u["path"])
        except ValueError as e:
            sys.exit(f"{uid}: {e}")
        screens = load_screens(out / "head/units" / u["slug"])
        expect.promote(path, screens, pr["head"]["simulator_version"])
        log(f"promoted {uid} for {pr['head']['simulator_version']}")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("device")
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
            b.add_argument("--repo", choices=bundle.REPOS, default="firmware", help="the repo the change is in")
            b.add_argument("--base", help="ref in that repo; the run compares head against its merge-base "
                           "with head (default: the upstream default branch)")
            b.add_argument("--head", required=True, help="ref in that repo, or WORKTREE (firmware only)")
            b.add_argument("--out", required=True)
            b.add_argument("--tests", nargs="+", default=[], metavar="FILE",
                           help="flow app: Playwright test files to run, relative to frontends/web (default: all)")
            b.add_argument("--flows", nargs="+", choices=report.FLOW_NAMES, metavar="FLOW",
                           help="run only these of the change's flows (" + "; ".join(
                               f"{r}: {' '.join(f)}" for r, f in FLOWS.items()) + "; default: all)")
            b.add_argument("--pr-info", metavar="FILE", help="the PR as JSON (`gh pr view --json url,number,"
                           "title,body`, plus repo): the report shows its title and description, and its "
                           "<!-- e2e expect: ... --> globs mark changes as expected")
        b.add_argument("--pin", action="append", default=[], metavar="REPO=REF",
                       help=f"run another repo at this ref on both sides ({', '.join(PIN_REPOS)}); default: "
                       "upstream master for the firmware and the app, Rabby v0.94.15, for the API repos the "
                       "sibling checkout's HEAD in the device flow and the version the app or Rabby ships with in theirs")
        b.add_argument("--graphical", nargs="+", action="extend", default=[], metavar="GLOB",
                       help="unit id globs to also run on the graphical simulator")
        b.add_argument("-j", "--jobs", type=int, default=bundle.JOBS,
                       help=f"units in parallel (default: half the CPUs, {bundle.JOBS})")
    rp = sub.add_parser("report", help="render report.html and summary.md again from a pr --out directory")
    rp.add_argument("dir")
    rp.add_argument("--pr-info", metavar="FILE", help="as for pr")
    fl = sub.add_parser("flows", help="the flows a change in repo runs, as a JSON list (for the CI job matrix)")
    fl.add_argument("repo", choices=FLOWS)
    pm = sub.add_parser("promote")
    pm.add_argument("dir", help="e2e.py pr --out directory")
    pm.add_argument("units", nargs="+")
    wc = sub.add_parser("wc", help="WalletConnect dapp -> app -> device flow, base vs head (wc/flow.py)")
    wc.add_argument("--base", required=True, help="bitbox-wallet-app ref")
    wc.add_argument("--head", required=True, help="bitbox-wallet-app ref")
    wc.add_argument("--firmware", help="firmware ref (or WORKTREE) for the simulator; default: master of the "
                    "remote pointing at BitBoxSwiss/bitbox02-firmware, else origin/master")
    wc.add_argument("--out", required=True)
    rb = sub.add_parser("rabby", help="Rabby extension -> fake BitBoxBridge -> graphical simulator, firmware base vs head (rabby/)")
    rb.add_argument("--base", required=True, help="firmware ref")
    rb.add_argument("--head", required=True, help="firmware ref or WORKTREE")
    rb.add_argument("--out", required=True)
    rb.add_argument("--rabby-ref", default="v0.94.15", help="Rabby tag/commit, built unpacked from a clone")
    args = p.parse_args()
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


def dispatch(args):
    if args.cmd == "wc":
        from wc import flow
        return flow.cmd_wc(args)
    if args.cmd == "rabby":
        from rabby import rabby_e2e
        return rabby_e2e.cmd_rabby(args)
    if args.cmd == "device":
        ensure_driver()
        meta = cmd_device(args)
        return 1 if False in (meta["headless_ok"], meta["graphical_ok"]) else 0
    if args.cmd == "baseline":
        return cmd_baseline(args)
    if args.cmd == "pr":
        return cmd_pr(args)
    if args.cmd == "flows":
        print(json.dumps(FLOWS[args.repo]))
        return 0
    if args.cmd == "report":
        out = Path(args.dir)
        if args.pr_info:
            (out / "pr-info.json").write_text(Path(args.pr_info).read_text())
        render_report(out)
        return 0
    return cmd_promote(args)


if __name__ == "__main__":
    main()
