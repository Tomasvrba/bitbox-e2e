#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pillow>=10", "python-xlib>=0.33"]
# ///
"""BitBox simulator e2e: run a scenario against base/head firmware and report the device diff.

  e2e.py compare --scenario scenarios/x.json --base <ref> --head <ref|WORKTREE> --out <dir>
  e2e.py device  --scenario scenarios/x.json --sim-dir <dir> --out <dir>/<side>
  e2e.py report  <dir>

Each side records two views of the same scenario:
  headless/  C simulator, auto-accepts everything, prints every screen as text (exact, diffable)
  graphical/ graphical simulator on Xvfb, driven by XTest, one PNG per distinct LCD frame
"""

import argparse
import base64
import difflib
import html
import json
import os
import queue
import re
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

E2E_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(E2E_DIR))

DRIVER = E2E_DIR / "driver" / "target" / "release" / "bitbox-e2e-driver"
SIM_PORT = 15423  # simulator-graphical cannot change it, so both views use it, sequentially.


def log(msg):
    print(f"[e2e] {msg}", file=sys.stderr, flush=True)


def ensure_driver():
    log("building driver")
    subprocess.run(["cargo", "build", "--release", "-q"], cwd=E2E_DIR / "driver", check=True)


def wait_port_free(port, timeout_s=30):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        with socket.socket() as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                s.bind(("127.0.0.1", port))
                return
            except OSError:
                time.sleep(0.2)
    raise RuntimeError(f"port {port} still busy; is another simulator running?")


QUIET_S = 0.3


def start_driver(scenario, stderr):
    return subprocess.Popen([str(DRIVER), str(scenario), f"127.0.0.1:{SIM_PORT}"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=stderr, text=True,
                            env=dict(os.environ, E2E_HANDSHAKE="1"))


def pump(stream, tag, q):
    for line in iter(stream.readline, ""):
        q.put((time.time(), tag, line.rstrip("\n")))
    q.put((time.time(), tag, None))


# --- headless -------------------------------------------------------------------------------------

SCREEN_START = re.compile(r"^(.*) SCREEN START$")
SCREEN_END = re.compile(r"^(.*) SCREEN END$")
FIELD = re.compile(r"^([A-Z]+): ?(.*)$")


class ScreenParser:
    """Collects the `<KIND> SCREEN START ... END` blocks the headless simulator prints."""

    def __init__(self):
        self.screens, self.cur, self.field = [], None, None

    def feed(self, line, step):
        if m := SCREEN_START.match(line):
            self.cur, self.field = {"step": step, "kind": m.group(1), "fields": {}}, None
        elif self.cur and SCREEN_END.match(line):
            self.screens.append(self.cur)
            self.cur = None
        elif self.cur is not None:
            if m := FIELD.match(line):
                self.field = m.group(1)
                self.cur["fields"][self.field] = m.group(2)
            elif self.field:
                self.cur["fields"][self.field] += "\n" + line


def run_headless(sim_dir, scenario, out):
    out.mkdir(parents=True, exist_ok=True)
    wait_port_free(SIM_PORT)
    # The headless sim keeps no state unless FAKE_MEMORY_FILEPATH is set; it boots unseeded.
    env = {k: v for k, v in os.environ.items() if k != "FAKE_MEMORY_FILEPATH"}
    sim = subprocess.Popen(["stdbuf", "-oL", str(sim_dir / "simulator"), "--port", str(SIM_PORT)],
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
    q = queue.Queue()
    threading.Thread(target=pump, args=(sim.stdout, "sim", q), daemon=True).start()
    while True:
        _, _, line = q.get(timeout=20)
        if line is None or line.startswith("Listening on port"):
            break
    drv = start_driver(scenario, stderr=subprocess.PIPE)
    threading.Thread(target=pump, args=(drv.stdout, "driver", q), daemon=True).start()
    threading.Thread(target=pump, args=(drv.stderr, "driver-stderr", q), daemon=True).start()

    combined, events = [], []
    parser = ScreenParser()
    state = {"step": "connect"}

    open_streams = 2
    deadline = time.time() + 120
    pending_go = False
    while open_streams and time.time() < deadline:
        try:
            _, tag, line = q.get(timeout=QUIET_S if pending_go else 1)
        except queue.Empty:
            if pending_go:
                # Simulator output for the previous step has drained; release the driver.
                state["step"] = next_step
                drv.stdin.write("go\n")
                drv.stdin.flush()
                pending_go = False
            elif drv.poll() is not None:
                break
            continue
        if line is None:
            open_streams -= tag.startswith("driver")
            continue
        combined.append(f"{tag:>13} | {line}")
        if tag == "driver":
            ev = json.loads(line)
            events.append(ev)
            if ev.get("event") == "step-start":
                pending_go = True
                next_step = ev["step"]
        elif tag == "sim":
            parser.feed(line, state["step"])
    drv.wait(10)
    sim.terminate()
    sim.wait(5)
    (out / "combined.log").write_text("\n".join(combined) + "\n")
    (out / "driver.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    screens = parser.screens
    (out / "screens.json").write_text(json.dumps(screens, indent=2))
    log(f"headless: {len(screens)} screens, driver exit {drv.returncode}")
    return drv.returncode == 0


# --- graphical ------------------------------------------------------------------------------------

def run_graphical(sim_dir, scenario, out, disp):
    import gsim

    out.mkdir(parents=True, exist_ok=True)
    for f in out.glob("*.png"):
        f.unlink()
    wait_port_free(SIM_PORT)
    memdir = out / "fake-memory"
    shutil.rmtree(memdir, ignore_errors=True)
    memdir.mkdir()
    xvfb = gsim.Xvfb(disp, out / "xvfb.log")
    sim = drv = None
    frames, events = [], []
    try:
        sim = gsim.GraphicalSim(str(sim_dir / "simulator-graphical"), disp, str(out / "simulator.log"),
                                str(memdir))
        step = "boot"

        def capture(h):
            name = f"{len(frames):02d}-{re.sub(r'[^a-z0-9]+', '-', step.lower())}.png"
            sim.lcd_image().save(out / name)
            frames.append({"file": name, "step": step, "hash": h})

        capture(sim.wait_stable())
        sim.tap("bottom", 128)  # orientation screen: "Tap this side"
        capture(sim.wait_stable())

        def spawn():
            q = queue.Queue()
            d = start_driver(scenario, stderr=subprocess.STDOUT)
            threading.Thread(target=pump, args=(d.stdout, "driver", q), daemon=True).start()
            return d, q

        drv, q = spawn()
        paired, retries = False, 0

        # Policy for an unknown screen once it is stable: tap the top-right control (accept/next),
        # then try the hold-to-confirm gesture. Never touch top-left, which rejects.
        actions = [lambda: sim.tap("top", 255), lambda: sim.pinch(3.0)]
        last, tried = frames[-1]["hash"], 0
        deadline = time.time() + 180
        done = False
        while time.time() < deadline:
            while not q.empty():
                _, _, line = q.get()
                if line is None:
                    drv.wait(5)
                    # The simulator accepts a client only after its startup task finished and
                    # drops earlier connections, so a driver dying before pairing is retried.
                    if drv.returncode != 0 and not paired and retries < 20:
                        retries += 1
                        log("graphical: simulator not accepting yet, retrying")
                        time.sleep(1)
                        drv, q = spawn()
                        break
                    done = True
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                events.append(ev)
                if ev.get("event") == "step-end" and ev.get("step") == "pairing":
                    paired = True
                if ev.get("event") == "step-start":
                    step = ev["step"]
                    drv.stdin.write("go\n")
                    drv.stdin.flush()
                if ev.get("event") == "done":
                    done = True
            h = sim.wait_stable(settle_s=0.5)
            if h != last:
                capture(h)
                last, tried = h, 0
                continue
            if done:
                break
            if tried < len(actions):
                actions[tried]()
                tried += 1
            else:
                time.sleep(0.5)  # waiting on the host, or nothing we know how to press
        if drv.poll() is None:
            drv.kill()
        drv.wait(5)
        # Final state after the last step (e.g. "Transaction confirmed" toast fading out).
        h = sim.wait_stable(settle_s=1.0)
        if h != last:
            capture(h)
    finally:
        if sim:
            sim.stop()
        xvfb.stop()
        shutil.rmtree(memdir, ignore_errors=True)
    (out / "frames.json").write_text(json.dumps(frames, indent=2))
    (out / "driver.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    log(f"graphical: {len(frames)} frames, driver exit {drv.returncode if drv else None}")
    return drv is not None and drv.returncode == 0


def cmd_device(args):
    sim_dir, out = Path(args.sim_dir), Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    meta = {
        "label": args.label or out.name,
        "source": (sim_dir / "source.txt").read_text().strip() if (sim_dir / "source.txt").exists() else "",
        "simulator_version": (sim_dir / "version.txt").read_text().strip()
        if (sim_dir / "version.txt").exists() else "",
    }
    meta["headless_ok"] = run_headless(sim_dir, Path(args.scenario), out / "headless")
    meta["graphical_ok"] = None if args.no_graphical else run_graphical(
        sim_dir, Path(args.scenario), out / "graphical", os.environ.get("E2E_DISPLAY", ":77"))
    (out / "meta.json").write_text(json.dumps(meta, indent=2))
    return meta


def cmd_compare(args):
    ensure_driver()
    out = Path(args.out)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    shutil.copy(args.scenario, out / "scenario.json")
    for side, ref in (("base", args.base), ("head", args.head)):
        log(f"{side}: building simulators for {ref}")
        res = subprocess.run([str(E2E_DIR / "build-sim.sh"), ref], stdout=subprocess.PIPE, text=True, check=True)
        sim_dir = res.stdout.strip().splitlines()[-1]
        cmd_device(argparse.Namespace(scenario=args.scenario, sim_dir=sim_dir, out=str(out / side),
                                      label=f"{side}: {ref}", no_graphical=args.no_graphical))
    report(out)


# --- wallet app (Playwright) ----------------------------------------------------------------------

WALLET_REPO = Path(os.environ.get("BITBOX_WALLET_APP", E2E_DIR.parent / "bitbox-wallet-app"))
# Volatile device screens in app tests: random pairing codes and the current time.
VOLATILE_TITLES = {"Pairing code", "Is now?"}


def wallet_clone(ref):
    """Clean clone of the app at ref. Kept between runs (keyed by sha) to reuse node_modules."""
    sha = subprocess.run(["git", "-C", str(WALLET_REPO), "rev-parse", "--verify", f"{ref}^{{commit}}"],
                         stdout=subprocess.PIPE, text=True, check=True).stdout.strip()
    src = Path(os.environ["BITBOX_E2E_CACHE"]) / "src" / f"bitbox-wallet-app-{sha}"
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
    log(f"wallet {sha[:9]}: playwright {' '.join(tests)}")
    with open(out / "playwright.log", "w") as f:
        subprocess.run(["npx", "playwright", "test", "-c", "playwright.evidence.config.ts", *tests],
                       cwd=web, env=env, stdout=f, stderr=subprocess.STDOUT, timeout=1800)
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
    out = Path(args.out)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    res = subprocess.run([str(E2E_DIR / "build-sim.sh"), args.firmware], stdout=subprocess.PIPE, text=True, check=True)
    sim_dir = res.stdout.strip().splitlines()[-1]
    for side, ref in (("base", args.base), ("head", args.head)):
        (out / side).mkdir()
        run_wallet(ref, sim_dir, args.tests, out / side)
    wallet_report(out)


def wallet_report(out):
    out = Path(out)
    meta = {s: json.loads((out / s / "meta.json").read_text()) for s in ("base", "head")}
    tb = {t["title"]: t for t in json.loads((out / "base/tests.json").read_text())}
    th = {t["title"]: t for t in json.loads((out / "head/tests.json").read_text())}
    md = ["# BitBoxApp e2e diff", "", "| | base | head |", "|---|---|---|",
          f"| source | {meta['base']['source']} | {meta['head']['source']} |",
          f"| simulator | {meta['base']['simulator']} | {meta['head']['simulator']} |",
          f"| passed | {meta['base']['passed']}/{meta['base']['total']} | {meta['head']['passed']}/{meta['head']['total']} |"]
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


def images_equal(a, b):
    from PIL import Image, ImageChops
    return ImageChops.difference(Image.open(a).convert("RGB"), Image.open(b).convert("RGB")).getbbox() is None


def fmt_status(t):
    st = t.get("status")
    return "-" if st is None else ("pass" if st == "passed" else f"**{st.upper()}**")


def strip_ansi(s):
    return re.sub(r"\x1b\[[0-9;]*m", "", s)


# --- report ---------------------------------------------------------------------------------------

# Device setup done by the driver. Pairing codes and the "Is now?" clock differ on every run, so
# these steps are left out of the diffs and only appear in the full filmstrips.
SETUP_STEPS = {"boot", "connect", "pairing", "seed"}


def screen_lines(screens):
    """One line per screen, grouped by driver step. Bodies keep their newlines escaped."""
    lines = []
    for s in screens:
        if s["step"] in SETUP_STEPS:
            continue
        f = " | ".join(f"{k}={v!r}" for k, v in s["fields"].items())
        lines.append(f"[{s['step']}] {s['kind']}: {f}")
    return lines


def results(side_dir):
    p = side_dir / "headless" / "driver.jsonl"
    rows = {}
    if p.exists():
        for line in p.read_text().splitlines():
            ev = json.loads(line)
            if ev.get("event") == "step-end":
                rows[ev["step"]] = ("error: " + ev["error"]) if "error" in ev else "ok"
    return rows


def frame_ops(base_frames, head_frames):
    a = [f["hash"] for f in base_frames]
    b = [f["hash"] for f in head_frames]
    assert all(f["step"] not in SETUP_STEPS for f in base_frames + head_frames)
    return difflib.SequenceMatcher(a=a, b=b, autojunk=False).get_opcodes()


def pixel_diff(a_path, b_path, out_path):
    from PIL import Image, ImageChops
    a, b = Image.open(a_path).convert("RGB"), Image.open(b_path).convert("RGB")
    if a.size != b.size:
        return None
    mask = ImageChops.difference(a, b).convert("L").point(lambda v: 255 if v else 0)
    red = Image.new("RGB", a.size, (255, 40, 40))
    Image.composite(red, b.point(lambda v: v // 3), mask).save(out_path)
    return out_path


def report(out):
    out = Path(out)
    scenario = json.loads((out / "scenario.json").read_text())
    meta = {s: json.loads((out / s / "meta.json").read_text()) for s in ("base", "head")}
    md = [f"# BitBox e2e diff: `{scenario_name(out)}`", ""]
    if scenario.get("description"):
        md += [scenario["description"], ""]
    md += ["| | base | head |", "|---|---|---|"]
    md.append(f"| source | {meta['base']['source']} | {meta['head']['source']} |")
    md.append(f"| simulator | {meta['base']['simulator_version']} | {meta['head']['simulator_version']} |")
    for k in ("headless_ok", "graphical_ok"):
        md.append(f"| {k.replace('_ok', '')} run | {fmt_ok(meta['base'][k])} | {fmt_ok(meta['head'][k])} |")
    rb, rh = results(out / "base"), results(out / "head")
    for step in dict.fromkeys(list(rb) + list(rh)):
        md.append(f"| step `{step}` | {rb.get(step, '-')} | {rh.get(step, '-')} |")
    md.append("")

    # Text transcript: exact firmware strings, the authoritative diff.
    sb = screen_lines(json.loads((out / "base/headless/screens.json").read_text()))
    sh = screen_lines(json.loads((out / "head/headless/screens.json").read_text()))
    diff = list(difflib.unified_diff(sb, sh, "base", "head", lineterm="", n=1))
    md += ["## Device screens (headless simulator transcript)", ""]
    if diff:
        md += ["```diff", *diff, "```", ""]
    else:
        md += [f"No change ({len(sb)} screens).", ""]

    # Screenshots: show only frames that differ, aligned by content hash.
    gb, gh = out / "base/graphical", out / "head/graphical"
    if (gb / "frames.json").exists() and (gh / "frames.json").exists():
        all_b, all_h = (json.loads((g / "frames.json").read_text()) for g in (gb, gh))
        fb = [f for f in all_b if f["step"] not in SETUP_STEPS]
        fh = [f for f in all_h if f["step"] not in SETUP_STEPS]
        md += ["## Device screenshots (graphical simulator)", ""]
        rows = []
        diffdir = out / "diff"
        diffdir.mkdir(exist_ok=True)
        for tag, i1, i2, j1, j2 in frame_ops(fb, fh):
            if tag == "equal":
                continue
            n = max(i2 - i1, j2 - j1)
            for k in range(n):
                a = fb[i1 + k] if i1 + k < i2 else None
                b = fh[j1 + k] if j1 + k < j2 else None
                d = None
                if a and b:
                    d = pixel_diff(gb / a["file"], gh / b["file"], diffdir / f"{a['file'][:-4]}--{b['file']}")
                rows.append((a, b, d))
        if not rows:
            md += [f"No change ({len(fb)} frames).", ""]
        else:
            md += ["| step | base | head | changed pixels |", "|---|---|---|---|"]
            for a, b, d in rows:
                step = (b or a)["step"]
                ca = f"![](base/graphical/{a['file']})" if a else "*(absent)*"
                cb = f"![](head/graphical/{b['file']})" if b else "*(absent)*"
                cd = f"![]({d.relative_to(out)})" if d else ""
                md.append(f"| `{step}` | {ca} | {cb} | {cd} |")
            md += ["", f"{len(rows)} differing frame(s); {len(fb)} base / {len(fh)} head frames total.", ""]
        for side, frames in (("base", all_b), ("head", all_h)):
            md += [f"<details><summary>All {side} frames</summary>", ""]
            md += [" ".join(f"![{f['step']}]({side}/graphical/{f['file']})" for f in frames), "", "</details>", ""]

    (out / "report.md").write_text("\n".join(md))
    (out / "report.html").write_text(md_to_html(out, md))
    log(f"report: {out / 'report.md'}")
    print(out / "report.md")


def scenario_name(out):
    return json.loads((out / "scenario.json").read_text()).get("name") or out.name


def fmt_ok(v):
    return "skipped" if v is None else ("pass" if v else "**FAIL**")


def md_to_html(out, md):
    """Tiny renderer for the subset report() emits, with images inlined so the file stands alone."""
    def img(m):
        data = base64.b64encode((out / m.group(2)).read_bytes()).decode()
        return f'<img title="{html.escape(m.group(1))}" src="data:image/png;base64,{data}">'

    def inline(s):
        s = html.escape(s, quote=False)
        s = re.sub(r"!\[([^\]]*)\]\(([^)]+)\)", img, s)
        s = re.sub(r"\*\*([^*]+)\*\*", r"<b>\1</b>", s)
        s = re.sub(r"\*([^*]+)\*", r"<i>\1</i>", s)
        return re.sub(r"`([^`]+)`", r"<code>\1</code>", s)

    body, i = [], 0
    while i < len(md):
        line = md[i]
        if line.startswith("```"):
            j = md.index("```", i + 1)
            rows = []
            for l in md[i + 1:j]:
                cls = {"+": "add", "-": "del", "@": "hunk"}.get(l[:1], "")
                rows.append(f'<span class="{cls}">{html.escape(l)}</span>')
            body.append("<pre>" + "\n".join(rows) + "</pre>")
            i = j + 1
            continue
        if line.startswith("|"):
            rows = []
            while i < len(md) and md[i].startswith("|"):
                if not re.match(r"^\|[-| ]+\|$", md[i]):
                    rows.append(md[i])
                i += 1
            cells = [[inline(c.strip()) for c in r.strip("|").split("|")] for r in rows]
            t = "<tr>" + "".join(f"<th>{c}</th>" for c in cells[0]) + "</tr>"
            t += "".join("<tr>" + "".join(f"<td>{c}</td>" for c in r) + "</tr>" for r in cells[1:])
            body.append(f"<table>{t}</table>")
            continue
        if line.startswith("<"):
            body.append(line if "summary" in line or "details" in line else html.escape(line))
        elif line.startswith("## "):
            body.append(f"<h2>{inline(line[3:])}</h2>")
        elif line.startswith("# "):
            body.append(f"<h1>{inline(line[2:])}</h1>")
        elif line:
            body.append(f"<p>{inline(line)}</p>")
        i += 1
    css = ("body{font:14px system-ui,sans-serif;max-width:1100px;margin:2em auto;padding:0 1em}"
           "table{border-collapse:collapse;margin:1em 0}td,th{border:1px solid #ccc;padding:4px 8px;"
           "vertical-align:top}img{image-rendering:pixelated;margin:2px;border:1px solid #ddd}"
           "pre{background:#f6f8fa;padding:1em;overflow-x:auto}.add{color:#116329;background:#dafbe1}"
           ".del{color:#82071e;background:#ffebe9}.hunk{color:#8250df}")
    return f"<!doctype html><meta charset=utf-8><title>BitBox e2e diff</title><style>{css}</style>\n" + "\n".join(body)


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("compare")
    c.add_argument("--scenario", required=True)
    c.add_argument("--base", required=True)
    c.add_argument("--head", required=True)
    c.add_argument("--out", required=True)
    c.add_argument("--no-graphical", action="store_true")
    d = sub.add_parser("device")
    d.add_argument("--scenario", required=True)
    d.add_argument("--sim-dir", required=True)
    d.add_argument("--out", required=True)
    d.add_argument("--label")
    d.add_argument("--no-graphical", action="store_true")
    w = sub.add_parser("wallet-compare")
    w.add_argument("--base", required=True, help="bitbox-wallet-app ref")
    w.add_argument("--head", required=True, help="bitbox-wallet-app ref")
    w.add_argument("--firmware", required=True, help="firmware ref (or WORKTREE) for the simulator")
    w.add_argument("--out", required=True)
    w.add_argument("tests", nargs="+", help="Playwright test files, relative to frontends/web")
    r = sub.add_parser("report")
    r.add_argument("dir")
    args = p.parse_args()
    if args.cmd == "compare":
        cmd_compare(args)
    elif args.cmd == "wallet-compare":
        cmd_wallet_compare(args)
    elif args.cmd == "device":
        ensure_driver()
        cmd_device(args)
    else:
        report(args.dir)


if __name__ == "__main__":
    main()
