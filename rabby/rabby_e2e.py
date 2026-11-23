#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pillow>=10", "python-xlib>=0.33"]
# ///
"""Rabby extension -> BitBoxBridge (fake) -> graphical simulator: firmware or Rabby base vs head.

  e2e.py rabby --base <fw-ref> --head <fw-ref> --out <dir> [--rabby-ref v0.94.15]
  e2e.py pr --repo rabby [--base <rabby-ref>] --head <rabby-ref> --out <dir> [--pin firmware=<ref>]
  rabby/rabby_e2e.py repeat --firmware <fw-ref> -n 3 --out <dir>    same flow N times, frame identity
  rabby/rabby_e2e.py record --firmware <fw-ref> --out <dir>         re-record fixtures/rabby-api.json.gz
  rabby/rabby_e2e.py report <dir>

Builds outside the netns (Rabby, simulators, node/uv caches), runs each flow in isolated.sh.
"""

import argparse
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

RABBY_DIR = Path(__file__).resolve().parent
E2E_DIR = RABBY_DIR.parent
sys.path.insert(0, str(E2E_DIR))

import bundle  # noqa: E402
from bundle import build_sim  # noqa: E402
from rabby.run import SETUP_STEPS  # noqa: E402
from report import fmt_ok, frame_rows, md_to_html  # noqa: E402

RABBY_REPO = bundle.REPOS["rabby"].dir
DEFAULT_REF = "v0.94.15"


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
    """Unpacked chrome-mv3 build of Rabby at ref, cached by commit in rabby-dist/<sha>. All refs are
    built in one --shared clone (the working checkout is never touched), so node_modules exists
    once: two clones of it nearly filled a CI runner. A build takes ~10 min."""
    sha = bundle.resolve(ref, "rabby")
    dist = bundle.CACHE / "rabby-dist" / sha
    if (dist / "manifest.json").exists():
        return dist
    src = bundle.CACHE / "src" / "rabby"
    if not (src / ".git").exists():
        subprocess.run(["git", "clone", "-q", "--shared", "--no-checkout", str(RABBY_REPO), str(src)], check=True)
    # -f: a build rewrites tracked files (e.g. the theme it generates).
    subprocess.run(["git", "-C", str(src), "checkout", "-q", "-f", "--detach", sha], check=True)
    shutil.rmtree(src / "dist", ignore_errors=True)
    yarn = re.search(r"^yarnPath:\s*(\S+)", (src / ".yarnrc.yml").read_text(), re.M).group(1)
    log(f"building Rabby {ref} ({sha[:9]}) in {src}")
    for script in ("install", "build:pro"):
        subprocess.run(["node", yarn, script], cwd=src, check=True, stdout=sys.stderr)
    tmp = dist.with_name(sha + ".tmp")
    shutil.rmtree(tmp, ignore_errors=True)
    shutil.copytree(src / "dist", tmp)
    tmp.rename(dist)
    return dist


def ensure_tools():
    """Everything the netns run needs that would otherwise hit the network. Fails right away where
    the flow can't run at all."""
    bundle.use_node()
    # run.py listens on fixed ports (bridge, anvils, proxy, dapp) and must stay offline.
    if not bundle.namespaces():
        raise SystemExit("the Rabby flow runs in isolated.sh, which needs network namespaces; unavailable here")
    if not (RABBY_DIR / "node_modules" / "playwright").exists():
        subprocess.run(["npm", "ci", "--ignore-scripts", "--no-audit", "--no-fund"], cwd=RABBY_DIR, check=True, stdout=sys.stderr)
    subprocess.run(["npx", "playwright", "install", "chromium"], cwd=RABBY_DIR, check=True, stdout=sys.stderr)
    # uv caches script environments; populate them while we have network.
    for cmd in ([str(E2E_DIR / "bridge" / "sim_bridge.py"), "--help"],
                [str(RABBY_DIR / "mitmdump.py"), "--version"],
                [str(RABBY_DIR / "run.py"), "--help"]):
        subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL)
    anvil = bundle.TOOLS / "foundry" / "bin" / "anvil"
    if not anvil.exists():
        raise SystemExit(f"anvil missing at {anvil}: run e2e/setup.sh")


def run_flow(sim_dir, dist, out, record_sock=None):
    cmd = [str(E2E_DIR / "isolated.sh"), str(RABBY_DIR / "run.py"), "--sim-dir", str(sim_dir),
           "--rabby-dist", str(dist), "--out", str(out)]
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
    sim_dir = build_sim(args.firmware)
    out = Path(args.out)
    port = 18089
    if not free_port(port):
        raise SystemExit(f"127.0.0.1:{port} busy")
    fixture = out / "rabby-api.json.gz"
    out.mkdir(parents=True, exist_ok=True)
    fixture.unlink(missing_ok=True)
    # In a private directory: a predictable /tmp name could be claimed by another user first.
    sock = str(Path(tempfile.mkdtemp(prefix="bitbox-e2e-rabby-")) / "proxy.sock")
    procs = [subprocess.Popen([str(RABBY_DIR / "mitmdump.py"), "--listen-host",
                               "127.0.0.1", "-p", str(port), "-s", str(RABBY_DIR / "api_proxy.py"),
                               "--set", f"fixture={fixture}", "--set", "record=true"],
                              stdout=open(out / "record-proxy.log", "w"), stderr=subprocess.STDOUT),
             subprocess.Popen([sys.executable, str(RABBY_DIR / "egress.py"), "unix-to-tcp", sock, f"127.0.0.1:{port}"])]
    try:
        time.sleep(3)
        ok = run_flow(sim_dir, dist, out / "run", record_sock=sock)
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
    """Firmware base vs head through one Rabby build."""
    return compare({"base": (args.base, args.rabby_ref), "head": (args.head, args.rabby_ref)}, Path(args.out))


def cmd_rabby_change(args, firmware=None):
    """`e2e.py pr --repo rabby`: Rabby at the merge-base vs head, on the pinned firmware or upstream master."""
    head = bundle.resolve(args.head, "rabby")
    base = bundle.git("merge-base", bundle.resolve(args.base, "rabby"), head, cwd=RABBY_REPO)
    firmware = firmware or bundle.resolve(bundle.default_firmware_ref())
    return compare({"base": (firmware, base), "head": (firmware, head)}, Path(args.out))


def compare(sides, out):
    """sides: {"base"|"head": (firmware ref, Rabby ref)}. Exits 1 if the flow failed on either."""
    ensure_tools()
    dists = {side: ensure_rabby(rabby) for side, (_, rabby) in sides.items()}
    sims = {side: build_sim(fw) for side, (fw, _) in sides.items()}
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    failed = []
    for side in ("base", "head"):
        if not run_flow(sims[side], dists[side], out / side):
            failed.append(side)
        m = json.loads((out / side / "meta.json").read_text()) if (out / side / "meta.json").exists() else {}
        m["label"] = f"{side}: {sides[side][0]}"
        m["rabby_ref"] = sides[side][1]
        (out / side).mkdir(exist_ok=True)
        (out / side / "meta.json").write_text(json.dumps(m, indent=2))
    report(out)
    if failed:
        log(f"flow failed on {', '.join(failed)}")
    return 1 if failed else 0


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
    sim_dir = build_sim(args.firmware)
    out = Path(args.out)
    shutil.rmtree(out, ignore_errors=True)
    runs = []
    for i in range(args.n):
        t = time.time()
        ok = run_flow(sim_dir, dist, out / f"run{i}")
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
    changed = "Rabby" if b["meta"].get("rabby_ref") != h["meta"].get("rabby_ref") else "firmware"
    md = [f"# BitBox e2e: Rabby extension, {changed} base vs head", "",
          "Unmodified Rabby talks to the graphical simulator through a fake BitBoxBridge "
          "(bridge/sim_bridge.py); Rabby's backend is replayed from rabby/fixtures, chains are local anvils.", "",
          "| | base | head |", "|---|---|---|"]
    for k, label in (("label", "firmware"), ("source", "source"), ("simulator_version", "simulator"),
                     ("rabby_ref", "Rabby"), ("seconds", "run time (s)")):
        md.append(f"| {label} | {b['meta'].get(k, '-')} | {h['meta'].get(k, '-')} |")
    md.append(f"| flow | {fmt_ok(b['meta'].get('ok'))} | {fmt_ok(h['meta'].get('ok'))} |")
    md.append("")

    sections, summary = [], ["| step | base | head | device frames (base/head) | changed |", "|---|---|---|---|---|"]
    for step in steps:
        fb = [f for f in b["frames"] if f["step"] == step]
        fh = [f for f in h["frames"] if f["step"] == step]
        rows, changed = [], 0
        setup = step in SETUP_STEPS
        if not setup:
            for tag, fa, fc, d in frame_rows(fb, fh, out / "base", out / "head", diffdir):
                changed += tag != "equal"
                rows.append((fa, fc, "same" if tag == "equal" else f"![]({d.relative_to(out)})" if d else "**changed**"))
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
    (out / "report.html").write_text(md_to_html(out, md))
    log(f"report: {out / 'report.html'}")
    print(out / "report.html")


def add_common(p):
    p.add_argument("--rabby-ref", default=DEFAULT_REF, help="Rabby git ref (tag), built from a clone of $BITBOX_RABBY")


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
