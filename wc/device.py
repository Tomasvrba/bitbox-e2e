"""Graphical simulator as a long-running device for app-driven tests (the WalletConnect flow).

Starts Xvfb + simulator-graphical (preseeded) and records it with gsim.Recorder, the device policy
of every graphical run: every new stable screen is captured, then accepted (tap top-right, then
hold-to-confirm). Never rejects. The host is whoever connects to 127.0.0.1:15423 (the app backend).

  python wc/device.py --sim-dir <dir> --out <dir>

stdin, one command per line; stdout, one JSON event per line:
  -> {"event": "ready"}                       simulator accepts clients
  <- step <name>                              settle, close the current step, label new frames <name>
  -> {"event": "step", "closed": <prev>}
  <- quit
  -> {"event": "quit", "frames": [...]}      frames.json written, simulator and Xvfb stopped
"""

import argparse
import json
import select
import shutil
import sys
from pathlib import Path

from bitbox_e2e import gsim

# Screens seen while the app connects/unlocks/pairs. Same role as SETUP_STEPS in report.py: the
# pairing code differs every run, so these are kept out of diffs.
SETUP_STEPS = {"boot", "setup"}


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--sim-dir", required=True)
    p.add_argument("--out", required=True)
    args = p.parse_args()

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for f in out.glob("*.png"):
        f.unlink()
    memdir = out / "fake-memory"
    shutil.rmtree(memdir, ignore_errors=True)
    memdir.mkdir()
    xvfb = gsim.Xvfb(out / "xvfb.log")
    sim = rec = None
    try:
        sim = gsim.GraphicalSim(str(Path(args.sim_dir) / "simulator-graphical"), xvfb.disp,
                                str(out / "simulator.log"), str(memdir))
        rec = gsim.Recorder(sim, out, SETUP_STEPS)
        emit({"event": "ready"})
        rec.step = "setup"
        while True:
            if select.select([sys.stdin], [], [], 0)[0]:
                cmd = sys.stdin.readline().split()
                if not cmd or cmd[0] == "quit":
                    break
                if cmd[0] == "step":
                    rec.settle(1.0)
                    emit({"event": "step", "closed": rec.step})
                    rec.step = cmd[1]
                continue
            if sim.proc.poll() is not None:
                raise RuntimeError(f"simulator-graphical exited with {sim.proc.returncode}")
            if not rec.settle():
                rec.act()
    finally:
        if sim:
            sim.stop()
        xvfb.stop()
        shutil.rmtree(memdir, ignore_errors=True)
    frames = rec.finish(out / "frames.json", final_is_idle=False)
    emit({"event": "quit", "frames": frames})


if __name__ == "__main__":
    main()
