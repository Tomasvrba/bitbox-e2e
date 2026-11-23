#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pillow>=10", "python-xlib>=0.33"]
# ///
"""BitBox simulator e2e: run scenarios against base/head firmware and report the device diff.

  e2e.py baseline <ref> [--graphical <glob>...] [-j N]       record the bundle for a firmware commit
  e2e.py pr [--repo firmware|app|api-rs|api-go|api-ts|rabby] [--base <ref>] --head <ref> --out <dir>
            [--pin <repo>=<ref>...] [--graphical <glob>...] [--tests <file>...] [--wc] [-j N]
  e2e.py promote <pr-out-dir> <unit-id>...                    head screens become expectations
  e2e.py device  --scenario scenarios/x.json --sim-dir <dir> --out <dir>   one scenario, one simulator
  e2e.py wallet-compare --base <app-ref> --head <app-ref> --firmware <ref> --out <dir> <test>...
  e2e.py wc --base <app-ref> --head <app-ref> --out <dir> [--firmware <ref>]   WalletConnect flow (wc/)
  e2e.py rabby --base <fw-ref> --head <fw-ref> --out <dir> [--rabby-ref v0.94.15]  Rabby extension flow (rabby/)

Simulator runs are in runs.py, report building blocks in report.py, wallet-compare in wallet_app.py.
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
from report import SETUP_STEPS, frames_md, md_to_html, results, screen_lines  # noqa: E402
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


def unreached_note(unreached, unmapped):
    """Summary of changed code with no unit known to run it. Says "none" only when the coverage map
    saw the whole diff: C, protos, token lists and build files have no coverage."""
    if unreached is None:
        return "n/a (no coverage map for the merge-base)"
    s = ", ".join(f"`{f}`" for f in unreached)
    if not unmapped:
        return s or "none"
    return ((s + "; " if s else "no mapped Rust function; ") + f"**{len(unmapped)} changed file(s) outside the "
            "coverage map**, no unit known to reach them: " + ", ".join(f"`{f}`" for f in unmapped))


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
    for mode in modes:
        for suite, by_mode in head_apis["suites"].items():
            if err := by_mode.get(mode, {}).get("error"):
                reasons.append(f"{suite} ({mode} vectors): suite error on head: {err}")
        reasons += [f"{suite} ({mode} vectors): newly failing on head: " + ", ".join(tests)
                    for suite, tests in api_newly_failing(base_apis, head_apis, mode).items()]
    return reasons


def api_newly_failing(base_apis, head_apis, mode):
    """{suite: tests} that passed on base and fail on head, for one vector mode."""
    base_suites = (base_apis or {}).get("suites", {})
    bv, hv = (base_apis or {}).get("simulator"), head_apis.get("simulator")
    out = {}
    for suite, modes in head_apis["suites"].items():
        r, b = modes.get(mode) or {}, base_suites.get(suite, {}).get(mode)
        if b is None:
            continue  # not run on base (new suite, or no firmware vectors at the merge-base)
        # Bundles recorded before results listed passing tests only have the failing ones.
        if "passing" in b:
            was = {api_test_key(t, bv) for t in b["passing"]}
            newly = [t for t in r.get("failing", []) if api_test_key(t, hv) in was]
        else:
            was = {api_test_key(t, bv) for t in b.get("failing", [])}
            newly = [t for t in r.get("failing", []) if api_test_key(t, hv) not in was]
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
    # An absolute path (pr.json from before paths were relative) survives the join unchanged.
    full = (E2E_DIR / p).resolve()
    if not full.is_relative_to(scenarios) or full.suffix != ".json":
        raise ValueError(f"{p}: not a scenario file under {scenarios}")
    return full


# Repos that --pin can fix for both sides; the app and Rabby are only ever the change under test.
PIN_REPOS = ("firmware", "api-rs", "api-go", "api-ts")


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
    return {k: v for k, v in pins.items() if k != "firmware"}


def cmd_pr(args):
    """One change, base vs head, in the repo given by --repo; the other repos at their pins."""
    args.base = args.base or bundle.default_ref(args.repo)
    pins = parse_pins(args.pin, args.repo)
    if args.repo in ("app", "rabby") and set(pins) - {"firmware"}:
        sys.exit(f"--pin: only the firmware is used in a {args.repo} change")
    if args.repo == "app":
        return wallet_app.cmd_app_change(args, pins.get("firmware"))
    if args.repo == "rabby":
        from rabby import rabby_e2e
        return rabby_e2e.cmd_rabby_change(args, pins.get("firmware"))
    return cmd_device_change(args, pins)


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
    # Base evidence next to the head's, so report.md's relative links work from a CI artifact.
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
                 "revisions": m.get("revisions") or (revisions(base_apis) | {"driver bitbox-api-rs": None}
                                                     if base_apis else {}),
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


def api_cell(r):
    if r is None:
        return "-"
    if r.get("error") and not r.get("failing"):
        return f"**ERROR** ({r['error']})"
    s = f"{r['passed']} pass"
    return s + (f" / **{r['failed']} FAIL**" if r["failed"] else "")


def run_mode_note(rm):
    if not rm:
        return "-"
    g = {k: bundle.GRAPHICAL_MODES.get(rm[k], "none run") for k in ("graphical_head", "graphical_base")}
    return (f"network namespaces {'available' if rm['namespaces'] else 'unavailable'}; headless units in parallel, "
            f"own port each; graphical on head: {g['graphical_head']}, on base: {g['graphical_base']}; "
            f"API suites: {rm['api_suites']}")


def short_sha(s):
    return "-" if not s else s[:9] + ("+dirty" if s.endswith("+dirty") else "")


def pr_report(out):
    out = Path(out)
    pr = json.loads((out / "pr.json").read_text())
    units = pr["units"]
    by = {k: [i for i, u in units.items() if u["status"] == k] for k in ("changed", "new", "frames-only", "unchanged")}
    failed = [i for i, u in units.items() if not u["head_ok"]]
    newly = [i for i in failed if units[i]["base_ok"]]
    exp = {i: u["expect"] for i, u in units.items() if u["expect"]}
    exp_n = {k: sum(e["status"] == k for e in exp.values()) for k in ("pass", "fail", "no-expectation")}
    base, head = pr["base"], pr["head"]
    repo = pr.get("repo", "firmware")  # pr.json from before --repo: firmware

    md = [f"# BitBox e2e: {repo} `{base['ref']}` -> `{head['ref']}`", "",
          "| | base (merge-base) | head |", "|---|---|---|",
          f"| {repo} | `{base['sha'][:9]}` | `{head['ref']}` (`{head['sha'][:9]}`) |",
          *([f"| firmware | `{base['firmware'][:9]}` | `{head['firmware'][:9]}` |"] if repo != "firmware" else []),
          f"| simulator | {base['simulator_version']} | {head['simulator_version']} |",
          f"| harness | `{short_sha(base['harness_sha'])}` | `{short_sha(head['harness_sha'])}` |"]
    base_revs, head_revs = base.get("revisions") or {}, head.get("revisions") or {}
    md += [f"| {k} | `{short_sha(base_revs.get(k))}` | `{short_sha(v)}` |" for k, v in head_revs.items() if k != repo]
    md.append("")
    if base["harness_sha"] != head["harness_sha"]:
        md += ["*The baseline was recorded with another harness revision; harness changes can show up as diffs.*", ""]
    if diffs := revision_diffs(base_revs, head_revs, None if repo == "firmware" else repo):
        md += [f"*Not only the {repo} differs: {', '.join(diffs)} ran at another revision on base than on head; "
               "their changes can show up as diffs or API test failures (not gating by themselves).*", ""]
    md += ["## Verdict", ""]
    if pr["verdict"]:
        md += [f"**REGRESSION**: {len(pr['verdict'])} reason(s), the run exits 1.", "",
               *[f"- {r}" for r in pr["verdict"]], ""]
    else:
        md += ["**No regressions**: every unit and API test that passed on the merge-base passes on head.", ""]
    drift = api_newly_failing(pr["base_apis"], pr["apis"], "committed") if repo == "firmware" else {}
    md += [f"Not gating, for review: {len(by['changed'])} changed and {len(by['new'])} new unit(s), "
           f"{exp_n['fail']} expectation mismatch(es) (promote after the firmware PR merges)"
           + (f", {sum(map(len, drift.values()))} API test(s) newly failing with the API repos' committed "
              f"vectors ({', '.join(drift)}: sync their vector copies after the merge)" if drift else "") + ".", ""]
    md += ["## Summary", "",
           f"- **Units**: {len(units)} run headless, {len(by['changed'])} changed "
           f"({sum(not units[i].get('same_as') for i in by['changed'])} distinct), "
           f"{len(by['new'])} new, {len(by['frames-only'])} with graphical-only differences, "
           f"{len(failed)} failed on head ({len(newly)} newly)"
           + (f", {len(pr['removed'])} removed" if pr["removed"] else ""),
           f"- **Expectations** (not gating): {exp_n['pass']} pass, {exp_n['fail']} fail, {exp_n['no-expectation']} without "
           f"expectation for this version, {len(units) - len(exp)} units without expectations",
           "- **Unreached changed functions**: " + (unreached_note(pr["unreached"], pr.get("unmapped"))
                                                     if repo == "firmware" else "n/a (coverage maps firmware code only)"),
           *[f"- **No BTC vectors file at {side}** (`{pr[side].get('firmware', pr[side]['sha'])[:9]}`, before firmware 3bdaf583b): btc-vector "
             "units unavailable there, the API suites ran with their committed vectors only"
             for side in ("base", "head") if pr[side].get("vectors") is False],
           "- **Run mode**: " + run_mode_note(pr.get("run_mode")),
           "- **Timings**: " + ", ".join(f"{k} {v}s" for k, v in pr["timings"].items()), ""]

    suites = pr["apis"]["suites"]
    bs = (pr["base_apis"] or {}).get("suites", {})
    md += ["### API simulator suites", "",
           "Committed vectors: the repo's own copy (drift vs. firmware). Firmware vectors: the head firmware's "
           "vectors copied in (regression).", "",
           "| suite | head: committed vectors | head: firmware vectors | base: committed | base: firmware |",
           "|---|---|---|---|---|"]
    for s, modes in suites.items():
        md.append(f"| {s} | {api_cell(modes.get('committed'))} | {api_cell(modes.get('firmware'))} | "
                  f"{api_cell(bs.get(s, {}).get('committed'))} | {api_cell(bs.get(s, {}).get('firmware'))} |")
    md.append("")
    for s, modes in suites.items():
        for mode, r in modes.items():
            if r.get("failing"):
                md += [f"<details><summary>{s} ({mode} vectors): {len(r['failing'])} failing, log apis/{r['log']}"
                       "</summary>", "", *[f"- `{n}`" for n in r["failing"]], "", "</details>", ""]

    if exp_n["fail"]:
        md += ["## Expectation failures", ""]
        for i, e in exp.items():
            if e["status"] == "fail":
                md += [f"### {i}", "", "```diff", *e["diff"].splitlines(), "```", ""]

    titles = {"changed": "Changed units", "new": "New units",
              "frames-only": "Graphical-only differences (same text; often capture timing, e.g. a toast)"}
    for kind in titles:
        if by[kind]:
            md += [f"## {titles[kind]}", ""]
        for i in by[kind]:
            u = units[i]
            if u.get("same_as"):
                continue
            b, h = Path("base/units") / u["slug"], Path("head/units") / u["slug"]
            md += [f"### {i}", ""]
            same = [j for j, v in units.items() if v.get("same_as") == i]
            if same:
                md += [f"Same screen change in {len(same)} more unit(s): " + ", ".join(f"`{j}`" for j in same), ""]
            if not u["head_ok"] or u["base_ok"] is False:
                rb, rh = results(out / b), results(out / h)
                md += ["| step | base | head |", "|---|---|---|"]
                md += [f"| `{st}` | {rb.get(st, '-')} | {rh.get(st, '-')} |" for st in dict.fromkeys([*rb, *rh])]
                md.append("")
            sb, sh = screen_lines(load_screens(out / b)), screen_lines(load_screens(out / h))
            diff = list(difflib.unified_diff(sb, sh, "base", "head", lineterm="", n=1))
            md += ["Headless screens:", ""]
            md += ["```diff", *diff, "```", ""] if diff else [f"No change ({len(sb)} screens).", ""]
            if u["graphical"]:
                md += ["Graphical frames:", ""] + frames_md(out, str(b / "graphical"), str(h / "graphical"),
                                                            str(Path("diff") / u["slug"]))

    if by["unchanged"]:
        md += ["## Unchanged units", "", f"<details><summary>{len(by['unchanged'])} units with identical screens"
               "</summary>", ""]
        for i in by["unchanged"]:
            u = units[i]
            n = len(screen_lines(load_screens(out / "head/units" / u["slug"])))
            md.append(f"- `{i}`: {n} screens" + ("" if u["head_ok"] else ", **failed on base and head**"))
        md += ["", "</details>", ""]

    (out / "report.md").write_text("\n".join(md))
    (out / "report.html").write_text(md_to_html(out, md))
    log(f"report: {out / 'report.html'}")
    print(out / "report.html")


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
                           help="app change: Playwright test files to run, relative to frontends/web (default: all)")
            b.add_argument("--wc", action="store_true", help="app change: also the WalletConnect flow")
        b.add_argument("--pin", action="append", default=[], metavar="REPO=REF",
                       help=f"run another repo at this ref on both sides ({', '.join(PIN_REPOS)}); "
                       "default: the sibling checkout's HEAD for the API repos, upstream master for the firmware")
        b.add_argument("--graphical", nargs="+", action="extend", default=[], metavar="GLOB",
                       help="unit id globs to also run on the graphical simulator")
        b.add_argument("-j", "--jobs", type=int, default=bundle.JOBS,
                       help=f"units in parallel (default: half the CPUs, {bundle.JOBS})")
    pm = sub.add_parser("promote")
    pm.add_argument("dir", help="e2e.py pr --out directory")
    pm.add_argument("units", nargs="+")
    w = sub.add_parser("wallet-compare")
    w.add_argument("--base", required=True, help="bitbox-wallet-app ref")
    w.add_argument("--head", required=True, help="bitbox-wallet-app ref")
    w.add_argument("--firmware", required=True, help="firmware ref (or WORKTREE) for the simulator")
    w.add_argument("--out", required=True)
    w.add_argument("tests", nargs="+", help="Playwright test files, relative to frontends/web")
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
    if args.cmd == "wallet-compare":
        return wallet_app.cmd_wallet_compare(args)
    if args.cmd == "device":
        ensure_driver()
        meta = cmd_device(args)
        return 1 if False in (meta["headless_ok"], meta["graphical_ok"]) else 0
    if args.cmd == "baseline":
        return cmd_baseline(args)
    if args.cmd == "pr":
        return cmd_pr(args)
    return cmd_promote(args)


if __name__ == "__main__":
    main()
