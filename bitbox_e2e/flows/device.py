"""Device flow: every scenario unit + BTC test vectors + host API simulator suites, base vs head."""

from __future__ import annotations

import difflib
import fnmatch
import json
import re
import shutil
from pathlib import Path

from bitbox_e2e import apis, config, report, runs
from bitbox_e2e import units as units_mod
from bitbox_e2e.config import build_sim, log, read_text, sim_version
from bitbox_e2e.report import (
    SETUP_STEPS,
    CompareRow,
    FindingItem,
    Findings,
    ScreenChange,
    frame_rows,
    plural,
    results,
    screen_lines,
)

VECTOR_MODES = {
    "firmware": "the firmware's test vectors",
    "committed": "the test vectors committed in {suite}",
}


def matching(unit_list: list[dict], globs: list[str] | None) -> set[str]:
    return {u["id"] for u in unit_list if any(fnmatch.fnmatchcase(u["id"], g) for g in globs or ())}


def load_screens(unit_dir: Path) -> list[dict]:
    p = unit_dir / "headless" / "screens.json"
    return json.loads(p.read_text()) if p.exists() else []


def frame_hashes(unit_dir: Path) -> list[str] | None:
    p = unit_dir / "graphical" / "frames.json"
    return [f["hash"] for f in json.loads(p.read_text()) if f["step"] not in SETUP_STEPS] if p.exists() else None


def rejects(u: dict) -> bool:
    return u.get("outcome") not in (None, "success")


def api_test_key(name: str, simulator: str | None) -> str:
    """API test name without the simulator version: api-go and api-ts put it into test names, so a
    PR that bumps the version would otherwise rename every test and hide its regressions."""
    m = re.search(r"\d+\.\d+\.\d+", simulator or "")
    return name.replace(simulator, "<simulator>").replace(m[0], "<version>") if m else name


def api_newly_failing(base_apis: dict | None, head_apis: dict, mode: str) -> dict[str, list[str]]:
    """{suite: tests} that passed on base and fail on head, for one vector mode."""
    base_suites = (base_apis or {}).get("suites", {})
    bv, hv = (base_apis or {}).get("simulator"), head_apis.get("simulator")
    out: dict[str, list[str]] = {}
    for suite, modes in head_apis["suites"].items():
        r, b = modes.get(mode) or {}, base_suites.get(suite, {}).get(mode)
        if b is None:
            continue  # not run on base (new suite, or no firmware vectors at the merge-base)
        was = {api_test_key(t, bv) for t in b["passing"]}
        newly = [t for t in r.get("failing", []) if api_test_key(t, hv) in was]
        if newly:
            out[suite] = newly
    return out


def verdict(units: dict, base_apis: dict | None, head_apis: dict, modes=("firmware",)) -> list[str]:
    """Why the PR fails the gate, empty if it passes. Takes pr.json's units, base_apis and apis.
    Only regressions against the merge-base count: changed screens are evidence for the reviewer.

    modes: the API vector modes that gate. For a firmware change only the run with head's own
    vectors: the API repos' committed copies assert the screens of the firmware they were synced
    from, so they fail on any PR that changes one (drift). For an API change both: the firmware is
    the same on both sides, so any newly failing test is the change's."""
    reasons = [
        f"{uid}: completed on base, fails on head" + (f" ({u['head_error']})" if u.get("head_error") else "")
        for uid, u in sorted(units.items())
        if u["base_ok"] and not u["head_ok"]
    ]
    # A vector the device must reject (outcome invalid_input, unsupported) "fails" by design; one
    # that was rejected on base and completes on head is the regression.
    reasons += [
        f"{uid}: must be rejected ({u['outcome']}), was rejected on base, completes on head"
        for uid, u in sorted(units.items())
        if rejects(u) and u["base_ok"] is False and u["head_ok"]
    ]
    for mode in modes:
        for suite, by_mode in head_apis["suites"].items():
            if err := by_mode.get(mode, {}).get("error"):
                reasons.append(f"{suite} ({mode} vectors): suite error on head: {err}")
        reasons += [
            f"{suite} ({mode} vectors): newly failing on head: " + ", ".join(tests)
            for suite, tests in api_newly_failing(base_apis, head_apis, mode).items()
        ]
    return reasons


def unit_error(unit_dir: Path) -> str | None:
    """Short reason a headless run failed: the runner's (timeout, crash) or the first failing step."""
    meta = unit_dir / "meta.json"
    err = json.loads(meta.read_text()).get("headless_error") if meta.exists() else None
    return err or next((r.splitlines()[0][:160] for r in results(unit_dir).values() if r != "ok"), None)


def run_mode_note(rm: dict | None) -> str:
    if not rm:
        return "-"
    g = {k: config.GRAPHICAL_MODES.get(rm[k], "none run") for k in ("graphical_head", "graphical_base")}
    return (
        f"network namespaces {'available' if rm['namespaces'] else 'unavailable'}; headless units in parallel, "
        f"own port each; graphical on head: {g['graphical_head']}, on base: {g['graphical_base']}; "
        f"API suites: {rm['api_suites']}"
    )


def short_sha(s: str | None) -> str:
    return "-" if not s else s[:9] + ("+dirty" if s.endswith("+dirty") else "")


def ref_label(ref: str | None, sha: str) -> str:
    """`my-branch` (`859df2be9`), or the commit alone when the ref is one."""
    name = re.sub(r"^origin/", "", ref or "")
    return f"`{sha[:9]}`" if not name or sha.startswith(name) else f"`{name}` (`{sha[:9]}`)"


def api_failure(
    out: Path,
    suite: str,
    mode: str,
    test: str,
    r: dict,
    simulator: str | None,
    units: dict,
) -> FindingItem:
    """A newly failing API test as a report item: the test's own message, and the changed vector unit
    it checks, if any."""
    name = test.replace(f"/{simulator}", "") if simulator else test
    detail = f"Run with {VECTOR_MODES[mode].format(suite=suite)}."
    excerpt = None
    log_path = out / "apis" / (r.get("log") or "")
    if r.get("log") and log_path.is_file():
        excerpt = apis.failure_excerpt(suite, log_path.read_text(errors="replace"), test)
    text = name + "\n" + (excerpt or "")
    hit = next(
        (
            i
            for i, u in units.items()
            if u["kind"] == "btc-vector"
            and u["status"] == "changed"
            and re.search(rf"\b{re.escape(i.split('/', 1)[1])}\b", text)
        ),
        None,
    )
    if hit:
        detail += (
            f" The test compares the device screens with the expected screens of vector "
            f"`{hit.split('/', 1)[1]}`, and this PR changes them (see `{hit}` below). If the "
            "change is intended, update the vector's expected screens in the firmware's "
            "btc-transaction-test-vectors.json in this PR."
        )
    return FindingItem(title=f"{suite} simulator test `{name}`", detail=detail, excerpt=excerpt)


def graphical_compare(out: Path, b: Path, h: Path, unit_slug: str) -> list[CompareRow]:
    """Compare rows for the frames of a unit that differ between base and head."""

    def frames(d: Path):
        p = out / d / "graphical" / "frames.json"
        return [x for x in json.loads(p.read_text()) if x["step"] not in SETUP_STEPS] if p.exists() else []

    rows = frame_rows(frames(b), frames(h), out / b / "graphical", out / h / "graphical", out / "diff" / unit_slug)
    return report.frame_compare(rows, out, f"{b}/graphical", f"{h}/graphical")


def pr_findings(out: Path) -> Findings:
    """pr.json and the evidence next to it as report.Findings."""
    out = Path(out)
    pr = json.loads((out / "pr.json").read_text())
    units, repo, base, head = pr["units"], pr["repo"], pr["base"], pr["head"]
    modes = ("firmware",) if repo == "firmware" else ("committed", "firmware")
    head_sim, base_sim = pr["apis"].get("simulator"), (pr["base_apis"] or {}).get("simulator")

    kinds: dict[str, int] = {}
    for uid in units:
        k = {"btc-vector": "BTC test vector", "eth-matrix": "Ethereum chain case"}.get(uid.split("/")[0], "scenario")
        kinds[k] = kinds.get(k, 0) + 1

    f = Findings(
        title=re.sub(r"^origin/", "", head["ref"]),
        repo=config.REPOS[repo].slug,
        sides={
            "base": ref_label(base["ref"], base["sha"]) + ", where the PR branched off",
            "head": ref_label(head["ref"], head["sha"]),
        },
        scope=f"{plural(len(units), 'device scenario')} and {plural(len(pr['apis']['suites']), 'API suite')}",
    )

    for uid, u in sorted(units.items()):
        if u["base_ok"] and not u["head_ok"]:
            f.broke.append(
                FindingItem(
                    title=f"Scenario `{uid}` no longer completes",
                    detail="It completed before this PR.",
                    excerpt=u.get("head_error"),
                )
            )
        elif rejects(u) and u["base_ok"] is False and u["head_ok"]:
            f.broke.append(
                FindingItem(
                    title=f"Vector `{uid}` is no longer rejected",
                    detail=(
                        f"The device must reject it ({u['outcome'].replace('_', ' ')}). It did "
                        "before this PR; with it, the device completes it."
                    ),
                )
            )
        elif rejects(u) and not u["head_ok"]:
            pass  # rejected as it must be
        elif u["base_ok"] is False and not u["head_ok"]:
            f.known.append(f"Scenario `{uid}` fails before and after this PR: {u.get('head_error') or 'see its log'}")

    base_suites = (pr["base_apis"] or {}).get("suites", {})
    for suite, by_mode in pr["apis"]["suites"].items():
        for mode, r in by_mode.items():
            gating = mode in modes
            if r.get("error"):
                if gating:
                    f.broke.append(
                        FindingItem(
                            title=f"{suite} simulator tests did not run ({VECTOR_MODES[mode].format(suite=suite)})",
                            detail=f"Error: {r['error']}. Log: apis/{r.get('log')}",
                        )
                    )
                else:
                    mode_desc = VECTOR_MODES[mode].format(suite=suite)
                    f.known.append(f"{suite} simulator tests did not run with {mode_desc}: {r['error']}")
            newly = api_newly_failing(pr["base_apis"], pr["apis"], mode).get(suite, [])
            if gating:
                f.broke.extend(api_failure(out, suite, mode, t, r, head_sim, units) for t in newly)
            elif newly:
                f.followups.append(
                    f"Update the copy of the firmware's test vectors in {suite}: "
                    f"{plural(len(newly), 'test')} now {'fails' if len(newly) == 1 else 'fail'} with it."
                )
            b = base_suites.get(suite, {}).get(mode) or {}
            failed_before = {api_test_key(t, base_sim) for t in b.get("failing", [])}
            old = [t for t in r.get("failing", []) if api_test_key(t, head_sim) in failed_before]
            if old:
                f.known.append(
                    f"{suite} with {VECTOR_MODES[mode].format(suite=suite)}: "
                    f"{plural(len(old), 'test')} {'fails' if len(old) == 1 else 'fail'} before and after this PR"
                    + (" (its copy of the vectors is out of date)" if mode == "committed" else "")
                )

    def unit_dirs(u):
        return Path("base/units") / u["slug"], Path("head/units") / u["slug"]

    for uid, u in units.items():
        b, h = unit_dirs(u)
        if u["status"] in ("changed", "new") and not u.get("same_as"):
            ids = [uid] + [j for j, v in units.items() if v.get("same_as") == uid]
            pairs = report.screen_changes(
                load_screens(out / b) if u["status"] == "changed" else [],
                load_screens(out / h),
            )
            notes = []
            if u["base_ok"] != u["head_ok"]:
                notes.append(
                    f"Run result: {'completed' if u['base_ok'] else 'failed'} before, "
                    f"{'completed' if u['head_ok'] else 'failed'} after."
                )
            f.changes.append(
                ScreenChange(
                    title=f"New scenario `{uid}`" if u["status"] == "new" else report.change_title(pairs),
                    where=ids,
                    screens=pairs,
                    compare=graphical_compare(out, b, h, u["slug"]) if u["graphical"] else [],
                    notes=notes,
                )
            )
        elif u["status"] == "frames-only":
            f.minor.append(
                ScreenChange(title=f"`{uid}`", compare=graphical_compare(out, b, h, u["slug"]))
            )
        if u["head_ok"] and u["graphical"]:
            frames_path = out / h / "graphical" / "frames.json"
            if frames_path.exists():
                frames = [x for x in json.loads(frames_path.read_text()) if x["step"] not in SETUP_STEPS]
                if frames:
                    f.proof.append(
                        ScreenChange(
                            title=f"`{uid}`",
                            where=[uid],
                            compare=[
                                CompareRow(
                                    label=f"{fr['step']} #{idx + 1}",
                                    after=f"{h}/graphical/{fr['file']}",
                                )
                                for idx, fr in enumerate(frames)
                            ],
                        )
                    )

    for uid in pr["removed"]:
        f.changes.append(ScreenChange(title=f"Scenario `{uid}` was removed", where=[uid]))

    if base["harness_sha"] != head["harness_sha"]:
        f.cautions.append(
            "Before and after ran with different versions of this test tool; some differences "
            "can come from the tool."
        )
    base_revs, head_revs = base.get("revisions") or {}, head.get("revisions") or {}
    if diffs := runs.revision_diffs(base_revs, head_revs, None if repo == "firmware" else repo):
        f.cautions.append(
            f"Not only {repo} differs: {', '.join(diffs)} ran at another version before than "
            "after. Their changes can show up here too."
        )
    for side in ("base", "head"):
        if pr[side].get("vectors") is False:
            f.cautions.append(
                f"The firmware {'before' if side == 'base' else 'after'} this PR has no BTC test "
                "vectors file: no btc-vector scenarios and no API run with the firmware's vectors."
            )

    n_graph = sum(u["graphical"] for u in units.values())
    fw = "" if repo == "firmware" else f", firmware `{base['firmware'][:9]}`"
    f.tested = [
        f"{plural(len(units), 'device scenario')} ({', '.join(plural(n, k) for k, n in kinds.items())}), "
        f"each run on the simulator before and after this PR{fw}. Their screens are compared as text.",
        (
            f"{n_graph} of them also as screenshots: the changed ones"
            + (f" and {', '.join(f'`{g}`' for g in pr['graphical_globs'])}" if pr.get("graphical_globs") else "")
            + "."
        )
        if n_graph
        else "No screenshots: no screen changed.",
    ]
    for suite, by_mode in pr["apis"]["suites"].items():
        parts = []
        for mode, r in by_mode.items():
            b = base_suites.get(suite, {}).get(mode)
            total = r["passed"] + r["failed"]
            before = f" (before: {b['passed']} of {b['passed'] + b['failed']})" if b else ""
            parts.append(f"{r['passed']} of {total} pass with {VECTOR_MODES[mode].format(suite=suite)}{before}")
        f.tested.append(f"The {suite} library's simulator tests: " + "; ".join(parts) + ".")

    f.details = [(repo, short_sha(base["sha"]), short_sha(head["sha"]))]
    if repo != "firmware":
        f.details.append(("firmware", short_sha(base["firmware"]), short_sha(head["firmware"])))
    f.details += [
        ("simulator", base["simulator_version"], head["simulator_version"]),
        ("test tool", short_sha(base["harness_sha"]), short_sha(head["harness_sha"])),
    ]
    f.details += [(k, short_sha(base_revs.get(k)), short_sha(v)) for k, v in head_revs.items() if k != repo]
    f.notes = [
        "Run mode: " + run_mode_note(pr.get("run_mode")),
        "Timings: " + ", ".join(f"{k} {v}s" for k, v in pr["timings"].items()),
        "Data: pr.json; logs: head/units/*/headless, apis/*.log.",
    ]
    return f


def render(flow_dir: Path) -> Path:
    page = report.render_flow(Path(flow_dir), pr_findings(flow_dir))
    log(f"report: {page}")
    print(page)
    return page


pr_report = render


def run(
    repo: str,
    base: str,
    head: str,
    pins: dict[str, str],
    graphical: list[str],
    jobs: int,
    out: Path,
) -> int:
    """A firmware or API change: every unit and the API suites, head against the merge-base."""
    timer = runs.Timer()
    out = Path(out)
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    head_sha = config.resolve(head, repo)
    base_sha = config.git("merge-base", config.resolve(base, repo), head_sha, cwd=config.REPOS[repo].dir)
    revs = {k: v for k, v in pins.items() if k in config.API_REPOS}
    if repo == "firmware":
        fw_base, fw_head, base_revs, head_revs = base_sha, head, revs, revs
    else:
        fw_base = fw_head = pins.get("firmware") or config.resolve(config.default_firmware_ref())
        base_revs, head_revs = revs | {repo: base_sha}, revs | {repo: head_sha}
    root, m, base_units = runs.ensure_baseline(fw_base, jobs, timer, revs=base_revs)
    timer.done("baseline")

    head_sim = build_sim(fw_head)
    head_fw = config.firmware_dir(fw_head)
    head_units = units_mod.load_units(head_fw)
    by_id = {u["id"]: u for u in head_units}
    head_driver = runs.driver_for(head_revs.get("api-rs"))
    timer.done("head build")

    picked = matching(head_units, graphical)
    head_ok = runs.run_units(head_sim, head_units, out / "head", "headless", jobs, head_driver)
    timer.done("head headless")

    def unit_dirs(uid: str):
        s = config.slug(uid)
        return root / "units" / s, out / "head" / "units" / s

    # One firmware change usually shows up identically in many units (a reworded warning hits
    # every vector that triggers it); evidence for one of them is evidence for all.
    groups: dict[tuple, list[str]] = {}
    for u in head_units:
        b, h = unit_dirs(u["id"])
        lb, lh = screen_lines(load_screens(b)), screen_lines(load_screens(h))
        ok_b = m["units"].get(u["id"], {}).get("headless_ok")
        if u["id"] in m["units"] and (lb != lh or ok_b != head_ok[u["id"]]):
            key = (
                ok_b,
                head_ok[u["id"]],
                *(
                    line
                    for line in difflib.unified_diff(lb, lh, lineterm="", n=0)
                    if line[:1] in "+-" and line[:3] not in ("+++", "---")
                ),
            )
            groups.setdefault(key, []).append(u["id"])
    same_as = {i: ids[0] for ids in groups.values() for i in ids[1:]}
    changed = {i for ids in groups.values() for i in ids}
    new = set(by_id) - set(m["units"])
    to_graph = (changed - set(same_as)) | new | picked
    base_mode = runs.baseline_graphical(root, m, fw_base, base_units, to_graph, jobs)
    runs.run_units(head_sim, [by_id[i] for i in sorted(to_graph)], out / "head", "graphical", jobs, head_driver)

    # Frames can differ while the text does not (layout, fonts), but also by capture timing (a toast
    # caught on one side only), so these are reported apart from text changes.
    frames_only = set()
    for uid in to_graph - new - changed:
        b, h = unit_dirs(uid)
        if frame_hashes(b) is not None and frame_hashes(h) is not None and frame_hashes(b) != frame_hashes(h):
            frames_only.add(uid)
    # Base evidence next to the head's, so the report's relative image links work from a CI artifact.
    for uid in changed | to_graph:
        b, _ = unit_dirs(uid)
        if b.exists():
            shutil.copytree(b, out / "base" / "units" / b.name, dirs_exist_ok=True)
    timer.done("graphical")

    head_version = sim_version(head_sim)
    api_results = apis.run(head_sim, head_fw / config.FW_VECTORS, out / "apis", revs=head_revs)
    timer.done("lane B")

    base_apis_path = root / "apis" / "results.json"
    base_apis = json.loads(base_apis_path.read_text()) if base_apis_path.exists() else None
    pr = {
        "repo": repo,
        "base": {
            "ref": base,
            "sha": base_sha,
            "firmware": fw_base,
            "bundle": str(root),
            "simulator_version": m["simulator_version"],
            "harness_sha": m.get("harness_sha"),
            "revisions": m["revisions"],
            "vectors": config.has_vectors(config.firmware_dir(fw_base)),
        },
        "head": {
            "ref": head,
            "sha": head_sha,
            "firmware": config.resolve(fw_head),
            "simulator_version": head_version,
            "source": read_text(head_sim / "source.txt"),
            "harness_sha": config.harness_sha(),
            "revisions": runs.revisions(api_results, head_revs),
            "vectors": config.has_vectors(head_fw),
        },
        "run_mode": {
            "namespaces": config.namespaces(),
            "graphical_head": config.graphical_mode(head_sim) if to_graph else None,
            "graphical_base": base_mode,
            "api_suites": "parallel, one network namespace each"
            if config.namespaces()
            else f"one at a time on {config.SIM_PORT} (no network namespaces here)",
        },
        "graphical_globs": graphical,
        "units": {
            u["id"]: {
                "slug": config.slug(u["id"]),
                "kind": u["kind"],
                "status": "new"
                if u["id"] in new
                else "changed"
                if u["id"] in changed
                else "frames-only"
                if u["id"] in frames_only
                else "unchanged",
                "base_ok": m["units"].get(u["id"], {}).get("headless_ok"),
                "head_ok": head_ok[u["id"]],
                "head_error": None if head_ok[u["id"]] else unit_error(unit_dirs(u["id"])[1]),
                "graphical": u["id"] in to_graph,
                "same_as": same_as.get(u["id"]),
                "outcome": units_mod.vector_outcome(u, head_version),
            }
            for u in head_units
        },
        "removed": sorted(set(m["units"]) - set(by_id)),
        "base_apis": base_apis,
        "apis": api_results,
        "timings": timer.phases,
    }
    pr["verdict"] = verdict(
        pr["units"],
        pr["base_apis"],
        pr["apis"],
        ("firmware",) if repo == "firmware" else ("committed", "firmware"),
    )
    (out / "pr.json").write_text(json.dumps(pr, indent=2))
    render(out)
    timer.done("report")
    pr["timings"] = timer.phases
    (out / "pr.json").write_text(json.dumps(pr, indent=2))
    log(f"timings: {timer.phases}")
    for r in pr["verdict"]:
        log(f"regression: {r}")
    log("verdict: " + (f"{len(pr['verdict'])} regression(s)" if pr["verdict"] else "no regressions"))
    return 1 if pr["verdict"] else 0


cmd_device_change = run
