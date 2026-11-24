"""The app's Playwright tests before and after a change to the app, the firmware or bitbox02-api-go
(`e2e.py pr`, flow "app")."""

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

import bundle
from bundle import E2E_DIR, log
import report
from report import strip_ansi
from runs import ScreenParser

WALLET_REPO = bundle.REPOS["app"].dir
# What the app's own CI installs for its tests (tests/util/aopp/server.py), on python3's PATH.
TEST_PYTHON_DEPS = ("ecdsa==0.19.1", "bech32==1.2.0")
# Volatile device screens in app tests: random pairing codes and the current time.
VOLATILE_TITLES = {"Pairing code", "Is now?", "Is today?"}


def wallet_clone(ref, api_go=None):
    """Clean clone of the app at ref, building against bitbox02-api-go at api_go instead of the version
    it vendors if given. Kept between runs (keyed by both) to reuse node_modules."""
    sha = subprocess.run(["git", "-C", str(WALLET_REPO), "rev-parse", "--verify", f"{ref}^{{commit}}"],
                         stdout=subprocess.PIPE, text=True, check=True).stdout.strip()
    go_sha, go_src = bundle.clone_at("api-go", api_go) if api_go else (None, None)
    src = bundle.CACHE / "src" / (f"bitbox-wallet-app-{sha}" + (f"-api-go-{go_sha}" if go_sha else ""))
    if not (src / ".git").exists():
        subprocess.run(["git", "clone", "-q", "--shared", "--no-checkout", str(WALLET_REPO), str(src)], check=True)
        subprocess.run(["git", "-C", str(src), "checkout", "-q", "--detach", sha], check=True)
    injected = src / ".git" / "e2e-api-go"
    if go_sha and not injected.exists():
        log(f"wallet {sha[:9]}: bitbox02-api-go {go_sha[:9]} in place of the vendored one")
        # The app builds from vendor/: replace the module, then vendor it again (downloads the
        # app's other modules into the Go module cache once).
        subprocess.run(["go", "mod", "edit", f"-replace=github.com/BitBoxSwiss/bitbox02-api-go={go_src}"], cwd=src, check=True)
        subprocess.run(["go", "mod", "vendor"], cwd=src, check=True, stdout=sys.stderr)
        injected.touch()
    web = src / "frontends" / "web"
    stamp = web / "node_modules" / ".e2e-built"
    if not stamp.exists():
        log(f"wallet {sha[:9]}: npm ci + build")
        subprocess.run(["npm", "ci", "--ignore-scripts"], cwd=web, check=True, stdout=sys.stderr)
        subprocess.run(["npm", "run", "build"], cwd=web, check=True, stdout=sys.stderr)
        # Browser revision follows the lockfile's Playwright version, not whatever is installed.
        subprocess.run(["npx", "playwright", "install", "chromium"], cwd=web, check=True, stdout=sys.stderr)
        stamp.touch()
    return sha, web


def backend_build_error(web):
    """The compiler's output if the app's backend does not build (e.g. against another
    bitbox02-api-go), else None. The tests would only say that servewallet exited."""
    res = subprocess.run(["go", "build", "-o", os.devnull, "./cmd/servewallet"], cwd=web.parent.parent,
                         stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, check=False)
    return None if res.returncode == 0 else "\n".join(res.stdout.splitlines()[-60:])


def run_wallet(ref, sim_dir, tests, out, api_go=None):
    sha, web = wallet_clone(ref, api_go)
    source = sha[:9] if ref == sha else f"{ref} ({sha[:9]})"
    if (err := backend_build_error(web)):
        log(f"wallet {sha[:9]}: the backend does not build")
        (out / "tests.json").write_text("[]")
        (out / "meta.json").write_text(json.dumps({"source": source, "simulator": (Path(sim_dir) / "source.txt").read_text().strip(),
                                                   "passed": 0, "total": 0, "build_error": err}, indent=2))
        return
    shutil.copy(E2E_DIR / "wallet" / "playwright.evidence.config.ts", web)
    shutil.rmtree(web / "test-results", ignore_errors=True)
    (web / "e2e-results.json").unlink(missing_ok=True)
    # The tests' helpers take GITHUB_WORKSPACE as the app's root when set; in CI it is this repo's.
    env = dict(os.environ, PLAYWRIGHT_USE_PREBUILT_WEB="1", SIMULATOR_PATH=str(Path(sim_dir) / "simulator"),
               GITHUB_WORKSPACE=str(web.parent.parent))
    log(f"wallet {sha[:9]}: playwright {' '.join(tests) or '(all tests)'}")
    deps = [a for d in TEST_PYTHON_DEPS for a in ("--with", d)]
    with open(out / "playwright.log", "w") as f:
        subprocess.run(["uv", "run", "-q", "--no-project", *deps, "--", "npx", "playwright", "test", "-c",
                        "playwright.evidence.config.ts", *tests],
                       cwd=web, env=env, stdout=f, stderr=subprocess.STDOUT, timeout=3600)
    if not (web / "e2e-results.json").exists():
        tail = (out / "playwright.log").read_text().splitlines()[-20:]
        raise RuntimeError(f"playwright produced no results for {ref}:\n" + "\n".join(tail))
    shutil.copytree(web / "test-results", out / "test-results")
    shutil.copy(web / "e2e-results.json", out / "e2e-results.json")

    tests_out = []

    def walk(suite, prefix):
        for spec in suite.get("specs", []):
            for t in spec["tests"]:
                for r in t["results"]:
                    shots = [a["path"] for a in r.get("attachments", [])
                             if a.get("contentType") == "image/png" and a.get("path")]
                    tests_out.append({"title": f"{prefix}{spec['title']}", "status": r["status"],
                                      "error": (r.get("error") or {}).get("message", "")[:2000],
                                      "screenshot": shots[-1] if shots else None})
        for sub in suite.get("suites", []):
            walk(sub, prefix)

    for suite in json.loads((out / "e2e-results.json").read_text())["suites"]:
        walk(suite, f"{suite['title']} > ")
    for t in tests_out:
        if t["screenshot"]:
            src = Path(t["screenshot"])
            rel = Path("test-results") / src.relative_to(web / "test-results")
            t["screenshot"] = str(rel)
            log_path = out / rel.parent / "simulator.log"
            parser = ScreenParser()
            if log_path.exists():
                for line in log_path.read_text(errors="replace").splitlines():
                    parser.feed(line, "app")
            t["screens"] = [x for x in parser.screens if x["fields"].get("TITLE") not in VOLATILE_TITLES]
    (out / "tests.json").write_text(json.dumps(tests_out, indent=2))
    meta = {"source": source, "simulator": (Path(sim_dir) / "source.txt").read_text().strip(),
            "passed": sum(t["status"] == "passed" for t in tests_out), "total": len(tests_out)}
    (out / "meta.json").write_text(json.dumps(meta, indent=2))
    log(f"wallet {sha[:9]}: {meta['passed']}/{meta['total']} passed")


def compare(sides, tests, out):
    """The app's tests on both sides (report.load_sides format, repos firmware, app, api-go);
    returns 1 if a test that passed on base does not on head."""
    bundle.use_node()
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    (out / "sides.json").write_text(json.dumps(sides, indent=1))
    sims = {side: bundle.build_sim(sides[side]["firmware"]) for side in ("base", "head")}
    for side in ("base", "head"):
        (out / side).mkdir()
        run_wallet(sides[side]["app"], sims[side], tests, out / side, sides[side].get("api-go"))
    regressions = wallet_report(out)
    for r in regressions:
        log(f"regression: {r}")
    return 1 if regressions else 0


# App tests create fresh wallets and random request ids, so these differ on every run.
UUID = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")


def stable(screens):
    """Device screens with the values that are random per app test run replaced by placeholders."""
    out = []
    for s in screens:
        if s["fields"].get("TITLE") in VOLATILE_TITLES:
            continue
        fields = {k: UUID.sub("<random id>", v) for k, v in s["fields"].items()}
        if fields.get("TITLE") == "Address" and "BODY" in fields:
            fields["BODY"] = "<address of a random test wallet>"
        out.append(dict(s, fields=fields))
    return out


def wallet_report(out):
    """report.render for an app change; returns the regressions (tests that passed on base and do not
    pass on head)."""
    out = Path(out)
    meta = {s: json.loads((out / s / "meta.json").read_text()) for s in ("base", "head")}
    tb = {t["title"]: t for t in json.loads((out / "base/tests.json").read_text())}
    th = {t["title"]: t for t in json.loads((out / "head/tests.json").read_text())}
    sides = report.load_sides(out) or {"changed": "app", **{s: {"firmware": meta[s]["simulator"], "app": meta[s]["source"]}
                                                                for s in ("base", "head")}}
    changed = sides["changed"]
    pr_info = report.load_pr_info(out)
    globs = report.intent(pr_info).get("expect")
    f = {"repo": bundle.REPOS[changed].slug, "pr": pr_info, "intent": report.intent(pr_info),
         "title": (pr_info or {}).get("title")
         or f"BitBoxApp tests: {report.REPO_NAMES[changed]} {report.short_ref(sides['head'][changed])}",
         "sides": report.sides_header(sides), "broke": [], "fixed": [], "changes": [], "known": [], "cautions": []}
    regressions = []
    eb, eh = meta["base"].get("build_error"), meta["head"].get("build_error")
    if eh and not eb:
        regressions.append("the backend does not build")
        f["broke"].append({"title": "The BitBoxApp backend does not build", "excerpt": eh,
                           "detail": "It built before this PR, so none of its tests ran with it."})
    elif eb and eh:
        f["known"].append("The BitBoxApp backend does not build, before or after this PR, so no test ran: "
                          + eh.splitlines()[-1][:200])
    elif eb:
        f["fixed"].append("The BitBoxApp backend builds again (it did not before this PR, so the tests ran only with it).")
    for title in dict.fromkeys([*tb, *th]):
        b, h = tb.get(title), th.get(title)
        if not b or not h:
            continue
        pairs = report.screen_changes(stable(b.get("screens", [])), stable(h.get("screens", [])))
        if b["status"] == "passed" and h["status"] != "passed":
            regressions.append(f"`{title}`: passed on base, {h['status']} on head")
            f["broke"].append({
                "title": f"Test `{title}`", "detail": f"Passed before this PR, {h['status']} with it.",
                "excerpt": "\n".join(strip_ansi(h.get("error") or "").splitlines()[:40]) or None,
                "screens": pairs, "photo": True,
                "compare": [{"label": "Last screenshot of the test", "before": f"base/{b['screenshot']}" if b.get("screenshot") else None,
                             "after": f"head/{h['screenshot']}" if h.get("screenshot") else None}]})
        elif b["status"] != "passed" and h["status"] == "passed":
            f["fixed"].append(f"Test `{title}`: {b['status']} before this PR, passes with it.")
        elif b["status"] != "passed" and h["status"] != "passed":
            first = next((l for l in strip_ansi(h.get("error") or "").splitlines() if l.strip()), "")
            f["known"].append(f"Test `{title}` fails before and after this PR" + (f": {first[:160]}" if first else ""))
        elif pairs:
            f["changes"].append({"title": report.change_title(pairs), "where": [title],
                                 "expected": report.is_expected([title], globs), "screens": pairs})
    added, removed = ([t for t in th if t not in tb], [t for t in tb if t not in th]) if not (eb or eh) else ([], [])
    passed = {s: f"{meta[s]['passed']} of {meta[s]['total']}" for s in meta}
    f["scope"] = (f"the BitBoxApp's own Playwright tests with {report.sides_fixed(sides)}, "
                  f"{report.REPO_NAMES[changed]} before and after this PR")
    f["tested"] = [
        (f"The BitBoxApp's own Playwright tests, with {report.REPO_NAMES[changed]} before and after this PR and "
         f"{report.sides_fixed(sides)} on both sides, against the BitBox02 simulator: {passed['base']} passed "
         f"before, {passed['head']} after."),
        ("The device screens each test shows are compared as text. Values that are random in every test run "
        "(wallet addresses, request ids) are masked."),
        ("Screenshots are shown only for tests whose result changed. They are not compared pixel by pixel: "
        "charts animate and test wallets are random, so every screenshot differs a little."),
    ]
    if added:
        f["tested"].append("New tests in this PR: " + ", ".join(f"`{t}`" for t in added))
    if removed:
        f["tested"].append("Tests this PR removes: " + ", ".join(f"`{t}`" for t in removed))
    f["details"] = [*report.sides_details(sides),
                    ("simulator", meta["base"]["simulator"][:9], meta["head"]["simulator"][:9]),
                    *((f"`{t}`", tb.get(t, {}).get("status", "-"), th.get(t, {}).get("status", "-"))
                      for t in dict.fromkeys([*tb, *th]))]
    f["notes"] = ["Logs: base/playwright.log, head/playwright.log; Playwright output in */test-results."]
    page = report.render(out, f)
    log(f"report: {page}")
    print(page)
    return regressions
