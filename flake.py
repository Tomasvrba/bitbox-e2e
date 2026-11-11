#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.10"
# dependencies = ["pillow>=10", "python-xlib>=0.33"]
# ///
"""Repeat graphical runs of the same scenarios and check that the frame sequence is stable.

  flake.py --sim-dir <dir> --out /tmp/e2e-gfx/flake [--runs 10] [--jobs 3] scenarios/*.json [--unit 'btc-vector/*fee*']

Each run is `isolated.sh flake.py one ...` on its own Xvfb display (:91 + slot). A scenario is
stable when every run produced the same non-setup frame hashes, the sequence the report diffs.
"""

import argparse
import fnmatch
import json
import os
import statistics
import subprocess
import sys
import threading
import time
from collections import Counter
from pathlib import Path

E2E_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(E2E_DIR))


def load_e2e():
    import importlib.util
    spec = importlib.util.spec_from_file_location("e2e", E2E_DIR / "e2e.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def one(args):
    e2e = load_e2e()
    t0 = time.time()
    try:
        ok = e2e.run_graphical(Path(args.sim_dir), Path(args.scenario), Path(args.out),
                               os.environ["E2E_DISPLAY"])
    except Exception as e:  # recorded as a failed run, the batch goes on
        ok = False
        print(f"[flake] {e!r}", file=sys.stderr)
    (Path(args.out) / "run.json").write_text(json.dumps({"ok": ok, "seconds": time.time() - t0}))


def scenario_files(args):
    """(name, path) for each scenario file and each unit matching --unit (written to <out>/units)."""
    items = [(Path(p).stem, Path(p)) for p in args.scenarios]
    if args.unit:
        import units
        fw = Path(os.environ.get("BITBOX_FIRMWARE", E2E_DIR.parent / "bitbox02-firmware"))
        udir = Path(args.out) / "units"
        udir.mkdir(parents=True, exist_ok=True)
        for u in units.load_units(fw, E2E_DIR / "scenarios"):
            if any(fnmatch.fnmatch(u["id"], g) for g in args.unit):
                name = units.slug(u["id"])
                (udir / f"{name}.json").write_text(json.dumps(u["scenario"]))
                items.append((name, udir / f"{name}.json"))
    return items


def batch(args):
    e2e = load_e2e()
    out = Path(args.out)
    jobs = [(name, path, i) for name, path in scenario_files(args) for i in range(args.runs)]
    slots = list(range(args.jobs))
    lock = threading.Lock()

    def worker():
        while True:
            with lock:
                if not jobs:
                    return
                name, path, i = jobs.pop(0)
                slot = slots.pop()
            run_out = out / name / f"{i:02d}"
            env = dict(os.environ, E2E_DISPLAY=f":{91 + slot}")
            with open(out / name / f"{i:02d}.log", "w") as log:
                subprocess.run(["nice", str(E2E_DIR / "isolated.sh"), sys.executable, __file__, "one",
                                "--sim-dir", args.sim_dir, "--scenario", str(path), "--out", str(run_out)],
                               env=env, stdout=log, stderr=subprocess.STDOUT, timeout=400)
            with lock:
                slots.append(slot)

    for name, _, _ in jobs:
        (out / name).mkdir(parents=True, exist_ok=True)
    threads = [threading.Thread(target=worker) for _ in range(args.jobs)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    summary = {}
    for d in sorted(p for p in out.iterdir() if p.is_dir() and p.name != "units"):
        seqs, times, fails = Counter(), [], 0
        for run in sorted(p for p in d.iterdir() if p.is_dir()):
            res = json.loads((run / "run.json").read_text()) if (run / "run.json").exists() else {"ok": False}
            fails += not res["ok"]
            times.append(res.get("seconds", 0))
            fj = run / "frames.json"
            frames = json.loads(fj.read_text()) if fj.exists() else []
            seqs[tuple(f["hash"][:10] for f in frames if f["step"] not in e2e.SETUP_STEPS)] += 1
        n = sum(seqs.values())
        top = seqs.most_common(1)[0][1] if seqs else 0
        summary[d.name] = {"runs": n, "identical": top, "variants": len(seqs), "failed": fails,
                           "median_s": round(statistics.median(times), 1),
                           "min_s": round(min(times), 1), "max_s": round(max(times), 1)}
        print(f"{d.name}: {top}/{n} identical ({len(seqs)} variants), {fails} failed, "
              f"median {summary[d.name]['median_s']}s [{summary[d.name]['min_s']}..{summary[d.name]['max_s']}]")
        if len(seqs) > 1:
            for seq, c in seqs.most_common():
                print(f"  {c}x {' '.join(seq)}")
    (out / "summary.json").write_text(json.dumps(summary, indent=2))


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd")
    o = sub.add_parser("one")
    o.add_argument("--sim-dir", required=True)
    o.add_argument("--scenario", required=True)
    o.add_argument("--out", required=True)
    if len(sys.argv) > 1 and sys.argv[1] == "one":
        one(p.parse_args())
        return
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--sim-dir", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--runs", type=int, default=10)
    p.add_argument("--jobs", type=int, default=3)
    p.add_argument("--unit", action="append", help="unit id glob from units.py, repeatable")
    p.add_argument("scenarios", nargs="*")
    args = p.parse_args()
    if "E2E_DRIVER" not in os.environ:
        sys.exit("set E2E_DRIVER to a prebuilt driver (no cargo inside the namespace)")
    batch(args)


if __name__ == "__main__":
    main()
