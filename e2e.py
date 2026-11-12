#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pillow>=10", "python-xlib>=0.33"]
# ///
"""BitBox simulator e2e: run scenarios against base/head firmware and report the device diff.

  e2e.py baseline <ref> [--graphical <glob>...] [-j N]       record the bundle for a firmware commit
  e2e.py pr --base <ref> --head <ref|WORKTREE> --out <dir> [--graphical <glob>...] [-j N]
  e2e.py promote <pr-out-dir> <unit-id>...                    head screens become expectations
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
import fnmatch
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

import apis  # noqa: E402
import bundle  # noqa: E402

# E2E_DRIVER points at a prebuilt driver and skips the cargo build (CI artifacts, parallel runs).
DRIVER = Path(os.environ.get("E2E_DRIVER", E2E_DIR / "driver" / "target" / "release" / "bitbox-e2e-driver"))
SIM_PORT = 15423  # simulator-graphical cannot change it, so both views use it, sequentially.


def log(msg):
    print(f"[e2e] {msg}", file=sys.stderr, flush=True)


def ensure_driver():
    if "E2E_DRIVER" in os.environ:
        return
    log("building driver")
    subprocess.run(["cargo", "build", "--release", "-q"], cwd=E2E_DIR / "driver", check=True)
    # Unit runs in isolated.sh have no network, so they must not build it again.
    os.environ["E2E_DRIVER"] = str(DRIVER)


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
    try:
        sim.wait(5)
    except subprocess.TimeoutExpired:
        # Seen under load: the simulator ignoring SIGTERM. The transcript is complete already.
        sim.kill()
        sim.wait()
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

        sim.choose_orientation()
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
        actions = [lambda: sim.tap("top", 255), sim.pinch]
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
                # Give the new screen another settle period before acting: some take input only
                # after they have been up a while, e.g. the password entry ignores an immediate tap.
                capture(h)
                last, tried = h, 0
                continue
            if done:
                break
            if tried < len(actions):
                actions[tried]()
                tried += 1
            else:
                time.sleep(0.1)  # waiting on the host, or nothing we know how to press
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
    # Between two requests of a step the device shows its idle screen for as long as the host
    # takes, so whether it lasts the settle time is a race. Drop idle screens (anything seen during
    # setup, and the final screen) inside the scenario; the final frame stays.
    idle = {f["hash"] for f in frames if f["step"] in SETUP_STEPS} | {frames[-1]["hash"]} if frames else set()
    for f in frames[:-1]:
        if f["step"] not in SETUP_STEPS and f["hash"] in idle:
            (out / f["file"]).unlink()
    frames = [f for i, f in enumerate(frames)
              if i == len(frames) - 1 or f["step"] in SETUP_STEPS or f["hash"] not in idle]
    (out / "frames.json").write_text(json.dumps(frames, indent=2))
    (out / "driver.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    log(f"graphical: {len(frames)} frames, driver exit {drv.returncode if drv else None}")
    return drv is not None and drv.returncode == 0


def cmd_device(args):
    sim_dir, out = Path(args.sim_dir), Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    # The two views may run in separate invocations (pr runs graphical only for changed units),
    # so keep what an earlier run recorded.
    prev = json.loads((out / "meta.json").read_text()) if (out / "meta.json").exists() else {}
    meta = {
        "headless_ok": None, "graphical_ok": None, **prev,
        "label": args.label or out.name,
        "source": read_text(sim_dir / "source.txt"),
        "simulator_version": read_text(sim_dir / "version.txt"),
    }
    if not getattr(args, "no_headless", False):
        meta["headless_ok"] = run_headless(sim_dir, Path(args.scenario), out / "headless")
    if not args.no_graphical:
        meta["graphical_ok"] = run_graphical(sim_dir, Path(args.scenario), out / "graphical",
                                             os.environ.get("E2E_DISPLAY", ":77"))
    (out / "meta.json").write_text(json.dumps(meta, indent=2))
    return meta


def read_text(p):
    return p.read_text().strip() if p.exists() else ""


def build_sim(ref):
    log(f"building simulators for {ref}")
    res = subprocess.run([str(E2E_DIR / "build-sim.sh"), ref], stdout=subprocess.PIPE, text=True, check=True)
    return Path(res.stdout.strip().splitlines()[-1])


def cmd_compare(args):
    ensure_driver()
    out = Path(args.out)
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    shutil.copy(args.scenario, out / "scenario.json")
    for side, ref in (("base", args.base), ("head", args.head)):
        cmd_device(argparse.Namespace(scenario=args.scenario, sim_dir=build_sim(ref), out=str(out / side),
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
    src = Path(os.environ.get("BITBOX_E2E_CACHE", Path.home() / ".cache" / "bitbox-e2e")) / "src" / f"bitbox-wallet-app-{sha}"
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
    if (out / "base/graphical/frames.json").exists() and (out / "head/graphical/frames.json").exists():
        md += ["## Device screenshots (graphical simulator)", ""]
        md += frames_md(out, "base/graphical", "head/graphical", "diff")

    (out / "report.md").write_text("\n".join(md))
    (out / "report.html").write_text(md_to_html(out, md))
    log(f"report: {out / 'report.md'}")
    print(out / "report.md")


def frames_md(out, base_rel, head_rel, diff_rel):
    """Graphical frames that differ between two runs, aligned by content hash. Paths are relative
    to `out`, which is where report.md lives. Either side may be missing (new unit, failed run)."""
    gb, gh = out / base_rel, out / head_rel
    all_b, all_h = ([] if not (g / "frames.json").exists() else json.loads((g / "frames.json").read_text())
                    for g in (gb, gh))
    fb = [f for f in all_b if f["step"] not in SETUP_STEPS]
    fh = [f for f in all_h if f["step"] not in SETUP_STEPS]
    md, rows = [], []
    diffdir = out / diff_rel
    diffdir.mkdir(parents=True, exist_ok=True)
    for tag, i1, i2, j1, j2 in frame_ops(fb, fh):
        if tag == "equal":
            continue
        for k in range(max(i2 - i1, j2 - j1)):
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
            ca = f"![]({base_rel}/{a['file']})" if a else "*(absent)*"
            cb = f"![]({head_rel}/{b['file']})" if b else "*(absent)*"
            cd = f"![]({d.relative_to(out)})" if d else ""
            md.append(f"| `{step}` | {ca} | {cb} | {cd} |")
        md += ["", f"{len(rows)} differing frame(s); {len(fb)} base / {len(fh)} head frames total.", ""]
    for side, rel, frames in (("base", base_rel, all_b), ("head", head_rel, all_h)):
        if frames:
            md += [f"<details><summary>All {side} frames</summary>", ""]
            md += [" ".join(f"![{f['step']}]({rel}/{f['file']})" for f in frames), "", "</details>", ""]
    return md


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
        if line.startswith("- "):
            items = []
            while i < len(md) and md[i].startswith("- "):
                items.append(f"<li>{inline(md[i][2:])}</li>")
                i += 1
            body.append("<ul>" + "".join(items) + "</ul>")
            continue
        if line.startswith("<"):
            body.append(line if "summary" in line or "details" in line else html.escape(line))
        elif line.startswith("### "):
            body.append(f"<h3>{inline(line[4:])}</h3>")
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


def ensure_baseline(sha, jobs, timer=None, force=False):
    """Bundle for a firmware commit: created on first use (every unit headless + lane B), reused
    after. Returns (root, manifest, sim_dir, units)."""
    root = bundle.bundle_dir(sha)
    sim_dir = build_sim(sha)
    fw_dir = bundle.firmware_dir(sha)
    units = bundle.load_units(fw_dir)
    m = None if force else bundle.read_manifest(root)
    if m is not None:
        log(f"reusing baseline {root}")
        return root, m, sim_dir, units
    # coverage/ is written by coverage.py map and costs far more to rebuild; keep it.
    for d in ("units", "apis"):
        shutil.rmtree(root / d, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    m = {"sha": sha, "simulator_version": read_text(sim_dir / "version.txt"), "harness_sha": bundle.harness_sha(),
         "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "units": {}}
    ok = bundle.run_units(sim_dir, units, root, "headless", jobs)
    m["units"] = {i: {"headless_ok": v, "graphical_ok": None} for i, v in ok.items()}
    bundle.write_manifest(root, m)
    timer and timer.done("baseline headless")
    apis.run(sim_dir, fw_dir / bundle.FW_VECTORS, root / "apis")
    timer and timer.done("baseline apis")
    return root, m, sim_dir, units


def baseline_graphical(root, m, sim_dir, units, ids, jobs):
    """Adds graphical runs to the bundle for units that do not have one yet."""
    todo = [u for u in units if u["id"] in ids and u["id"] in m["units"] and m["units"][u["id"]]["graphical_ok"] is None]
    if not todo:
        return
    for i, v in bundle.run_units(sim_dir, todo, root, "graphical", jobs).items():
        m["units"][i]["graphical_ok"] = v
    bundle.write_manifest(root, m)


def cmd_baseline(args):
    ensure_driver()
    timer = Timer()
    sha = bundle.resolve(args.ref)
    root, m, sim_dir, units = ensure_baseline(sha, args.jobs, timer, force=args.force)
    baseline_graphical(root, m, sim_dir, units, matching(units, args.graphical), args.jobs)
    timer.done("baseline graphical")
    ok = sum(bool(u["headless_ok"]) for u in m["units"].values())
    log(f"baseline {sha[:9]}: {ok}/{len(m['units'])} units ok, timings {timer.phases}")
    print(root)


def select_units(base_sha, head, units):
    """(unit ids to run, unreached changed functions or None, note for the report)."""
    try:
        import coverage
    except ImportError:
        coverage = None
    if coverage is None or not (bundle.bundle_dir(base_sha) / "coverage" / "map.json").exists():
        return {u["id"] for u in units}, None, "all (no coverage map)"
    r = coverage.select(base_sha, head)
    note = f"coverage, {r['changed_functions']} changed functions"
    return set(r["units"]), [f"{u['file']}:{u['lines'][0]} {u['function']}" for u in r["unreached"]], note


def cmd_pr(args):
    ensure_driver()
    timer = Timer()
    out = Path(args.out)
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    head_sha = bundle.resolve(args.head)
    base_sha = bundle.git("merge-base", bundle.resolve(args.base), head_sha)
    root, m, base_sim, base_units = ensure_baseline(base_sha, args.jobs, timer)
    timer.done("baseline")

    head_sim = build_sim(args.head)
    head_fw = bundle.firmware_dir(args.head)
    head_units = bundle.load_units(head_fw)
    by_id = {u["id"]: u for u in head_units}
    timer.done("head build")

    sel, unreached, sel_note = select_units(base_sha, args.head, head_units)
    globbed = matching(head_units, args.graphical)
    run = [u for u in head_units if u["id"] in sel | globbed]
    head_ok = bundle.run_units(head_sim, run, out / "head", "headless", args.jobs)
    timer.done("head headless")

    def unit_dirs(uid):
        s = bundle.slug(uid)
        return root / "units" / s, out / "head" / "units" / s

    # One firmware change usually shows up identically in many units (a reworded warning hits
    # every vector that triggers it); evidence for one of them is evidence for all.
    groups = {}
    for u in run:
        b, h = unit_dirs(u["id"])
        lb, lh = screen_lines(load_screens(b)), screen_lines(load_screens(h))
        ok_b = m["units"].get(u["id"], {}).get("headless_ok")
        if u["id"] in m["units"] and (lb != lh or ok_b != head_ok[u["id"]]):
            key = (ok_b, head_ok[u["id"]], *(line for line in difflib.unified_diff(lb, lh, lineterm="", n=0)
                                              if line[:1] in "+-" and line[:3] not in ("+++", "---")))
            groups.setdefault(key, []).append(u["id"])
    same_as = {i: ids[0] for ids in groups.values() for i in ids[1:]}
    changed = {i for ids in groups.values() for i in ids}
    new = {u["id"] for u in run} - set(m["units"])
    graphical = (changed - set(same_as)) | new | globbed
    baseline_graphical(root, m, base_sim, base_units, graphical, args.jobs)
    bundle.run_units(head_sim, [by_id[i] for i in sorted(graphical)], out / "head", "graphical", args.jobs)
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

    head_version = read_text(head_sim / "version.txt")
    expectations = {}
    try:
        import expect
    except ImportError:
        expect = None
    if expect:
        for u in run:
            if u.get("expect"):
                expectations[u["id"]] = expect.check(u, load_screens(unit_dirs(u["id"])[1]), head_version)
    api_results = apis.run(head_sim, head_fw / bundle.FW_VECTORS, out / "apis")
    timer.done("lane B")

    pr = {
        "base": {"ref": args.base, "sha": base_sha, "bundle": str(root), "simulator_version": m["simulator_version"],
                 "harness_sha": m.get("harness_sha")},
        "head": {"ref": args.head, "sha": head_sha, "simulator_version": head_version,
                 "source": read_text(head_sim / "source.txt"), "harness_sha": bundle.harness_sha()},
        "selection": sel_note,
        "unreached": unreached,
        "units": {u["id"]: {
            "slug": bundle.slug(u["id"]), "kind": u["kind"], "path": u.get("path"),
            "status": "new" if u["id"] in new else "changed" if u["id"] in changed
            else "frames-only" if u["id"] in frames_only else "unchanged",
            "base_ok": m["units"].get(u["id"], {}).get("headless_ok"), "head_ok": head_ok[u["id"]],
            "graphical": u["id"] in graphical, "same_as": same_as.get(u["id"]), "expect": expectations.get(u["id"]),
        } for u in run},
        "removed": sorted(set(m["units"]) - set(by_id)),
        "base_apis": json.loads((root / "apis" / "results.json").read_text())
        if (root / "apis" / "results.json").exists() else None,
        "apis": api_results,
        "timings": timer.phases,
    }
    (out / "pr.json").write_text(json.dumps(pr, indent=2))
    pr_report(out)
    timer.done("report")
    pr["timings"] = timer.phases
    (out / "pr.json").write_text(json.dumps(pr, indent=2))
    log(f"timings: {timer.phases}")


def api_cell(r):
    if r is None:
        return "-"
    if r.get("error") and not r.get("failing"):
        return f"**ERROR** ({r['error']})"
    s = f"{r['passed']} pass"
    return s + (f" / **{r['failed']} FAIL**" if r["failed"] else "")


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

    md = [f"# BitBox e2e: `{base['ref']}` -> `{head['ref']}`", "",
          "| | base (merge-base) | head |", "|---|---|---|",
          f"| firmware | `{base['sha'][:9]}` | `{head['ref']}` (`{head['sha'][:9]}`) |",
          f"| simulator | {base['simulator_version']} | {head['simulator_version']} |",
          f"| harness | `{short_sha(base['harness_sha'])}` | `{short_sha(head['harness_sha'])}` |", ""]
    if base["harness_sha"] != head["harness_sha"]:
        md += ["*The baseline was recorded with another harness revision; harness changes can show up as diffs.*", ""]
    md += ["## Summary", "",
           f"- **Units**: {len(units)} run (selection: {pr['selection']}), {len(by['changed'])} changed "
           f"({sum(not units[i].get('same_as') for i in by['changed'])} distinct), "
           f"{len(by['new'])} new, {len(by['frames-only'])} with graphical-only differences, "
           f"{len(failed)} failed on head ({len(newly)} newly)"
           + (f", {len(pr['removed'])} removed" if pr["removed"] else ""),
           f"- **Expectations**: {exp_n['pass']} pass, {exp_n['fail']} fail, {exp_n['no-expectation']} without "
           f"expectation for this version, {len(units) - len(exp)} units without expectations",
           "- **Unreached changed functions**: " + ("n/a (no coverage map)" if pr["unreached"] is None
                                                    else (", ".join(f"`{f}`" for f in pr["unreached"]) or "none")),
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
    import expect
    out = Path(args.dir)
    pr = json.loads((out / "pr.json").read_text())
    for uid in args.units:
        u = pr["units"].get(uid)
        if u is None:
            sys.exit(f"{uid}: not in {out / 'pr.json'}")
        if not u.get("path"):
            sys.exit(f"{uid}: only scenario units can be promoted; vector expectations live in the firmware repo")
        screens = load_screens(out / "head/units" / u["slug"])
        expect.promote(u["path"], screens, pr["head"]["simulator_version"])
        log(f"promoted {uid} for {pr['head']['simulator_version']}")


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
    d.add_argument("--no-headless", action="store_true")
    for name in ("baseline", "pr"):
        b = sub.add_parser(name)
        if name == "baseline":
            b.add_argument("ref", help="firmware commit")
            b.add_argument("--force", action="store_true", help="re-record an existing bundle")
        else:
            b.add_argument("--base", required=True, help="firmware ref; its merge-base with head keys the bundle")
            b.add_argument("--head", required=True, help="firmware ref or WORKTREE")
            b.add_argument("--out", required=True)
        b.add_argument("--graphical", "--graphical-units", nargs="+", action="extend", default=[], metavar="GLOB",
                       help="unit id globs to also run on the graphical simulator")
        b.add_argument("-j", "--jobs", type=int, default=4, help="units in parallel (one netns + X display each)")
    pm = sub.add_parser("promote")
    pm.add_argument("dir", help="e2e.py pr --out directory")
    pm.add_argument("units", nargs="+")
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
    elif args.cmd == "baseline":
        cmd_baseline(args)
    elif args.cmd == "pr":
        cmd_pr(args)
    elif args.cmd == "promote":
        cmd_promote(args)
    else:
        report(args.dir)


if __name__ == "__main__":
    main()
