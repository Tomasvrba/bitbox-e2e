"""`e2e.py pr --repo app` and `e2e.py wallet-compare`: the app's Playwright tests on one simulator,
app base vs head."""

import argparse
import difflib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import bundle
from bundle import E2E_DIR, log
from report import fmt_status, images_equal, md_to_html, pixel_diff, screen_lines, strip_ansi
from runs import ScreenParser

WALLET_REPO = bundle.REPOS["app"].dir
# What the app's own CI installs for its tests (tests/util/aopp/server.py), on python3's PATH.
TEST_PYTHON_DEPS = ("ecdsa==0.19.1", "bech32==1.2.0")
# Volatile device screens in app tests: random pairing codes and the current time.
VOLATILE_TITLES = {"Pairing code", "Is now?"}


def wallet_clone(ref):
    """Clean clone of the app at ref. Kept between runs (keyed by sha) to reuse node_modules."""
    sha = subprocess.run(["git", "-C", str(WALLET_REPO), "rev-parse", "--verify", f"{ref}^{{commit}}"],
                         stdout=subprocess.PIPE, text=True, check=True).stdout.strip()
    src = bundle.CACHE / "src" / f"bitbox-wallet-app-{sha}"
    if not (src / ".git").exists():
        subprocess.run(["git", "clone", "-q", "--shared", "--no-checkout", str(WALLET_REPO), str(src)], check=True)
        subprocess.run(["git", "-C", str(src), "checkout", "-q", "--detach", sha], check=True)
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


def run_wallet(ref, sim_dir, tests, out):
    sha, web = wallet_clone(ref)
    shutil.copy(E2E_DIR / "wallet" / "playwright.evidence.config.ts", web)
    shutil.rmtree(web / "test-results", ignore_errors=True)
    (web / "e2e-results.json").unlink(missing_ok=True)
    env = dict(os.environ, PLAYWRIGHT_USE_PREBUILT_WEB="1", SIMULATOR_PATH=str(Path(sim_dir) / "simulator"))
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
    meta = {"source": f"{ref} ({sha})", "simulator": (Path(sim_dir) / "source.txt").read_text().strip(),
            "passed": sum(t["status"] == "passed" for t in tests_out), "total": len(tests_out)}
    (out / "meta.json").write_text(json.dumps(meta, indent=2))
    log(f"wallet {sha[:9]}: {meta['passed']}/{meta['total']} passed")


def cmd_wallet_compare(args):
    return compare(args.base, args.head, args.firmware, args.tests, Path(args.out))


def compare(base, head, firmware, tests, out):
    bundle.use_node()
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    sim_dir = bundle.build_sim(firmware)
    for side, ref in (("base", base), ("head", head)):
        (out / side).mkdir()
        run_wallet(ref, sim_dir, tests, out / side)
    regressions = wallet_report(out)
    for r in regressions:
        log(f"regression: {r}")
    return 1 if regressions else 0


def cmd_app_change(args, firmware=None):
    """`e2e.py pr --repo app`: the tests (all, or --tests) at the merge-base and at head, on the
    pinned firmware or upstream master; with --wc also the WalletConnect flow, in <out>/wc."""
    head = bundle.resolve(args.head, "app")
    base = bundle.git("merge-base", bundle.resolve(args.base, "app"), head, cwd=WALLET_REPO)
    firmware = firmware or bundle.resolve(bundle.default_firmware_ref())
    out = Path(args.out)
    rc = compare(base, head, firmware, args.tests, out)
    if args.wc:
        from wc import flow
        wc_rc = flow.cmd_wc(argparse.Namespace(base=base, head=head, firmware=firmware, out=str(out / "wc")))
        with open(out / "report.md", "a") as f:
            f.write(f"\n## WalletConnect flow\n\n{'**Failed**' if wc_rc else 'Passed'} on "
                    f"{'one side or both' if wc_rc else 'both sides'}: [wc/report.html](wc/report.html)\n")
        rc = max(rc, wc_rc)
    return rc


def wallet_report(out):
    out = Path(out)
    meta = {s: json.loads((out / s / "meta.json").read_text()) for s in ("base", "head")}
    tb = {t["title"]: t for t in json.loads((out / "base/tests.json").read_text())}
    th = {t["title"]: t for t in json.loads((out / "head/tests.json").read_text())}
    md = ["# BitBoxApp e2e diff", "", "| | base | head |", "|---|---|---|",
          f"| source | {meta['base']['source']} | {meta['head']['source']} |",
          f"| simulator | {meta['base']['simulator']} | {meta['head']['simulator']} |",
          f"| passed | {meta['base']['passed']}/{meta['base']['total']} | {meta['head']['passed']}/{meta['head']['total']} |"]
    # Tests missing on one side were added or removed by the head ref; only passed -> not passed gates.
    regressions = [f"`{t}`: passed on base, {th[t]['status']} on head" for t, b in tb.items()
                   if b["status"] == "passed" and t in th and th[t]["status"] != "passed"]
    md[1:1] = ["", "**Regressions**: " + ("; ".join(regressions) if regressions else "none")]
    for title in dict.fromkeys(list(tb) + list(th)):
        b, h = tb.get(title, {}), th.get(title, {})
        md.append(f"| `{title}` | {fmt_status(b)} | {fmt_status(h)} |")
    md.append("")
    diffdir = out / "diff"
    diffdir.mkdir(exist_ok=True)
    for title in dict.fromkeys(list(tb) + list(th)):
        b, h = tb.get(title, {}), th.get(title, {})
        sections = []
        for side, t in (("base", b), ("head", h)):
            if t.get("status") not in (None, "passed") and t.get("error"):
                sections += [f"{side} error:", "```", *strip_ansi(t["error"]).splitlines()[:15], "```", ""]
        diff = list(difflib.unified_diff(screen_lines(b.get("screens", [])), screen_lines(h.get("screens", [])),
                                         "base", "head", lineterm="", n=1))
        if diff:
            sections += ["Device screens:", "```diff", *diff, "```", ""]
        sb, sh = b.get("screenshot"), h.get("screenshot")
        if sb and sh:
            if not images_equal(out / "base" / sb, out / "head" / sh):
                d = pixel_diff(out / "base" / sb, out / "head" / sh, diffdir / f"{len(md)}.png")
                cd = f"![]({d.relative_to(out)})" if d else "(size differs)"
                sections += ["| base (final) | head (final) | changed pixels |", "|---|---|---|",
                             f"| ![](base/{sb}) | ![](head/{sh}) | {cd} |", ""]
        elif sb or sh:
            side, shot = ("base", sb) if sb else ("head", sh)
            sections += [f"| {side} (final) |", "|---|", f"| ![]({side}/{shot}) |", ""]
        if sections:
            md += [f"## {title}", "", *sections]
    (out / "report.md").write_text("\n".join(md))
    (out / "report.html").write_text(md_to_html(out, md))
    log(f"report: {out / 'report.md'}")
    print(out / "report.md")
    return regressions

