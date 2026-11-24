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

--firmware defaults to master of the remote pointing at BitBoxSwiss/bitbox02-firmware (else origin).
  flow.py same <run-dir>...       which steps produced identical app screenshots / device frames
  flow.py report <dir>            render report.html and summary.md again from <dir>/{base,head}

Needs the public WalletConnect relay (wss://relay.walletconnect.org) and, for transactions,
the app's Etherscan proxy: run in the host network namespace, not in isolated.sh.
"""

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

import bundle  # noqa: E402
import report as report_mod  # noqa: E402
from wallet import chains  # noqa: E402
import wallet_app  # noqa: E402
from bundle import build_sim, log, read_text  # noqa: E402
from report import frame_rows, images_equal, strip_ansi  # noqa: E402

TEST = "walletconnect.test.ts"
# vite preview, servewallet, simulator (the app's simulator mode connects to 15423). Playwright
# reuses whatever listens on 8080, so a stale server there would silently serve another ref.
PORTS = (8080, 8082, bundle.SIM_PORT)


def ensure_dapp():
    if not (WC_DIR / "node_modules" / "@walletconnect" / "sign-client").exists():
        log("wc: npm ci (dapp)")
        subprocess.run(["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"], cwd=WC_DIR, check=True, stdout=sys.stderr)


def check_ports():
    busy = []
    for port in PORTS:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                busy.append(port)
    if busy:
        raise RuntimeError(f"ports {busy} busy on 127.0.0.1; stop the other app/simulator first")


def run_side(ref, sim_dir, out, api_go=None):
    """One flow at one app ref (built with bitbox02-api-go at api_go if given). Evidence lands in
    out/{app,device}/, Playwright output alongside."""
    sha, web = wallet_app.wallet_clone(ref, api_go)
    wallet_app.reset_app_data(web)
    out.mkdir(parents=True, exist_ok=True)
    source = sha[:9] if ref == sha else f"{ref} ({sha[:9]})"
    # servewallet is `go run` from the test with a startup timeout; compile it first.
    if (err := wallet_app.backend_build_error(web)):
        log(f"wc {sha[:9]}: the backend does not build")
        meta = {"source": source, "simulator": read_text(Path(sim_dir) / "source.txt"),
                "simulator_version": read_text(Path(sim_dir) / "version.txt"), "passed": False,
                "error": "The BitBoxApp backend does not build:\n" + err, "build_error": True}
        (out / "meta.json").write_text(json.dumps(meta, indent=2))
        return meta
    check_ports()
    shutil.copy(E2E_DIR / "wallet" / "playwright.evidence.config.ts", web)
    test_dst = web / "tests" / TEST
    shutil.copy(E2E_DIR / "wallet" / "tests" / TEST, test_dst)
    shutil.rmtree(web / "test-results", ignore_errors=True)
    (web / "e2e-results.json").unlink(missing_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    log(f"wc {sha[:9]}: playwright {TEST}")
    try:
        with chains.local_chains(out / "chains") as c, open(out / "playwright.log", "w") as f:
            (out / "app-config.json").write_text(json.dumps(chains.app_config(c["socks"])))
            # SSL_CERT_FILE: the app backend (Go) trusts the local Etherscan proxy's certificate.
            env = dict(os.environ, PLAYWRIGHT_USE_PREBUILT_WEB="1", WC_OUT=str(out), WC_DAPP=str(WC_DIR / "dapp.mjs"),
                       WC_DEVICE=str(WC_DIR / "device.py"), SIMULATOR_DIR=str(sim_dir),
                       WC_APP_CONFIG=str(out / "app-config.json"), SSL_CERT_FILE=c["ca_file"],
                       WC_CHAIN_RPC=json.dumps({str(k): v for k, v in c["rpc"].items()}))
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
    meta = {"source": source, "simulator": read_text(Path(sim_dir) / "source.txt"),
            "simulator_version": read_text(Path(sim_dir) / "version.txt"),
            "passed": res.returncode == 0, "error": error}
    (out / "meta.json").write_text(json.dumps(meta, indent=2))
    log(f"wc {sha[:9]}: {'passed' if meta['passed'] else 'FAILED'}")
    return meta


def first_error(results):
    def walk(suite):
        for spec in suite.get("specs", []):
            for t in spec["tests"]:
                for r in t["results"]:
                    if r.get("error"):
                        return strip_ansi(r["error"].get("message", ""))[:2000]
        for sub in suite.get("suites", []):
            if (m := walk(sub)):
                return m
        return ""
    return next((m for s in results.get("suites", []) if (m := walk(s))), "")


def cmd_wc(args):
    """`e2e.py wc`: app base vs head on one firmware."""
    fw = args.firmware or bundle.default_firmware_ref()
    return compare({"changed": "app", "base": {"firmware": fw, "app": args.base, "api-go": None},
                    "head": {"firmware": fw, "app": args.head, "api-go": None}}, Path(args.out))


def compare(sides, out):
    """The flow on both sides (report.load_sides format, repos firmware, app, api-go); returns 1 if
    a request that worked on base does not on head."""
    bundle.use_node()
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    (out / "sides.json").write_text(json.dumps(sides, indent=1))
    ensure_dapp()
    sims = {side: build_sim(sides[side]["firmware"]) for side in ("base", "head")}
    for side in ("base", "head"):
        run_side(sides[side]["app"], sims[side], out / side, sides[side].get("api-go"))
    # Like the other flows: exit 1 only for what got worse from base to head. The test itself
    # tolerates rejected requests; the report compares what the dapp got per request.
    broke = report(out)["broke"]
    for b in broke:
        log(f"regression: {b['title']}")
    return 1 if broke else 0


# --- report ---------------------------------------------------------------------------------------

def load_side(d):
    steps = json.loads((d / "app" / "steps.json").read_text()) if (d / "app" / "steps.json").exists() else []
    frames = json.loads((d / "device" / "frames.json").read_text()) if (d / "device" / "frames.json").exists() else []
    return steps, frames


TX_METHODS = {"eth_sendTransaction", "eth_signTransaction"}
CHAIN_NAMES = {"1": "Ethereum", "11155111": "Sepolia", "42161": "Arbitrum One", "137": "Polygon", "10": "OP Mainnet",
               "8453": "Base", "56": "BNB Chain"}


def dapp_text(rec):
    """What the dapp got back for one step, in words."""
    ev = (rec or {}).get("dapp")
    if not ev:
        return "no answer"
    if ev.get("event") == "session":
        return f"session with {len(ev.get('chains') or [])} chains, {len(ev.get('methods') or [])} methods"
    if ev.get("ok"):
        chk = ev.get("check") or {}
        if chk.get("onChain") is False:
            return "the app answered with a transaction hash, but the transaction is not on the chain"
        if "signerMatches" in chk:
            verb = "sent from" if chk.get("onChain") else "signed by"
            return f"{verb} the right account" if chk["signerMatches"] else f"{verb} the WRONG account"
        return "ok, " + str(ev.get("result"))[:20]
    err = ev.get("error") or {}
    return f"error {err.get('code', '')} {err.get('message', '')}".strip().rstrip(".")[:160]


def dapp_ok(rec):
    ev = (rec or {}).get("dapp") or {}
    chk = ev.get("check") or {}
    return bool(ev.get("event") == "session"
                or (ev.get("ok") and chk.get("signerMatches") is not False and chk.get("onChain") is not False))


def proxy_errors(side_dir, chain=None):
    """The app's requests to the local Etherscan proxy that failed (on one chain id, or all), once
    per kind, in words."""
    p = side_dir / "chains" / "etherscan.jsonl"
    seen = {}
    for e in (json.loads(line) for line in p.read_text().splitlines()) if p.exists() else ():
        if e.get("error") and chain in (None, e.get("chain")):
            where = {"url": "chain in the URL", "body": "chain only in the body", None: "no chain"}[e.get("chain_in")]
            key = f"{e['method']} {e.get('action')} ({where}): {e['error'].split(', please see')[0][:120]}"
            seen[key] = seen.get(key, 0) + 1
    return [f"{k}" + (f" ({n} times)" if n > 1 else "") for k, n in seen.items()]


def step_frames(frames, step):
    return [f for f in frames if f["step"] == step]


def report(out):
    """report.render for the WalletConnect flow, app base vs head."""
    out = Path(out)
    meta = {s: json.loads((out / s / "meta.json").read_text()) for s in ("base", "head")}
    (sb, fb), (sh, fh) = load_side(out / "base"), load_side(out / "head")
    rb, rh = {r["step"]: r for r in sb}, {r["step"]: r for r in sh}
    names = list(dict.fromkeys([r["step"] for r in sb] + [r["step"] for r in sh]))
    diffdir = out / "diff"
    sides = report_mod.load_sides(out) or {"changed": "app", **{s: {"firmware": meta[s]["simulator"], "app": meta[s]["source"]}
                                                                    for s in ("base", "head")}}
    changed = report_mod.REPO_NAMES[sides["changed"]]
    pr_info = report_mod.load_pr_info(out)
    globs = report_mod.intent(pr_info).get("expect")
    f = {"repo": bundle.REPOS[sides["changed"]].slug, "pr": pr_info, "intent": report_mod.intent(pr_info),
         "title": (pr_info or {}).get("title")
         or f"WalletConnect: {changed} {report_mod.short_ref(sides['head'][sides['changed']])}",
         "sides": report_mod.sides_header(sides),
         "scope": f"a dapp signs through the BitBoxApp over WalletConnect, with {report_mod.sides_fixed(sides)}, "
                  f"{changed} before and after this PR",
         "broke": [], "fixed": [], "changes": [], "minor": [], "known": []}

    def rows(name):
        b, h = rb.get(name, {}), rh.get(name, {})
        out_rows, app_changed = [], False
        for label in dict.fromkeys([s["label"] for s in b.get("app", [])] + [s["label"] for s in h.get("app", [])]):
            ib = next((s["file"] for s in b.get("app", []) if s["label"] == label), None)
            ih = next((s["file"] for s in h.get("app", []) if s["label"] == label), None)
            if ib and ih and images_equal(out / "base/app" / ib, out / "head/app" / ih):
                continue
            app_changed = True
            out_rows.append({"label": f"App: {label}", "before": f"base/app/{ib}" if ib else None,
                             "after": f"head/app/{ih}" if ih else None, "photo": True})
        dev = report_mod.frame_compare(frame_rows(step_frames(fb, name), step_frames(fh, name), out / "base/device",
                                                  out / "head/device", diffdir), out, "base/device", "head/device")
        return dev, out_rows, app_changed

    local = (out / "head" / "chains" / "etherscan.jsonl").exists()
    not_built = meta["base"].get("build_error") or meta["head"].get("build_error")
    tested = []
    for name in names:
        b, h = rb.get(name), rh.get(name)
        rec = h or b
        chain_id = (rec.get("chainId") or "?").split(":")[-1]
        what = (f"`{rec['method']}` on {CHAIN_NAMES.get(chain_id, f'chain {chain_id}')} (`{name}`)"
                if rec.get("method") else f"`{name}`")
        tested.append(f"{what}: before {dapp_text(b) if b else 'not run'}; after {dapp_text(h) if h else 'not run'}.")
        if not_built:  # its steps did not run: nothing to compare them with
            continue
        dev, app_rows, app_changed = rows(name)
        if dapp_ok(b) and not dapp_ok(h):
            chain = (rec.get("chainId") or "").split(":")[-1]
            errors = [e for e in proxy_errors(out / "head", chain) if e not in proxy_errors(out / "base", chain)]
            f["broke"].append({"title": f"WalletConnect request {what} fails",
                               "detail": f"Before this PR the dapp got: {dapp_text(b)}. With it: {dapp_text(h)}."
                                         + (" Requests of the app to the Etherscan proxy that fail only with this "
                                            "PR: " + "; ".join(errors) + "." if errors else ""),
                               "compare": dev + app_rows})
        elif b and h and not dapp_ok(b) and dapp_ok(h):
            f["fixed"].append(f"WalletConnect request {what}: before this PR {dapp_text(b)}; with it {dapp_text(h)}.")
        elif b and h and not dapp_ok(b) and not dapp_ok(h):
            f["known"].append(f"WalletConnect request {what} fails before and after this PR: {dapp_text(h)}")
        elif dev and (local or rec.get("method") not in TX_METHODS):
            f["changes"].append({"title": f"Device screens changed for WalletConnect request {what}", "where": [name],
                                 "expected": report_mod.is_expected([name], globs), "compare": dev + app_rows})
        elif dev or app_changed:
            f["minor"].append({"title": f"`{name}`", "compare": dev + app_rows})
    eb, eh = meta["base"].get("error"), meta["head"].get("error")
    if eh and not eb:
        f["broke"].append({"title": "The BitBoxApp backend does not build" if meta["head"].get("build_error")
                           else "The WalletConnect test fails", "detail": "It passed before this PR.", "excerpt": eh})
    elif eb and eh:
        f["known"].append(f"The WalletConnect test fails before and after this PR: {eh.splitlines()[0][:160]}")
    elif eb:
        f["fixed"].append(f"The WalletConnect test passes; before this PR: {eb.splitlines()[0][:160]}")
    f["tested"] = [
        "A test dapp pairs with the BitBoxApp over the public WalletConnect relay and sends signing requests. "
        f"The app approves each one, and the graphical simulator confirms it on the device. {changed} is "
        f"before and after this PR; {report_mod.sides_fixed(sides)} on both sides.",
        "The app's Etherscan proxy is a local stand-in in front of local test chains, with the real proxy's "
        "request rules; the dapp checks that each sent transaction is on the chain." if local else
        "Transactions take their gas price from the live Etherscan proxy, so their fee screens differ on every "
        "run: their differences are listed under pixel-only differences.",
        *tested]
    f["details"] = [*report_mod.sides_details(sides),
                    ("simulator", meta["base"]["simulator_version"], meta["head"]["simulator_version"])]
    f["notes"] = ["Logs: */playwright.log, */device.log, */dapp.log."]
    page = report_mod.render(out, f)
    log(f"report: {page}")
    print(page)
    return f


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
            ok = all(files) and all(images_equal(files[0], f) for f in files[1:])
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
    r.add_argument("--firmware")
    sub.add_parser("same").add_argument("dirs", nargs="+")
    sub.add_parser("report").add_argument("dir")
    args = p.parse_args()
    if args.cmd == "run":
        bundle.use_node()
        ensure_dapp()
        out = Path(args.out)
        shutil.rmtree(out, ignore_errors=True)
        run_side(args.ref, build_sim(args.firmware or bundle.default_firmware_ref()), out)
    elif args.cmd == "same":
        same(args.dirs)
    else:
        report(args.dir)


if __name__ == "__main__":
    main()
