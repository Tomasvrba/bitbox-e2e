#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pillow>=10", "python-xlib>=0.33"]
# ///
"""Graphical simulator as a long-running device for app-driven tests (the WalletConnect flow).

Starts Xvfb + simulator-graphical (preseeded), finishes the orientation screen, then auto-advances
like e2e.py run_graphical: every new stable screen is captured, then accepted (tap top-right, then
hold-to-confirm). Never rejects. The host is whoever connects to 127.0.0.1:15423 (the app backend).

  device.py --sim-dir <dir> --out <dir> [--display :72]

stdin, one command per line; stdout, one JSON event per line:
  -> {"event": "ready"}                       simulator accepts clients
  <- step <name>                              settle, close the current step, label new frames <name>
  -> {"event": "step", "closed": <prev>}
  <- quit
  -> {"event": "quit", "frames": [...]}      frames.json written, simulator and Xvfb stopped
"""

import argparse
import json
import os
import re
import select
import shutil
import sys
import time
from pathlib import Path

E2E_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(E2E_DIR))

import gsim  # noqa: E402

# Screens seen while the app connects/unlocks/pairs. Same role as SETUP_STEPS in e2e.py: the pairing
# code differs every run, so these are kept out of diffs.
SETUP_STEPS = {"boot", "setup"}


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--sim-dir", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--display", default=os.environ.get("E2E_DISPLAY", ":72"))
    args = p.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for f in out.glob("*.png"):
        f.unlink()
    memdir = out / "fake-memory"
    shutil.rmtree(memdir, ignore_errors=True)
    memdir.mkdir()
    xvfb = gsim.Xvfb(args.display, out / "xvfb.log")
    sim = None
    frames = []
    step = "boot"
    try:
        sim = gsim.GraphicalSim(str(Path(args.sim_dir) / "simulator-graphical"), args.display,
                                str(out / "simulator.log"), str(memdir))

        def capture(h):
            name = f"{len(frames):02d}-{re.sub(r'[^a-z0-9]+', '-', step.lower())}.png"
            sim.lcd_image().save(out / name)
            frames.append({"file": name, "step": step, "hash": h})

        sim.choose_orientation()
        last = sim.wait_stable()
        capture(last)
        emit({"event": "ready"})
        step = "setup"

        actions = [lambda: sim.tap("top", 255), sim.pinch]
        tried = 0
        while True:
            if select.select([sys.stdin], [], [], 0)[0]:
                cmd = sys.stdin.readline().split()
                if not cmd or cmd[0] == "quit":
                    break
                if cmd[0] == "step":
                    h = sim.wait_stable(settle_s=1.0)
                    if h != last:
                        capture(h)
                        last, tried = h, 0
                    emit({"event": "step", "closed": step})
                    step = cmd[1]
                continue
            if sim.proc.poll() is not None:
                raise RuntimeError(f"simulator-graphical exited with {sim.proc.returncode}")
            h = sim.wait_stable(settle_s=0.5)
            if h != last:
                # Act only after the new screen stayed up for another settle period; some screens
                # ignore input right after they appear (password entry).
                capture(h)
                last, tried = h, 0
                continue
            # Don't poke the idle screen between requests: the app's next request can land while the
            # pinch is held, and its release then taps (accepts) that request's first screen
            # before it was captured (seen: the unusual-keypath warning missing).
            idle = step not in SETUP_STEPS and any(f["hash"] == last for f in frames if f["step"] in SETUP_STEPS)
            if tried < len(actions) and not idle:
                actions[tried]()
                tried += 1
            else:
                time.sleep(0.1)
    finally:
        if sim:
            sim.stop()
        xvfb.stop()
        shutil.rmtree(memdir, ignore_errors=True)
    # The idle screen shows up between requests for as long as the host takes, so whether a step
    # captures it is a race. Drop setup-time screens (logo, lockscreen, idle) inside request steps.
    idle = {f["hash"] for f in frames if f["step"] in SETUP_STEPS}
    for f in frames:
        if f["step"] not in SETUP_STEPS and f["hash"] in idle:
            (out / f["file"]).unlink()
    frames = [f for f in frames if f["step"] in SETUP_STEPS or f["hash"] not in idle]
    (out / "frames.json").write_text(json.dumps(frames, indent=2))
    emit({"event": "quit", "frames": frames})


if __name__ == "__main__":
    main()
