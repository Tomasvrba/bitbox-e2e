#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pillow>=10", "python-xlib>=0.33"]
# ///
"""WalletConnect e2e: a test dapp pairs with the BitBoxApp over the public relay, sends signing
requests, the app approves them and the graphical simulator confirms on device. Runs at two app
refs with one firmware simulator and reports app screenshots next to device frames per step.

  e2e.py wc --base <app-ref> --head <app-ref> --out <dir> [--firmware <fw-ref|WORKTREE>]
  flow.py run --ref <app-ref> --out <dir> [--firmware <fw-ref>]   one side only
  flow.py same <run-dir>...       which steps produced identical app screenshots / device frames
  flow.py report <dir>            re-render report.html from <dir>/{base,head}

Needs the public WalletConnect relay (wss://relay.walletconnect.org) and, for transactions,
the app's Etherscan proxy: run in the host network namespace, not in isolated.sh.
"""

import difflib
import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

WC_DIR = Path(__file__).resolve().parent
E2E_DIR = WC_DIR.parent
sys.path.insert(0, str(E2E_DIR))

import e2e  # noqa: E402

TEST = "walletconnect.test.ts"
# vite preview, servewallet, simulator. Playwright reuses whatever listens on 8080, so a stale
# server there would silently serve another ref.
PORTS = (8080, 8082, 15423)
# Steps whose device screens differ every run (pairing code) or that belong to no request.
SETUP_STEPS = {"boot", "setup"}


def ensure_dapp():
    if not (WC_DIR / "node_modules" / "@walletconnect" / "sign-client").exists():
        e2e.log("wc: npm ci (dapp)")
        subprocess.run(["npm", "ci", "--no-audit", "--no-fund"], cwd=WC_DIR, check=True, stdout=sys.stderr)


def check_ports():
    busy = []
    for port in PORTS:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                busy.append(port)
    if busy:
        raise RuntimeError(f"ports {busy} busy on 127.0.0.1; stop the other app/simulator first")


def run_side(ref, sim_dir, out):
    """One flow at one app ref. Evidence lands in out/{app,device}/, Playwright output alongside."""
    sha, web = e2e.wallet_clone(ref)
    # servewallet is `go run` from the test with a startup timeout; compile it first.
    subprocess.run(["go", "build", "-o", os.devnull, "./cmd/servewallet"], cwd=web.parent.parent, check=True)
    check_ports()
    shutil.copy(E2E_DIR / "wallet" / "playwright.evidence.config.ts", web)
    test_dst = web / "tests" / TEST
    shutil.copy(E2E_DIR / "wallet" / "tests" / TEST, test_dst)
    shutil.rmtree(web / "test-results", ignore_errors=True)
    (web / "e2e-results.json").unlink(missing_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    env = dict(os.environ, PLAYWRIGHT_USE_PREBUILT_WEB="1", WC_OUT=str(out),
               WC_DAPP=str(WC_DIR / "dapp.mjs"), WC_DEVICE=str(WC_DIR / "device.py"),
               SIMULATOR_DIR=str(sim_dir), E2E_DISPLAY=os.environ.get("E2E_DISPLAY", ":72"))
    e2e.log(f"wc {sha[:9]}: playwright {TEST}")
    try:
        with open(out / "playwright.log", "w") as f:
            res = subprocess.run(["npx", "playwright", "test", "-c", "playwright.evidence.config.ts", f"tests/{TEST}"],
                                 cwd=web, env=env, stdout=f, stderr=subprocess.STDOUT, timeout=1800)
    finally:
        test_dst.unlink(missing_ok=True)
    if (web / "test-results").exists():
        shutil.copytree(web / "test-results", out / "test-results")
    error = ""
    if (web / "e2e-results.json").exists():
        shutil.copy(web / "e2e-results.json", out / "e2e-results.json")
        error = first_error(json.loads((out / "e2e-results.json").read_text()))
    meta = {"source": f"{ref} ({sha})", "simulator": e2e.read_text(Path(sim_dir) / "source.txt"),
            "simulator_version": e2e.read_text(Path(sim_dir) / "version.txt"),
            "passed": res.returncode == 0, "error": error}
    (out / "meta.json").write_text(json.dumps(meta, indent=2))
    e2e.log(f"wc {sha[:9]}: {'passed' if meta['passed'] else 'FAILED'}")
    return meta


def first_error(results):
    def walk(suite):
        for spec in suite.get("specs", []):
            for t in spec["tests"]:
                for r in t["results"]:
                    if r.get("error"):
                        return e2e.strip_ansi(r["error"].get("message", ""))[:2000]
        for sub in suite.get("suites", []):
            if (m := walk(sub)):
                return m
        return ""
    return next((m for s in results.get("suites", []) if (m := walk(s))), "")


def cmd_wc(args):
    out = Path(args.out)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    ensure_dapp()
    sim_dir = e2e.build_sim(args.firmware)
    for side, ref in (("base", args.base), ("head", args.head)):
        run_side(ref, sim_dir, out / side)
    report(out)


# --- report ---------------------------------------------------------------------------------------

def load_side(d):
    steps = json.loads((d / "app" / "steps.json").read_text()) if (d / "app" / "steps.json").exists() else []
    frames = json.loads((d / "device" / "frames.json").read_text()) if (d / "device" / "frames.json").exists() else []
    return steps, frames


def dapp_cell(rec):
    ev = (rec or {}).get("dapp")
    if not ev:
        return "-"
    if ev.get("event") == "session":
        return f"session: {len(ev.get('chains') or [])} chains, {len(ev.get('methods') or [])} methods"
    if ev.get("ok"):
        chk = ev.get("check") or {}
        if "signerMatches" in chk:
            return "signed, signer " + ("matches" if chk["signerMatches"] else "**MISMATCH**")
        return "ok " + str(ev.get("result"))[:20]
    err = ev.get("error") or {}
    return f"**error** {err.get('code', '')} {err.get('message', '')}".replace("|", "/")[:120]


def step_frames(frames, step):
    return [f for f in frames if f["step"] == step]


def report(out):
    out = Path(out)
    meta = {s: json.loads((out / s / "meta.json").read_text()) for s in ("base", "head")}
    (sb, fb), (sh, fh) = load_side(out / "base"), load_side(out / "head")
    rb, rh = {r["step"]: r for r in sb}, {r["step"]: r for r in sh}
    names = list(dict.fromkeys([r["step"] for r in sb] + [r["step"] for r in sh]))
    diffdir = out / "diff"
    diffdir.mkdir(exist_ok=True)

    md = ["# BitBoxApp WalletConnect e2e: base vs head", "",
          "A test dapp pairs with the app over WalletConnect and sends signing requests; the app "
          "approves each one and the graphical simulator confirms it on device.", "",
          "| | base | head |", "|---|---|---|",
          f"| app | {meta['base']['source']} | {meta['head']['source']} |",
          f"| simulator | {meta['base']['simulator']} {meta['base']['simulator_version']} | "
          f"{meta['head']['simulator']} {meta['head']['simulator_version']} |",
          f"| test | {e2e.fmt_ok(meta['base']['passed'])} | {e2e.fmt_ok(meta['head']['passed'])} |", ""]
    for side in ("base", "head"):
        if meta[side].get("error"):
            md += [f"{side} error:", "```", *meta[side]["error"].splitlines()[:15], "```", ""]

    summary = ["| step | request | base dapp | head dapp | app | device |", "|---|---|---|---|---|---|"]
    sections = []
    for name in names:
        b, h = rb.get(name, {}), rh.get(name, {})
        req = f"`{(h or b).get('method', '')}` {(h or b).get('chainId', '')}" if (h or b).get("method") else ""
        rows, app_changed = [], False
        labels = list(dict.fromkeys([s["label"] for s in b.get("app", [])] + [s["label"] for s in h.get("app", [])]))
        for label in labels:
            ib = next((s["file"] for s in b.get("app", []) if s["label"] == label), None)
            ih = next((s["file"] for s in h.get("app", []) if s["label"] == label), None)
            ca = f"![](base/app/{ib})" if ib else "*(absent)*"
            ch = f"![](head/app/{ih})" if ih else "*(absent)*"
            if ib and ih and e2e.images_equal(out / "base/app" / ib, out / "head/app" / ih):
                rows.append(f"| app: {label} | {ca} | {ch} | same |")
                continue
            app_changed = True
            d = ib and ih and e2e.pixel_diff(out / "base/app" / ib, out / "head/app" / ih, diffdir / f"app-{name}-{label}.png")
            rows.append(f"| app: {label} **changed** | {ca} | {ch} | {f'![]({d.relative_to(out)})' if d else ''} |")

        db, dh = step_frames(fb, name), step_frames(fh, name)
        dev_changed = [f["hash"] for f in db] != [f["hash"] for f in dh]
        cb = " ".join(f"![{f['step']}](base/device/{f['file']})" for f in db) or "*(none)*"
        ch = " ".join(f"![{f['step']}](head/device/{f['file']})" for f in dh) or "*(none)*"
        diffs = []
        if dev_changed:
            sm = difflib.SequenceMatcher(a=[f["hash"] for f in db], b=[f["hash"] for f in dh], autojunk=False)
            for tag, i1, i2, j1, j2 in sm.get_opcodes():
                for k in range(min(i2 - i1, j2 - j1) if tag == "replace" else 0):
                    a, c = db[i1 + k], dh[j1 + k]
                    d = e2e.pixel_diff(out / "base/device" / a["file"], out / "head/device" / c["file"],
                                       diffdir / f"device-{name}-{a['file'][:-4]}--{c['file']}")
                    if d:
                        diffs.append(f"![]({d.relative_to(out)})")
        rows.append(f"| device{' **changed**' if dev_changed else ''} | {cb} | {ch} | "
                    f"{' '.join(diffs) or ('same' if not dev_changed else 'frames added/removed')} |")

        summary.append(f"| `{name}` | {req} | {dapp_cell(b)} | {dapp_cell(h)} | "
                       f"{'**changed**' if app_changed else 'same'} | {'**changed**' if dev_changed else 'same'} |")
        sections += [f"## `{name}` {req}", "", "| | base | head | changed pixels |", "|---|---|---|---|",
                     f"| dapp | {dapp_cell(b)} | {dapp_cell(h)} | |", *rows, ""]

    md += ["## Summary", "", *summary, "",
           "App screenshots are compared pixel by pixel; device frames by content hash (frames that "
           "replaced each other get a pixel diff). Device setup (unlock, pairing code) is only in the "
           "filmstrips below. App screenshots leave out the banners (testnet, update, attestation, microSD).", "",
           "Transaction steps take gas price and limit from the live Etherscan proxy: their Total/Fee "
           "device screens (and fee amounts in app errors) differ between any two runs. Only a change in "
           "the other frames, or in the frame sequence, is a real difference.", "", *sections]
    for side, frames in (("base", fb), ("head", fh)):
        setup = [f for f in frames if f["step"] in SETUP_STEPS]
        if setup:
            md += [f"<details><summary>{side} device setup frames</summary>", "",
                   " ".join(f"![{f['step']}]({side}/device/{f['file']})" for f in setup), "", "</details>", ""]
    (out / "report.md").write_text("\n".join(md))
    (out / "report.html").write_text(e2e.md_to_html(out, md))
    e2e.log(f"report: {out / 'report.html'}")
    print(out / "report.html")


# --- determinism ------------------------------------------------------------------------------------

def same(dirs):
    """Per step: are app screenshots and device frames identical across runs (same app ref)."""
    runs = [load_side(Path(d)) for d in dirs]
    names = list(dict.fromkeys(r["step"] for steps, _ in runs for r in steps))
    print(f"{'step':28} {'app':40} device")
    for name in names:
        recs = [next((r for r in steps if r["step"] == name), {}) for steps, _ in runs]
        app = []
        for label in dict.fromkeys(s["label"] for r in recs for s in r.get("app", [])):
            files = [next((Path(d) / "app" / s["file"] for s in r.get("app", []) if s["label"] == label), None)
                     for d, r in zip(dirs, recs)]
            ok = all(files) and all(e2e.images_equal(files[0], f) for f in files[1:])
            app.append(f"{label}={'same' if ok else 'DIFF'}")
        seqs = [[f["hash"] for f in step_frames(frames, name)] for _, frames in runs]
        dev = "same" if all(s == seqs[0] for s in seqs) else "DIFF " + "/".join(str(len(s)) for s in seqs)
        print(f"{name:28} {' '.join(app):40} {dev} ({len(seqs[0])} frames)")


def main():
    import argparse
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--ref", required=True)
    r.add_argument("--out", required=True)
    r.add_argument("--firmware", default="bitbox/master")
    sub.add_parser("same").add_argument("dirs", nargs="+")
    sub.add_parser("report").add_argument("dir")
    args = p.parse_args()
    if args.cmd == "run":
        ensure_dapp()
        out = Path(args.out)
        shutil.rmtree(out, ignore_errors=True)
        run_side(args.ref, e2e.build_sim(args.firmware), out)
    elif args.cmd == "same":
        same(args.dirs)
    else:
        report(args.dir)


if __name__ == "__main__":
    main()
