"""One scenario on one simulator: the driver, the headless and graphical runs, `e2e.py device`.

  headless/  C simulator, auto-accepts everything, prints every screen as text (exact, diffable)
  graphical/ graphical simulator on Xvfb, driven by XTest, one PNG per distinct LCD frame
"""

import hashlib
import json
import os
import queue
import re
import shutil
import subprocess
import threading
import time
from pathlib import Path

import bundle
from bundle import E2E_DIR, log, read_text
from report import SETUP_STEPS

# E2E_DRIVER points at a prebuilt driver and skips the cargo build (CI artifacts, parallel runs).
DRIVER = Path(os.environ.get("E2E_DRIVER", E2E_DIR / "driver" / "target" / "release" / "bitbox-e2e-driver"))


def ensure_driver():
    if "E2E_DRIVER" in os.environ:
        return
    log("building driver")
    subprocess.run(["cargo", "build", "--release", "-q"], cwd=E2E_DIR / "driver", check=True)
    # Unit runs in isolated.sh have no network, so they must not build it again.
    os.environ["E2E_DRIVER"] = str(DRIVER)


def driver_for(api_rs=None):
    """Driver binary linking bitbox-api-rs at commit api_rs; None: the sibling checkout as it is
    (or E2E_DRIVER). Cached by api_rs and the driver's own sources."""
    if api_rs is None:
        ensure_driver()
        return Path(os.environ["E2E_DRIVER"])
    src = E2E_DIR / "driver"
    files = [src / "Cargo.toml", src / "Cargo.lock", *sorted((src / "src").rglob("*.rs"))]
    h = hashlib.sha256(b"".join(f.read_bytes() for f in files)).hexdigest()[:12]
    out = bundle.CACHE / "drivers" / f"{api_rs}-{h}" / "bitbox-e2e-driver"
    if out.exists():
        return out
    # The driver's path dependency is ../../bitbox-api-rs: the same layout around a clone at api_rs.
    root = bundle.CACHE / "src" / "driver-api-rs"
    api = root / "bitbox-api-rs"
    if not (api / ".git").exists():
        subprocess.run(["git", "clone", "-q", "--shared", "--no-checkout", str(bundle.REPOS["api-rs"].dir), str(api)],
                       check=True)
    subprocess.run(["git", "-C", str(api), "checkout", "-q", "-f", "--detach", api_rs], check=True)
    shutil.rmtree(root / "e2e" / "driver" / "src", ignore_errors=True)
    shutil.copytree(src, root / "e2e" / "driver", dirs_exist_ok=True, ignore=shutil.ignore_patterns("target"))
    log(f"building driver with bitbox-api-rs {api_rs[:9]}")
    target = bundle.CACHE / "drivers" / "target"
    subprocess.run(["cargo", "build", "--release", "-q", "--target-dir", str(target)], cwd=root / "e2e" / "driver",
                   check=True)
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(target / "release" / "bitbox-e2e-driver", out)
    return out


QUIET_S = 0.3


def start_driver(scenario, port, stderr):
    return subprocess.Popen([str(DRIVER), str(scenario), f"127.0.0.1:{port}"],
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
    # The headless sim keeps no state unless FAKE_MEMORY_FILEPATH is set; it boots unseeded.
    env = {k: v for k, v in os.environ.items() if k != "FAKE_MEMORY_FILEPATH"}
    sim = drv = None
    combined, events = [], []
    parser = ScreenParser()
    try:
        # Each run gets its own port, so units run side by side without network namespaces. Another
        # process can take the port before the simulator binds it; then it exits, try another.
        for _ in range(3):
            port = bundle.free_port()
            sim = subprocess.Popen(["stdbuf", "-oL", str(sim_dir / "simulator"), "--port", str(port)],
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env)
            q = queue.Queue()
            threading.Thread(target=pump, args=(sim.stdout, "sim", q), daemon=True).start()
            while True:
                _, _, line = q.get(timeout=20)
                if line is None or line.startswith("Listening on port") or line.startswith("ERROR binding"):
                    break
            if line is None or not line.startswith("ERROR binding"):
                break
            sim.wait(5)
        drv = start_driver(scenario, port, stderr=subprocess.PIPE)
        threading.Thread(target=pump, args=(drv.stdout, "driver", q), daemon=True).start()
        threading.Thread(target=pump, args=(drv.stderr, "driver-stderr", q), daemon=True).start()

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
        # The driver can exit before the pump thread delivered the simulator's output for its last
        # request (seen with one-screen steps like eth-address); the simulator logs "Waiting for new
        # clients" once the driver's connection is gone.
        while True:
            try:
                _, tag, line = q.get(timeout=1)
            except queue.Empty:
                break
            if line is None or tag != "sim":
                continue
            combined.append(f"{tag:>13} | {line}")
            parser.feed(line, state["step"])
            if line.startswith("Waiting for new clients"):
                break
        drv.wait(10)
    finally:
        # Also on a stuck driver or a simulator that never listened: nothing may outlive the unit.
        for p in (drv, sim):
            if p is None or p.poll() is not None:
                continue
            p.terminate()
            try:
                p.wait(5)
            except subprocess.TimeoutExpired:
                # Seen under load: the simulator ignoring SIGTERM. The transcript is complete already.
                p.kill()
                p.wait()
    (out / "combined.log").write_text("\n".join(combined) + "\n")
    (out / "driver.jsonl").write_text("".join(json.dumps(e) + "\n" for e in events))
    screens = parser.screens
    (out / "screens.json").write_text(json.dumps(screens, indent=2))
    log(f"headless: {len(screens)} screens, driver exit {drv.returncode}")
    return drv.returncode == 0


# --- graphical ------------------------------------------------------------------------------------

def run_graphical(sim_dir, scenario, out):
    import gsim

    out.mkdir(parents=True, exist_ok=True)
    for f in out.glob("*.png"):
        f.unlink()
    # Without --port the simulator binds 15423, alone on the host or in its own network namespace.
    port = bundle.free_port() if bundle.takes_port(sim_dir) else None
    if port is None:
        bundle.wait_port_free(bundle.SIM_PORT, reuseaddr=True)
    memdir = out / "fake-memory"
    shutil.rmtree(memdir, ignore_errors=True)
    memdir.mkdir()
    xvfb = gsim.Xvfb(out / "xvfb.log")
    sim = drv = rec = None
    events = []
    try:
        sim = gsim.GraphicalSim(str(sim_dir / "simulator-graphical"), xvfb.disp, str(out / "simulator.log"),
                                str(memdir), port)
        rec = gsim.Recorder(sim, out, SETUP_STEPS)

        def spawn():
            q = queue.Queue()
            d = start_driver(scenario, sim.port, stderr=subprocess.STDOUT)
            threading.Thread(target=pump, args=(d.stdout, "driver", q), daemon=True).start()
            return d, q

        drv, q = spawn()
        paired, retries = False, 0
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
                    rec.step = ev["step"]
                    drv.stdin.write("go\n")
                    drv.stdin.flush()
                if ev.get("event") == "done":
                    done = True
            if rec.settle():
                continue
            if done:
                break
            rec.act()
        if drv.poll() is None:
            drv.kill()
        drv.wait(5)
        # Final state after the last step (e.g. "Transaction confirmed" toast fading out).
        rec.settle(1.0)
    finally:
        if sim:
            sim.stop()
        xvfb.stop()
        shutil.rmtree(memdir, ignore_errors=True)
    frames = rec.finish(out / "frames.json") if rec else []
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
        meta["graphical_ok"] = run_graphical(sim_dir, Path(args.scenario), out / "graphical")
    (out / "meta.json").write_text(json.dumps(meta, indent=2))
    return meta

