"""Scenario runs (headless & graphical), driver builds, parallel unit runner, and baseline bundles.

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
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from bitbox_e2e import apis, config, units
from bitbox_e2e.config import CACHE, E2E_DIR, log, read_text, sim_version
from bitbox_e2e.report import SETUP_STEPS

# E2E_DRIVER points at a prebuilt driver and skips the cargo build (CI artifacts, parallel runs).
DRIVER = Path(os.environ.get("E2E_DRIVER", E2E_DIR / "driver" / "target" / "release" / "bitbox-e2e-driver"))

QUIET_S = 0.3


class Timer:
    def __init__(self):
        self.t, self.phases = time.time(), {}

    def done(self, name: str) -> None:
        now = time.time()
        self.phases[name] = round(now - self.t)
        self.t = now
        log(f"phase {name}: {self.phases[name]}s")


def ensure_driver() -> None:
    if "E2E_DRIVER" in os.environ:
        return
    log("building driver")
    subprocess.run(["cargo", "build", "--release", "-q"], cwd=E2E_DIR / "driver", check=True)
    # Unit runs in isolated.sh have no network, so they must not build it again.
    os.environ["E2E_DRIVER"] = str(DRIVER)


def driver_for(api_rs: str | None = None) -> Path:
    """Driver binary linking bitbox-api-rs at commit api_rs; None: the sibling checkout as it is
    (or E2E_DRIVER). Cached by api_rs and the driver's own sources."""
    if api_rs is None:
        ensure_driver()
        return Path(os.environ["E2E_DRIVER"])
    src = E2E_DIR / "driver"
    files = [src / "Cargo.toml", src / "Cargo.lock", *sorted((src / "src").rglob("*.rs"))]
    h = hashlib.sha256(b"".join(f.read_bytes() for f in files)).hexdigest()[:12]
    out = CACHE / "drivers" / f"{api_rs}-{h}" / "bitbox-e2e-driver"
    if out.exists():
        return out
    # The driver's path dependency is ../../bitbox-api-rs: the same layout around a clone at api_rs.
    root = CACHE / "src" / "driver-api-rs"
    api = root / "bitbox-api-rs"
    if not (api / ".git").exists():
        subprocess.run(
            ["git", "clone", "-q", "--shared", "--no-checkout", str(config.REPOS["api-rs"].dir), str(api)],
            check=True,
        )
    subprocess.run(["git", "-C", str(api), "checkout", "-q", "-f", "--detach", api_rs], check=True)
    shutil.rmtree(root / "e2e" / "driver" / "src", ignore_errors=True)
    shutil.copytree(src, root / "e2e" / "driver", dirs_exist_ok=True, ignore=shutil.ignore_patterns("target"))
    log(f"building driver with bitbox-api-rs {api_rs[:9]}")
    target = CACHE / "drivers" / "target"
    subprocess.run(
        ["cargo", "build", "--release", "-q", "--target-dir", str(target)],
        cwd=root / "e2e" / "driver",
        check=True,
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(target / "release" / "bitbox-e2e-driver", out)
    return out


def start_driver(scenario, port, stderr):
    drv_bin = os.environ.get("E2E_DRIVER", str(DRIVER))
    return subprocess.Popen(
        [drv_bin, str(scenario), f"127.0.0.1:{port}"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=stderr,
        text=True,
        env=dict(os.environ, E2E_HANDSHAKE="1"),
    )


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


def run_headless(sim_dir: Path, scenario: Path, out: Path) -> bool:
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
            port = config.free_port()
            sim = subprocess.Popen(
                ["stdbuf", "-oL", str(sim_dir / "simulator"), "--port", str(port)],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                env=env,
            )
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
        next_step = "connect"
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


def run_graphical(sim_dir: Path, scenario: Path, out: Path) -> bool:
    from bitbox_e2e import gsim

    out.mkdir(parents=True, exist_ok=True)
    for f in out.glob("*.png"):
        f.unlink()
    # Without --port the simulator binds 15423, alone on the host or in its own network namespace.
    port = config.free_port() if config.takes_port(sim_dir) else None
    if port is None:
        config.wait_port_free(config.SIM_PORT, reuseaddr=True)
    memdir = out / "fake-memory"
    shutil.rmtree(memdir, ignore_errors=True)
    memdir.mkdir()
    xvfb = gsim.Xvfb(out / "xvfb.log")
    sim = drv = rec = None
    events = []
    try:
        sim = gsim.GraphicalSim(
            str(sim_dir / "simulator-graphical"),
            xvfb.disp,
            str(out / "simulator.log"),
            str(memdir),
            port,
        )
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


def run_device(
    sim_dir: Path,
    scenario: Path,
    out: Path,
    label: str | None = None,
    no_graphical: bool = False,
    no_headless: bool = False,
) -> dict:
    sim_dir, scenario, out = Path(sim_dir), Path(scenario), Path(out)
    out.mkdir(parents=True, exist_ok=True)
    # The two views may run in separate invocations (pr runs graphical only for changed units),
    # so keep what an earlier run recorded.
    prev = json.loads((out / "meta.json").read_text()) if (out / "meta.json").exists() else {}
    meta = {
        "headless_ok": None,
        "graphical_ok": None,
        **prev,
        "label": label or out.name,
        "source": read_text(sim_dir / "source.txt"),
        "simulator_version": read_text(sim_dir / "version.txt"),
    }
    if not no_headless:
        meta["headless_ok"] = run_headless(sim_dir, scenario, out / "headless")
    if not no_graphical:
        meta["graphical_ok"] = run_graphical(sim_dir, scenario, out / "graphical")
    (out / "meta.json").write_text(json.dumps(meta, indent=2))
    return meta


def cmd_device(args) -> dict:
    return run_device(
        Path(args.sim_dir),
        Path(args.scenario),
        Path(args.out),
        label=args.label,
        no_graphical=args.no_graphical,
        no_headless=getattr(args, "no_headless", False),
    )


# --- parallel unit runner & baseline bundles ------------------------------------------------------


def run_units(sim_dir: Path, unit_list: list[dict], root: Path, view: str, jobs: int, driver: Path | None = None):
    """Runs `python -m bitbox_e2e.cli device` per unit, `jobs` at a time, writing
    <root>/units/<slug>/. view is 'headless' or 'graphical'; driver is a driver binary
    (driver_for), default E2E_DRIVER. Returns {id: ok}."""
    isolate = False
    if view == "graphical":
        mode = config.graphical_mode(sim_dir)
        isolate, jobs = mode == "netns", 1 if mode == "serial" else jobs
        log(f"graphical on {Path(sim_dir).name[:9]}: {config.GRAPHICAL_MODES[mode]}")

    def one(u):
        d = Path(root) / "units" / config.slug(u["id"])
        d.mkdir(parents=True, exist_ok=True)
        (d / "scenario.json").write_text(json.dumps(u["scenario"], indent=2))
        error = None
        try:
            skip = "--no-graphical" if view == "headless" else "--no-headless"
            cmd = [
                "nice",
                *([str(E2E_DIR / "isolated.sh")] if isolate else []),
                sys.executable,
                "-m",
                "bitbox_e2e.cli",
                "device",
                "--scenario",
                str(d / "scenario.json"),
                "--sim-dir",
                str(sim_dir),
                "--out",
                str(d),
                "--label",
                u["id"],
                skip,
            ]
            env = dict(os.environ, **({"E2E_DRIVER": str(driver)} if driver else {}))
            with open(d / f"{view}.log", "w") as f:
                subprocess.run(cmd, stdout=f, stderr=subprocess.STDOUT, timeout=900, env=env)
        except subprocess.TimeoutExpired:
            error = "timeout"
        meta_path = d / "meta.json"
        meta = json.loads(meta_path.read_text()) if meta_path.exists() else {}
        if error is None and meta.get(f"{view}_ok") is None:
            error = f"no result, see {view}.log"  # cli device crashed before writing meta.json
        if error:
            meta.update({f"{view}_ok": False, f"{view}_error": error})
            meta_path.write_text(json.dumps(meta, indent=2))
        return meta[f"{view}_ok"]

    t0 = time.time()
    with ThreadPoolExecutor(jobs) as ex:
        res = dict(zip([u["id"] for u in unit_list], ex.map(one, unit_list)))
    log(f"{view}: {sum(bool(v) for v in res.values())}/{len(res)} units ok in {time.time() - t0:.0f}s")
    return res


def revisions(api_results: dict, pins: dict | None = None) -> dict:
    """Code under test besides the firmware: the bitbox-api-rs the driver links and the API repo
    revisions the suites ran."""
    revs = {"driver bitbox-api-rs": (pins or {}).get("api-rs") or config.checkout_rev(config.REPOS["api-rs"].dir)}
    for suite, modes in api_results["suites"].items():
        revs[suite] = next((r["repo_sha"] for r in modes.values() if r.get("repo_sha")), None)
    return revs


def revision_diffs(base: dict, head: dict, changed: str | None = None) -> list[str]:
    """Keys of revisions() known on both sides that differ, other than the changed repo's own."""
    own = {changed, f"driver bitbox-{changed}"} if changed else set()
    return [k for k, v in head.items() if v and base.get(k) and base[k] != v and k not in own]


def ensure_baseline(sha: str, jobs: int, timer: Timer | None = None, force: bool = False, revs: dict | None = None):
    """Bundle for a firmware commit: created on first use (every unit headless + lane B), reused
    after. Returns (root, manifest, units). The simulator is built only to record: a bundle restored
    from a CI cache must not cost a simulator build."""
    revs = revs or {}
    root = config.bundle_dir(sha, revs)
    fw_dir = config.firmware_dir(sha)
    unit_list = units.load_units(fw_dir)
    m = None if force else config.read_manifest(root)
    if m is not None:
        log(f"reusing baseline {root}")
        return root, m, unit_list
    sim_dir = config.build_sim(sha)
    (root / "manifest.json").unlink(missing_ok=True)
    for d in ("units", "apis"):
        shutil.rmtree(root / d, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    m = {
        "sha": sha,
        "simulator_version": sim_version(sim_dir),
        "harness_sha": config.harness_sha(),
        "created": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "pins": revs,
        "units": {},
    }
    ok = run_units(sim_dir, unit_list, root, "headless", jobs, driver_for(revs.get("api-rs")))
    if not any(ok.values()):
        # Every head unit would compare against "failed on base", so the gate could never fire.
        raise RuntimeError(
            f"no unit completed on {sha[:9]}; not recording the baseline (logs: {root}/units/*/headless.log)"
        )
    m["units"] = {i: {"headless_ok": v, "graphical_ok": None} for i, v in ok.items()}
    if timer:
        timer.done("baseline headless")
    m["revisions"] = revisions(apis.run(sim_dir, fw_dir / config.FW_VECTORS, root / "apis", revs=revs), revs)
    if timer:
        timer.done("baseline apis")
    # The manifest marks the bundle complete, so a recording that died halfway is redone next time.
    config.write_manifest(root, m)
    return root, m, unit_list


def baseline_graphical(root: Path, m: dict, sha: str, unit_list: list[dict], ids: set[str], jobs: int) -> str | None:
    """Adds graphical runs to the bundle for units that do not have one yet. Builds the simulator
    only then. Returns how they ran (config.graphical_mode), None if nothing was missing."""
    todo = [
        u
        for u in unit_list
        if u["id"] in ids and u["id"] in m["units"] and m["units"][u["id"]]["graphical_ok"] is None
    ]
    if not todo:
        return None
    sim_dir = config.build_sim(sha)
    driver = driver_for((m.get("pins") or {}).get("api-rs"))
    for i, v in run_units(sim_dir, todo, root, "graphical", jobs, driver).items():
        m["units"][i]["graphical_ok"] = v
    config.write_manifest(root, m)
    return config.graphical_mode(sim_dir)
