#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pillow>=10", "python-xlib>=0.33"]
# ///
"""Rabby extension -> BitBoxBridge (fake) -> graphical simulator: firmware base vs head.

  e2e.py rabby --base <fw-ref> --head <fw-ref> --out <dir> [--rabby-ref v0.94.15]
  rabby/rabby_e2e.py repeat --firmware <fw-ref> -n 3 --out <dir>    same flow N times, frame identity
  rabby/rabby_e2e.py record --firmware <fw-ref> --out <dir>         re-record fixtures/rabby-api.json.gz
  rabby/rabby_e2e.py report <dir>

Builds outside the netns (Rabby, simulators, node/uv caches), runs each flow in isolated.sh.
"""

import argparse
import difflib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

RABBY_DIR = Path(__file__).resolve().parent
E2E_DIR = RABBY_DIR.parent
sys.path.insert(0, str(E2E_DIR))

import e2e  # noqa: E402  (pixel_diff, md_to_html, build_sim)

CACHE = Path(os.environ.get("BITBOX_E2E_CACHE", Path.home() / ".cache" / "bitbox-e2e"))
RABBY_REPO = Path(os.environ.get("BITBOX_RABBY", E2E_DIR.parent / "Rabby"))
DEFAULT_REF = "v0.94.15"
SETUP_STEPS = {"boot", "pairing"}


def log(msg):
    print(f"[rabby] {msg}", file=sys.stderr, flush=True)


def rabby_shots_equal(a, b, max_pixels=64):
    """Rabby screenshots, with a few pixels of slack: the green custom-RPC dot next to the chain
    icon anti-aliases differently from run to run (3-9 pixels). Device frames are
    compared exactly elsewhere."""
    from PIL import Image, ImageChops
    ia, ib = Image.open(a).convert("RGB"), Image.open(b).convert("RGB")
    if ia.size != ib.size:
        return False
    mask = ImageChops.difference(ia, ib).convert("L").point(lambda v: 255 if v else 0)
    return mask.histogram()[255] <= max_pixels


def ensure_rabby(ref):
    """Unpacked chrome-mv3 build of Rabby at ref, from a --shared clone (the working checkout is
    never touched). Cached by ref name; a build takes ~10 min."""
    sha = subprocess.run(["git", "-C", str(RABBY_REPO), "rev-parse", "--verify", f"{ref}^{{commit}}"],
                         stdout=subprocess.PIPE, text=True, check=True).stdout.strip()
    src = CACHE / "src" / f"rabby-{re.sub(r'[^A-Za-z0-9._-]', '_', ref)}"
    stamp = src / "dist" / ".e2e-built"
    if stamp.exists() and stamp.read_text().strip() == sha:
        return src / "dist"
    if not (src / ".git").exists():
        subprocess.run(["git", "clone", "-q", "--shared", "--no-checkout", str(RABBY_REPO), str(src)], check=True)
    subprocess.run(["git", "-C", str(src), "checkout", "-q", "--detach", sha], check=True)
    yarn = re.search(r"^yarnPath:\s*(\S+)", (src / ".yarnrc.yml").read_text(), re.M).group(1)
    log(f"building Rabby {ref} ({sha[:9]}) in {src}")
    for script in ("install", "build:pro"):
        subprocess.run(["node", yarn, script], cwd=src, check=True, stdout=sys.stderr)
    stamp.write_text(sha + "\n")
    return src / "dist"


def ensure_tools():
    """Everything the netns run needs that would otherwise hit the network."""
    if not (RABBY_DIR / "node_modules" / "playwright").exists():
        subprocess.run(["npm", "ci", "--no-audit", "--no-fund"], cwd=RABBY_DIR, check=True, stdout=sys.stderr)
    subprocess.run(["npx", "playwright", "install", "chromium"], cwd=RABBY_DIR, check=True, stdout=sys.stderr)
    # uv caches script environments; populate them while we have network.
    for cmd in ([str(E2E_DIR / "bridge" / "sim_bridge.py"), "--help"],
                ["uv", "run", "--no-project", "--with", "mitmproxy==11.0.2", "mitmdump", "--version"],
                [str(RABBY_DIR / "run.py"), "--help"]):
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL)
    anvil = Path(os.environ.get("BITBOX_E2E_TOOLS", Path.home() / ".local/share/bitbox-e2e/tools")) / "foundry/bin/anvil"
    if not anvil.exists():
        raise SystemExit(f"anvil missing at {anvil}: download foundry_stable_linux_amd64.tar.gz from "
                         "github.com/foundry-rs/foundry/releases and unpack it into that bin/ directory")


def run_flow(sim_dir, dist, out, display, browser_display, record_sock=None):
    cmd = [str(E2E_DIR / "isolated.sh"), str(RABBY_DIR / "run.py"), "--sim-dir", str(sim_dir),
           "--rabby-dist", str(dist), "--out", str(out), "--display", display, "--browser-display", browser_display]
    if record_sock:
        cmd += ["--record-sock", record_sock]
    log(f"flow: {out}")
    res = subprocess.run(cmd, env=dict(os.environ, UV_OFFLINE="1"))
    return res.returncode == 0


def free_port(port):
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def cmd_record(args):
    """Record Rabby's API answers for this exact flow. The proxy runs on the host (internet), the
    browser in the netns reaches it through a unix socket (egress.py)."""
    ensure_tools()
    dist = ensure_rabby(args.rabby_ref)
    sim_dir = e2e.build_sim(args.firmware)
    out = Path(args.out)
    port = 18089
    if not free_port(port):
        raise SystemExit(f"127.0.0.1:{port} busy")
    fixture = out / "rabby-api.json.gz"
    out.mkdir(parents=True, exist_ok=True)
    fixture.unlink(missing_ok=True)
    sock = f"/tmp/bitbox-e2e-rabby-{os.getpid()}.sock"
    procs = [subprocess.Popen(["uv", "run", "--no-project", "--with", "mitmproxy==11.0.2", "mitmdump", "--listen-host",
                               "127.0.0.1", "-p", str(port), "-s", str(RABBY_DIR / "api_proxy.py"),
                               "--set", f"fixture={fixture}", "--set", "record=true"],
                              stdout=open(out / "record-proxy.log", "w"), stderr=subprocess.STDOUT),
             subprocess.Popen([sys.executable, str(RABBY_DIR / "egress.py"), "unix-to-tcp", sock, f"127.0.0.1:{port}"])]
    try:
        time.sleep(3)
        ok = run_flow(sim_dir, dist, out / "run", args.display, args.browser_display, record_sock=sock)
    finally:
        for p in procs:
            p.terminate()
            p.wait(10)
        Path(sock).unlink(missing_ok=True)
    if not ok:
        raise SystemExit(f"recording run failed, fixture left at {fixture}")
    shutil.copy(fixture, RABBY_DIR / "fixtures" / "rabby-api.json.gz")
    log(f"fixture: {RABBY_DIR / 'fixtures' / 'rabby-api.json.gz'} ({fixture.stat().st_size} bytes)")


def cmd_rabby(args):
    ensure_tools()
    dist = ensure_rabby(args.rabby_ref)
    sims = {side: e2e.build_sim(ref) for side, ref in (("base", args.base), ("head", args.head))}
    out = Path(args.out)
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    for side in ("base", "head"):
        run_flow(sims[side], dist, out / side, args.display, args.browser_display)
        m = json.loads((out / side / "meta.json").read_text()) if (out / side / "meta.json").exists() else {}
        m["label"] = f"{side}: {getattr(args, side)}"
        m["rabby_ref"] = args.rabby_ref
        (out / side).mkdir(exist_ok=True)
        (out / side / "meta.json").write_text(json.dumps(m, indent=2))
    report(out)


def frame_seq(run_dir):
    p = Path(run_dir) / "frames.json"
    frames = json.loads(p.read_text()) if p.exists() else []
    seq = {}
    for f in frames:
        if f["step"] not in SETUP_STEPS:
            seq.setdefault(f["step"], []).append(f["hash"])
    return seq


def cmd_repeat(args):
    ensure_tools()
    dist = ensure_rabby(args.rabby_ref)
    sim_dir = e2e.build_sim(args.firmware)
    out = Path(args.out)
    shutil.rmtree(out, ignore_errors=True)
    runs = []
    for i in range(args.n):
        t = time.time()
        ok = run_flow(sim_dir, dist, out / f"run{i}", args.display, args.browser_display)
        runs.append({"ok": ok, "seconds": round(time.time() - t, 1), "steps": frame_seq(out / f"run{i}")})
    ref = runs[0]["steps"]
    steps = list(dict.fromkeys(s for r in runs for s in r["steps"]))
    rows = {s: [r["steps"].get(s) == ref.get(s) for r in runs] for s in steps}
    # Rabby approval screenshots: same Rabby, same fixture, so they should match too (dapp-side
    # shots show signatures, which anti-klepto randomizes).
    approvals = sorted(p.name for p in (out / "run0" / "rabby").glob("*-approval.png"))
    shots = {n: [(out / f"run{i}" / "rabby" / n).exists() and
                 rabby_shots_equal(out / "run0" / "rabby" / n, out / f"run{i}" / "rabby" / n) for i in range(args.n)]
             for n in approvals}
    summary = {"firmware": args.firmware, "runs": [{"ok": r["ok"], "seconds": r["seconds"]} for r in runs],
               "rabby_approvals_identical": all(all(v) for v in shots.values()),
               "rabby_approvals": shots,
               "identical": all(all(v) for v in rows.values()) and all(r["ok"] for r in runs),
               "steps": {s: {"frames": [len(r["steps"].get(s, [])) for r in runs], "same_as_run0": v}
                         for s, v in rows.items()}}
    (out / "repeat.json").write_text(json.dumps(summary, indent=1))
    print(json.dumps(summary, indent=1))


# --- report ---------------------------------------------------------------------------------------

def load(side_dir):
    side_dir = Path(side_dir)
    rd = lambda n, d: json.loads((side_dir / n).read_text()) if (side_dir / n).exists() else d  # noqa: E731
    events = [json.loads(l) for l in (side_dir / "events.jsonl").read_text().splitlines()] \
        if (side_dir / "events.jsonl").exists() else []
    results = {}
    for ev in events:
        if ev.get("event") == "step-end":
            results[ev["step"]] = ("error: " + ev["error"].splitlines()[0][:200]) if "error" in ev else "ok"
        elif ev.get("event") == "retry":
            results[ev["step"] + "#retry"] = results.get(ev["step"] + "#retry", 0) + 1
    return {"meta": rd("meta.json", {}), "frames": rd("frames.json", []), "shots": rd("shots.json", []),
            "results": results, "order": [e["step"] for e in events if e.get("event") == "step-start"]}


def report(out):
    out = Path(out)
    b, h = load(out / "base"), load(out / "head")
    diffdir = out / "diff"
    diffdir.mkdir(exist_ok=True)
    steps = list(dict.fromkeys(h["order"] + b["order"]))
    md = ["# BitBox e2e: Rabby extension, firmware base vs head", "",
          "Unmodified Rabby talks to the graphical simulator through a fake BitBoxBridge "
          "(bridge/sim_bridge.py); Rabby's backend is replayed from rabby/fixtures, chains are local anvils.", "",
          "| | base | head |", "|---|---|---|"]
    for k, label in (("label", "firmware"), ("source", "source"), ("simulator_version", "simulator"),
                     ("rabby_ref", "Rabby"), ("seconds", "run time (s)")):
        md.append(f"| {label} | {b['meta'].get(k, '-')} | {h['meta'].get(k, '-')} |")
    md.append(f"| flow | {e2e.fmt_ok(b['meta'].get('ok'))} | {e2e.fmt_ok(h['meta'].get('ok'))} |")
    md.append("")

    sections, summary = [], ["| step | base | head | device frames (base/head) | changed |", "|---|---|---|---|---|"]
    for step in steps:
        fb = [f for f in b["frames"] if f["step"] == step]
        fh = [f for f in h["frames"] if f["step"] == step]
        rows, changed = [], 0
        setup = step in SETUP_STEPS
        ops = [] if setup else difflib.SequenceMatcher(a=[f["hash"] for f in fb], b=[f["hash"] for f in fh], autojunk=False).get_opcodes()
        for tag, i1, i2, j1, j2 in ops:
            for k in range(max(i2 - i1, j2 - j1)):
                fa = fb[i1 + k] if i1 + k < i2 else None
                fc = fh[j1 + k] if j1 + k < j2 else None
                if tag == "equal":
                    rows.append((fa, fc, "same"))
                    continue
                changed += 1
                d = None
                if fa and fc:
                    d = e2e.pixel_diff(out / "base" / fa["file"], out / "head" / fc["file"],
                                       diffdir / f"{Path(fa['file']).stem}--{Path(fc['file']).name}")
                rows.append((fa, fc, f"![]({d.relative_to(out)})" if d else "**changed**"))
        retries = sum(x["results"].get(step + "#retry", 0) for x in (b, h))
        summary.append(f"| `{step}` | {b['results'].get(step, '-')} | {h['results'].get(step, '-')} | "
                       f"{len(fb)}/{len(fh)} | {'(setup, not compared)' if setup else ('**' + str(changed) + '**' if changed else '0')}"
                       f"{f' ({retries} retries)' if retries else ''} |")
        sec = [f"## `{step}`", ""]
        # Dapp-side shots show signatures/tx hashes (anti-klepto nonce), never equal; only Rabby's
        # own screens are compared, and the base one is shown only if it differs.
        heads, cells = [], []
        for shot in [x for x in h["shots"] if x["step"] == step]:
            heads.append(f"{'dapp' if shot['what'] == 'dapp' else 'Rabby'}: {shot['what']}")
            cells.append(f"![{shot['what']}](head/{shot['file']})")
            twin = next((x for x in b["shots"] if x["file"] == shot["file"]), None)
            if twin and shot["what"] != "dapp" and step not in SETUP_STEPS and \
                    not rabby_shots_equal(out / "base" / twin["file"], out / "head" / shot["file"]):
                heads.append(f"**base differs**: {shot['what']}")
                cells.append(f"![{shot['what']}](base/{twin['file']})")
        if cells:
            sec += ["| " + " | ".join(heads) + " |", "|" + "---|" * len(cells), "| " + " | ".join(cells) + " |", ""]
        if setup:
            sec += ["Device (setup: unlock and a random pairing code, not compared):", "",
                    " ".join(f"![]({s}/{f['file']})" for s, fs in (("head", fh),) for f in fs), ""]
        elif rows:
            sec += ["| base | head | |", "|---|---|---|"]
            for fa, fc, st in rows:
                ca = f"![]({'base/' + fa['file']})" if fa else "*(absent)*"
                cc = f"![]({'head/' + fc['file']})" if fc else "*(absent)*"
                sec.append(f"| {ca} | {cc} | {st} |")
            sec.append("")
        else:
            sec += ["No device screens in this step.", ""]
        sections += sec
    md += ["## Steps", ""] + summary + [""] + sections
    (out / "report.md").write_text("\n".join(md))
    (out / "report.html").write_text(e2e.md_to_html(out, md))
    log(f"report: {out / 'report.html'}")
    print(out / "report.html")


def add_common(p):
    p.add_argument("--rabby-ref", default=DEFAULT_REF, help="Rabby git ref (tag), built from a clone of $BITBOX_RABBY")
    p.add_argument("--display", default=os.environ.get("E2E_DISPLAY", ":71"), help="X display for the simulator")
    p.add_argument("--browser-display", default=":73", help="X display for Chromium")


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("compare")
    c.add_argument("--base", required=True)
    c.add_argument("--head", required=True)
    c.add_argument("--out", required=True)
    for name in ("repeat", "record"):
        r = sub.add_parser(name)
        r.add_argument("--firmware", required=True)
        r.add_argument("--out", required=True)
        if name == "repeat":
            r.add_argument("-n", type=int, default=3)
    for x in sub.choices.values():
        add_common(x)
    rp = sub.add_parser("report")
    rp.add_argument("dir")
    args = p.parse_args()
    {"compare": cmd_rabby, "repeat": cmd_repeat, "record": cmd_record,
     "report": lambda a: report(a.dir)}[args.cmd](args)


if __name__ == "__main__":
    main()
